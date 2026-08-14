"""Configuration for the live coach service.

Everything here has a default that points at a service already in the BD Coach
compose stack. The intent is that the only thing an operator must supply is the
Agora credential pair — the transcription and model paths stay in-house without
any further decisions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ── Agora: transport only ──────────────────────────────────────────────
    agora_app_id: str
    agora_app_certificate: str
    token_ttl: int

    # ── In-house pipeline ──────────────────────────────────────────────────
    whisper_url: str
    whisper_model: str
    litellm_url: str
    litellm_key: str
    coach_model: str

    # ── Coaching behaviour ─────────────────────────────────────────────────
    competitors: tuple[str, ...]
    expected_duration: float
    prompt_path: str
    dlp_rules_path: str

    # ── Optional outbound ──────────────────────────────────────────────────
    mattermost_webhook: str

    @property
    def configured(self) -> bool:
        """Can we mint tokens at all? Everything else has a working default."""
        return self.agora_app_id != "" and self.agora_app_certificate != ""


def load_settings() -> Settings:
    competitors = tuple(
        part.strip() for part in _env("COACH_COMPETITORS").split(",") if part.strip()
    )
    return Settings(
        agora_app_id=_env("AGORA_APP_ID"),
        agora_app_certificate=_env("AGORA_APP_CERTIFICATE"),
        token_ttl=_int("AGORA_TOKEN_TTL", 3600),
        # faster-whisper-server speaks the OpenAI audio API.
        whisper_url=_env("COACH_WHISPER_URL", "http://whisper:8000/v1/audio/transcriptions"),
        whisper_model=_env("COACH_WHISPER_MODEL", "Systran/faster-whisper-base"),
        litellm_url=_env("COACH_LITELLM_URL", "http://litellm:4000/v1/chat/completions"),
        litellm_key=_env("LITELLM_MASTER_KEY"),
        # Pinned to the BD persona model; see docs for the failover caveat.
        coach_model=_env("COACH_MODEL", "bd-coach-bd"),
        competitors=competitors,
        expected_duration=float(_int("COACH_EXPECTED_DURATION", 1800)),
        prompt_path=_env("COACH_PROMPT_PATH", "/config/prompts/live_coach.v1.0.md"),
        dlp_rules_path=_env("COACH_DLP_RULES", "/dlp/restricted_hr_comp.yaml"),
        mattermost_webhook=_env("MM_HOOK_COACH"),
    )
