"""Offline checks for replacing a failed image reference during verification.

These tests use temporary layout files and synthetic screenshots. No Tk windows,
browser connections, keyboard input, or machine operations are performed.
"""

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


class ReferenceRecaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.original_box = (10, 20, 30, 30)
        self.replacement_box = (40, 50, 70, 65)
        self.original_image = self.folder / "original.png"
        Image.new("RGB", (20, 10), "black").save(self.original_image)
        self.metadata = self.folder / "focus-ready.json"
        self.metadata.write_text(json.dumps({
            "version": 1, "kind": "frame", "box": list(self.original_box),
            "image": self.original_image.name,
        }), encoding="utf-8")
        (self.folder / "layout.json").write_text(json.dumps({
            "version": 1, "viewport": [640, 480],
        }), encoding="utf-8")
        self.original_metadata = self.metadata.read_bytes()
        self.original_pixels = self.original_image.read_bytes()
        self.screen = Image.new("RGB", (640, 480), "white")
        self.page = mock.Mock()
        self.patch(psl, "_page", self.page)
        self.patch(psl, "_layout", None)
        self.patch(psl, "_click_guard", None)
        self.viewport = self.patch(psl, "viewportSize", return_value=(640, 480))
        self.capture = self.patch(psl, "viewportGrab",
                                  side_effect=lambda **kwargs: self.screen.copy())
        self.patch(psl, "_emit")
        self.patch(psl, "info")
        self.alarm = self.patch(psl, "alarm", side_effect=[3])
        self.patch(psl, "_VerifyPopup", side_effect=RuntimeError("Headless test"))
        self.patch(psl.time, "sleep")
        self.patch(layouts, "prompt_definition",
                   side_effect=AssertionError("Must show the old reference"))
        self.prompt = self.patch(layouts, "prompt_recapture", return_value=True)
        self.picker = self.patch(layouts, "pick_on_image", return_value=self.replacement_box)
        self.confirm = self.patch(layouts, "confirm_reference", return_value=True)
        psl.loadLayout(self.folder)

    def patch(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def assert_no_input(self):
        self.page.mouse.click.assert_not_called()
        self.page.mouse.dblclick.assert_not_called()
        self.page.mouse.down.assert_not_called()
        self.page.keyboard.press.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def assert_original_unchanged(self):
        self.assertEqual(self.metadata.read_bytes(), self.original_metadata)
        self.assertEqual(self.original_image.read_bytes(), self.original_pixels)
        self.assertEqual(list(self.folder.glob("*.png")), [self.original_image])

    def test_replacement_updates_bounds_and_rechecks_a_fresh_capture(self):
        self.assertTrue(psl.verifyFrame("focus-ready"))
        saved = json.loads(self.metadata.read_text(encoding="utf-8"))
        self.assertEqual(saved["box"], list(self.replacement_box))
        self.assertNotEqual(saved["image"], self.original_image.name)
        with Image.open(self.folder / saved["image"]) as reference:
            self.assertEqual(reference.size, (30, 15))
            self.assertEqual(reference.convert("RGB").tobytes(),
                             self.screen.crop(self.replacement_box).tobytes())
        self.assertEqual(self.original_image.read_bytes(), self.original_pixels)
        self.assertEqual(self.capture.call_count, 3)
        self.assertEqual(self.prompt.call_args.args[0], "focus-ready")
        old_preview = self.prompt.call_args.args[1]
        self.assertEqual(old_preview.size, (20, 10))
        self.assertEqual(old_preview.getpixel((0, 0)), (0, 0, 0))
        self.assertIs(self.picker.call_args.kwargs["reference"], old_preview)
        buttons = self.alarm.call_args.kwargs["buttons"]
        self.assertEqual(tuple(buttons[:3]), (
            "Try compare again", "Show differences", "Skip and continue"))
        self.assertEqual(buttons[3], "Recapture reference")
        self.assertEqual(buttons[-1], "Stop the script")
        self.assert_no_input()

        # A later call must resolve the saved rectangle rather than the old one.
        self.screen.paste("black", self.original_box)
        self.assertTrue(psl.verifyFrame("focus-ready"))
        self.assertEqual(self.prompt.call_count, 1)
        self.assertEqual(self.alarm.call_count, 1)

    def test_accepting_replacement_does_not_pass_if_fresh_screen_changes(self):
        self.capture.side_effect = [
            self.screen.copy(), self.screen.copy(),
            Image.new("RGB", (640, 480), "black"),
        ]
        self.alarm.side_effect = [3, 4]
        with self.assertRaises(SystemExit) as stopped:
            psl.verifyFrame("focus-ready")
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.capture.call_count, 3)
        self.assertEqual(self.alarm.call_count, 2)
        self.assert_no_input()

    def test_cancel_old_preview_returns_to_failed_compare_without_saving(self):
        self.prompt.return_value = False
        self.alarm.side_effect = [3, 4]
        with self.assertRaises(SystemExit) as stopped:
            psl.verifyFrame("focus-ready")
        self.assertEqual(stopped.exception.code, 2)
        self.assertEqual(self.alarm.call_count, 2)
        self.assertEqual(self.capture.call_count, 1)
        self.picker.assert_not_called()
        self.confirm.assert_not_called()
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_cancel_rectangle_returns_to_failed_compare_without_saving(self):
        self.picker.return_value = None
        self.alarm.side_effect = [3, 4]
        with self.assertRaises(SystemExit):
            psl.verifyFrame("focus-ready")
        self.assertEqual(self.alarm.call_count, 2)
        self.assertEqual(self.capture.call_count, 2)
        self.confirm.assert_not_called()
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_reject_replacement_returns_to_failed_compare_without_saving(self):
        self.confirm.return_value = False
        self.alarm.side_effect = [3, 4]
        with self.assertRaises(SystemExit):
            psl.verifyFrame("focus-ready")
        self.assertEqual(self.alarm.call_count, 2)
        self.assertEqual(self.capture.call_count, 2)
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_metadata_write_failure_preserves_original_and_does_not_pass(self):
        with mock.patch.object(layouts, "_write_json",
                               side_effect=layouts.LayoutError("Disk full")):
            with self.assertRaisesRegex(layouts.LayoutError, "Disk full"):
                psl.verifyFrame("focus-ready")
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_invalid_rectangle_cannot_replace_reference(self):
        self.picker.return_value = (10, 20, 900, 30)
        with self.assertRaises(layouts.LayoutError):
            psl.verifyFrame("focus-ready")
        self.confirm.assert_not_called()
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_viewport_change_during_old_preview_prevents_capture_and_save(self):
        def change_viewport(*args, **kwargs):
            self.viewport.return_value = (800, 600)
            return True

        self.prompt.side_effect = change_viewport
        with self.assertRaises(layouts.LayoutError):
            psl.verifyFrame("focus-ready")
        self.picker.assert_not_called()
        self.confirm.assert_not_called()
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_viewport_change_during_confirmation_prevents_commit(self):
        def change_viewport(*args, **kwargs):
            self.viewport.return_value = (800, 600)
            return True

        self.confirm.side_effect = change_viewport
        with self.assertRaises(layouts.LayoutError):
            psl.verifyFrame("focus-ready")
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_legacy_recapture_keeps_box_and_backs_up_previous_pixels(self):
        self.assertTrue(psl.verifyFrame(self.original_image, self.original_box))
        self.assertEqual(self.capture.call_count, 3)
        self.picker.assert_not_called()
        self.assertEqual(self.prompt.call_args.kwargs["fixed_box"], self.original_box)
        with Image.open(self.original_image) as reference:
            self.assertEqual(reference.size, (20, 10))
            self.assertEqual(reference.convert("RGB").tobytes(),
                             self.screen.crop(self.original_box).tobytes())
        backups = list(self.folder.glob("*before-recapture-*.png"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original_pixels)
        self.assertEqual(self.metadata.read_bytes(), self.original_metadata)
        self.assert_no_input()

    def test_legacy_cancel_preserves_reference_and_creates_no_backup(self):
        self.confirm.return_value = False
        self.alarm.side_effect = [3, 4]
        with self.assertRaises(SystemExit):
            psl.verifyFrame(self.original_image, self.original_box)
        self.assertEqual(self.alarm.call_count, 2)
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_legacy_atomic_replace_failure_preserves_original(self):
        with mock.patch.object(psl.os, "replace", side_effect=OSError("Replace failed")):
            with self.assertRaisesRegex(OSError, "Replace failed"):
                psl.verifyFrame(self.original_image, self.original_box)
        self.assertEqual(self.original_image.read_bytes(), self.original_pixels)
        self.assertEqual(self.metadata.read_bytes(), self.original_metadata)
        self.assertFalse(list(self.folder.glob("*.tmp")))
        self.assert_no_input()

    def test_legacy_viewport_change_during_confirmation_prevents_commit(self):
        def change_viewport(*args, **kwargs):
            self.viewport.return_value = (800, 600)
            return True

        self.confirm.side_effect = change_viewport
        with self.assertRaises(layouts.LayoutError):
            psl.verifyFrame(self.original_image, self.original_box)
        self.assert_original_unchanged()
        self.assert_no_input()

    def test_quiet_failed_compare_does_not_offer_recapture(self):
        self.assertFalse(psl.verifyFrame("focus-ready", fallthru=True))
        self.alarm.assert_not_called()
        self.prompt.assert_not_called()
        self.assert_original_unchanged()
        self.assert_no_input()


if __name__ == "__main__":
    unittest.main()
