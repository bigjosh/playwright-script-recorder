"""Locate a recorded popup patch in supplied screenshots, without desktop access.

The template must include distinctive, stable text or controls at the recorded
CSS pixel scale. Crop closely around that content; large areas of background
make a poor reference. No OCR, resizing, clicks, or browser operations occur
here. OpenCV and NumPy are loaded only when a matcher is constructed.
"""

from __future__ import annotations

import importlib
import math
from numbers import Real

from PIL import Image

__all__ = ["PopupDetectionError", "PopupMatcher"]


class PopupDetectionError(RuntimeError):
    """Popup detection could not reliably inspect the supplied pixels."""


class PopupMatcher:
    """Search the whole screenshot for a fixed-size grayscale template.

    References must be at least 20 x 10 pixels. On the 0--255 grayscale scale,
    their standard deviation must be at least 10 and their 1st-to-99th
    percentile contrast must be at least 32. These checks reject flat or
    nearly flat selections, including a tiny mark in a large blank area;
    they do not establish that an arbitrary reference uniquely identifies CRD.

    ``threshold`` is a finite normalized correlation in the range (0, 1].
    The image scale must match the saved reference; no scaling is attempted.
    """

    def __init__(self, reference: Image.Image, threshold: float = 0.90):
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, Real)
            or not 0 < threshold <= 1
            or not math.isfinite(threshold)
        ):
            raise PopupDetectionError("Popup match threshold must be a finite number greater than 0 and at most 1.")
        self.threshold = float(threshold)
        self._validate_image(reference, "Popup reference")
        if reference.width < 20 or reference.height < 10:
            raise PopupDetectionError(
                "Popup reference must be at least 20 x 10 pixels. Select a tightly cropped, distinctive part of the popup."
            )
        try:
            self._np = importlib.import_module("numpy")
            self._cv2 = importlib.import_module("cv2")
        except (ImportError, OSError) as exc:
            raise PopupDetectionError(
                "Popup detection requires NumPy and OpenCV. Install the library requirements, including opencv-python-headless."
            ) from exc

        self._template = self._gray(reference, "Popup reference")
        low, high = self._np.percentile(self._template, (1, 99))
        if float(self._template.std()) < 10 or float(high - low) < 32:
            raise PopupDetectionError(
                "Popup reference has too little contrast or too much blank background. "
                "Select a tightly cropped patch containing distinctive dark text and its lighter background "
                "(grayscale standard deviation >= 10 and 1st-to-99th percentile contrast >= 32)."
            )
        self._size = reference.size

    @staticmethod
    def _validate_image(image, description):
        if not isinstance(image, Image.Image):
            raise PopupDetectionError(f"{description} must be a Pillow image.")
        if image.width < 1 or image.height < 1:
            raise PopupDetectionError(f"{description} is empty.")

    def _gray(self, image, description):
        try:
            rgb = self._np.ascontiguousarray(self._np.asarray(image.convert("RGB"), dtype=self._np.uint8))
            return self._cv2.cvtColor(rgb, self._cv2.COLOR_RGB2GRAY)
        except Exception as exc:
            raise PopupDetectionError(f"Could not read {description.lower()} pixels: {exc}") from exc

    def locate(self, image: Image.Image) -> tuple[int, int, float] | None:
        """Return ``(left, top, score)`` for the best match, or ``None``.

        Invalid input, a screenshot smaller than the reference, or an engine
        failure raises :class:`PopupDetectionError` instead of reporting the
        popup absent. Coordinates are screenshot pixels. The reference array
        is prepared once and is unaffected by later edits to its source image.
        """
        self._validate_image(image, "Popup screenshot")
        if image.width < self._size[0] or image.height < self._size[1]:
            raise PopupDetectionError(
                f"Popup screenshot {image.width} x {image.height} is smaller than the "
                f"reference {self._size[0]} x {self._size[1]}; popup absence cannot be determined."
            )
        gray = self._gray(image, "Popup screenshot")
        try:
            result = self._cv2.matchTemplate(gray, self._template, self._cv2.TM_CCOEFF_NORMED)
            _, score, _, position = self._cv2.minMaxLoc(result)
        except Exception as exc:
            raise PopupDetectionError(f"Popup template matching failed: {exc}") from exc
        if not math.isfinite(score):
            raise PopupDetectionError("Popup template matching returned an invalid correlation score.")
        if score < self.threshold:
            return None
        return int(position[0]), int(position[1]), float(score)
