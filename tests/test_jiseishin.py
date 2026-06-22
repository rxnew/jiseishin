#!/usr/bin/env python3
"""Tests for jiseishin.py — Python standard library (unittest) only.

Run: python3 -m unittest discover -s tests
  or python3 tests/test_jiseishin.py

The TZ is pinned to UTC for the whole module so the UTC transcript timestamps
("...Z") map to the same calendar date the test asserts on, regardless of where
the test runs. Date attribution is exercised explicitly in its own test.
"""
import os
import sys
import json
import time
import shutil
import tempfile
import datetime
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import jiseishin as j


def asst_line(message_id, *, model="claude-opus-4-8", timestamp,
              request_id=None, uuid=None, **usage):
    """Build one assistant transcript line (a billed response)."""
    return json.dumps({
        "type": "assistant",
        "timestamp": timestamp,
        "requestId": request_id or (message_id + "-req"),
        "uuid": uuid or (message_id + "-uuid"),
        "message": {"id": message_id, "model": model, "usage": usage},
    })


def user_line(text, *, timestamp="2026-06-22T00:00:00.000Z"):
    return json.dumps({
        "type": "user",
        "timestamp": timestamp,
        "uuid": "u-" + str(abs(hash(text)) % 10**8),
        "message": {"content": text},
    })


def cost_on(date_str):
    """date_cost for a YYYY-MM-DD string (test convenience)."""
    return j.date_cost(datetime.date.fromisoformat(date_str))


def write_transcript(path, lines):
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def append_transcript(path, lines):
    with open(path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


class PricingTest(unittest.TestCase):
    def test_per_model_rates(self):
        # 1M input @ $5 + 1M output @ $25 for Opus.
        self.assertAlmostEqual(
            j.usage_cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                             "claude-opus-4-8"), 30.0)
        # Sonnet: 3 + 15.
        self.assertAlmostEqual(
            j.usage_cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                             "claude-sonnet-4-6"), 18.0)
        # Haiku: 1 + 5.
        self.assertAlmostEqual(
            j.usage_cost_usd({"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                             "claude-haiku-4-5"), 6.0)

    def test_unknown_model_falls_back_to_opus(self):
        self.assertEqual(j.price_for_model("some-future-model"), j.DEFAULT_PRICE)
        self.assertEqual(j.price_for_model(None), j.DEFAULT_PRICE)

    def test_model_normalization(self):
        for name in ("claude-opus-4-8[1m]", "us.anthropic.claude-opus-4-8",
                     "claude-opus-4-8-20260101", "claude-opus-4-8@20260101"):
            self.assertEqual(j.price_for_model(name), (5.0, 25.0), name)

    def test_cache_read_and_write_multipliers(self):
        # cache read: 1M @ 5 * 0.1 = 0.5
        self.assertAlmostEqual(
            j.usage_cost_usd({"cache_read_input_tokens": 1_000_000}, "claude-opus-4-8"), 0.5)
        # cache creation aggregate billed at 5m rate: 1M @ 5 * 1.25 = 6.25
        self.assertAlmostEqual(
            j.usage_cost_usd({"cache_creation_input_tokens": 1_000_000}, "claude-opus-4-8"), 6.25)

    def test_cache_creation_breakdown_overrides_aggregate(self):
        usage = {
            "cache_creation_input_tokens": 1_000_000,  # ignored when breakdown present
            "cache_creation": {
                "ephemeral_5m_input_tokens": 1_000_000,  # 5 * 1.25 = 6.25
                "ephemeral_1h_input_tokens": 1_000_000,  # 5 * 2.0  = 10.0
            },
        }
        self.assertAlmostEqual(j.usage_cost_usd(usage, "claude-opus-4-8"), 16.25)


class MessageDateTest(unittest.TestCase):
    def test_utc_timestamp_to_local_date(self):
        # Under TZ=UTC (set in setUpModule) a Z timestamp keeps its date.
        self.assertEqual(j.message_date({"timestamp": "2026-06-22T10:00:00.000Z"}),
                         "2026-06-22")

    def test_missing_timestamp_falls_back_to_today(self):
        self.assertEqual(j.message_date({}), datetime.date.today().isoformat())


class StateTestBase(unittest.TestCase):
    """Points STATE_ROOT at a fresh temp dir and gives each test its own
    transcript directory."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jiseishin-test-")
        self._orig_state_root = j.STATE_ROOT
        j.STATE_ROOT = os.path.join(self.tmp, "state")
        self.tx = os.path.join(self.tmp, "tx")
        os.makedirs(self.tx)

    def tearDown(self):
        j.STATE_ROOT = self._orig_state_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, name):
        return os.path.join(self.tx, name + ".jsonl")


class SingleContextTest(StateTestBase):
    def test_content_block_lines_counted_once(self):
        # Claude Code writes one line per content block, all repeating the same
        # message.id and usage. The response must be billed once.
        line = asst_line("msg1", timestamp="2026-06-22T10:00:00.000Z",
                         input_tokens=1_000_000, output_tokens=1_000_000)
        path = self.transcript("s1")
        write_transcript(path, [line, line, line])  # 3 content blocks
        j.update_context("s1", path)
        self.assertAlmostEqual(j.date_cost(datetime.date(2026, 6, 22)), 30.0)

    def test_incremental_append_not_recounted(self):
        path = self.transcript("s1")
        write_transcript(path, [
            asst_line("msg1", timestamp="2026-06-22T10:00:00.000Z",
                      input_tokens=1_000_000, output_tokens=1_000_000),
        ])
        j.update_context("s1", path)
        self.assertAlmostEqual(cost_on("2026-06-22"), 30.0)

        # Append a second response; re-running must add only the new one.
        append_transcript(path, [
            asst_line("msg2", timestamp="2026-06-22T11:00:00.000Z",
                      input_tokens=1_000_000),
        ])
        j.update_context("s1", path)
        self.assertAlmostEqual(cost_on("2026-06-22"), 35.0)

        # Re-running with no new bytes changes nothing.
        j.update_context("s1", path)
        self.assertAlmostEqual(cost_on("2026-06-22"), 35.0)

    def test_truncated_transcript_rescans_safely(self):
        path = self.transcript("s1")
        write_transcript(path, [
            asst_line("msg1", timestamp="2026-06-22T10:00:00.000Z", input_tokens=1_000_000),
            asst_line("msg2", timestamp="2026-06-22T10:00:00.000Z", input_tokens=1_000_000),
        ])
        j.update_context("s1", path)
        self.assertAlmostEqual(cost_on("2026-06-22"), 10.0)
        # Rewrite shorter (offset now past EOF) -> full re-scan, dedup keeps it correct.
        write_transcript(path, [
            asst_line("msg1", timestamp="2026-06-22T10:00:00.000Z", input_tokens=1_000_000),
        ])
        j.update_context("s1", path)
        self.assertAlmostEqual(cost_on("2026-06-22"), 5.0)


class CrossContextDedupTest(StateTestBase):
    """The core regression: a resumed/forked session is a new context whose
    transcript copies the prior messages (same ids). The day total must count
    each id once across contexts, not once per context."""
    def test_resumed_session_not_double_counted(self):
        shared = [
            asst_line("shared1", timestamp="2026-06-22T10:00:00.000Z", input_tokens=1_000_000),
            asst_line("shared2", timestamp="2026-06-22T10:05:00.000Z", input_tokens=1_000_000),
        ]
        # Original session.
        p1 = self.transcript("orig")
        write_transcript(p1, shared)
        j.update_context("orig", p1)

        # Resumed session: copies the shared history, then adds its own message.
        p2 = self.transcript("resumed")
        write_transcript(p2, shared + [
            asst_line("new1", timestamp="2026-06-22T11:00:00.000Z", input_tokens=1_000_000),
        ])
        j.update_context("resumed", p2)

        # Naive per-context sum would be (5+5) + (5+5+5) = 25; deduped = 15.
        self.assertAlmostEqual(j.date_cost(datetime.date(2026, 6, 22)), 15.0)

    def test_distinct_contexts_both_counted(self):
        p1 = self.transcript("a")
        write_transcript(p1, [asst_line("a1", timestamp="2026-06-22T10:00:00.000Z",
                                        input_tokens=1_000_000)])
        p2 = self.transcript("b")
        write_transcript(p2, [asst_line("b1", timestamp="2026-06-22T10:00:00.000Z",
                                        input_tokens=1_000_000)])
        j.update_context("a", p1)
        j.update_context("b", p2)
        self.assertAlmostEqual(j.date_cost(datetime.date(2026, 6, 22)), 10.0)


class DateAttributionTest(StateTestBase):
    """A session resumed from an earlier day carries that day's messages; they
    must be billed on their own date, not lumped onto today."""
    def test_cost_bucketed_by_message_timestamp(self):
        path = self.transcript("s")
        write_transcript(path, [
            asst_line("d19", timestamp="2026-06-19T10:00:00.000Z", input_tokens=1_000_000),
            asst_line("d22a", timestamp="2026-06-22T10:00:00.000Z", input_tokens=1_000_000),
            asst_line("d22b", timestamp="2026-06-22T12:00:00.000Z", input_tokens=1_000_000),
        ])
        j.update_context("s", path)
        self.assertAlmostEqual(j.date_cost(datetime.date(2026, 6, 19)), 5.0)
        self.assertAlmostEqual(j.date_cost(datetime.date(2026, 6, 22)), 10.0)
        self.assertAlmostEqual(j.date_cost(datetime.date(2026, 6, 20)), 0.0)


class ClearTest(StateTestBase):
    def _today_ts(self, hour=10):
        return f"{datetime.date.today().isoformat()}T{hour:02d}:00:00.000Z"

    def test_clear_today_keeps_other_days(self):
        path = self.transcript("s")
        write_transcript(path, [
            asst_line("old", timestamp="2026-06-19T10:00:00.000Z", input_tokens=1_000_000),
            asst_line("now1", timestamp=self._today_ts(), input_tokens=1_000_000),
        ])
        j.update_context("s", path)
        self.assertAlmostEqual(j.today_cost(), 5.0)

        removed = j.clear_today()
        self.assertEqual(removed, 1)
        self.assertAlmostEqual(j.today_cost(), 0.0)
        # Other day untouched.
        self.assertAlmostEqual(j.date_cost(datetime.date(2026, 6, 19)), 5.0)

    def test_after_clear_only_new_usage_counts(self):
        path = self.transcript("s")
        write_transcript(path, [
            asst_line("now1", timestamp=self._today_ts(10), input_tokens=1_000_000),
        ])
        j.update_context("s", path)
        j.clear_today()
        self.assertAlmostEqual(j.today_cost(), 0.0)

        # Session continues: a new response appears, the cleared one must not return.
        append_transcript(path, [
            asst_line("now2", timestamp=self._today_ts(11), input_tokens=1_000_000),
        ])
        j.update_context("s", path)
        self.assertAlmostEqual(j.today_cost(), 5.0)

    def test_clear_all_removes_state_root(self):
        path = self.transcript("s")
        write_transcript(path, [asst_line("now1", timestamp=self._today_ts(),
                                          input_tokens=1_000_000)])
        j.update_context("s", path)
        self.assertTrue(os.path.isdir(j.STATE_ROOT))
        rc = j.cmd_clear(["--all"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.isdir(j.STATE_ROOT))


class HookFlowTest(StateTestBase):
    def _seed_today(self, key, cost_tokens):
        path = self.transcript(key)
        write_transcript(path, [
            asst_line("m-" + key, timestamp=f"{datetime.date.today().isoformat()}T10:00:00.000Z",
                      input_tokens=cost_tokens),
        ])
        j.update_context(key, path)
        return path

    def test_check_blocks_at_or_above_limit(self):
        os.environ["JISEISHIN_MAX_DAILY_COST_USD"] = "10"
        try:
            self._seed_today("s", 1_000_000)  # $5, under limit
            self.assertEqual(j.cmd_check({"prompt": "hi"}), 0)
            self._seed_today("s2", 1_000_000)  # +$5 -> $10, at limit
            self.assertEqual(j.cmd_check({"prompt": "hi"}), 2)
        finally:
            del os.environ["JISEISHIN_MAX_DAILY_COST_USD"]

    def test_check_exempts_recovery_commands(self):
        os.environ["JISEISHIN_MAX_DAILY_COST_USD"] = "1"
        try:
            self._seed_today("s", 1_000_000)  # $5, over limit
            self.assertEqual(j.cmd_check({"prompt": "/jiseishin:set-limit 50"}), 0)
            self.assertEqual(j.cmd_check({"prompt": "/jiseishin:status"}), 0)
            self.assertEqual(j.cmd_check({"prompt": "do something"}), 2)
        finally:
            del os.environ["JISEISHIN_MAX_DAILY_COST_USD"]

    def test_guard_exempts_recovery_command_mid_turn(self):
        # A turn started by an exempt command must be able to finish even over
        # the limit: guard reads the last human prompt from the transcript and,
        # if it is exempt, does not stop the loop.
        today = datetime.date.today().isoformat()
        path = self.transcript("sess")
        write_transcript(path, [
            user_line("/jiseishin:status", timestamp=f"{today}T09:59:00.000Z"),
            asst_line("r1", timestamp=f"{today}T10:00:00.000Z", input_tokens=1_000_000),
        ])
        data = {"session_id": "sess", "transcript_path": path}
        os.environ["JISEISHIN_MAX_DAILY_COST_USD"] = "1"  # $5 spend is over it
        try:
            self.assertEqual(j.cmd_guard(data), 0)  # exempt -> not blocked
        finally:
            del os.environ["JISEISHIN_MAX_DAILY_COST_USD"]

    def test_record_then_guard_share_offset(self):
        # Main session: context_key == session_id; record and guard update the
        # same file incrementally and must agree.
        path = self.transcript("sess")
        write_transcript(path, [
            asst_line("r1", timestamp=f"{datetime.date.today().isoformat()}T10:00:00.000Z",
                      input_tokens=1_000_000),
        ])
        data = {"session_id": "sess", "transcript_path": path}
        self.assertEqual(j.cmd_record(data), 0)
        self.assertAlmostEqual(j.today_cost(), 5.0)
        # guard sees no new bytes -> still $5, not blocked under a high limit.
        os.environ["JISEISHIN_MAX_DAILY_COST_USD"] = "100"
        try:
            self.assertEqual(j.cmd_guard(data), 0)
            self.assertAlmostEqual(j.today_cost(), 5.0)
        finally:
            del os.environ["JISEISHIN_MAX_DAILY_COST_USD"]


# ---- module-level TZ pin -------------------------------------------------

_ORIG_TZ = None


def setUpModule():
    global _ORIG_TZ
    _ORIG_TZ = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()


def tearDownModule():
    if _ORIG_TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _ORIG_TZ
    if hasattr(time, "tzset"):
        time.tzset()


if __name__ == "__main__":
    unittest.main(verbosity=2)
