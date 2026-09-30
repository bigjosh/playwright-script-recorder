"""Offline sendkeys popup guards: all browser, clock, and image input is fake."""

from pathlib import Path
from types import SimpleNamespace
import sys
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


class KeyboardPopupGuardTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.events = []
        self.page = mock.Mock()
        self.page.keyboard.press.side_effect = self.press
        self.page.keyboard.type.side_effect = self.type_text
        self.layout = SimpleNamespace(viewport=(320, 200), path=Path("unused-layout"))
        self.screen = Image.new("RGB", self.layout.viewport, "white")
        self.reference = Image.new("RGB", (70, 20), "blue")
        self.matcher = mock.Mock()
        self.matcher.locate.return_value = None
        self.patch(psl, "_page", self.page)
        self.patch(psl, "_layout", self.layout)
        self.patch(psl, "_click_guard", None)
        self.patch(psl, "_emit")
        self.patch(psl.time, "monotonic", side_effect=self.clock.monotonic)
        self.patch(psl.time, "sleep", side_effect=self.clock.sleep)
        self.patch(psl, "viewportSize", return_value=self.layout.viewport)
        self.capture = self.patch(psl, "viewportGrab", side_effect=self.capture_screen)
        self.patch(psl, "_layout_entry", return_value={"image": "unused-popup.png"})
        self.patch(psl, "loadFrame", return_value=self.reference)
        self.patch(psl._popup, "PopupMatcher", return_value=self.matcher)
        self.patch(psl, "wait", side_effect=AssertionError("Guard must not show a skippable wait"))
        self.patch(psl, "info", side_effect=AssertionError("Guard must not show a popup"))
        self.patch(psl, "alarm", side_effect=AssertionError("Guard must raise its error"))
        self.patch(psl, "_run_folder", side_effect=OSError("Offline diagnostics unavailable"))

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def press(self, key):
        self.events.append(("press", key))

    def type_text(self, text, delay):
        self.events.append(("type", text, delay))

    def capture_screen(self, **kwargs):
        self.events.append("capture")
        return self.screen.copy()

    def enable(self, **kwargs):
        psl.enableClickGuard("crd-popup", **kwargs)
        self.events.clear()

    def assert_no_keys(self):
        self.page.keyboard.press.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def assert_guarded_sequence(self, operations):
        expected = []
        for operation in operations:
            expected.extend(("capture", "capture", operation))
        self.assertEqual(self.events, expected)
        self.assertEqual(self.capture.call_count, 2 * len(operations))

    def test_disabled_preserves_prefix_type_and_enter_without_capture(self):
        psl.sendkeys("123\r", delay=7)
        self.assertEqual(self.events, [
            ("press", "Home"), ("press", "Shift+End"),
            ("type", "123", 7), ("press", "Enter"),
        ])
        self.capture.assert_not_called()
        self.assertEqual(self.clock.sleeps, [0.007, 0.007])

    def test_each_keyboard_operation_requires_two_new_clear_captures(self):
        self.enable()
        psl.sendkeys("-71222\r", delay=9)
        self.assert_guarded_sequence([
            ("press", "Home"), ("press", "Shift+End"),
            ("type", "-71222", 9), ("press", "Enter"),
        ])
        self.page.keyboard.type.assert_called_once_with("-71222", delay=9)

    def test_persistent_popup_blocks_all_keys_for_thirty_seconds(self):
        self.enable()
        self.matcher.locate.return_value = (50, 20, 0.98)
        with self.assertRaises(psl.ClickGuardTimeout) as error:
            psl.sendkeys("123\r", delay=0)
        self.assert_no_keys()
        self.assertAlmostEqual(self.clock.now, 30.0)
        self.assertIn("keyboard", str(error.exception).lower())
        self.assertNotIn("requested click", str(error.exception).lower())

    def test_popup_appearing_after_home_blocks_remaining_prefix_and_text(self):
        self.enable()

        def press_and_show_popup(key):
            self.press(key)
            self.matcher.locate.return_value = (50, 20, 0.98)

        self.page.keyboard.press.side_effect = press_and_show_popup
        with self.assertRaises(psl.ClickGuardTimeout) as error:
            psl.sendkeys("123\r", delay=0)
        self.page.keyboard.press.assert_called_once_with("Home")
        self.page.keyboard.type.assert_not_called()
        self.assertIn("keyboard", str(error.exception).lower())
        self.assertNotIn("sendkeys was NOT sent", str(error.exception))
        self.assertAlmostEqual(self.clock.now, 30.25)

    def test_popup_after_typing_blocks_enter_without_retyping(self):
        self.enable()

        def type_and_show_popup(text, delay):
            self.type_text(text, delay)
            self.matcher.locate.return_value = (50, 20, 0.98)

        self.page.keyboard.type.side_effect = type_and_show_popup
        with self.assertRaises(psl.ClickGuardTimeout):
            psl.sendkeys("123\r", delay=0)
        self.assertEqual(self.page.keyboard.press.call_args_list,
                         [mock.call("Home"), mock.call("Shift+End")])
        self.page.keyboard.type.assert_called_once_with("123", delay=0)
        self.assertAlmostEqual(self.clock.now, 30.75)

    def test_popup_disappearance_resumes_pending_operation_once(self):
        self.enable()
        results = iter([(50, 20, 0.98), (200, 170, 0.97), None, None])
        self.matcher.locate.side_effect = lambda image: next(results, None)
        psl.sendkeys("123", delay=0)
        self.assertEqual(self.events, [
            "capture", "capture", "capture", "capture", ("press", "Home"),
            "capture", "capture", ("press", "Shift+End"),
            "capture", "capture", ("type", "123", 0),
        ])

    def test_append_mode_guards_typing_and_enter_without_selection_prefix(self):
        self.enable()
        psl.sendkeys("abc\ndef", delay=3, HomeShiftEndPrefix=False)
        self.assert_guarded_sequence([
            ("type", "abc", 3), ("press", "Enter"), ("type", "def", 3),
        ])

    def test_newline_only_input_guards_each_enter(self):
        self.enable()
        psl.sendkeys("\r\n\n\r", delay=0)
        self.assert_guarded_sequence([("press", "Enter")] * 3)
        self.page.keyboard.type.assert_not_called()

    def test_multichunk_crlf_and_empty_chunks_preserve_key_sequence(self):
        self.enable()
        psl.sendkeys("first\r\n\nsecond\r", delay=0)
        self.assert_guarded_sequence([
            ("press", "Home"), ("press", "Shift+End"), ("type", "first", 0),
            ("press", "Enter"), ("press", "Enter"),
            ("press", "Home"), ("press", "Shift+End"), ("type", "second", 0),
            ("press", "Enter"),
        ])

    def test_empty_input_does_not_capture_or_send_keys(self):
        self.enable()
        self.matcher.locate.return_value = (50, 20, 0.98)
        psl.sendkeys("")
        psl.sendkeys("", HomeShiftEndPrefix=False)
        self.capture.assert_not_called()
        self.assert_no_keys()

    def test_each_sendkeys_call_requires_fresh_clear_captures(self):
        self.enable()
        psl.sendkeys("a", delay=0, HomeShiftEndPrefix=False)
        psl.sendkeys("b", delay=0, HomeShiftEndPrefix=False)
        self.assert_guarded_sequence([("type", "a", 0), ("type", "b", 0)])

    def test_capture_failure_after_prefix_never_sends_pending_operation(self):
        self.enable()

        def press_then_disconnect(key):
            self.press(key)
            self.capture.side_effect = RuntimeError("remote browser disconnected")

        self.page.keyboard.press.side_effect = press_then_disconnect
        with self.assertRaises(psl.ClickGuardError) as error:
            psl.sendkeys("123", delay=0)
        self.page.keyboard.press.assert_called_once_with("Home")
        self.page.keyboard.type.assert_not_called()
        self.assertIn("keyboard", str(error.exception).lower())
        self.assertNotIn("requested click", str(error.exception).lower())
        self.assertNotIn("sendkeys was NOT sent", str(error.exception))

    def test_keyboard_transport_failure_is_not_retried(self):
        self.enable()
        self.page.keyboard.type.side_effect = RuntimeError("keyboard transport failed")
        with self.assertRaisesRegex(RuntimeError, "keyboard transport failed"):
            psl.sendkeys("123\r", delay=0, HomeShiftEndPrefix=False)
        self.page.keyboard.type.assert_called_once_with("123", delay=0)
        self.page.keyboard.press.assert_not_called()
        self.assertEqual(self.capture.call_count, 2)

    def test_changed_page_before_sendkeys_prevents_all_keyboard_input(self):
        self.enable()
        other_page = mock.Mock()
        psl._page = other_page
        with self.assertRaises(psl.ClickGuardError):
            psl.sendkeys("123", delay=0)
        self.assert_no_keys()
        other_page.keyboard.press.assert_not_called()
        other_page.keyboard.type.assert_not_called()
        self.capture.assert_not_called()

    def test_changed_page_between_prefix_keys_prevents_using_cached_page(self):
        self.enable()
        other_page = mock.Mock()

        def press_then_change_page(key):
            self.press(key)
            psl._page = other_page

        self.page.keyboard.press.side_effect = press_then_change_page
        with self.assertRaises(psl.ClickGuardError):
            psl.sendkeys("123", delay=0)
        self.page.keyboard.press.assert_called_once_with("Home")
        self.page.keyboard.type.assert_not_called()
        other_page.keyboard.press.assert_not_called()
        other_page.keyboard.type.assert_not_called()

    def test_changed_layout_blocks_keyboard_input(self):
        self.enable()
        psl._layout = SimpleNamespace(viewport=self.layout.viewport)
        with self.assertRaises(psl.ClickGuardError):
            psl.sendkeys("123", delay=0)
        self.assert_no_keys()
        self.capture.assert_not_called()

    def test_resized_capture_cannot_authorize_keyboard_input(self):
        self.enable()
        self.screen = Image.new("RGB", (319, 200), "white")
        with self.assertRaises(psl.ClickGuardError):
            psl.sendkeys("123", delay=0)
        self.assert_no_keys()
        self.matcher.locate.assert_not_called()

    def test_slow_final_capture_cannot_authorize_keyboard_past_deadline(self):
        self.enable(timeout=1.0, pollInterval=0.01)
        durations = iter((0.98, 0.02))

        def slow_capture(**kwargs):
            self.clock.now += next(durations)
            return self.capture_screen(**kwargs)

        self.capture.side_effect = slow_capture
        with self.assertRaises(psl.ClickGuardTimeout):
            psl.sendkeys("123", delay=0)
        self.assert_no_keys()
        self.assertEqual(self.capture.call_count, 2)
        self.assertGreater(self.capture.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(self.capture.call_args.kwargs["timeout"], 11)

    def test_keyboard_guard_captures_use_bounded_browser_timeouts(self):
        self.enable()
        psl.sendkeys("123\r", delay=0)
        for call in self.capture.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 2000)

    def test_keyboard_interrupt_propagates_without_input(self):
        self.enable()
        self.capture.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            psl.sendkeys("123", delay=0)
        self.assert_no_keys()

    def test_disabling_guard_restores_capture_free_keyboard_input(self):
        self.enable()
        psl.disableClickGuard()
        psl.sendkeys("123", delay=0, HomeShiftEndPrefix=False)
        self.assertEqual(self.events, [("type", "123", 0)])
        self.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
