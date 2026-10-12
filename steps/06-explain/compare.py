"""SSIM, a changed-pixel mask, and an annotated PNG."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from skimage.metrics import structural_similarity

from normalize import NormalizedPair


@dataclass(frozen=True)
class CompareResult:
    ssim: float
    changed_area: float
    heatmap_path: Path
    boxes: tuple[tuple[int, int, int, int], ...]
    height_delta: int


def _win_size(height: int, width: int) -> int:
    side = min(height, width)
    if side >= 7:
        return 7
    size = side if side % 2 else side - 1
    return max(size, 1)


def _gray(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.uint8)


def _boxes(mask: np.ndarray, limit: int = 12) -> list[tuple[int, int, int, int]]:
    mask_u8 = mask.astype(np.uint8) * 255
    contours, _hierarchy = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h < 64:
            continue
        boxes.append((int(x), int(y), int(w), int(h)))
    boxes.sort(key=lambda item: item[2] * item[3], reverse=True)
    return boxes[:limit]


def _annotate(after: Image.Image, mask: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> Image.Image:
    original = np.array(after.convert("RGBA"))
    blended = original.copy()
    blended[mask] = (0.55 * original[mask] + 0.45 * np.array([220, 40, 40, 255])).astype(np.uint8)
    image = Image.fromarray(blended, "RGBA").convert("RGB")
    draw = ImageDraw.Draw(image)
    for x, y, w, h in boxes:
        draw.rectangle((x, y, x + w - 1, y + h - 1), outline=(180, 20, 20), width=2)
    return image


def compare_images(
    pair: NormalizedPair,
    heatmap_path: Path,
    pixel_threshold: int = 24,
) -> CompareResult:
    before = _gray(pair.before)
    after = _gray(pair.after)
    if before.shape != after.shape:
        raise ValueError(f"shape mismatch {before.shape} vs {after.shape}")
    win = _win_size(*before.shape)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        score = structural_similarity(before, after, data_range=255, win_size=win)
    if not np.isfinite(score):
        score = 1.0 if np.array_equal(before, after) else 0.0
    difference = cv2.absdiff(before, after)
    mask = difference > pixel_threshold
    changed = float(mask.mean()) if mask.size else 0.0
    boxes = _boxes(mask)
    heatmap_path = Path(heatmap_path)
    heatmap_path.parent.mkdir(parents=True, exist_ok=True)
    _annotate(pair.after, mask, boxes).save(heatmap_path, format="PNG")
    return CompareResult(
        ssim=float(score),
        changed_area=changed,
        heatmap_path=heatmap_path,
        boxes=tuple(boxes),
        height_delta=pair.height_delta,
    )
