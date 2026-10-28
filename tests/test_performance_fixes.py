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
    """
    Recompression must shrink a SAP hardCopy PNG hard, and change no pixel.

    This used to scavenge the reports tree for any file over 400KB and assume
    it was an uncompressed original. The backfill script emptied that
    population, so the search started returning ALREADY-compressed files, and
    the test failed asking why a 464KB file did not shrink to 46KB. A test
    whose fixture is whatever happens to be on disk reports the state of the
    disk, not the state of the code, so it now builds its own input.
    """

    @staticmethod
    def _hardcopy_like(path):
        """An uncompressed PNG shaped like a SAP GUI screen: flat title band,
        banded list rows, large areas of a single colour. compress_level=0
        reproduces what SAP hardCopy writes."""
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (1920, 1080), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 1920, 60], fill=(20, 60, 120))
        for y in range(80, 1080, 24):
            draw.rectangle([40, y, 900, y + 12],
                           fill=(30, 30, 30) if y % 48 else (200, 215, 235))
        img.save(path, "PNG", compress_level=0)
        return img.tobytes()

    def test_lossless_and_ocr_identical_pixels(self):
        from PIL import Image
        import sap_gui.screenshot as ss
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(), "shot.png")
        before_px = self._hardcopy_like(tmp)
        before = os.path.getsize(tmp)
        self.assertGreater(before, 1_000_000, "fixture is not an uncompressed PNG")

        ss._recompress(tmp)

        after = os.path.getsize(tmp)
        with Image.open(tmp) as reopened:
            after_px = reopened.convert("RGB").tobytes()
        # Pixel identity is the point: OCR reads the file after this runs, and
        # evidence that reads differently after compression is not evidence.
        self.assertEqual(before_px, after_px, "recompression changed pixels")
        self.assertLess(after, before / 10, "recompression saved less than 10x")

    def test_already_compressed_file_is_left_alone_and_intact(self):
        """Re-running the backfill over compressed evidence must be a no-op,
        not a corruption. The 400KB scavenge hid this case entirely."""
        from PIL import Image
        import sap_gui.screenshot as ss
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(), "shot.png")
        self._hardcopy_like(tmp)
        ss._recompress(tmp)
        once = os.path.getsize(tmp)
        with Image.open(tmp) as img:
            px = img.convert("RGB").tobytes()

        ss._recompress(tmp)

        with Image.open(tmp) as img:
            self.assertEqual(px, img.convert("RGB").tobytes())
        self.assertLessEqual(os.path.getsize(tmp), once)

    def test_missing_file_does_not_raise(self):
        import sap_gui.screenshot as ss
        ss._recompress("/nonexistent/nope.png")   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)