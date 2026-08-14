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

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .coaching import Coach, load_prompt
from .config import Settings, load_settings
from .cues import PROSPECT, SELLER, CueEngine, Utterance
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
    """Fail closed: with no rule file, redact nothing but say so loudly.

    An empty ruleset is not silently equivalent to a working one, so this logs
    at error level — but it does not stop the service, because a missing mount
    should not take a sales team's calls offline.
    """
    try:
        return Redactor.from_path(pathlib.Path(settings.dlp_rules_path))
    except (OSError, ValueError) as exc:
        log.error(
            "DLP rules unreadable at %s (%s) — transcripts will NOT be redacted "
            "before reaching the model gateway. Fix the /dlp mount.",
            settings.dlp_rules_path,
            exc,
        )
        return Redactor({"rules": {}})


app = FastAPI(title="BD Coach — live call coaching", lifespan=lifespan)


class TokenRequest(BaseModel):
    call_id: str = Field(min_length=1, max_length=48)
    role: str = Field(default=SELLER)


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
            "transcription": "in-house (whisper)",
            "model_gateway": settings.coach_model,
        }
    )


@app.post("/token")
async def token(request: TokenRequest) -> JSONResponse:
    settings: Settings = state["settings"]  # type: ignore[assignment]
    if not settings.configured:
        raise HTTPException(status_code=503, detail="AGORA_APP_ID / AGORA_APP_CERTIFICATE are unset")

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
    await websocket.accept()

    settings: Settings = state["settings"]  # type: ignore[assignment]
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

            at = float(message.get("at", 0.0))
            duration = float(message.get("duration", 0.0))
            engine.add(Utterance(speaker=speaker, text=text, at=at, duration=duration))
            await websocket.send_json({"type": "transcript", "speaker": speaker, "text": text})

            fired = engine.evaluate()
            if not fired:
                continue

            # One nudge at a time — the highest-priority cue wins and the rest
            # keep their cooldowns intact for later in the call.
            cue = fired[0]
            engine.accept(cue)
            await websocket.send_json(await coach.phrase(cue, _recent(engine)))

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
