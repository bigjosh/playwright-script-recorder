"""Click preview checks using a frozen screenshot and the existing fake Tk UI."""

import sys
import unittest
from unittest import mock

from PIL import Image, ImageTk

from test_recapture_ui import Harness
import playwrightscriptlayout as layouts


class ClickPreviewTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.enterContext(mock.patch.dict(sys.modules, {"tkinter": self.h.tk}))
        self.enterContext(mock.patch.object(layouts, "_root", side_effect=self.h.root))
        self.enterContext(mock.patch.object(ImageTk, "PhotoImage", side_effect=lambda img, master: img))

    def test_target_matches_overview_and_magnified_crop(self):
        for size, point in [((2542, 1455), (1225, 173)), ((2542, 1455), (2541, 1454)),
                            ((1, 1), (0, 0))]:
            with self.subTest(size=size, point=point):
                self.h.widgets.clear()
                img = Image.new("RGB", size, "white")
                img.putpixel(point, (12, 34, 56))
                source = img.tobytes()
                self.h.action = lambda root: self.h.click("Back to step")
                layouts.show_click_preview("move-absolute", img, point)

                overview, detail = self.h.images()
                overview_center = (int((point[0] + 0.5) * overview.width / img.width),
                                   int((point[1] + 0.5) * overview.height / img.height))
                detail_center = (detail.width // 2, detail.height // 2)
                self.assertEqual(detail.getpixel(detail_center), (12, 34, 56))
                for canvas, (cx, cy) in zip(self.h.image_canvases(), (overview_center, detail_center)):
                    marks = [coords for kind, coords, _ in canvas.items if kind == "line"]
                    self.assertIn((cx + 5, cy, cx + 20, cy), marks)
                    self.assertIn((cx, cy - 5, cx, cy - 20), marks)
                self.assertEqual(img.tobytes(), source)
                self.assertTrue(all(widget.destroyed for widget in self.h.widgets))

    def test_escape_and_window_close_return_to_step(self):
        for close in (lambda root: root.bindings["<Escape>"](None),
                      lambda root: root.protocols["WM_DELETE_WINDOW"]()):
            with self.subTest(close=close):
                self.h.action = close
                self.assertIsNone(layouts.show_click_preview("test", Image.new("RGB", (2, 2)), (1, 1)))
                self.assertTrue(all(widget.destroyed for widget in self.h.widgets))

    def test_invalid_point_rejected_before_window_opens(self):
        with self.assertRaises(layouts.LayoutError):
            layouts.show_click_preview("test", Image.new("RGB", (10, 10)), (10, 0))
        self.assertFalse(self.h.roots)
