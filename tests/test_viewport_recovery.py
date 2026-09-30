"""Offline startup viewport recovery; never open a browser or operator window."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlayout as layouts
import playwrightscriptlib as psl


BUTTONS = ("Restore saved size", "Check again", "Stop the script")


class ViewportRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.selected = layouts.Layout(self.folder)
        self.saved = (2542, 1455)
        self.actual = (2560, 1465)
        self.selected.check_viewport(self.saved)
        self.metadata = self.folder / "layout.json"
        self.original_metadata = self.metadata.read_bytes()
        self.page = mock.Mock()
        self.patch(psl, "_page", self.page)
        self.patch(psl, "_layout", self.selected)
        self.patch(psl, "_click_guard", None)
        self.patch(psl, "_emit")
        self.patch(psl, "info")
        self.size = self.patch(psl, "viewportSize", side_effect=lambda: self.actual)
        self.resize = self.patch(psl, "setViewport", side_effect=self.restore)
        self.alarm = self.patch(psl, "alarm", side_effect=AssertionError("Unexpected alarm"))
        self.patch(psl, "viewportGrab", side_effect=AssertionError("Unexpected screenshot"))
        self.patch(layouts, "choose_layout_folder", side_effect=AssertionError("Unexpected picker"))
        self.patch(layouts, "prompt_definition", side_effect=AssertionError("Unexpected definition"))
        self.patch(layouts, "pick_on_image", side_effect=AssertionError("Unexpected image picker"))

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def restore(self, width, height):
        self.actual = (width, height)

    def choices(self, *values):
        # A bounded iterable makes an unintended recovery loop fail offline.
        self.alarm.side_effect = values

    def assert_metadata_unchanged(self):
        self.assertEqual(self.metadata.read_bytes(), self.original_metadata)
        self.assertEqual(self.selected.viewport, self.saved)

    def assert_recovery_buttons(self):
        for call in self.alarm.call_args_list:
            self.assertEqual(call.kwargs["buttons"], BUTTONS)

    def tearDown(self):
        self.page.mouse.click.assert_not_called()
        self.page.mouse.dblclick.assert_not_called()
        self.page.keyboard.press.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def test_matching_layout_returns_saved_size_without_dialog_or_resize(self):
        self.actual = self.saved
        self.assertEqual(psl.checkViewport(), self.saved)
        self.alarm.assert_not_called()
        self.resize.assert_not_called()
        self.assert_metadata_unchanged()

    def test_new_layout_binds_current_valid_size_without_recovery_dialog(self):
        new_layout = layouts.Layout(self.folder / "new")
        with mock.patch.object(psl, "_layout", new_layout):
            self.assertEqual(psl.checkViewport(), self.actual)
        self.assertEqual(new_layout.viewport, self.actual)
        saved = json.loads((new_layout.path / "layout.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["viewport"], list(self.actual))
        self.alarm.assert_not_called()
        self.resize.assert_not_called()

    def test_restore_resizes_to_saved_dimensions_then_reads_again(self):
        self.choices(0)
        self.assertEqual(psl.checkViewport(), self.saved)
        self.resize.assert_called_once_with(*self.saved)
        self.assertEqual(self.size.call_count, 2)
        self.assertEqual(self.alarm.call_count, 1)
        message = self.alarm.call_args.args[0]
        self.assertIn("2542x1455", message)
        self.assertIn("2560x1465", message)
        self.assert_recovery_buttons()
        self.assert_metadata_unchanged()

    def test_resize_success_does_not_imply_correct_size(self):
        self.resize.side_effect = None
        self.choices(0, 2)
        with self.assertRaises(SystemExit) as stopped:
            psl.checkViewport()
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.size.call_count, 2)
        self.assertEqual(self.alarm.call_count, 2)
        self.assert_recovery_buttons()
        self.assert_metadata_unchanged()

    def test_partial_resize_must_still_match_both_dimensions_exactly(self):
        self.resize.side_effect = lambda *args: setattr(self, "actual", (2542, 1456))
        self.choices(0, 2)
        with self.assertRaises(SystemExit):
            psl.checkViewport()
        self.assertIn("2542x1456", self.alarm.call_args.args[0])
        self.assert_metadata_unchanged()

    def test_check_again_accepts_operator_resize_without_programmatic_resize(self):
        def manually_restore(message, buttons):
            self.actual = self.saved
            return 1

        self.alarm.side_effect = manually_restore
        self.assertEqual(psl.checkViewport(), self.saved)
        self.resize.assert_not_called()
        self.assertEqual(self.size.call_count, 2)
        self.assert_recovery_buttons()
        self.assert_metadata_unchanged()

    def test_check_again_remains_blocked_while_size_is_wrong(self):
        self.choices(1, 1, 2)
        with self.assertRaises(SystemExit) as stopped:
            psl.checkViewport()
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.size.call_count, 3)
        self.resize.assert_not_called()
        self.assert_recovery_buttons()
        self.assert_metadata_unchanged()

    def test_stop_or_window_close_exits_with_code_two(self):
        # alarm() maps closing its window to the last button (index 2).
        self.choices(2)
        with self.assertRaises(SystemExit) as stopped:
            psl.checkViewport()
        self.assertEqual(stopped.exception.code, 2)
        self.resize.assert_not_called()
        self.assert_recovery_buttons()
        self.assert_metadata_unchanged()

    def test_resize_error_is_shown_on_next_prompt_and_can_be_retried(self):
        outcomes = iter((RuntimeError("screen too small"), None))

        def resize_with_first_failure(width, height):
            error = next(outcomes)
            if error is not None:
                raise error
            self.restore(width, height)

        self.resize.side_effect = resize_with_first_failure
        self.choices(0, 0)
        self.assertEqual(psl.checkViewport(), self.saved)
        self.assertEqual(self.resize.call_count, 2)
        self.assertEqual(self.size.call_count, 3)
        self.assertIn("screen too small", self.alarm.call_args_list[1].args[0])
        self.assert_recovery_buttons()
        self.assert_metadata_unchanged()

    def test_resize_error_cannot_continue_on_unchanged_size(self):
        self.resize.side_effect = RuntimeError("browser refused resize")
        self.choices(0, 2)
        with self.assertRaises(SystemExit) as stopped:
            psl.checkViewport()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("browser refused resize", self.alarm.call_args.args[0])
        self.assert_metadata_unchanged()

    def test_ctrl_c_during_resize_propagates_without_another_prompt(self):
        self.resize.side_effect = KeyboardInterrupt
        self.choices(0)
        with self.assertRaises(KeyboardInterrupt):
            psl.checkViewport()
        self.assertEqual(self.alarm.call_count, 1)
        self.assert_metadata_unchanged()

    def test_invalid_current_viewport_is_not_offered_size_restoration(self):
        for invalid in ((0, 1455), (True, 1455), (2542.0, 1455), None):
            with self.subTest(viewport=invalid):
                self.actual = invalid
                with self.assertRaises(layouts.LayoutError):
                    psl.checkViewport()
        self.alarm.assert_not_called()
        self.resize.assert_not_called()
        self.assert_metadata_unchanged()

    def test_invalid_new_viewport_does_not_bind_or_prompt(self):
        new_layout = layouts.Layout(self.folder / "new")
        self.actual = (0, 0)
        with mock.patch.object(psl, "_layout", new_layout):
            with self.assertRaises(layouts.LayoutError):
                psl.checkViewport()
        self.assertIsNone(new_layout.viewport)
        self.assertFalse((new_layout.path / "layout.json").exists())
        self.alarm.assert_not_called()
        self.resize.assert_not_called()

    def test_generic_layout_error_is_not_treated_as_dimension_mismatch(self):
        error = layouts.LayoutError("corrupt layout metadata")
        with mock.patch.object(self.selected, "check_viewport", side_effect=error):
            with self.assertRaises(layouts.LayoutError) as caught:
                psl.checkViewport()
        self.assertIs(caught.exception, error)
        self.alarm.assert_not_called()
        self.resize.assert_not_called()

    def test_viewport_read_error_propagates_before_or_after_restore(self):
        for readings in ([RuntimeError("disconnected")],
                         [self.actual, RuntimeError("disconnected")]):
            with self.subTest(readings=readings):
                self.size.side_effect = readings
                self.alarm.reset_mock()
                self.resize.reset_mock()
                self.choices(0)
                with self.assertRaisesRegex(RuntimeError, "disconnected"):
                    psl.checkViewport()
                self.assertEqual(self.alarm.call_count, len(readings) - 1)
                self.assertEqual(self.resize.call_count, len(readings) - 1)
        self.assert_metadata_unchanged()

    def test_layout_viewport_and_named_actions_do_not_offer_recovery(self):
        (self.folder / "alignment-tab.json").write_text(json.dumps(
            {"version": 1, "kind": "point", "x": 10, "y": 20}), encoding="utf-8")
        for operation in (psl.layoutViewport, lambda: psl.click("alignment-tab"),
                          lambda: psl.doubleClick("alignment-tab")):
            with self.subTest(operation=operation):
                with self.assertRaises(layouts.LayoutError):
                    operation()
        self.alarm.assert_not_called()
        self.resize.assert_not_called()
        self.assert_metadata_unchanged()


if __name__ == "__main__":
    unittest.main()
