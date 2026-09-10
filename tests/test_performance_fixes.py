"""
Regression tests for the three performance fixes.

Runs offline: no SAP GUI, no RFC, no network. Each test pins a behaviour that
was previously wrong, so a future change that reintroduces the old behaviour
fails here rather than in production.

    python -m pytest tests/test_performance_fixes.py -v
"""
import os, sys, time, unittest
sys.path.insert(0, os.getcwd())

from core import heartbeat


class Heartbeat(unittest.TestCase):
    def test_reset_and_beat(self):
        heartbeat.reset("t:start")
        self.assertLess(heartbeat.seconds_since_beat(), 0.5)
        self.assertEqual(heartbeat.last_stage(), "t:start")
        time.sleep(0.2)
        self.assertGreater(heartbeat.seconds_since_beat(), 0.15)
        heartbeat.beat("t:next")
        self.assertLess(heartbeat.seconds_since_beat(), 0.05)
        self.assertEqual(heartbeat.last_stage(), "t:next")

    def test_watchdog_lets_a_slow_but_progressing_run_finish(self):
        """The exact case that was killing healthy PS4 sweeps."""
        STALL = 0.5
        heartbeat.reset("run")
        done = []

        def slow_but_alive():
            for _ in range(8):          # 0.8s total, well past STALL
                time.sleep(0.1)
                heartbeat.beat("working")
            done.append(True)

        import threading
        th = threading.Thread(target=slow_but_alive); th.start()
        killed = False
        while th.is_alive():
            th.join(timeout=0.05)
            if th.is_alive() and heartbeat.seconds_since_beat() >= STALL:
                killed = True
                break
        th.join()
        self.assertFalse(killed, "progressing run was wrongly declared hung")
        self.assertTrue(done)

    def test_watchdog_still_catches_a_real_hang(self):
        STALL = 0.4
        heartbeat.reset("run")
        import threading
        stop = threading.Event()
        th = threading.Thread(target=lambda: stop.wait(10)); th.start()
        killed = False
        deadline = time.time() + 3
        while th.is_alive() and time.time() < deadline:
            th.join(timeout=0.05)
            if th.is_alive() and heartbeat.seconds_since_beat() >= STALL:
                killed = True
                break
        stop.set(); th.join()
        self.assertTrue(killed, "a genuinely hung run was not caught")


class GeminiQuota(unittest.TestCase):
    def setUp(self):
        from evaluation.providers import gemini
        self.g = gemini
        gemini.reset_quota_blocks()

    def tearDown(self):
        # Quota blocks are process-wide by design, so a test that sets one must
        # clear it or it leaks into the rest of the session.
        self.g.reset_quota_blocks()

    def test_quota_body_is_terminal(self):
        self.assertTrue(self.g._is_quota_429(
            "you exceeded your current quota, please check your plan", None))
        self.assertTrue(self.g._is_quota_429("resource_exhausted", "30"))

    def test_plain_rate_limit_with_retry_after_is_retryable(self):
        self.assertFalse(self.g._is_quota_429("too many requests", "5"))

    def test_ambiguous_429_without_retry_after_is_treated_as_quota(self):
        self.assertTrue(self.g._is_quota_429("too many requests", None))

    def test_block_is_remembered_and_key_is_not_stored_in_plaintext(self):
        key = "AIzaSy-SECRET-KEY-VALUE"
        self.assertEqual(self.g._quota_block_remaining(key), 0.0)
        self.g._quota_block(key)
        left = self.g._quota_block_remaining(key)
        self.assertGreater(left, 0)
        self.assertLessEqual(left, 24 * 3600)
        self.assertNotIn(key, str(self.g._quota_blocks))
        self.assertEqual(self.g._quota_block_remaining("a-different-key"), 0.0)

    def test_retry_after_is_parsed_and_capped(self):
        self.assertEqual(self.g._retry_after_seconds("5", 99), 5.0)
        self.assertEqual(self.g._retry_after_seconds("9999", 99), 30.0)
        self.assertEqual(self.g._retry_after_seconds(None, 2.5), 2.5)
        self.assertEqual(self.g._retry_after_seconds("garbage", 2.5), 2.5)

    def test_generate_skips_a_blocked_key_without_any_http_call(self):
        """
        A blocked key must short-circuit BEFORE any network call.

        NOTE: this patches gemini.requests.post with mock.patch rather than by
        assigning to the module attribute. Assigning leaks: the replacement
        survives the test and every later test in the same pytest session sees
        it, which showed up as an unrelated AttributeError
        ('NoneType' has no attribute 'status_code') in test_reliability_8_2.
        """
        from unittest import mock
        from evaluation.providers.grok import QuotaExhausted

        p = self.g.GeminiProvider(api_key="k-blocked")
        self.g._quota_block("k-blocked")

        with mock.patch.object(self.g.requests, "post") as post:
            with self.assertRaises(QuotaExhausted):
                p.generate("hello")
        post.assert_not_called()


class Recompress(unittest.TestCase):
    def test_lossless_and_ocr_identical_pixels(self):
        from PIL import Image
        import sap_gui.screenshot as ss
        import glob
        src = sorted(glob.glob('reports/**/screenshots/*.png', recursive=True))
        if not src:
            self.skipTest("no sample screenshots")
        import shutil, tempfile
        tmp = os.path.join(tempfile.mkdtemp(), "shot.png")
        shutil.copy(src[0], tmp)
        before_px = list(Image.open(tmp).convert("RGB").getdata())
        before = os.path.getsize(tmp)
        ss._recompress(tmp)
        after = os.path.getsize(tmp)
        after_px = list(Image.open(tmp).convert("RGB").getdata())
        self.assertEqual(before_px, after_px, "recompression changed pixels")
        self.assertLess(after, before / 10, "recompression saved less than 10x")

    def test_missing_file_does_not_raise(self):
        import sap_gui.screenshot as ss
        ss._recompress("/nonexistent/nope.png")   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
