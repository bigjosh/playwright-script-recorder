"""Offline click pause, preview, and reassignment checks."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlayout as layouts
import playwrightscriptlib as psl


POINT_BUTTONS = ("Next Step", "Run Continuously", "Reassign click location",
                 "Show click location", "STOP")
SHOW = POINT_BUTTONS.index("Show click location")
STOP = POINT_BUTTONS.index("STOP")


class ClickReassignmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        (self.folder / "layout.json").write_text(json.dumps({
            "version": 1, "viewport": [640, 480],
        }), encoding="utf-8")
        self.point_file = self.folder / "x-field.json"
        self.point_file.write_text(json.dumps({
            "version": 1, "kind": "point", "x": 20, "y": 30,
        }), encoding="utf-8")
        self.original = self.point_file.read_bytes()
        self.page = mock.Mock()
        self.patch(psl, "_page", self.page)
        self.patch(psl, "_layout", None)
        self.patch(psl, "_pause_on_info", True)
        self.patch(psl, "_shot_dir", None)
        self.patch(psl, "_click_guard", None)
        self.patch(psl, "_clicks_settle_time", 0)
        self.patch(psl, "_emit")
        self.viewport = self.patch(psl, "viewportSize", return_value=(640, 480))
        self.capture = self.patch(psl, "viewportGrab",
                                  return_value=Image.new("RGB", (640, 480), "white"))
        self.window = self.patch(psl, "_pause_window", side_effect=[2, 0])
        self.console = self.patch(psl, "_pause_console",
                                  side_effect=AssertionError("Unexpected console fallback"))
        self.prompt = self.patch(layouts, "prompt_definition",
                                 side_effect=AssertionError("Already opted into point selection"))
        self.picker = self.patch(layouts, "pick_on_image", return_value=(90, 80))
        self.preview = self.patch(layouts, "show_click_preview")
        self.events = []
        self.guard = self.patch(psl, "_wait_for_click_guard",
                               side_effect=lambda: self.events.append("guard"))
        self.page.mouse.click.side_effect = lambda x, y: self.events.append(("click", x, y))
        self.page.mouse.dblclick.side_effect = lambda x, y: self.events.append(("double", x, y))
        psl.loadLayout(self.folder)

    def patch(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def assert_no_input(self):
        self.assertEqual(self.events, [])
        self.page.mouse.click.assert_not_called()
        self.page.mouse.dblclick.assert_not_called()
        self.page.mouse.move.assert_not_called()
        self.page.keyboard.press.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def test_reassignment_returns_to_same_pause_without_input(self):
        def pause(message, buttons, **options):
            self.assert_no_input()
            self.assertEqual(options, {"close_choice": STOP})
            self.assertEqual(buttons, POINT_BUTTONS)
            if self.window.call_count == 1:
                self.assertEqual(self.point_file.read_bytes(), self.original)
                return 2
            saved = json.loads(self.point_file.read_text(encoding="utf-8"))
            self.assertEqual((saved["x"], saved["y"]), (90, 80))
            return 0

        self.window.side_effect = pause
        psl.info("Click X field", point="x-field")
        self.assertEqual(self.window.call_count, 2)
        self.assertEqual(self.window.call_args_list[0], self.window.call_args_list[1])
        self.assertTrue(psl._pause_on_info)
        self.prompt.assert_not_called()
        self.assert_no_input()

    def test_next_step_click_uses_saved_point_and_popup_guard_exactly_once(self):
        psl.info("Click X field", point="x-field")
        self.assert_no_input()
        psl.click("x-field")
        self.assertEqual(self.events, ["guard", ("click", 90, 80)])
        self.page.mouse.click.assert_called_once_with(90, 80)
        self.page.mouse.dblclick.assert_not_called()

    def test_next_step_doubleclick_uses_saved_point_and_popup_guard_exactly_once(self):
        psl.info("Double-click X field", point="x-field")
        self.assert_no_input()
        psl.doubleClick("x-field")
        self.assertEqual(self.events, ["guard", ("double", 90, 80)])
        self.page.mouse.dblclick.assert_called_once_with(90, 80)
        self.page.mouse.click.assert_not_called()

    def test_preview_returns_to_same_pause_and_stop_leaves_point_untouched(self):
        self.window.side_effect = [SHOW, STOP]
        with self.assertRaises(SystemExit) as stopped:
            psl.info("Click X field", point="x-field")
        self.assertEqual(stopped.exception.code, 2)
        self.preview.assert_called_once_with("x-field", self.capture.return_value, (20, 30))
        self.capture.assert_called_once()
        self.assertEqual(self.window.call_args_list[0], self.window.call_args_list[1])
        self.assertTrue(psl._pause_on_info)
        self.assertEqual(self.point_file.read_bytes(), self.original)
        self.picker.assert_not_called()
        self.assert_no_input()

    def test_preview_after_reassignment_uses_current_pixels_and_saved_point(self):
        self.window.side_effect = [SHOW, 2, SHOW, 0]
        first, picker_image, last = [
            Image.new("RGB", (640, 480), color) for color in ("red", "green", "blue")
        ]
        self.capture.side_effect = [first, picker_image, last]
        psl.info("Double-click X field", point="x-field")
        self.assertEqual(self.preview.call_args_list, [
            mock.call("x-field", first, (20, 30)), mock.call("x-field", last, (90, 80)),
        ])
        self.assert_no_input()
        psl.doubleClick("x-field")
        self.assertEqual(self.events, ["guard", ("double", 90, 80)])

    def test_missing_point_preview_leaves_definition_to_operator(self):
        self.point_file.unlink()
        self.window.side_effect = [SHOW, STOP]
        with self.assertRaises(SystemExit):
            psl.info("Click X field", point="x-field")
        self.assertIn("Reassign click location", self.window.call_args.args[0])
        self.assertFalse(self.point_file.exists())
        self.prompt.assert_not_called()
        self.picker.assert_not_called()
        self.preview.assert_not_called()
        self.assert_no_input()

    def test_preview_refuses_mismatched_capture_then_waits_for_choice(self):
        self.capture.return_value = Image.new("RGB", (639, 480))
        self.window.side_effect = [SHOW, STOP]
        with self.assertRaises(SystemExit):
            psl.info("Click X field", point="x-field")
        self.assertNotEqual(self.window.call_args_list[0], self.window.call_args_list[1])
        self.preview.assert_not_called()
        self.assert_no_input()

    def test_preview_failures_report_error_and_allow_fresh_retry(self):
        self.window.side_effect = [SHOW, SHOW, SHOW, STOP]
        self.capture.side_effect = [RuntimeError("Screen disconnected"),
                                    self.capture.return_value, self.capture.return_value]
        self.preview.side_effect = [RuntimeError("Viewer unavailable"), None]
        with self.assertRaises(SystemExit):
            psl.info("Click X field", point="x-field")
        messages = [call.args[0] for call in self.window.call_args_list]
        self.assertIn("Screen disconnected", messages[1])
        self.assertIn("Viewer unavailable", messages[2])
        self.assertEqual(messages[0], messages[3])
        self.assertEqual(self.preview.call_count, 2)
        self.assertEqual(self.point_file.read_bytes(), self.original)
        self.assert_no_input()

    def test_cancel_reassignment_returns_to_pause_and_stop_sends_nothing(self):
        self.picker.return_value = None
        self.window.side_effect = [2, STOP]
        with self.assertRaises(SystemExit) as stopped:
            psl.info("Click X field", point="x-field")
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.window.call_count, 2)
        self.assertEqual(self.point_file.read_bytes(), self.original)
        self.assert_no_input()

    def test_stop_after_successful_edit_does_not_send_pending_click(self):
        self.window.side_effect = [2, STOP]
        with self.assertRaises(SystemExit):
            psl.info("Click X field", point="x-field")
        saved = json.loads(self.point_file.read_text(encoding="utf-8"))
        self.assertEqual((saved["x"], saved["y"]), (90, 80))
        self.assert_no_input()

    def test_save_failure_stops_without_console_fallback_or_input(self):
        with mock.patch.object(layouts, "_write_json", side_effect=layouts.LayoutError("Disk full")):
            with self.assertRaisesRegex(layouts.LayoutError, "Disk full"):
                psl.info("Click X field", point="x-field")
        self.console.assert_not_called()
        self.assertEqual(self.point_file.read_bytes(), self.original)
        self.assert_no_input()

    def test_viewport_change_while_selecting_preserves_saved_point(self):
        def pick(*args, **kwargs):
            self.viewport.return_value = (800, 600)
            return (90, 80)

        self.picker.side_effect = pick
        with self.assertRaises(layouts.LayoutError):
            psl.info("Click X field", point="x-field")
        self.console.assert_not_called()
        self.assertEqual(self.point_file.read_bytes(), self.original)
        self.assert_no_input()

    def test_page_change_while_selecting_preserves_saved_point(self):
        def pick(*args, **kwargs):
            psl._page = mock.Mock()
            return (90, 80)

        self.picker.side_effect = pick
        with self.assertRaises(layouts.LayoutError):
            psl.info("Click X field", point="x-field")
        self.assertEqual(self.point_file.read_bytes(), self.original)
        self.assert_no_input()

    def test_reassignment_cannot_overwrite_nonpoint_entry(self):
        self.point_file.write_text(json.dumps({
            "version": 1, "kind": "region", "box": [10, 20, 30, 40],
        }), encoding="utf-8")
        previous = self.point_file.read_bytes()
        with self.assertRaises(layouts.LayoutError):
            psl.info("Click X field", point="x-field")
        self.picker.assert_not_called()
        self.assertEqual(self.point_file.read_bytes(), previous)
        self.assert_no_input()

    def test_point_pause_ui_failure_falls_back_to_console_with_stop_on_eof(self):
        self.window.side_effect = RuntimeError("No display")
        self.console.side_effect = None
        self.console.return_value = STOP
        with self.assertRaises(SystemExit):
            psl.info("Click X field", point="x-field")
        self.assertEqual(self.console.call_args.kwargs, {"close_choice": STOP})
        self.picker.assert_not_called()
        self.assert_no_input()

    def test_continuous_mode_does_not_open_reassignment_ui(self):
        psl._pause_on_info = False
        psl.info("Click X field", point="x-field")
        self.window.assert_not_called()
        self.capture.assert_not_called()
        self.picker.assert_not_called()
        self.assertEqual(self.point_file.read_bytes(), self.original)
        self.assert_no_input()

    def test_ordinary_pause_retains_original_buttons_and_call_signature(self):
        self.window.side_effect = [0]
        psl.info("Waiting for readiness")
        self.window.assert_called_once_with(
            "Waiting for readiness", ("Next Step", "Run Continuously", "STOP"))
        self.picker.assert_not_called()

    def test_run_continuously_after_edit_is_explicit_and_uses_new_point(self):
        self.window.side_effect = [2, 1]
        psl.info("Click X field", point="x-field")
        self.assertFalse(psl._pause_on_info)
        self.assert_no_input()
        psl.click("x-field")
        self.assertEqual(self.events, ["guard", ("click", 90, 80)])


class PauseCloseBehaviorTests(unittest.TestCase):
    def test_console_eof_stops_named_click_pause(self):
        with mock.patch("builtins.input", side_effect=EOFError), mock.patch("builtins.print"):
            self.assertEqual(psl._pause_console("Click", POINT_BUTTONS, close_choice=STOP), STOP)

    def test_console_eof_keeps_existing_ordinary_pause_behavior(self):
        with mock.patch("builtins.input", side_effect=EOFError), mock.patch("builtins.print"):
            self.assertEqual(psl._pause_console("Wait", ("Next", "Run", "STOP")), 1)

    def test_named_pause_close_uses_stop_choice(self):
        import tkinter as tk

        root = mock.Mock()
        root.winfo_screenwidth.return_value = 1280
        root.winfo_screenheight.return_value = 900
        root.winfo_width.return_value = 800
        root.winfo_height.return_value = 300
        with mock.patch.object(tk, "Tk", return_value=root), \
                mock.patch.object(tk, "Label"), mock.patch.object(tk, "Frame"), \
                mock.patch.object(tk, "Button"):
            choice = psl._pause_window("Click", POINT_BUTTONS, close_choice=STOP)
        self.assertEqual(choice, STOP)
        root.destroy.assert_called_once_with()

    def test_pause_construction_error_cleans_up_window(self):
        import tkinter as tk

        root = mock.Mock()
        with mock.patch.object(tk, "Tk", return_value=root), \
                mock.patch.object(tk, "Label", side_effect=RuntimeError("UI failed")):
            with self.assertRaisesRegex(RuntimeError, "UI failed"):
                psl._pause_window("Click", POINT_BUTTONS, close_choice=STOP)
        root.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
