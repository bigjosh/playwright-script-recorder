"""Offline popup matching checks using synthetic pixels only."""

import math
import runpy
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import playwrightscriptpopup as popup


def reference():
    image = Image.new("RGB", (146, 32), (242, 243, 244))
    draw = ImageDraw.Draw(image)
    draw.text((5, 8), "Desktop is shared", fill=(28, 32, 38))
    draw.rectangle((117, 6, 138, 25), outline=(60, 70, 90), width=2)
    draw.line((121, 10, 134, 21), fill=(45, 48, 52), width=2)
    draw.line((134, 10, 121, 21), fill=(45, 48, 52), width=2)
    return image


class PopupMatcherTests(unittest.TestCase):
    def test_detects_reference_after_it_moves_anywhere_in_viewport(self):
        template = reference()
        matcher = popup.PopupMatcher(template)
        for position in ((0, 0), (300, 10), (48, 201), (494, 328)):
            with self.subTest(position=position):
                screenshot = Image.new("RGB", (640, 360), (190, 202, 221))
                screenshot.paste(template, position)
                found = matcher.locate(screenshot)
                self.assertEqual(found[:2], position)
                self.assertGreaterEqual(found[2], 0.999)

    def test_blank_viewport_is_absent(self):
        matcher = popup.PopupMatcher(reference())
        for color in ("white", "black", (120, 121, 122)):
            with self.subTest(color=color):
                self.assertIsNone(matcher.locate(Image.new("RGB", (640, 360), color)))

    def test_other_text_is_absent(self):
        screenshot = Image.new("RGB", (640, 360), (242, 243, 244))
        draw = ImageDraw.Draw(screenshot)
        draw.text((80, 90), "Machine ready to align", fill="black")
        draw.rectangle((400, 20, 450, 44), outline="black", width=2)
        self.assertIsNone(popup.PopupMatcher(reference()).locate(screenshot))

    def test_noisy_reference_remains_present(self):
        import numpy as np

        template = reference()
        pixels = np.asarray(template).astype(np.int16)
        noise = np.random.default_rng(17).normal(0, 5, pixels.shape)
        noisy = Image.fromarray(np.clip(pixels + noise, 0, 255).astype(np.uint8))
        screenshot = Image.new("RGB", (400, 220), "white")
        screenshot.paste(noisy, (104, 93))
        found = popup.PopupMatcher(template).locate(screenshot)
        self.assertEqual(found[:2], (104, 93))
        self.assertGreaterEqual(found[2], 0.90)

    def test_random_noise_is_absent(self):
        import numpy as np

        pixels = np.random.default_rng(24).integers(0, 256, (180, 320, 3), dtype=np.uint8)
        self.assertIsNone(popup.PopupMatcher(reference()).locate(Image.fromarray(pixels)))

    def test_grayscale_rgba_and_palette_inputs(self):
        template = reference()
        for mode in ("L", "RGBA", "P"):
            with self.subTest(mode=mode):
                converted = template.convert(mode)
                screenshot = Image.new(mode, (400, 220))
                if mode == "P":
                    screenshot.putpalette(converted.getpalette())
                screenshot.paste(converted, (54, 29))
                self.assertEqual(popup.PopupMatcher(converted).locate(screenshot)[:2], (54, 29))

    def test_same_size_image_and_threshold_one(self):
        template = reference()
        found = popup.PopupMatcher(template, threshold=1).locate(template)
        self.assertIsNotNone(found)
        self.assertEqual(found[:2], (0, 0))

    def test_template_is_cached_and_original_mutation_does_not_change_it(self):
        template = reference()
        original = template.copy()
        matcher = popup.PopupMatcher(template)
        template.paste("white", (0, 0, template.width, template.height))
        screenshot = Image.new("RGB", (400, 220), "white")
        screenshot.paste(original, (42, 75))
        self.assertEqual(matcher.locate(screenshot)[:2], (42, 75))
        self.assertEqual(matcher.locate(screenshot)[:2], (42, 75))

    def test_invalid_thresholds_fail(self):
        for threshold in (0, -0.1, 1.01, math.nan, math.inf, -math.inf, None, "0.9", True, False):
            with self.subTest(threshold=threshold):
                with self.assertRaisesRegex(popup.PopupDetectionError, "threshold"):
                    popup.PopupMatcher(reference(), threshold=threshold)

    def test_tiny_and_empty_templates_fail(self):
        for size in ((0, 10), (20, 0), (19, 30), (40, 9)):
            with self.subTest(size=size):
                with self.assertRaises(popup.PopupDetectionError):
                    popup.PopupMatcher(Image.new("RGB", size))

    def test_flat_and_low_contrast_templates_fail(self):
        images = [Image.new("RGB", (100, 25), "white")]
        nearly_flat = Image.new("RGB", (100, 25), (230, 230, 230))
        ImageDraw.Draw(nearly_flat).text((3, 5), "Desktop shared", fill=(223, 223, 223))
        images.append(nearly_flat)
        mostly_blank = Image.new("RGB", (1000, 1000), "white")
        ImageDraw.Draw(mostly_blank).text((5, 5), "tiny", fill="black")
        images.append(mostly_blank)
        for number, image in enumerate(images):
            with self.subTest(number=number):
                with self.assertRaisesRegex(popup.PopupDetectionError, "contrast"):
                    popup.PopupMatcher(image)

    def test_nonimage_inputs_fail(self):
        for value in (None, "popup.png", b"image", object()):
            with self.subTest(value=value):
                with self.assertRaisesRegex(popup.PopupDetectionError, "Pillow image"):
                    popup.PopupMatcher(value)
                with self.assertRaisesRegex(popup.PopupDetectionError, "Pillow image"):
                    popup.PopupMatcher(reference()).locate(value)

    def test_undersized_or_empty_screenshots_fail_instead_of_absent(self):
        matcher = popup.PopupMatcher(reference())
        for size in ((0, 50), (400, 0), (145, 400), (640, 31)):
            with self.subTest(size=size):
                with self.assertRaises(popup.PopupDetectionError):
                    matcher.locate(Image.new("RGB", size, "white"))

    def test_dependencies_are_only_imported_at_construction(self):
        with patch.object(popup.importlib, "import_module", side_effect=ImportError("missing runtime")) as load:
            isolated = runpy.run_path(popup.__file__)
            load.assert_not_called()
            with self.assertRaisesRegex(isolated["PopupDetectionError"], "opencv-python-headless"):
                isolated["PopupMatcher"](reference())

    def test_engine_failure_is_an_error(self):
        matcher = popup.PopupMatcher(reference())
        with patch.object(matcher._cv2, "matchTemplate", side_effect=RuntimeError("engine failed")):
            with self.assertRaisesRegex(popup.PopupDetectionError, "template matching failed"):
                matcher.locate(Image.new("RGB", (640, 360), "white"))

    def test_nonfinite_score_is_an_error(self):
        matcher = popup.PopupMatcher(reference())
        with patch.object(matcher._cv2, "minMaxLoc", return_value=(0, math.nan, (0, 0), (0, 0))):
            with self.assertRaisesRegex(popup.PopupDetectionError, "invalid correlation"):
                matcher.locate(Image.new("RGB", (640, 360), "white"))


if __name__ == "__main__":
    unittest.main()
