#!/usr/bin/env python3
"""Tests for the live-coach cue engine and transcript redaction.

Stdlib + pyyaml only, matching config/dlp/run_tests.py — CI must be able to run
these without installing FastAPI, httpx, or a model runtime. The two modules
under test are pure by design precisely so this is possible.

    python infra/coach/run_tests.py
"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.cues import (  # noqa: E402
    GLOBAL_COOLDOWN_SECONDS,
    PROSPECT,
    SELLER,
    CueEngine,
    Utterance,
)
from app.redaction import Redactor  # noqa: E402

REPO_ROOT = ROOT.parent.parent
DLP_RULES = REPO_ROOT / "config" / "dlp" / "restricted_hr_comp.yaml"


def seller(text: str, at: float, duration: float = 5.0) -> Utterance:
    return Utterance(speaker=SELLER, text=text, at=at, duration=duration)


def prospect(text: str, at: float, duration: float = 5.0) -> Utterance:
    return Utterance(speaker=PROSPECT, text=text, at=at, duration=duration)


def fired_ids(engine: CueEngine) -> list[str]:
    return [c.id for c in engine.evaluate()]


class TalkRatioTests(unittest.TestCase):
    def test_no_cues_on_an_empty_call(self):
        self.assertEqual(fired_ids(CueEngine()), [])

    def test_seller_dominating_fires_talk_ratio(self):
        engine = CueEngine()
        engine.add(prospect("Sure, go ahead.", at=0, duration=5))
        engine.add(seller("So the way this works is", at=5, duration=40))
        self.assertIn("talk_ratio", fired_ids(engine))

    def test_balanced_conversation_stays_quiet(self):
        engine = CueEngine()
        engine.add(seller("What are you using today?", at=0, duration=15))
        engine.add(prospect("Mostly spreadsheets and a lot of manual work.", at=15, duration=20))
        self.assertNotIn("talk_ratio", fired_ids(engine))

    def test_short_exchanges_do_not_trip_the_ratio(self):
        # Guards against firing three seconds into a call on "hi" / "hello".
        engine = CueEngine()
        engine.add(seller("Hi there.", at=0, duration=2))
        self.assertNotIn("talk_ratio", fired_ids(engine))

    def test_talk_ratio_is_measured_over_a_window_not_the_whole_call(self):
        engine = CueEngine()
        engine.add(seller("Long opening monologue.", at=0, duration=200))
        engine.add(prospect("That makes sense, tell me more about the rollout.", at=200, duration=60))
        # The old monologue has aged out; recent airtime is the prospect's.
        self.assertNotIn("talk_ratio", fired_ids(engine))


class MonologueTests(unittest.TestCase):
    def test_long_uninterrupted_stretch_fires(self):
        engine = CueEngine()
        engine.add(seller("Let me walk you through the architecture.", at=0, duration=80))
        self.assertIn("monologue", fired_ids(engine))

    def test_prospect_turn_resets_the_run(self):
        engine = CueEngine()
        engine.add(seller("First half.", at=0, duration=40))
        engine.add(prospect("Got it.", at=40, duration=3))
        engine.add(seller("Second half.", at=43, duration=40))
        self.assertNotIn("monologue", fired_ids(engine))


class ObjectionAndCompetitorTests(unittest.TestCase):
    def test_prospect_concern_fires_an_objection_cue(self):
        engine = CueEngine()
        engine.add(seller("What did you think?", at=0, duration=4))
        engine.add(prospect("Honestly it feels too expensive for us.", at=4, duration=6))
        self.assertIn("objection", fired_ids(engine))

    def test_seller_saying_the_same_words_does_not_fire_it(self):
        # The cue is about the prospect's position, not vocabulary on the call.
        engine = CueEngine()
        engine.add(prospect("Tell me about pricing tiers.", at=0, duration=10))
        engine.add(seller("Some customers worry it is too expensive at first.", at=10, duration=6))
        self.assertNotIn("objection", fired_ids(engine))

    def test_competitor_fires_only_when_configured(self):
        without = CueEngine()
        without.add(prospect("We already looked at Gong for this.", at=0, duration=8))
        self.assertNotIn("competitor", fired_ids(without))

        with_names = CueEngine(competitors=("Gong", "Chorus"))
        with_names.add(prospect("We already looked at Gong for this.", at=0, duration=8))
        self.assertIn("competitor", fired_ids(with_names))

    def test_competitor_match_is_case_insensitive(self):
        engine = CueEngine(competitors=("Gong",))
        engine.add(prospect("we evaluated GONG last quarter", at=0, duration=8))
        self.assertIn("competitor", fired_ids(engine))


class DiscoveryTests(unittest.TestCase):
    def test_pricing_before_discovery_fires(self):
        engine = CueEngine()
        engine.add(seller("Our pricing starts at the team tier.", at=0, duration=10))
        self.assertIn("early_pricing", fired_ids(engine))

    def test_pricing_after_real_discovery_does_not(self):
        engine = CueEngine()
        engine.add(seller("What does your process look like today?", at=0, duration=6))
        engine.add(prospect("Manual.", at=6, duration=20))
        engine.add(seller("How many reps are affected?", at=26, duration=5))
        engine.add(prospect("About thirty.", at=31, duration=20))
        engine.add(seller("Why does that matter now?", at=51, duration=5))
        engine.add(prospect("Board pressure.", at=56, duration=20))
        engine.add(seller("Then let us talk pricing.", at=76, duration=5))
        self.assertNotIn("early_pricing", fired_ids(engine))

    def test_presenting_without_asking_anything_fires_no_discovery(self):
        engine = CueEngine()
        engine.add(seller("Here is our platform overview.", at=0, duration=150))
        engine.add(prospect("Mm hm.", at=150, duration=100))
        engine.add(seller("And here is the integration story.", at=250, duration=30))
        self.assertIn("no_discovery", fired_ids(engine))

    def test_questions_are_detected_by_word_order_not_just_punctuation(self):
        engine = CueEngine()
        engine.add(seller("Walk me through how that works today", at=0, duration=5))
        self.assertEqual(engine.metrics(window=None).seller_questions, 1)


class NextStepTests(unittest.TestCase):
    def test_late_call_without_a_next_step_fires(self):
        engine = CueEngine(expected_duration=600)
        engine.add(seller("So that is the overview.", at=0, duration=300))
        engine.add(prospect("Understood.", at=300, duration=250))
        self.assertIn("no_next_step", fired_ids(engine))

    def test_proposing_a_next_step_clears_it(self):
        engine = CueEngine(expected_duration=600)
        engine.add(seller("So that is the overview.", at=0, duration=300))
        engine.add(prospect("Understood.", at=300, duration=200))
        engine.add(seller("Shall we schedule a follow up on Thursday?", at=500, duration=50))
        self.assertNotIn("no_next_step", fired_ids(engine))


class CooldownTests(unittest.TestCase):
    def test_global_cooldown_suppresses_everything_briefly(self):
        engine = CueEngine()
        engine.add(prospect("ok", at=0, duration=5))
        engine.add(seller("long stretch", at=5, duration=60))
        first = engine.evaluate()
        self.assertTrue(first)
        engine.accept(first[0])

        engine.add(seller("still going", at=65, duration=10))
        self.assertEqual(fired_ids(engine), [])

    def test_same_cue_does_not_repeat_within_its_cooldown(self):
        engine = CueEngine(cooldown=300, global_cooldown=10)
        engine.add(prospect("ok", at=0, duration=5))
        engine.add(seller("long stretch", at=5, duration=60))
        cue = engine.evaluate()[0]
        engine.accept(cue)

        engine.add(seller("more talking", at=100, duration=60))
        self.assertNotIn(cue.id, fired_ids(engine))

    def test_evaluating_without_accepting_does_not_burn_the_cooldown(self):
        # A caller that drops a nudge (coach muted, model down) must still be
        # able to surface that cue later.
        engine = CueEngine()
        engine.add(prospect("ok", at=0, duration=5))
        engine.add(seller("long stretch", at=5, duration=60))
        self.assertTrue(engine.evaluate())
        self.assertTrue(engine.evaluate())

    def test_cues_are_returned_highest_priority_first(self):
        engine = CueEngine()
        engine.add(prospect("this feels too expensive honestly", at=0, duration=5))
        engine.add(seller("Let me explain the pricing model in detail.", at=5, duration=60))
        ids = fired_ids(engine)
        self.assertTrue(ids)
        priorities = [c.priority for c in engine.evaluate()]
        self.assertEqual(priorities, sorted(priorities, reverse=True))


class MetricsTests(unittest.TestCase):
    def test_summary_metrics_cover_the_whole_call(self):
        engine = CueEngine()
        engine.add(seller("What are you using today?", at=0, duration=10))
        engine.add(prospect("Spreadsheets.", at=10, duration=30))
        whole = engine.metrics(window=None)
        self.assertAlmostEqual(whole.talk_ratio, 0.25)
        self.assertEqual(whole.seller_questions, 1)
        self.assertAlmostEqual(whole.elapsed, 40.0)

    def test_talk_ratio_is_zero_on_silence(self):
        self.assertEqual(CueEngine().metrics().talk_ratio, 0.0)


class RedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.redactor = Redactor.from_path(DLP_RULES)

    def test_clean_transcript_is_untouched(self):
        text = "They want to roll this out to the whole revenue team next quarter."
        result = self.redactor.redact(text)
        self.assertEqual(result.text, text)
        self.assertFalse(result.redacted)

    def test_email_is_removed_before_the_model_sees_it(self):
        result = self.redactor.redact("Send it to dana.miller@acme.example please")
        self.assertNotIn("dana.miller@acme.example", result.text)
        self.assertIn("pii_email", result.rule_ids)

    def test_salary_figures_are_removed(self):
        result = self.redactor.redact("His base salary is $180,000 apparently")
        self.assertNotIn("180,000", result.text)
        self.assertTrue(result.redacted)

    def test_long_numbers_read_aloud_are_removed(self):
        result = self.redactor.redact("The card is 4111 1111 1111 1111 ok?")
        self.assertNotIn("4111", result.text)
        self.assertIn("long_number", result.rule_ids)

    def test_placeholder_keeps_the_sentence_readable(self):
        result = self.redactor.redact("Email me at a@b.co")
        self.assertIn("[REDACTED:pii_email]", result.text)

    def test_empty_input_is_safe(self):
        result = self.redactor.redact("")
        self.assertEqual(result.text, "")
        self.assertFalse(result.redacted)

    def test_rule_ids_are_deduplicated(self):
        result = self.redactor.redact("a@b.co and c@d.co and e@f.co")
        self.assertEqual(result.rule_ids.count("pii_email"), 1)

    def test_missing_rules_redact_nothing_but_still_work(self):
        # Mirrors the fail-open-with-a-loud-log path in main._load_redactor.
        empty = Redactor({"rules": {}})
        result = empty.redact("base salary is $180,000")
        self.assertIn("180,000", result.text)


def main() -> int:
    if not DLP_RULES.is_file():
        print(f"DLP rules not found at {DLP_RULES}", file=sys.stderr)
        return 1

    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
