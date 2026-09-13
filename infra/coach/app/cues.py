"""Deterministic cue engine — decides *when* the coach should speak up.

The naive version of live coaching pipes every transcript chunk to an LLM and
shows whatever comes back. That fails twice: it burns a model call every few
seconds on a self-hosted box that is also serving the rest of BD Coach, and it
buries the seller in advice during a live call, which is worse than silence.

So the decision to interrupt is made here, in plain arithmetic over the
transcript, and the model is only asked to phrase the nudge once a cue has
actually fired. That keeps model calls rare and purposeful, makes every
interruption explainable after the fact ("talk ratio was 78% over 90s"), and
means the coaching behaviour is unit-testable without a model in the loop —
matching how the rest of BD Coach scores pipeline.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

SELLER = "seller"
PROSPECT = "prospect"

# Window over which talk-ratio and monologue are measured.
ROLLING_WINDOW_SECONDS = 90.0

# Per-cue cooldown: the same nudge must not fire twice in this many seconds.
DEFAULT_COOLDOWN_SECONDS = 240.0

# Nothing at all fires within this many seconds of the previous nudge,
# whatever the cue. One thing to read at a time.
GLOBAL_COOLDOWN_SECONDS = 45.0

_QUESTION = re.compile(r"\?|^(?:what|why|how|when|who|which|where|can you|could you|would you|tell me|walk me)\b", re.I)

_PRICING = re.compile(r"\b(price|pricing|cost|discount|budget|quote|rate card|per seat|licen[cs]e fee)\b", re.I)

_OBJECTION = re.compile(
    r"\b(too expensive|not sure|concerned|worried|hesitant|push ?back|"
    r"already (?:have|use)|competitor|not a priority|no budget|bad timing)\b",
    re.I,
)

_NEXT_STEP = re.compile(
    r"\b(next step|follow up|follow-up|send you|schedule|calendar|book (?:a|some) time|"
    r"circle back|proposal|trial|pilot)\b",
    re.I,
)


def finite_nonneg(value: object, default: float = 0.0) -> float:
    """Coerce a client-supplied timestamp/duration to a finite, non-negative float."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number) or number < 0:
        return default
    return number


@dataclass(frozen=True)
class Utterance:
    speaker: str
    text: str
    at: float
    """Seconds since the call started."""
    duration: float


@dataclass(frozen=True)
class Cue:
    id: str
    priority: int
    evidence: str
    """Short, factual statement of what was observed. Goes in the audit log."""
    intent: str
    """What the seller should consider doing. The model rephrases, not invents."""


@dataclass
class CallMetrics:
    seller_seconds: float = 0.0
    prospect_seconds: float = 0.0
    seller_questions: int = 0
    longest_seller_monologue: float = 0.0
    elapsed: float = 0.0

    @property
    def talk_ratio(self) -> float:
        """Share of speaking time taken by the seller, 0.0-1.0."""
        total = self.seller_seconds + self.prospect_seconds
        return 0.0 if total <= 0 else self.seller_seconds / total


class CueEngine:
    """Accumulates utterances and reports which cues have fired."""

    def __init__(
        self,
        *,
        competitors: tuple[str, ...] = (),
        expected_duration: float = 1800.0,
        cooldown: float = DEFAULT_COOLDOWN_SECONDS,
        global_cooldown: float = GLOBAL_COOLDOWN_SECONDS,
    ) -> None:
        self._utterances: list[Utterance] = []
        self._competitors = tuple(c.lower() for c in competitors if c.strip())
        self._expected_duration = expected_duration
        self._cooldown = cooldown
        self._global_cooldown = global_cooldown
        self._last_fired: dict[str, float] = {}
        self._last_any: float = -1e9

    def add(self, utterance: Utterance) -> None:
        at = finite_nonneg(utterance.at)
        duration = finite_nonneg(utterance.duration)
        if at != utterance.at or duration != utterance.duration:
            utterance = Utterance(
                speaker=utterance.speaker,
                text=utterance.text,
                at=at,
                duration=duration,
            )
        # Concurrent seller/prospect recorders can finish out of `at` order.
        # Metrics treat list order as the timeline, so insert by start time.
        utterances = self._utterances
        index = len(utterances)
        while index > 0 and utterances[index - 1].at > utterance.at:
            index -= 1
        utterances.insert(index, utterance)

    def recent(self, turns: int = 8) -> tuple[Utterance, ...]:
        """The last few turns, for building model context."""
        return tuple(self._utterances[-turns:])

    # ── metrics ────────────────────────────────────────────────────────────

    def metrics(self, window: float | None = ROLLING_WINDOW_SECONDS) -> CallMetrics:
        """Metrics over the trailing `window`, or the whole call when None."""
        if not self._utterances:
            return CallMetrics()

        now = self._now()
        cutoff = -1e9 if window is None else now - window

        m = CallMetrics(elapsed=now)
        run = 0.0
        previous_end: float | None = None
        for u in self._utterances:
            # Clip to the window rather than counting the whole utterance. A
            # four-minute monologue that ended just inside the window must not
            # contribute four minutes of airtime to a ninety-second measure —
            # that would keep the talk-ratio cue firing long after the seller
            # handed the call back.
            overlap = (u.at + u.duration) - max(u.at, cutoff)
            if overlap <= 0:
                # Fully aged-out turns must not rewind previous_end. A short
                # prospect utterance that starts during a longer in-window
                # seller stretch but ends before cutoff would otherwise make
                # the next seller chunk look like a silence gap and reset run.
                continue

            if u.speaker == SELLER:
                if previous_end is not None and u.at > previous_end:
                    run = 0.0
                m.seller_seconds += overlap
                run += overlap
                m.longest_seller_monologue = max(m.longest_seller_monologue, run)
                if _QUESTION.search(u.text):
                    m.seller_questions += 1
            else:
                m.prospect_seconds += overlap
                run = 0.0
            previous_end = u.at + u.duration
        return m

    def _now(self) -> float:
        return max(u.at + u.duration for u in self._utterances)

    def _said(self, pattern: re.Pattern[str], speaker: str | None = None, window: float = 60.0) -> bool:
        cutoff = self._now() - window
        return any(
            u.at + u.duration >= cutoff
            and (speaker is None or u.speaker == speaker)
            and pattern.search(u.text)
            for u in self._utterances
        )

    def _competitor_mentioned(self, window: float = 60.0) -> str | None:
        if not self._competitors:
            return None
        cutoff = self._now() - window
        for u in self._utterances:
            if u.at + u.duration < cutoff:
                continue
            lowered = u.text.lower()
            for name in self._competitors:
                if name in lowered:
                    return name
        return None

    # ── cues ───────────────────────────────────────────────────────────────

    def evaluate(self) -> list[Cue]:
        """Every cue currently firing, highest priority first, cooldowns applied."""
        if not self._utterances:
            return []

        now = self._now()
        if now - self._last_any < self._global_cooldown:
            return []

        window = self.metrics()
        whole = self.metrics(window=None)
        candidates: list[Cue] = []

        # A prospect who is not talking is not buying. This is the single most
        # reliable live signal, so it outranks everything else.
        if window.talk_ratio >= 0.70 and (window.seller_seconds + window.prospect_seconds) >= 30:
            candidates.append(
                Cue(
                    id="talk_ratio",
                    priority=100,
                    evidence=f"You have {window.talk_ratio:.0%} of the airtime over the last {int(ROLLING_WINDOW_SECONDS)}s.",
                    intent="Stop and ask an open question. Let them talk.",
                )
            )

        if window.longest_seller_monologue >= 75:
            candidates.append(
                Cue(
                    id="monologue",
                    priority=90,
                    evidence=f"You have been speaking for {int(window.longest_seller_monologue)}s without a break.",
                    intent="Land the point and hand back with a check-in question.",
                )
            )

        # An objection left unacknowledged hardens. Surface it immediately.
        if self._said(_OBJECTION, speaker=PROSPECT, window=45.0):
            candidates.append(
                Cue(
                    id="objection",
                    priority=95,
                    evidence="The prospect just raised a concern.",
                    intent="Acknowledge it and ask what is behind it before answering.",
                )
            )

        competitor = self._competitor_mentioned(window=45.0)
        if competitor is not None:
            candidates.append(
                Cue(
                    id="competitor",
                    priority=85,
                    evidence=f'A competitor was mentioned: "{competitor}".',
                    intent="Ask what they like about it before differentiating.",
                )
            )

        # Talking price before you know what they need is how discounts happen.
        if self._said(_PRICING, window=45.0) and whole.seller_questions < 3:
            candidates.append(
                Cue(
                    id="early_pricing",
                    priority=80,
                    evidence=f"Pricing came up after only {whole.seller_questions} discovery question(s).",
                    intent="Anchor on value: ask what problem this has to solve first.",
                )
            )

        if whole.elapsed >= 240 and whole.seller_questions < 2:
            candidates.append(
                Cue(
                    id="no_discovery",
                    priority=75,
                    evidence=f"{int(whole.elapsed / 60)} minutes in with {whole.seller_questions} question(s) asked.",
                    intent="Move into discovery — you are presenting, not qualifying.",
                )
            )

        # Late enough that a call ending without a next step is a lost call.
        if whole.elapsed >= self._expected_duration * 0.8 and not self._said(
            _NEXT_STEP, window=whole.elapsed
        ):
            candidates.append(
                Cue(
                    id="no_next_step",
                    priority=88,
                    evidence="The call is nearly over and no next step has been proposed.",
                    intent="Propose a specific next step with a date.",
                )
            )

        fired = [c for c in candidates if now - self._last_fired.get(c.id, -1e9) >= self._cooldown]
        fired.sort(key=lambda c: c.priority, reverse=True)
        return fired

    def accept(self, cue: Cue) -> None:
        """Record that a cue was actually shown, starting its cooldown.

        Separate from `evaluate()` so a caller that decides to drop a cue (model
        unavailable, seller muted the coach) does not silently burn its cooldown.
        """
        now = self._now()
        self._last_fired[cue.id] = now
        self._last_any = now
