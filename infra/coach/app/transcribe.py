"""Speech-to-text against the stack's own Whisper container.

BD Coach already runs `faster-whisper-server`, which exposes the OpenAI audio
API. Sending call audio there instead of to Agora's Real-Time Transcription is
the whole reason this integration is defensible: Agora relays the call, but the
words are only ever transcribed on hardware the operator controls.

Agora's hosted STT would be less code and lower latency. It would also mean
every sales conversation is processed by a third party, which contradicts the
self-hostable, DLP-gated posture the rest of BD Coach is built around. If a
team decides that trade is fine for them, `docs/LIVE-COACH.md` says what to
change; it is deliberately not the default.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger("coach.transcribe")


class Transcriber:
    def __init__(self, url: str, model: str, client: httpx.AsyncClient) -> None:
        self._url = url
        self._model = model
        self._client = client

    async def transcribe(self, audio: bytes, *, filename: str = "chunk.webm") -> str:
        """Transcribe one audio chunk. Returns "" on any failure.

        A dropped chunk degrades coaching quality slightly; a raised exception
        would tear down the websocket and end coaching entirely. During a live
        sales call the first is clearly preferable, so failures are swallowed
        and logged.
        """
        if not audio:
            return ""

        try:
            response = await self._client.post(
                self._url,
                files={"file": (filename, audio, "audio/webm")},
                data={"model": self._model, "response_format": "json"},
                timeout=20.0,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("transcription failed, dropping chunk: %s", exc)
            return ""

        return str(payload.get("text", "")).strip()


async def transcribe_many(
    transcriber: Transcriber, chunks: list[tuple[str, bytes]]
) -> list[tuple[str, str]]:
    """Transcribe several speakers' chunks concurrently, preserving order."""
    results = await asyncio.gather(
        *(transcriber.transcribe(audio) for _, audio in chunks),
        return_exceptions=False,
    )
    return [(speaker, text) for (speaker, _), text in zip(chunks, results)]
