"""OCR capture regressions with fake screenshots, OCR, and operator dialogs."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlib as psl
import playwrightscriptlayout as layout


class GrabOCRTextLineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.write("layout.json", {"version": 1, "viewport": [320, 200]})
        self.write("actual-xy.json", {"version": 1, "kind": "region", "box": [10, 20, 110, 40]})
        self.screen = Image.new("RGB", (320, 200), "blue")
        self.screen.paste("white", (10, 20, 110, 40))
        self.reader = mock.Mock(return_value="X=100.3, Y=0 µm\n\f")
        self.constructor = self.patch(psl._ocr, "TesseractReader", return_value=self.reader)
        self.page = self.patch(psl, "_page", mock.Mock())
        self.patch(psl, "_layout", None)
        self.patch(psl, "_emit")
        self.patch(psl, "viewportSize", return_value=(320, 200))
        self.capture = self.patch(psl, "viewportGrab", side_effect=lambda: self.screen.copy())
        for name in ("info", "alarm"):
            self.patch(psl, name, side_effect=AssertionError("Unexpected GUI or screenshot"))
        for name in ("choose_layout_folder", "prompt_definition", "pick_on_image", "confirm_reference"):
            self.patch(layout, name, side_effect=AssertionError("Unexpected operator dialog"))
        psl.loadLayout(self.folder)

    def tearDown(self):
        self.page.mouse.click.assert_not_called()
        self.page.mouse.dblclick.assert_not_called()
        self.page.keyboard.press.assert_not_called()
        self.page.keyboard.type.assert_not_called()

    def patch(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def write(self, filename, value):
        (self.folder / filename).write_text(json.dumps(value), encoding="utf-8")

    def test_named_region_reads_only_crop_and_refreshes_every_call(self):
        colors = []

        def read(pixels):
            self.assertEqual(pixels.size, (100, 20))
            self.assertEqual(len(pixels.getcolors()), 1)
            colors.append(pixels.getpixel((0, 0)))
            return "X=100.3, Y=0 µm\n" if len(colors) == 1 else "X=-20.1, Y=3 µm\n"

        self.reader.side_effect = read
        self.assertEqual(psl.grabOCRTextLine("actual-xy"), "X=100.3, Y=0 µm")
        self.screen.paste("black", (10, 20, 110, 40))
        self.assertEqual(psl.grabOCRTextLine("actual-xy"), "X=-20.1, Y=3 µm")
        self.assertEqual(colors, [(255, 255, 255), (0, 0, 0)])
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(list(self.folder.glob("*.png")), [])

    def test_numeric_rectangle_needs_no_layout_and_passes_executable_path(self):
        psl._layout = None
        executable = Path("custom-ocr") / "tesseract.exe"
        self.assertEqual(psl.grabOCRTextLine(10, 20, 110, 40, tesseract_cmd=executable),
                         "X=100.3, Y=0 µm")
        self.constructor.assert_called_once_with(executable)
        self.assertEqual(self.reader.call_args.args[0].size, (100, 20))
        layout.choose_layout_folder.assert_not_called()

    def test_returns_general_text_preserving_internal_characters_and_spacing(self):
        self.reader.return_value = "  Ready:  O/0 −12.30, +4 µm  \n\f"
        self.assertEqual(psl.grabOCRTextLine("actual-xy"), "Ready:  O/0 −12.30, +4 µm")

    def test_missing_region_is_saved_then_reads_fresh_capture(self):
        layout.prompt_definition.side_effect = None
        layout.prompt_definition.return_value = True
        layout.pick_on_image.side_effect = None

        def define(*args, **kwargs):
            # The actual display can change while the operator defines a crop.
            self.screen.paste("black", (10, 20, 110, 40))
            return (10, 20, 110, 40)

        layout.pick_on_image.side_effect = define
        psl.grabOCRTextLine("new-readout")
        saved = json.loads((self.folder / "new-readout.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kind"], "region")
        self.assertEqual(saved["box"], [10, 20, 110, 40])
        self.assertEqual(self.reader.call_args.args[0].getpixel((0, 0)), (0, 0, 0))
        layout.confirm_reference.assert_not_called()

    def test_cancel_definition_does_not_read_or_save(self):
        layout.prompt_definition.side_effect = layout.LayoutCancelled()
        with self.assertRaises(layout.LayoutCancelled):
            psl.grabOCRTextLine("new-readout")
        self.reader.assert_not_called()
        self.assertFalse((self.folder / "new-readout.json").exists())

    def test_viewport_change_in_fresh_capture_blocks_ocr(self):
        self.screen = Image.new("RGB", (321, 200))
        with self.assertRaises(layout.LayoutError):
            psl.grabOCRTextLine("actual-xy")
        self.reader.assert_not_called()

    def test_out_of_bounds_rectangle_blocks_ocr(self):
        with self.assertRaises(ValueError):
            psl.grabOCRTextLine(300, 20, 400, 40)
        self.reader.assert_not_called()

    def test_runtime_failure_happens_before_capture_or_definition(self):
        self.constructor.side_effect = psl.OCRError("Tesseract not found")
        with self.assertRaisesRegex(psl.OCRError, "Tesseract not found"):
            psl.grabOCRTextLine("new-readout")
        self.capture.assert_not_called()
        layout.prompt_definition.assert_not_called()

    def test_ocr_failure_empty_or_multiple_lines_never_returns_a_value(self):
        with self.subTest("engine failure"):
            self.reader.side_effect = psl.OCRError("OCR timed out")
            with self.assertRaisesRegex(psl.OCRError, "timed out"):
                psl.grabOCRTextLine("actual-xy")
        self.reader.side_effect = None
        for text in ("", " \n\f", "X=1\nY=2", "Ready\rError"):
            with self.subTest(text=text):
                self.reader.return_value = text
                with self.assertRaises(psl.OCRError):
                    psl.grabOCRTextLine("actual-xy")


if __name__ == "__main__":
    unittest.main()
