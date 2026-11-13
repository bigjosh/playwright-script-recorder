"""OCR-area editing and recovery with fake pixels, dialogs and machine API."""

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


NORMAL = ("Next Step", "Run Continuously", "Redefine OCR area", "STOP")
ERROR = ("Retry", "Redefine OCR area", "STOP")
CONFIRM = ("Save OCR area", "Select again", "Cancel")


class OCRRegionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.old_box = (10, 20, 30, 30)
        self.new_box = (40, 50, 70, 65)
        (self.folder / "layout.json").write_text(json.dumps({
            "version": 1, "viewport": [640, 480],
        }), encoding="utf-8")
        self.metadata = self.folder / "actual-xy.json"
        self.metadata.write_text(json.dumps({
            "version": 1, "kind": "region", "box": list(self.old_box),
        }), encoding="utf-8")
        self.original = self.metadata.read_bytes()
        self.screen = Image.new("RGB", (640, 480), "gray")
        self.screen.paste("white", self.old_box)
        self.screen.paste("black", self.new_box)
        self.page = mock.Mock()
        self.patch(psl, "_page", self.page)
        self.patch(psl, "_layout", None)
        self.patch(psl, "_pause_on_info", True)
        self.patch(psl, "_shot_dir", None)
        self.patch(psl, "_emit")
        self.patch(psl, "info", side_effect=AssertionError("Unexpected nested pause"))
        self.patch(psl, "alarm", side_effect=AssertionError("Unexpected generic alarm"))
        self.viewport = self.patch(psl, "viewportSize", return_value=(640, 480))
        self.capture = self.patch(psl, "viewportGrab", side_effect=lambda **kw: self.screen.copy())
        self.reader = mock.Mock(return_value="X=0, Y=0 um\n")
        self.patch(psl._ocr, "TesseractReader", return_value=self.reader)
        self.dialog = self.patch(layouts, "ocr_dialog", create=True, side_effect=[0])
        self.picker = self.patch(layouts, "pick_on_image", return_value=self.new_box)
        self.patch(layouts, "prompt_definition", side_effect=AssertionError("Unexpected definition prompt"))
        self.patch(layouts, "confirm_reference", side_effect=AssertionError("OCR region is not a reference frame"))
        psl.loadLayout(self.folder)

    def patch(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def tearDown(self):
        self.page.mouse.click.assert_not_called()
        self.page.mouse.dblclick.assert_not_called()
        self.page.mouse.down.assert_not_called()
        self.page.keyboard.press.assert_not_called()
        self.page.keyboard.type.assert_not_called()
        self.assertEqual(list(self.folder.glob("*.png")), [])

    def read(self, **kwargs):
        return psl.grabOCRTextLine("actual-xy", review=True, **kwargs)

    def buttons(self, index):
        return tuple(self.dialog.call_args_list[index].kwargs["buttons"])

    def assert_unchanged(self):
        self.assertEqual(self.metadata.read_bytes(), self.original)

    def test_default_call_remains_noninteractive_even_when_step_mode_enabled(self):
        self.assertEqual(psl.grabOCRTextLine("actual-xy"), "X=0, Y=0 um")
        self.dialog.assert_not_called()
        self.picker.assert_not_called()
        self.assertEqual(self.capture.call_count, 1)

    def test_default_ocr_error_still_raises_without_recovery_ui(self):
        self.reader.side_effect = psl.OCRError("Cannot read")
        with self.assertRaisesRegex(psl.OCRError, "Cannot read"):
            psl.grabOCRTextLine("actual-xy")
        self.dialog.assert_not_called()

    def test_setup_failure_stops_before_capture_or_recovery_ui(self):
        with mock.patch.object(psl._ocr, "TesseractReader",
                               side_effect=psl.OCRError("Tesseract not installed")):
            with self.assertRaisesRegex(psl.OCRError, "Tesseract not installed"):
                self.read()
        self.capture.assert_not_called()
        self.reader.assert_not_called()
        self.dialog.assert_not_called()
        self.assert_unchanged()

    def test_continuous_success_validates_fresh_read_without_dialog(self):
        psl._pause_on_info = False
        validator = mock.Mock()
        self.assertEqual(self.read(validator=validator), "X=0, Y=0 um")
        validator.assert_called_once_with("X=0, Y=0 um")
        self.dialog.assert_not_called()
        self.assertEqual(self.capture.call_count, 1)

    def test_next_step_recaptures_and_revalidates_instead_of_returning_preview(self):
        self.reader.side_effect = ["X=0, Y=0 um", "X=1, Y=2 um"]
        validator = mock.Mock()
        self.assertEqual(self.read(validator=validator), "X=1, Y=2 um")
        self.assertEqual(validator.call_args_list, [mock.call("X=0, Y=0 um"), mock.call("X=1, Y=2 um")])
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.buttons(0), NORMAL)
        name, crop, text = self.dialog.call_args.args[:3]
        self.assertEqual((name, crop.size, text), ("actual-xy", (20, 10), "X=0, Y=0 um"))

    def test_fresh_validation_failure_after_next_step_cannot_return_success(self):
        self.reader.side_effect = ["X=0, Y=0 um", "X=999, Y=999 um"]
        self.dialog.side_effect = [0, 2]

        def validate(text):
            if "999" in text:
                raise psl.OCRError("Position outside tolerance")

        with self.assertRaises(SystemExit) as stopped:
            self.read(validator=validate)
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.buttons(1), ERROR)
        self.assertIn("Position outside tolerance", str(self.dialog.call_args.kwargs["error"]))

    def test_run_continuously_requires_a_fresh_read_and_disables_later_previews(self):
        self.dialog.side_effect = [1]
        self.reader.side_effect = ["preview", "fresh", "later"]
        self.assertEqual(self.read(), "fresh")
        self.assertFalse(psl._pause_on_info)
        self.assertEqual(self.read(), "later")
        self.assertEqual(self.dialog.call_count, 1)

    def test_retry_after_engine_error_reads_again_without_replaying_actions(self):
        psl._pause_on_info = False
        self.reader.side_effect = [psl.OCRError("Unreadable"), "recovered"]
        self.dialog.side_effect = [0]
        self.assertEqual(self.read(), "recovered")
        self.assertEqual(self.buttons(0), ERROR)
        self.assertEqual(self.capture.call_count, 2)

    def test_false_validator_result_is_a_failure_with_no_skip_option(self):
        psl._pause_on_info = False
        self.dialog.side_effect = [2]
        with self.assertRaises(SystemExit):
            self.read(validator=lambda text: False)
        self.assertEqual(self.buttons(0), ERROR)

    def test_unexpected_validator_failure_propagates_without_recovery_dialog(self):
        with self.assertRaisesRegex(RuntimeError, "Programming error"):
            self.read(validator=mock.Mock(side_effect=RuntimeError("Programming error")))
        self.dialog.assert_not_called()

    def test_redefine_saves_only_region_after_confirmation_and_returns_to_pause(self):
        choices = iter([2, 0, 0])

        def dialog(name, crop, text, **kwargs):
            buttons = tuple(kwargs["buttons"])
            if self.dialog.call_count <= 2:
                self.assert_unchanged()
            else:
                saved = json.loads(self.metadata.read_text(encoding="utf-8"))
                self.assertEqual(saved, {"version": 1, "kind": "region", "box": list(self.new_box)})
                self.assertEqual(buttons, NORMAL)
            return next(choices)

        self.dialog.side_effect = dialog
        self.assertEqual(self.read(), "X=0, Y=0 um")
        self.assertEqual([self.buttons(i) for i in range(3)], [NORMAL, CONFIRM, NORMAL])
        self.assertEqual([call.args[0].size for call in self.reader.call_args_list],
                         [(20, 10), (30, 15), (30, 15), (30, 15)])
        # Both production checks share this entry; subsequent reads use it too.
        psl._pause_on_info = False
        self.read()
        self.assertEqual(self.reader.call_args.args[0].size, (30, 15))

    def test_cancel_picker_returns_to_same_pause_without_changing_region(self):
        self.picker.return_value = None
        self.dialog.side_effect = [2, 3]
        with self.assertRaises(SystemExit):
            self.read()
        self.assertEqual(self.dialog.call_count, 2)
        self.assertEqual(self.buttons(0), NORMAL)
        self.assertEqual(self.buttons(1), NORMAL)
        self.assertEqual(self.dialog.call_args_list[0].args[2], self.dialog.call_args_list[1].args[2])
        self.assert_unchanged()

    def test_cancel_confirmation_returns_to_pause_without_changing_region(self):
        self.dialog.side_effect = [2, 2, 3]
        with self.assertRaises(SystemExit):
            self.read()
        self.assertEqual([self.buttons(i) for i in range(3)], [NORMAL, CONFIRM, NORMAL])
        self.assert_unchanged()

    def test_unreadable_candidate_has_no_save_and_cancel_keeps_original(self):
        self.reader.side_effect = ["good", psl.OCRError("Candidate unreadable")]
        self.dialog.side_effect = [2, 1, 3]
        with self.assertRaises(SystemExit):
            self.read()
        self.assertEqual(self.buttons(1), ("Select again", "Cancel"))
        self.assert_unchanged()

    def test_select_again_after_unreadable_candidate_can_save_a_new_selection(self):
        self.reader.side_effect = ["good", "", "candidate", "preview", "fresh"]
        self.dialog.side_effect = [2, 0, 0, 0]
        self.assertEqual(self.read(), "fresh")
        self.assertEqual(self.picker.call_count, 2)
        self.assertEqual(self.buttons(1), ("Select again", "Cancel"))
        self.assertEqual(self.buttons(2), CONFIRM)

    def test_edit_after_continuous_error_still_requires_a_paused_step(self):
        psl._pause_on_info = False
        self.reader.side_effect = [psl.OCRError("Old region wrong"), "candidate", "preview", "fresh"]
        self.dialog.side_effect = [1, 0, 0]
        self.assertEqual(self.read(), "fresh")
        self.assertEqual([self.buttons(i) for i in range(3)], [ERROR, CONFIRM, NORMAL])

    def test_candidate_geometry_can_be_saved_but_wrong_position_still_blocks(self):
        self.reader.return_value = "X=999, Y=999 um"
        self.dialog.side_effect = [1, 0, 2]
        validator = mock.Mock(side_effect=psl.OCRError("Position outside tolerance"))
        with self.assertRaises(SystemExit):
            self.read(validator=validator)
        self.assertEqual(self.buttons(1), CONFIRM)
        self.assertEqual(self.buttons(2), ERROR)
        self.assertEqual(json.loads(self.metadata.read_text())["box"], list(self.new_box))

    def test_write_failure_preserves_old_rectangle_and_stops(self):
        self.dialog.side_effect = [2, 0]
        with mock.patch.object(layouts, "_write_json", side_effect=layouts.LayoutError("Disk full")):
            with self.assertRaisesRegex(layouts.LayoutError, "Disk full"):
                self.read()
        self.assert_unchanged()

    def test_candidate_preview_failure_cannot_save_via_console_fallback(self):
        self.dialog.side_effect = [2, RuntimeError("Image preview unavailable")]
        with mock.patch.object(psl, "_pause_console") as console:
            with self.assertRaisesRegex(RuntimeError, "Image preview unavailable"):
                self.read()
        console.assert_not_called()
        self.assert_unchanged()

    def test_viewport_change_at_save_prevents_rectangle_commit(self):
        def dialog(*args, **kwargs):
            if self.dialog.call_count == 1:
                return 2
            self.viewport.return_value = (800, 600)
            return 0

        self.dialog.side_effect = dialog
        with self.assertRaises(layouts.LayoutError):
            self.read()
        self.assert_unchanged()

    def test_page_change_while_picking_prevents_rectangle_commit(self):
        def pick(*args, **kwargs):
            psl._page = mock.Mock()
            return self.new_box

        self.picker.side_effect = pick
        self.dialog.side_effect = [2, 0]
        with self.assertRaises(layouts.LayoutError):
            self.read()
        self.assert_unchanged()

    def test_invalid_rectangle_is_not_padded_and_saved(self):
        self.picker.return_value = (620, 470, 700, 500)
        self.dialog.side_effect = [2]
        with self.assertRaises(layouts.LayoutError):
            self.read()
        self.assert_unchanged()

    def test_review_requires_named_region(self):
        with self.assertRaises((TypeError, ValueError)):
            psl.grabOCRTextLine(10, 20, 30, 30, review=True)
        self.dialog.assert_not_called()
        self.capture.assert_not_called()

    def test_region_cannot_overwrite_a_click_point(self):
        self.metadata.write_text(json.dumps({"version": 1, "kind": "point", "x": 20, "y": 30}))
        before = self.metadata.read_bytes()
        with self.assertRaises(layouts.LayoutError):
            self.read()
        self.assertEqual(self.metadata.read_bytes(), before)
        self.reader.assert_not_called()
        self.dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
