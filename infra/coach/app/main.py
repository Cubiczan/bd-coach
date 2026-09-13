"""BD Coach live call coaching service.

    browser  ──Agora Voice──▶  prospect          (transport: hosted)
       │
       │ local + remote audio chunks, over websocket
       ▼
    coach service  ──▶  whisper container        (transcription: in-house)
       │                     │
       │                     ▼
       │                cue engine               (decision: deterministic)
       │                     │
       │                     ▼
       └──── nudge ◀──  DLP redaction ──▶ LiteLLM  (phrasing: in-house, with
                                                    a documented cloud failover)

Agora carries the call. Nothing else leaves the box by default.
"""

from __future__ import annotations

import base64
import binascii
import logging
import pathlib
import re
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import bearer_token, join_secret_ok
from .coaching import Coach, load_prompt
from .config import Settings, load_settings
from .cues import PROSPECT, SELLER, CueEngine, Utterance, finite_nonneg
from .redaction import Redactor

# Vendored official Agora AccessToken2 builder; see vendor/agora/__init__.py.
from vendor.agora import RtcTokenBuilder, Role_Publisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("coach")

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"

# Agora's permitted channel-name character set. The call id reaches this from a
# request body and is interpolated into SDK calls, so it is validated, not trusted.
_CHANNEL_OK = re.compile(r"^[A-Za-z0-9!#$%&()+\-:;<=.>?@\[\]^_{|}~,]{1,64}$")

# One audio chunk should be a couple of seconds of Opus. Anything much larger is
# either a client bug or someone using the socket as an upload endpoint.
MAX_CHUNK_BYTES = 512 * 1024

state: dict[str, object] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    client = httpx.AsyncClient()
    redactor = _load_redactor(settings)

    state["settings"] = settings
    state["client"] = client
    state["redactor"] = redactor
    state["coach"] = Coach(
        url=settings.litellm_url,
        api_key=settings.litellm_key,
        model=settings.coach_model,
        system_prompt=load_prompt(settings.prompt_path),
        redactor=redactor,
        client=client,
    )
    from .transcribe import Transcriber

    state["transcriber"] = Transcriber(settings.whisper_url, settings.whisper_model, client)

    if not settings.configured:
        log.warning(
            "AGORA_APP_ID / AGORA_APP_CERTIFICATE are unset — the call surface is "
            "disabled. Every other BD Coach service is unaffected."
        )

    try:
        yield
    finally:
        await client.aclose()


def _load_redactor(settings: Settings) -> Redactor:
    """Fail closed for the model path when rules cannot be loaded.

    The service still starts (a missing mount must not take calls offline) but
    the returned redactor reports ``loaded=False`` so phrasing skips LiteLLM
    rather than sending an unredacted transcript to a gateway that may fail
    over to Groq.
    """
    try:
        return Redactor.from_path(pathlib.Path(settings.dlp_rules_path))
    except (OSError, ValueError) as exc:
        log.error(
            "DLP rules unreadable at %s (%s) — model phrasing is disabled. "
            "Fix the /dlp mount.",
            settings.dlp_rules_path,
            exc,
        )
        return Redactor({"rules": {}}, loaded=False)


app = FastAPI(title="BD Coach — live call coaching", lifespan=lifespan)


class TokenRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=48)
    role: Literal["seller", "prospect"] = SELLER


def _require_join_secret(provided: str | None, settings: Settings) -> None:
    if not settings.join_secret:
        raise HTTPException(
            status_code=503,
            detail="COACH_JOIN_SECRET is unset — call surface is locked",
        )
    if not join_secret_ok(provided, settings.join_secret):
        raise HTTPException(status_code=401, detail="unauthorized")


def _origin_allowed(origin: str | None, settings: Settings) -> bool:
    """Optional Origin check. Unset BD_COACH_DOMAIN skips it (local/dev)."""
    if not settings.public_domain:
        return True
    if not origin:
        return False
    expected = f"https://coach.{settings.public_domain}"
    return origin.rstrip("/") == expected


def _uid_for(seed: str) -> int:
    """Stable 32-bit uid so a reconnecting participant keeps its identity."""
    h = 2166136261
    for ch in seed:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 0xFFFFFFFE) + 1


@app.get("/healthz")
async def healthz() -> JSONResponse:
    settings: Settings = state["settings"]  # type: ignore[assignment]
    return JSONResponse(
        {
            "ok": True,
            "call_surface": settings.configured,
            "join_guard": bool(settings.join_secret),
            "transcription": "in-house (whisper)",
            "model_gateway": settings.coach_model,
        }
    )


@app.post("/token")
async def token(
    request: TokenRequest,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    settings: Settings = state["settings"]  # type: ignore[assignment]
    _require_join_secret(bearer_token(authorization), settings)
    if not settings.configured:
        raise HTTPException(
            status_code=503,
            detail=(
                "AGORA_APP_ID / AGORA_APP_CERTIFICATE are missing or not "
                "32-character hexadecimal"
            ),
        )

    channel = f"bdcall-{request.call_id}"
    if not _CHANNEL_OK.match(channel):
        raise HTTPException(status_code=400, detail="call_id contains unsupported characters")

    uid = _uid_for(f"{channel}:{request.role}")
    rtc_token = RtcTokenBuilder.build_token_with_uid(
        settings.agora_app_id,
        settings.agora_app_certificate,
        channel,
        uid,
        Role_Publisher,
        settings.token_ttl,
        settings.token_ttl,
    )
    if not rtc_token:
        raise HTTPException(
            status_code=503,
            detail=(
                "Agora token builder returned an empty token. "
                "AGORA_APP_ID and AGORA_APP_CERTIFICATE must each be "
                "32-character hexadecimal."
            ),
        )

    return JSONResponse(
        {
            "app_id": settings.agora_app_id,
            "channel": channel,
            "uid": uid,
            "token": rtc_token,
            "expires_at": int(time.time()) + settings.token_ttl,
        }
    )


@app.websocket("/ws/coach")
async def coach_socket(websocket: WebSocket) -> None:
    """Audio in, nudges out, for the duration of one call."""
    settings: Settings = state["settings"]  # type: ignore[assignment]
    _require_join_secret(websocket.query_params.get("token"), settings)
    if not _origin_allowed(websocket.headers.get("origin"), settings):
        raise HTTPException(status_code=403, detail="origin not allowed")

    await websocket.accept()

    coach: Coach = state["coach"]  # type: ignore[assignment]
    transcriber = state["transcriber"]

    engine = CueEngine(
        competitors=settings.competitors,
        expected_duration=settings.expected_duration,
    )

    await websocket.send_json({"type": "ready", "competitors": list(settings.competitors)})

    try:
        while True:
            message = await websocket.receive_json()
            kind = message.get("type")

            if kind == "end":
                await websocket.send_json(_summary(engine))
                return

            if kind != "audio":
                continue

            speaker = SELLER if message.get("speaker") == SELLER else PROSPECT
            try:
                audio = base64.b64decode(message.get("data", ""), validate=True)
            except (binascii.Error, ValueError):
                log.warning("dropping chunk with undecodable audio payload")
                continue

            if len(audio) > MAX_CHUNK_BYTES:
                log.warning("dropping oversized chunk (%d bytes)", len(audio))
                continue

            text = await transcriber.transcribe(audio)
            if not text:
                continue

            at = finite_nonneg(message.get("at"))
            duration = finite_nonneg(message.get("duration"))
            engine.add(Utterance(speaker=speaker, text=text, at=at, duration=duration))
            await websocket.send_json({"type": "transcript", "speaker": speaker, "text": text})

            fired = engine.evaluate()
            if not fired:
                continue

            # Phrase and deliver first. accept() starts cooldowns; burning them
            # on a disconnect during phrasing would suppress the cue for minutes
            # with no nudge shown.
            cue = fired[0]
            payload = await coach.phrase(cue, _recent(engine))
            await websocket.send_json(payload)
            engine.accept(cue)

    except WebSocketDisconnect:
        log.info("coach socket closed by client")
    except Exception:  # noqa: BLE001 - never let one call take the service down
        log.exception("coach socket failed")
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass


def _recent(engine: CueEngine, turns: int = 8) -> str:
    return "\n".join(f"{u.speaker}: {u.text}" for u in engine.recent(turns))


def _summary(engine: CueEngine) -> dict:
    whole = engine.metrics(window=None)
    return {
        "type": "summary",
        "duration_seconds": round(whole.elapsed, 1),
        "talk_ratio": round(whole.talk_ratio, 3),
        "seller_questions": whole.seller_questions,
        "longest_monologue_seconds": round(whole.longest_seller_monologue, 1),
    }


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
