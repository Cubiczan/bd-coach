"""Turns a fired cue into one short line for the seller.

The model's job here is narrow on purpose. The *decision* to interrupt and the
*substance* of the advice both come from `cues.py`, deterministically. The model
only phrases it — which is why a model outage degrades to showing the cue's own
wording rather than to no coaching at all.
"""

from __future__ import annotations

import logging
import pathlib

import httpx

from .cues import Cue
from .redaction import Redactor

log = logging.getLogger("coach.coaching")

FALLBACK_PROMPT = """You are BD Coach, whispering to a salesperson during a live call.

You will be given an observation about the call and the intent of the nudge.
Rewrite it as ONE line the seller can absorb at a glance while still talking.

RULES:
- One sentence. Under 15 words. No preamble, no sign-off, no quotes.
- Imperative voice: tell them what to do now.
- Never invent facts about the prospect, the product, or the pricing.
- Never repeat the transcript back to them.
- If the observation is ambiguous, prefer the safest coaching action.
"""

# A nudge nobody can read mid-sentence is worse than no nudge.
MAX_TOKENS = 40
TIMEOUT_SECONDS = 8.0


def load_prompt(path: str) -> str:
    try:
        text = pathlib.Path(path).read_text().strip()
        return text or FALLBACK_PROMPT
    except OSError:
        log.warning("coach prompt not found at %s, using built-in", path)
        return FALLBACK_PROMPT


class Coach:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        redactor: Redactor,
        client: httpx.AsyncClient,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt
        self._redactor = redactor
        self._client = client

    async def phrase(self, cue: Cue, recent_transcript: str = "") -> dict:
        """Produce the nudge to show. Always returns something displayable."""
        # Unreadable DLP rules: fail closed for the model path. Show the cue's
        # own wording rather than sending an unredacted transcript to LiteLLM
        # (which may fail over to Groq).
        if not self._redactor.loaded:
            log.error("DLP rules not loaded — skipping model phrasing")
            return {
                "type": "nudge",
                "cue": cue.id,
                "evidence": cue.evidence,
                "text": cue.intent,
                "model_used": False,
                "redacted_rules": [],
            }

        # Redact before the text leaves this process — LiteLLM may fail over to
        # a cloud provider, and that decision is made downstream of here.
        scrubbed = self._redactor.redact(recent_transcript)

        user = (
            f"Observation: {cue.evidence}\n"
            f"Intent: {cue.intent}\n"
            f"Recent call context (redacted): {scrubbed.text[-800:] or '(none)'}"
        )

        text = await self._complete(user)
        return {
            "type": "nudge",
            "cue": cue.id,
            "evidence": cue.evidence,
            "text": text or cue.intent,
            "model_used": bool(text),
            "redacted_rules": list(scrubbed.rule_ids),
        }

    async def _complete(self, user_message: str) -> str:
        try:
            response = await self._client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "max_tokens": MAX_TOKENS,
                    "temperature": 0.3,
                    "messages": [
                        {"role": "system", "content": self._system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    # Read by the existing LiteLLM audit + DLP hooks.
                    "metadata": {"persona": "USA_BD", "source": "live-coach"},
                },
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Degrade to the cue's own wording rather than dropping the nudge.
            log.warning("coach model call failed, falling back to cue text: %s", exc)
            return ""

        try:
            return str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError):
            return ""
