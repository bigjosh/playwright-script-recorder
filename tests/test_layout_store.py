"""Offline persistence/definition tests; never connect to a browser."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import playwrightscriptlayout as layout_module
from playwrightscriptlayout import Layout, LayoutCancelled, LayoutError


class LayoutStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "layout"
        self.layout = Layout(self.path)
        self.layout.check_viewport((100, 80))
        self.image = Image.new("RGB", (100, 80), "red")

    def write_entry(self, name, value):
        (self.path / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")

    def point(self, name="alignment-tab", point=(21, 33)):
        with patch.object(layout_module, "prompt_definition", return_value=True), \
                patch.object(layout_module, "pick_on_image", return_value=point):
            return self.layout.define(name, "point", lambda: self.image)

    def frame(self, name="focus-ready", accept=True):
        with patch.object(layout_module, "prompt_definition", return_value=True), \
                patch.object(layout_module, "pick_on_image", return_value=(10, 15, 30, 45)), \
                patch.object(layout_module, "confirm_reference", return_value=accept):
            return self.layout.define(name, "frame", lambda: self.image)

    def test_new_layout_does_not_open_gui_or_bind_before_use(self):
        with patch.object(layout_module, "prompt_definition") as prompt:
            other = Layout(self.path / "other")
        self.assertTrue(other.path.is_dir())
        self.assertIsNone(other.viewport)
        self.assertFalse((other.path / "layout.json").exists())
        prompt.assert_not_called()

    def test_viewport_roundtrip_and_mismatch(self):
        reopened = Layout(self.path)
        self.assertEqual((100, 80), reopened.viewport)
        reopened.check_viewport((100, 80))
        with self.assertRaises(LayoutError):
            reopened.check_viewport((101, 80))
        self.assertEqual((100, 80), Layout(self.path).viewport)

    def test_invalid_viewports(self):
        for value in ([100.0, 80], [True, 80], [0, 80], [100], None):
            with self.subTest(value=value), self.assertRaises(LayoutError):
                self.layout.check_viewport(value)

    def test_invalid_names(self):
        for name in ("../x", "a/b", "a\\b", "", "layout", "LAYOUT", "CON", "nul",
                     "LPT1", "COM9", "name.", "_name", "a:b", "a" * 101, None):
            with self.subTest(name=name), self.assertRaises(LayoutError):
                self.layout.resolve(name, "point", lambda: self.image)

    def test_point_definition_and_reuse_do_not_grab_or_prompt(self):
        self.assertEqual({"version": 1, "kind": "point", "x": 21, "y": 33}, self.point())
        with patch.object(layout_module, "prompt_definition") as prompt:
            found = Layout(self.path).resolve("alignment-tab", "point", None)
        self.assertEqual((21, 33), (found["x"], found["y"]))
        prompt.assert_not_called()

    def test_missing_point_cancels_before_grab(self):
        with patch.object(layout_module, "prompt_definition", return_value=False), \
                self.assertRaises(LayoutCancelled) as cancelled:
            self.layout.resolve("alignment-tab", "point", None)
        self.assertEqual(2, cancelled.exception.code)
        self.assertFalse((self.path / "alignment-tab.json").exists())

    def test_cancelled_picker_preserves_existing_point(self):
        self.point()
        with patch.object(layout_module, "prompt_definition", return_value=True), \
                patch.object(layout_module, "pick_on_image", return_value=None), \
                self.assertRaises(LayoutCancelled):
            self.layout.define("alignment-tab", "point", lambda: self.image)
        found = self.layout.resolve("alignment-tab", "point", None)
        self.assertEqual((21, 33), (found["x"], found["y"]))

    def test_definition_screenshot_must_match_viewport(self):
        with patch.object(layout_module, "prompt_definition", return_value=True), \
                patch.object(layout_module, "pick_on_image") as picker, \
                self.assertRaises(LayoutError):
            self.layout.define("alignment-tab", "point", lambda: Image.new("RGB", (99, 80)))
        picker.assert_not_called()

    def test_outside_or_noninteger_point_rejected(self):
        for point in ((100, 20), (20, 80), (-1, 20), (1.5, 20), (True, 20)):
            with self.subTest(point=point), self.assertRaises(LayoutError):
                self.point(point=point)
        self.assertFalse((self.path / "alignment-tab.json").exists())

    def test_region_can_include_full_viewport(self):
        with patch.object(layout_module, "prompt_definition", return_value=True), \
                patch.object(layout_module, "pick_on_image", return_value=(0, 0, 100, 80)):
            entry = self.layout.define("actual-xy", "region", lambda: self.image)
        self.assertEqual([0, 0, 100, 80], entry["box"])
        self.assertEqual([], list(self.path.glob("*.png")))

    def test_frame_saves_exact_accepted_crop_and_relative_disk_path(self):
        entry = self.frame()
        saved = json.loads((self.path / "focus-ready.json").read_text(encoding="utf-8"))
        self.assertFalse(Path(saved["image"]).is_absolute())
        self.assertTrue(Path(entry["image"]).is_absolute())
        with Image.open(entry["image"]) as reference:
            self.assertEqual((20, 30), reference.size)
            self.assertEqual(self.image.crop((10, 15, 30, 45)).tobytes(), reference.tobytes())
        self.assertEqual(entry, self.layout.resolve("focus-ready", "frame", None))

    def test_reference_rejection_preserves_existing_reference(self):
        original = self.frame()
        original_json = (self.path / "focus-ready.json").read_bytes()
        self.image.paste("blue", (0, 0, 100, 80))
        with self.assertRaises(LayoutCancelled):
            self.frame(accept=False)
        self.assertEqual(original_json, (self.path / "focus-ready.json").read_bytes())
        self.assertEqual(original, self.layout.resolve("focus-ready", "frame", None))
        self.assertEqual(1, len(list(self.path.glob("*.png"))))

    def test_json_failure_does_not_replace_previous_reference(self):
        original = self.frame()
        with patch.object(layout_module, "_write_json", side_effect=LayoutError("disk full")), \
                self.assertRaises(LayoutError):
            self.frame()
        self.assertEqual(original, self.layout.resolve("focus-ready", "frame", None))
        self.assertEqual(1, len(list(self.path.glob("*.png"))))

    def test_invalid_entry_is_not_treated_as_missing(self):
        (self.path / "broken.json").write_text("{broken", encoding="utf-8")
        with patch.object(layout_module, "prompt_definition") as prompt, \
                self.assertRaises(LayoutError):
            self.layout.resolve("broken", "point", None)
        prompt.assert_not_called()

    def test_entry_kind_mismatch_fails(self):
        self.point()
        with self.assertRaises(LayoutError):
            self.layout.resolve("alignment-tab", "region", None)

    def test_invalid_saved_geometry_fails(self):
        for box in ([0, 0, 0, 20], [20, 0, 10, 20], [0, 0, 101, 20], [0, 0, True, 20]):
            with self.subTest(box=box):
                self.write_entry("bad", {"version": 1, "kind": "region", "box": box})
                with self.assertRaises(LayoutError):
                    self.layout.resolve("bad", "region", None)

    def test_escaping_reference_path_fails(self):
        for filename in ("../outside.png", str(self.path.parent / "outside.png")):
            with self.subTest(filename=filename):
                self.write_entry("bad", {"version": 1, "kind": "frame", "box": [0, 0, 10, 10],
                                         "image": filename})
                with self.assertRaises(LayoutError):
                    self.layout.resolve("bad", "frame", None)

    def test_wrong_dimensions_missing_and_corrupt_reference_fail(self):
        entry = self.frame()
        image_path = Path(entry["image"])
        Image.new("RGB", (1, 1)).save(image_path)
        with self.assertRaises(LayoutError):
            self.layout.resolve("focus-ready", "frame", None)
        image_path.write_bytes(b"broken PNG")
        with self.assertRaises(LayoutError):
            self.layout.resolve("focus-ready", "frame", None)
        image_path.unlink()
        with self.assertRaises(LayoutError):
            self.layout.resolve("focus-ready", "frame", None)

    def test_corrupt_metadata_fails_without_dialog(self):
        for contents in ("{}", "{broken", '{"version":2,"viewport":[100,80]}',
                         '{"version":true,"viewport":[100,80]}', '{"version":1}'):
            (self.path / "layout.json").write_text(contents, encoding="utf-8")
            with self.subTest(contents=contents), self.assertRaises(LayoutError):
                Layout(self.path)

    def test_missing_metadata_with_existing_entries_is_not_a_new_layout(self):
        self.point()
        (self.path / "layout.json").unlink()
        with self.assertRaises(LayoutError):
            Layout(self.path)
        self.assertFalse((self.path / "layout.json").exists())


if __name__ == "__main__":
    unittest.main()
