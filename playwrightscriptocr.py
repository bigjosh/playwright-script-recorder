"""Read text from a bounded image using the external Tesseract executable.

This module processes supplied images only; it never connects to a browser or
interacts with the desktop. Requires Pillow and Tesseract with English data.
"""

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Optional, Union

from PIL import Image, ImageOps

__all__ = ["OCRError", "TesseractReader", "preprocess"]


class OCRError(RuntimeError):
    """The selected pixels or OCR runtime could not be read reliably."""


def preprocess(image: Image.Image) -> Image.Image:
    """Enlarge tiny screen text and supply a light margin for single-line OCR."""
    if image.width < 1 or image.height < 1:
        raise OCRError("The selected rectangle is empty.")
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, "white")
    gray = Image.alpha_composite(background, rgba).convert("L")
    gray = ImageOps.autocontrast(gray)
    enlarged = gray.resize((gray.width * 4, gray.height * 4), Image.Resampling.LANCZOS)
    return ImageOps.expand(enlarged, border=10, fill=255)


def _find_tesseract(executable: Optional[Union[str, os.PathLike]]) -> str:
    if executable is not None:
        selected = os.fspath(executable)
        found = shutil.which(selected)
        if found:
            return found
        if Path(selected).is_file():
            return str(Path(selected).resolve())
        raise OCRError(f"Tesseract executable not found: {selected}")

    found = shutil.which("tesseract")
    if found:
        return found
    candidates = []
    for variable, fallback in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        candidates.append(Path(os.environ.get(variable, fallback)) / "Tesseract-OCR" / "tesseract.exe")
    if os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"])
        candidates.extend((
            local / "Programs" / "Tesseract-OCR" / "tesseract.exe",
            local / "Tesseract-OCR" / "tesseract.exe",
        ))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise OCRError(
        "Tesseract OCR was not found. Install Tesseract with English language "
        "data, then add it to PATH or provide its executable path."
    )


class TesseractReader:
    """Callable single-line OCR reader that preflights Tesseract and English.

    ``executable`` can be an explicit executable path or a name on PATH. If
    omitted, PATH and standard Windows installation folders are searched.
    Each call uses fresh pixels and a temporary image. Subprocesses time out
    after 15 seconds and do not create console windows on Windows.

    Returns Tesseract's raw text, including any trailing newline. No characters
    are repaired or guessed. Failed or empty OCR raises :class:`OCRError`.
    """

    def __init__(self, executable: Optional[Union[str, os.PathLike]] = None):
        self.executable = _find_tesseract(executable)
        version = self._run(["--version"], "Checking the Tesseract version")
        combined = version.stdout + "\n" + version.stderr
        if re.search(r"^tesseract\s+v?\d", combined, re.MULTILINE | re.IGNORECASE) is None:
            raise OCRError("The selected executable did not identify itself as Tesseract.")
        languages = self._run(["--list-langs"], "Checking Tesseract language data")
        installed = {line.strip() for line in (languages.stdout + "\n" + languages.stderr).splitlines()}
        if "eng" not in installed:
            raise OCRError(
                "Tesseract's English language data ('eng') is missing. "
                "Install eng.traineddata for this Tesseract installation before reading text."
            )

    def _run(self, arguments: list[str], operation: str) -> subprocess.CompletedProcess:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise OCRError(f"{operation} timed out after 15 seconds.") from exc
        except OSError as exc:
            raise OCRError(f"{operation} failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "No error details.").strip()
            raise OCRError(f"{operation} failed (exit {completed.returncode}): {detail[:600]}")
        return completed

    def __call__(self, image: Image.Image) -> str:
        prepared = preprocess(image)
        try:
            with tempfile.TemporaryDirectory(prefix="playwrightscript-ocr-") as directory:
                input_path = Path(directory) / "text-line.png"
                prepared.save(input_path)
                result = self._run(
                    [
                        str(input_path), "stdout", "-l", "eng", "--psm", "7",
                        "-c", "load_system_dawg=0", "-c", "load_freq_dawg=0",
                    ],
                    "Reading text from the selected rectangle",
                )
        except OSError as exc:
            raise OCRError(f"Could not prepare the selected pixels for OCR: {exc}") from exc
        if not result.stdout.strip():
            raise OCRError("OCR returned no text for the selected rectangle.")
        return result.stdout
