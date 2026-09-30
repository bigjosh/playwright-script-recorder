"""Offline OCR runtime checks; no browser, desktop, or machine interaction."""

from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import playwrightscriptocr as ocr


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class PreprocessTests(unittest.TestCase):
    def test_resizing_border_and_alpha(self):
        source = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
        source.putpixel((5, 5), (0, 0, 0, 255))
        result = ocr.preprocess(source)
        self.assertEqual(result.size, (100, 60))
        self.assertEqual(result.mode, "L")
        self.assertEqual(result.getpixel((0, 0)), 255)
        self.assertEqual(result.getpixel((15, 15)), 255)
        self.assertLess(result.getpixel((32, 32)), 80)
        self.assertEqual(source.size, (20, 10))

    def test_empty_image_fails(self):
        with self.assertRaises(ocr.OCRError):
            ocr.preprocess(Image.new("RGB", (0, 10)))


class TesseractTests(unittest.TestCase):
    def setUp(self):
        self.locate = patch("playwrightscriptocr._find_tesseract", return_value="fake-tesseract")
        self.locate.start()
        self.addCleanup(self.locate.stop)

    def make_reader(self, run):
        run.side_effect = [completed("tesseract 5.5.0\n"), completed("List of available languages (1):\neng\n")]
        reader = ocr.TesseractReader()
        self.assertEqual([call.args[0][1:] for call in run.call_args_list], [["--version"], ["--list-langs"]])
        run.reset_mock(side_effect=True)
        return reader

    @patch("playwrightscriptocr.subprocess.run")
    def test_preflight_rejects_missing_english(self, run):
        run.side_effect = [completed("tesseract 5.5.0"), completed("List of available languages (1):\nosd\n")]
        with self.assertRaisesRegex(ocr.OCRError, "English language data"):
            ocr.TesseractReader()

    @patch("playwrightscriptocr.subprocess.run", return_value=completed("different program"))
    def test_preflight_rejects_wrong_executable(self, run):
        with self.assertRaisesRegex(ocr.OCRError, "identify itself"):
            ocr.TesseractReader()

    @patch("playwrightscriptocr.subprocess.run")
    def test_preflight_accepts_windows_version_prefix(self, run):
        run.side_effect = [completed("tesseract v5.5.3.20260724\n"), completed("eng\n")]
        ocr.TesseractReader()

    @patch("playwrightscriptocr.subprocess.run")
    def test_reader_uses_fresh_pixels_single_line_and_raw_output(self, run):
        reader = self.make_reader(run)
        inputs = []

        def recognize(command, **kwargs):
            self.assertEqual(command[0], "fake-tesseract")
            self.assertEqual(command[2:7], ["stdout", "-l", "eng", "--psm", "7"])
            self.assertIn("load_system_dawg=0", command)
            self.assertIn("load_freq_dawg=0", command)
            self.assertFalse(any("whitelist" in arg for arg in command))
            self.assertEqual(kwargs["timeout"], 15)
            self.assertEqual(
                kwargs["creationflags"],
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.assertNotIn("shell", kwargs)
            inputs.append(Path(command[1]))
            with Image.open(inputs[-1]) as prepared:
                self.assertEqual(prepared.size, (420, 100))
            return completed("X=100.3, Y=0 µm\n")

        run.side_effect = recognize
        for _ in range(2):
            self.assertEqual(reader(Image.new("RGB", (100, 20), "white")), "X=100.3, Y=0 µm\n")
        self.assertNotEqual(inputs[0], inputs[1])
        self.assertTrue(all(not path.exists() for path in inputs))

    @patch("playwrightscriptocr.subprocess.run")
    def test_arbitrary_line_is_returned_without_character_repair(self, run):
        reader = self.make_reader(run)
        raw = "Ready: O/0, -3.5 \u00b5m (laser OFF)\n\f"
        run.return_value = completed(raw)
        self.assertEqual(reader(Image.new("RGB", (120, 20))), raw)

    @patch("playwrightscriptocr.subprocess.run")
    def test_failed_image_save_is_ocr_error_and_temp_file_is_cleaned(self, run):
        reader = self.make_reader(run)
        attempted_paths = []

        def fail_save(path):
            attempted_paths.append(Path(path))
            raise OSError("could not save pixels")

        with patch("PIL.Image.Image.save", side_effect=fail_save):
            with self.assertRaisesRegex(ocr.OCRError, "prepare the selected pixels"):
                reader(Image.new("RGB", (20, 10)))
        run.assert_not_called()
        self.assertEqual(len(attempted_paths), 1)
        self.assertFalse(attempted_paths[0].parent.exists())

    @patch("playwrightscriptocr.subprocess.run")
    def test_empty_ocr_fails(self, run):
        reader = self.make_reader(run)
        run.return_value = completed("\n\f")
        with self.assertRaisesRegex(ocr.OCRError, "no text"):
            reader(Image.new("RGB", (100, 20)))

    @patch("playwrightscriptocr.subprocess.run")
    def test_subprocess_failures_are_readout_errors(self, run):
        reader = self.make_reader(run)
        failures = [
            subprocess.TimeoutExpired(["fake-tesseract"], 15),
            OSError("executable unavailable"),
            completed(stderr="bad image", returncode=1),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                run.side_effect = [failure]
                with self.assertRaises(ocr.OCRError):
                    reader(Image.new("RGB", (100, 20)))


class ExecutableTests(unittest.TestCase):
    @patch("playwrightscriptocr.shutil.which", return_value=None)
    def test_explicit_existing_executable_path_is_resolved(self, which):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "tesseract.exe"
            executable.write_bytes(b"")
            self.assertEqual(ocr._find_tesseract(executable), str(executable.resolve()))
        which.assert_called_once_with(str(executable))

    @patch("playwrightscriptocr.shutil.which", return_value="installed-tesseract")
    def test_path_runtime_is_selected_without_installation_scan(self, which):
        with patch("playwrightscriptocr.Path.is_file") as is_file:
            self.assertEqual(ocr._find_tesseract(None), "installed-tesseract")
        which.assert_called_once_with("tesseract")
        is_file.assert_not_called()

    def test_explicit_missing_executable_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "nonexistent-tesseract.exe"
            with self.assertRaisesRegex(ocr.OCRError, "not found"):
                ocr.TesseractReader(missing)


if __name__ == "__main__":
    unittest.main()
