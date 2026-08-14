"""Transcript redaction, applied before any text reaches the model gateway.

BD Coach's existing DLP hook (`infra/litellm/hooks/dlp_hook.py`) guards the
*outbound* direction: it blocks model responses that leak comp data to the
wrong persona. Live call coaching creates the opposite exposure — raw speech
from a call, including whatever the prospect happened to say, flowing *into*
a model.

That matters because LiteLLM is configured with a Groq fallback. On a normal
day the transcript never leaves the box; on the day Ollama is down it does.
Redacting at the source means the failover path is safe by construction rather
than by hoping the primary stays up.

Same rule file as the outbound hook, read in the other direction: anything the
hook would block on the way out is scrubbed on the way in.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

import yaml

# Mounted read-only into the container, same path the LiteLLM hook uses.
DEFAULT_RULES_PATH = pathlib.Path("/dlp/restricted_hr_comp.yaml")

# Long digit runs that look like card or account numbers. Not in the shared
# rule file because the outbound hook has no reason to care — but a prospect
# reading a number aloud on a call is exactly the inbound case.
_LONG_NUMBER = re.compile(r"\b(?:\d[ -]?){13,19}\b")


@dataclass(frozen=True)
class RedactionResult:
    text: str
    rule_ids: tuple[str, ...] = field(default=())

    @property
    def redacted(self) -> bool:
        return len(self.rule_ids) > 0


class Redactor:
    """Applies the shared DLP patterns as substitutions rather than as a gate."""

    def __init__(self, rules: dict) -> None:
        compiled: list[tuple[str, re.Pattern[str]]] = []
        rule_block = rules.get("rules", {})
        # First, so a card number read aloud is labelled `long_number` rather
        # than being partially eaten by the looser `phone_number` pattern. The
        # placeholder label ends up in the audit trail, so it should be right.
        if rule_block:
            compiled.append(("long_number", _LONG_NUMBER))
        # Both severities are redacted inbound. `warn_log_only` is only "warn"
        # for an outbound response the operator already trusts; inbound it is
        # raw PII from a third party and gets the same treatment.
        for section in ("block_for_non_ceo", "warn_log_only"):
            for rule in rule_block.get(section, []) or []:
                compiled.append((rule["id"], re.compile(rule["pattern"])))
        self._rules = tuple(compiled)

    @classmethod
    def from_path(cls, path: pathlib.Path = DEFAULT_RULES_PATH) -> "Redactor":
        return cls(yaml.safe_load(path.read_text()))

    def redact(self, text: str) -> RedactionResult:
        """Replace every match with a labelled placeholder.

        The placeholder keeps the rule id so a coach prompt still reads
        sensibly ("they mentioned [REDACTED:salary_amount]") and so the audit
        trail can say what was removed without storing what it was.
        """
        if not text:
            return RedactionResult(text="", rule_ids=())

        hits: list[str] = []
        out = text
        for rule_id, pattern in self._rules:
            out, count = pattern.subn(f"[REDACTED:{rule_id}]", out)
            if count:
                hits.append(rule_id)

        # Preserve rule order, drop duplicates.
        seen: set[str] = set()
        ordered = tuple(r for r in hits if not (r in seen or seen.add(r)))
        return RedactionResult(text=out, rule_ids=ordered)
