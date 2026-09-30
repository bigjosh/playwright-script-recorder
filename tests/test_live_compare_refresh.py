"""Offline live-compare checks: captures and UI callbacks are all substituted.

Nothing connects to a browser, opens a window, or issues an input event.
"""

from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlib as psl


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class LiveCompareRefreshTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.crop = Image.new("RGB", (20, 10), "black")
        self.full = Image.new("RGB", (640, 480), "white")
        self.capture = mock.Mock(return_value=(self.crop, self.full))
        self.on_frame = mock.Mock()
        self.on_status = mock.Mock()
        self.refresh = psl._LiveCompareRefresh(
            self.capture, self.on_frame, self.on_status,
            interval=1.0, clock=self.clock)
        self.addCleanup(self.refresh.close)

    def test_first_poll_captures_immediately_and_waits_until_due(self):
        self.refresh.poll()
        self.capture.assert_called_once_with()
        self.on_frame.assert_called_once_with(self.crop, self.full)
        self.clock.now = 0.999
        self.refresh.poll()
        self.capture.assert_called_once_with()
        self.clock.now = 1.0
        self.refresh.poll()
        self.assertEqual(self.capture.call_count, 2)

    def test_interval_begins_after_capture_and_frame_processing_finish(self):
        def capture():
            self.clock.now = 4.0
            return self.crop, self.full

        def display(*args):
            self.clock.now = 5.0

        self.capture.side_effect = capture
        self.on_frame.side_effect = display
        self.refresh.poll()
        self.clock.now = 5.9
        self.refresh.poll()
        self.capture.assert_called_once_with()
        self.clock.now = 6.0
        self.refresh.poll()
        self.assertEqual(self.capture.call_count, 2)

    def test_capture_and_callbacks_stay_on_calling_thread(self):
        threads = []
        caller = threading.get_ident()

        def capture():
            threads.append(threading.get_ident())
            return self.crop, self.full

        self.capture.side_effect = capture
        self.on_frame.side_effect = lambda *args: threads.append(threading.get_ident())
        self.on_status.side_effect = lambda *args, **kwargs: threads.append(threading.get_ident())
        self.refresh.poll()
        self.assertGreaterEqual(len(threads), 3)
        self.assertEqual(set(threads), {caller})

    def test_paused_auto_does_not_capture_and_manual_refresh_runs_once(self):
        self.refresh.set_enabled(False)
        self.refresh.poll()
        self.clock.now = 100.0
        self.refresh.poll()
        self.capture.assert_not_called()
        self.refresh.request_refresh()
        self.refresh.poll()
        self.capture.assert_called_once_with()
        self.clock.now = 200.0
        self.refresh.poll()
        self.capture.assert_called_once_with()

    def test_resume_requests_immediate_fresh_capture(self):
        self.refresh.poll()
        self.refresh.set_enabled(False)
        self.refresh.set_enabled(True)
        self.refresh.poll()
        self.assertEqual(self.capture.call_count, 2)

    def test_pause_cancels_a_pending_manual_request(self):
        self.refresh.request_refresh()
        self.refresh.set_enabled(False)
        self.refresh.poll()
        self.capture.assert_not_called()

    def test_manual_requests_coalesce_before_poll(self):
        self.refresh.set_enabled(False)
        self.refresh.request_refresh()
        self.refresh.request_refresh()
        self.refresh.poll()
        self.refresh.poll()
        self.capture.assert_called_once_with()

    def test_close_is_final_even_if_controls_request_more_work(self):
        self.refresh.close()
        self.refresh.close()
        self.refresh.request_refresh()
        self.refresh.set_enabled(True)
        self.clock.now = 100.0
        self.refresh.poll()
        self.capture.assert_not_called()
        self.on_frame.assert_not_called()

    def test_capture_error_marks_previous_image_stale_then_retries(self):
        self.refresh.poll()
        self.capture.side_effect = RuntimeError("browser disconnected")
        self.clock.now = 1.0
        self.refresh.poll()
        self.on_frame.assert_called_once_with(self.crop, self.full)
        message = self.on_status.call_args.args[0]
        self.assertIn("browser disconnected", message)
        self.assertTrue(self.on_status.call_args.kwargs["stale"])
        self.clock.now = 1.99
        self.refresh.poll()
        self.assertEqual(self.capture.call_count, 2)
        recovered = Image.new("RGB", (20, 10), "red")
        self.capture.side_effect = None
        self.capture.return_value = (recovered, self.full)
        self.clock.now = 2.0
        self.refresh.poll()
        self.on_frame.assert_called_with(recovered, self.full)
        self.assertFalse(self.on_status.call_args.kwargs.get("stale", False))

    def test_keyboard_interrupt_propagates_without_becoming_stale_status(self):
        self.capture.side_effect = KeyboardInterrupt
        self.on_status.reset_mock()
        with self.assertRaises(KeyboardInterrupt):
            self.refresh.poll()
        self.on_frame.assert_not_called()
        stale = [call for call in self.on_status.call_args_list if call.kwargs.get("stale")]
        self.assertEqual(stale, [])

    def test_reentrant_poll_cannot_start_an_overlapping_capture(self):
        def capture():
            self.refresh.poll()
            return self.crop, self.full

        self.capture.side_effect = capture
        self.on_frame.side_effect = lambda *args: self.refresh.poll()
        self.refresh.poll()
        self.capture.assert_called_once_with()
        self.on_frame.assert_called_once_with(self.crop, self.full)

    def test_render_error_propagates_and_releases_capture_guard(self):
        self.on_frame.side_effect = RuntimeError("viewer closed during capture")
        with self.assertRaisesRegex(RuntimeError, "viewer closed during capture"):
            self.refresh.poll()
        self.on_frame.side_effect = None
        self.refresh.request_refresh()
        self.refresh.poll()
        self.assertEqual(self.capture.call_count, 2)


class LiveCompareCaptureTests(unittest.TestCase):
    def setUp(self):
        self.box = (10, 20, 30, 30)
        self.crop = Image.new("RGB", (20, 10), "black")
        self.full = Image.new("RGB", (640, 480), "white")

    def test_one_bounded_capture_returns_crop_and_full_from_same_frame(self):
        with mock.patch.object(psl, "_frame_and_full", return_value=(self.crop, self.full)) as capture:
            actual, context = psl._grab_live_compare(self.box, (20, 10), (640, 480))
        capture.assert_called_once_with(*self.box, timeout=2000)
        self.assertIs(actual, self.crop)
        self.assertIs(context, self.full)

    def test_changed_viewport_is_rejected_even_when_crop_size_matches(self):
        resized = Image.new("RGB", (800, 600), "white")
        with mock.patch.object(psl, "_frame_and_full", return_value=(self.crop, resized)) as capture:
            with self.assertRaises(ValueError):
                psl._grab_live_compare(self.box, (20, 10), (640, 480))
        capture.assert_called_once()

    def test_wrong_crop_size_is_rejected_without_rescaling(self):
        wrong_crop = Image.new("RGB", (19, 10), "black")
        with mock.patch.object(psl, "_frame_and_full", return_value=(wrong_crop, self.full)) as capture:
            with self.assertRaises(ValueError):
                psl._grab_live_compare(self.box, (20, 10))
        capture.assert_called_once()

    def test_capture_error_propagates_instead_of_reusing_old_pixels(self):
        with mock.patch.object(psl, "_frame_and_full", side_effect=RuntimeError("capture timed out")):
            with self.assertRaisesRegex(RuntimeError, "capture timed out"):
                psl._grab_live_compare(self.box, (20, 10))


class LivePreviewVerificationTests(unittest.TestCase):
    def setUp(self):
        self.box = (10, 20, 30, 30)
        self.expected = Image.new("RGB", (20, 10), "white")
        self.mismatch = Image.new("RGB", (20, 10), "black")
        self.full = Image.new("RGB", (640, 480), "white")
        self.start_patch("loadFrame", return_value=self.expected)
        self.start_patch("info")
        self.start_patch("_emit")
        self.capture = self.start_patch("_frame_and_full")
        self.alarm = self.start_patch("alarm")
        self.viewer = self.start_patch("_diff_viewer")
        self.save_diff = self.start_patch("_save_diff_files", side_effect=AssertionError("Viewer failed"))
        self.page = self.start_patch("_page", mock.Mock())

    def start_patch(self, attribute, *args, **kwargs):
        patcher = mock.patch.object(psl, attribute, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def preview_matching_frame(self, *args, **kwargs):
        callback = kwargs["refresh_callback"]
        crop, full = callback()
        self.assertEqual(crop.tobytes(), self.expected.tobytes())
        self.assertIs(full, self.full)

    def test_matching_live_preview_does_not_resume_or_bypass_operator_stop(self):
        self.capture.side_effect = [(self.mismatch, self.full), (self.expected, self.full)]
        self.viewer.side_effect = self.preview_matching_frame
        self.alarm.side_effect = [1, 3]
        with self.assertRaises(SystemExit) as stopped:
            psl.verifyFrame("saved.png", self.box)
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.alarm.call_count, 2)
        self.assertEqual(self.capture.call_count, 2)
        self.save_diff.assert_not_called()
        self.page.mouse.click.assert_not_called()
        self.page.keyboard.press.assert_not_called()

    def test_try_again_uses_new_verification_capture_after_matching_preview(self):
        self.capture.side_effect = [
            (self.mismatch, self.full), (self.expected, self.full),
            (self.expected, self.full)]
        self.viewer.side_effect = self.preview_matching_frame
        self.alarm.side_effect = [1, 0]
        self.assertTrue(psl.verifyFrame("saved.png", self.box))
        self.assertEqual(self.alarm.call_count, 2)
        self.assertEqual(self.capture.call_count, 3)
        self.assertEqual(self.capture.call_args_list[0].kwargs, {})
        self.assertEqual(self.capture.call_args_list[1].kwargs, {"timeout": 2000})
        self.assertEqual(self.capture.call_args_list[2].kwargs, {})
        self.save_diff.assert_not_called()
        self.page.mouse.click.assert_not_called()
        self.page.keyboard.press.assert_not_called()

    def test_preview_match_followed_by_new_mismatch_still_stops(self):
        self.capture.side_effect = [
            (self.mismatch, self.full), (self.expected, self.full),
            (self.mismatch, self.full)]
        self.viewer.side_effect = self.preview_matching_frame
        self.alarm.side_effect = [1, 0, 3]
        with self.assertRaises(SystemExit) as stopped:
            psl.verifyFrame("saved.png", self.box)
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.capture.call_count, 3)
        self.assertEqual(self.alarm.call_count, 3)
        self.save_diff.assert_not_called()


if __name__ == "__main__":
    unittest.main()
