"""Offline popup-guard regressions; no GUI, browser, or machine input is used."""

from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlib as psl


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("Negative sleep")
        self.sleeps.append(seconds)
        self.now += seconds
        if len(self.sleeps) > 1000:
            raise AssertionError("Guard failed to stop at its deadline")


class ClickPopupGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.capture_threads = []
        self.layout = SimpleNamespace(viewport=(320, 200), path=Path("unused-layout"))
        self.page = mock.Mock()
        self.screen = Image.new("RGB", self.layout.viewport, "white")
        self.reference = Image.new("RGB", (70, 20), "blue")
        self.matcher = mock.Mock()
        self.matcher.locate.return_value = None
        self.patch(psl, "_page", self.page)
        self.patch(psl, "_layout", self.layout)
        self.patch(psl, "_click_guard", None)
        self.patch(psl, "_clicks_settle_time", 0)
        self.patch(psl, "_emit")
        self.patch(psl.time, "monotonic", side_effect=self.clock.monotonic)
        self.patch(psl.time, "sleep", side_effect=self.clock.sleep)
        self.patch(psl, "viewportSize", return_value=self.layout.viewport)
        self.capture = self.patch(psl, "viewportGrab", side_effect=self.capture_screen)
        self.resolve = self.patch(psl, "_layout_entry", side_effect=self.resolve_entry)
        self.load = self.patch(psl, "loadFrame", return_value=self.reference)
        self.factory = self.patch(psl._popup, "PopupMatcher", return_value=self.matcher)
        self.patch(psl, "wait", side_effect=AssertionError("Guard must not show a skippable wait"))
        self.patch(psl, "info", side_effect=AssertionError("Guard must not show a popup"))
        self.patch(psl, "alarm", side_effect=AssertionError("Guard must raise its error"))
        # Error screenshot/log diagnostics must not write to the real workspace.
        self.patch(psl, "_run_folder", side_effect=OSError("offline test: diagnostics unavailable"))

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def capture_screen(self, **kwargs):
        self.events.append("capture")
        self.capture_threads.append(threading.get_ident())
        return self.screen.copy()

    def resolve_entry(self, name, kind):
        self.events.append("resolve:" + kind)
        if kind == "frame":
            return {"image": "unused-popup.png", "box": (10, 10, 80, 30)}
        self.assertEqual(kind, "point")
        return {"x": 12, "y": 34}

    def enable(self, **kwargs):
        psl.enableClickGuard("crd-popup", **kwargs)
        self.events.clear()

    def assert_no_click(self):
        self.page.mouse.click.assert_not_called()
        self.page.mouse.dblclick.assert_not_called()

    def test_default_disabled_keeps_legacy_click_api_capture_free(self):
        psl.click(12, 34)
        psl.doubleClick(56, 78)
        self.page.mouse.click.assert_called_once_with(12, 34)
        self.page.mouse.dblclick.assert_called_once_with(56, 78)
        self.capture.assert_not_called()
        self.resolve.assert_not_called()

    def test_enable_loads_named_reference_without_actuating(self):
        self.enable(matchLevel=0.93)
        self.resolve.assert_called_once_with("crd-popup", "frame")
        self.load.assert_called_once_with("unused-popup.png")
        self.factory.assert_called_once_with(self.reference, threshold=0.93)
        self.assert_no_click()

    def test_define_frame_uses_saved_viewport_image_without_live_capture(self):
        self.load.return_value = self.screen
        self.layout.check_viewport = mock.Mock()
        selected_images = []

        def define(name, kind, grab):
            self.assertEqual((name, kind), ("crd-popup", "frame"))
            selected_images.append(grab())
            selected_images.append(grab())
            return {"image": "saved-reference.png", "box": [10, 20, 80, 40]}

        self.layout.define = mock.Mock(side_effect=define)
        result = psl.defineFrame("crd-popup", screenshot=Path("saved-viewport.png"))
        self.assertEqual(result, ("saved-reference.png", (10, 20, 80, 40)))
        self.load.assert_called_once_with("saved-viewport.png")
        self.assertEqual(len(selected_images), 2)
        for selected in selected_images:
            self.assertIsNot(selected, self.screen)
            self.assertEqual(selected.size, self.screen.size)
            self.assertEqual(selected.tobytes(), self.screen.tobytes())
        self.assertIsNot(selected_images[0], selected_images[1])
        self.assertEqual(self.layout.check_viewport.call_args_list,
                         [mock.call((320, 200))] * 3)
        self.resolve.assert_not_called()
        self.capture.assert_not_called()
        self.assert_no_click()

    def test_define_frame_rejects_wrong_saved_viewport_before_recording(self):
        self.load.return_value = Image.new("RGB", (319, 200), "white")

        def check_viewport(size):
            if size != self.layout.viewport:
                raise psl._layouts.LayoutError("Recorded viewport differs")

        self.layout.check_viewport = mock.Mock(side_effect=check_viewport)
        self.layout.define = mock.Mock()
        with self.assertRaises(psl._layouts.LayoutError):
            psl.defineFrame("crd-popup", screenshot="saved-viewport.png")
        self.layout.define.assert_not_called()
        self.capture.assert_not_called()
        self.assert_no_click()

    def test_enable_imports_supplied_reference_before_resolving_frame(self):
        define = self.patch(psl, "defineFrame",
                            side_effect=lambda *args, **kwargs: self.events.append("define-saved"))
        psl.enableClickGuard("crd-popup", referenceScreenshot="saved-viewport.png")
        define.assert_called_once_with("crd-popup", screenshot="saved-viewport.png")
        self.assertEqual(self.events, ["define-saved", "resolve:frame"])
        self.factory.assert_called_once_with(self.reference, threshold=0.90)
        self.capture.assert_not_called()
        self.assert_no_click()

    def test_clear_screen_needs_two_fresh_captures_before_single_click(self):
        self.enable()
        self.page.mouse.click.side_effect = lambda *args: self.events.append("click")
        psl.click(12, 34)
        self.assertEqual(self.events, ["capture", "capture", "click"])
        self.assertEqual(self.matcher.locate.call_count, 2)
        self.page.mouse.click.assert_called_once_with(12, 34)
        self.assertGreater(self.clock.now, 0)

    def test_named_point_resolves_before_fresh_guard_captures(self):
        self.enable()
        self.page.mouse.click.side_effect = lambda *args: self.events.append("click")
        psl.click("move-absolute")
        self.assertEqual(self.events, ["resolve:point", "capture", "capture", "click"])
        self.page.mouse.click.assert_called_once_with(12, 34)

    def test_double_click_has_same_guard_and_only_one_dblclick(self):
        self.enable()
        self.matcher.locate.side_effect = [(50, 20, 0.98), None, None]
        psl.doubleClick("x-field")
        self.assertEqual(self.capture.call_count, 3)
        self.page.mouse.dblclick.assert_called_once_with(12, 34)
        self.page.mouse.click.assert_not_called()

    def test_banner_moving_between_captures_still_blocks(self):
        self.enable()
        self.matcher.locate.side_effect = [(50, 20, 0.98), (220, 170, 0.97), None, None]
        psl.click(12, 34)
        self.assertEqual(self.capture.call_count, 4)
        for call in self.matcher.locate.call_args_list:
            self.assertEqual(call.args[0].size, self.layout.viewport)
        self.page.mouse.click.assert_called_once_with(12, 34)

    def test_reappearance_resets_consecutive_clear_requirement(self):
        self.enable()
        self.matcher.locate.side_effect = [None, (50, 20, 0.98), None, None]
        psl.click(12, 34)
        self.assertEqual(self.capture.call_count, 4)
        self.page.mouse.click.assert_called_once_with(12, 34)

    def test_persistent_banner_times_out_after_thirty_seconds_without_input(self):
        self.enable()
        self.matcher.locate.return_value = (50, 20, 0.98)
        with self.assertRaises(psl.ClickGuardTimeout):
            psl.click(12, 34)
        self.assert_no_click()
        self.assertAlmostEqual(self.clock.now, 30.0)
        self.assertGreater(self.capture.call_count, 2)
        self.assertTrue(issubclass(psl.ClickGuardTimeout, psl.ClickGuardError))

    def test_screenshot_failure_is_not_treated_as_clear(self):
        self.enable()
        self.capture.side_effect = RuntimeError("remote browser disconnected")
        with self.assertRaises(psl.ClickGuardError):
            psl.click(12, 34)
        self.assert_no_click()
        self.matcher.locate.assert_not_called()

    def test_matcher_failure_is_not_treated_as_clear(self):
        self.enable()
        self.matcher.locate.side_effect = RuntimeError("invalid matcher result")
        with self.assertRaises(psl.ClickGuardError):
            psl.click(12, 34)
        self.assert_no_click()

    def test_changed_page_requires_reconfiguration(self):
        self.enable()
        different_page = mock.Mock()
        psl._page = different_page
        with self.assertRaises(psl.ClickGuardError):
            psl.click(12, 34)
        self.assert_no_click()
        different_page.mouse.click.assert_not_called()

    def test_changed_layout_requires_reconfiguration(self):
        self.enable()
        psl._layout = SimpleNamespace(viewport=self.layout.viewport)
        with self.assertRaises(psl.ClickGuardError):
            psl.click(12, 34)
        self.assert_no_click()

    def test_resized_screenshot_cannot_mean_popup_absent(self):
        self.enable()
        self.screen = Image.new("RGB", (319, 200), "white")
        with self.assertRaises(psl.ClickGuardError):
            psl.click(12, 34)
        self.assert_no_click()
        self.matcher.locate.assert_not_called()

    def test_last_capture_finishing_after_deadline_cannot_authorize_click(self):
        self.enable(timeout=1.0, pollInterval=0.01)
        durations = iter((0.98, 0.02))

        def slow_capture(**kwargs):
            self.clock.now += next(durations)
            return self.capture_screen(**kwargs)

        self.capture.side_effect = slow_capture
        with self.assertRaises(psl.ClickGuardTimeout):
            psl.click(12, 34)
        self.assert_no_click()
        self.assertEqual(self.capture.call_count, 2)
        last_timeout = self.capture.call_args.kwargs["timeout"]
        self.assertGreater(last_timeout, 0)
        self.assertLessEqual(last_timeout, 11)

    def test_matcher_processing_counts_against_overall_deadline(self):
        self.enable(timeout=1.0)

        def slow_match(image):
            self.clock.now += 1.1
            return None

        self.matcher.locate.side_effect = slow_match
        with self.assertRaises(psl.ClickGuardTimeout):
            psl.click(12, 34)
        self.assert_no_click()
        self.assertEqual(self.capture.call_count, 1)

    def test_each_capture_uses_bounded_browser_timeout(self):
        self.enable()
        psl.click(12, 34)
        for call in self.capture.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 2000)

    def test_captures_and_matcher_run_on_calling_thread(self):
        self.enable()
        matcher_threads = []
        self.matcher.locate.side_effect = lambda image: matcher_threads.append(threading.get_ident())
        caller = threading.get_ident()
        psl.click(12, 34)
        self.assertEqual(self.capture_threads, [caller, caller])
        self.assertEqual(matcher_threads, [caller, caller])

    def test_keyboard_interrupt_propagates_without_actuation(self):
        self.enable()
        self.capture.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            psl.click(12, 34)
        self.assert_no_click()

    def test_mouse_error_is_not_retried_after_guard_passes(self):
        self.enable()
        self.page.mouse.click.side_effect = RuntimeError("mouse transport failed")
        with self.assertRaisesRegex(RuntimeError, "mouse transport failed"):
            psl.click(12, 34)
        self.page.mouse.click.assert_called_once_with(12, 34)
        self.assertEqual(self.capture.call_count, 2)

    def test_each_click_takes_new_captures_instead_of_reusing_clear_state(self):
        self.enable()
        psl.click(12, 34)
        psl.click(56, 78)
        self.assertEqual(self.capture.call_count, 4)
        self.assertEqual(self.page.mouse.click.call_count, 2)

    def test_disable_returns_to_capture_free_clicking(self):
        self.enable()
        psl.disableClickGuard()
        psl.click(12, 34)
        self.capture.assert_not_called()
        self.page.mouse.click.assert_called_once_with(12, 34)

    def test_failed_reconfiguration_does_not_silently_disable_previous_guard(self):
        self.enable()
        original = psl._click_guard
        self.factory.side_effect = ValueError("blank reference image")
        with self.assertRaises(Exception):
            psl.enableClickGuard("invalid-popup")
        self.assertIs(psl._click_guard, original)
        self.matcher.locate.return_value = (50, 20, 0.98)
        with self.assertRaises(psl.ClickGuardTimeout):
            psl.click(12, 34)
        self.assert_no_click()

    def test_invalid_configuration_is_rejected_before_enabling(self):
        invalid = [
            {"timeout": 0}, {"timeout": -1}, {"timeout": float("inf")},
            {"timeout": float("nan")}, {"pollInterval": 0},
            {"pollInterval": -1}, {"pollInterval": float("inf")},
            {"pollInterval": float("nan")}, {"pollInterval": 30},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((ValueError, psl.ClickGuardError)):
                    psl.enableClickGuard("crd-popup", **kwargs)
                self.assertIsNone(psl._click_guard)
        self.assert_no_click()


if __name__ == "__main__":
    unittest.main()
