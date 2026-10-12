"""Resize a pair to one width, then crop to the shared height."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class NormalizedPair:
    before: Image.Image
    after: Image.Image
    width: int
    before_height: int
    after_height: int
    height_delta: int
    shared_height: int


def _fit_width(image: Image.Image, width: int) -> Image.Image:
    image = image.convert("RGB")
    if image.width == width:
        return image
    height = max(1, round(image.height * (width / image.width)))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def normalize_pair(before: Image.Image, after: Image.Image, width: int) -> NormalizedPair:
    fitted_before = _fit_width(before, width)
    fitted_after = _fit_width(after, width)
    shared = min(fitted_before.height, fitted_after.height)
    if shared < 1:
        raise ValueError("normalized height is empty")
    box = (0, 0, width, shared)
    return NormalizedPair(
        before=fitted_before.crop(box),
        after=fitted_after.crop(box),
        width=width,
        before_height=fitted_before.height,
        after_height=fitted_after.height,
        height_delta=abs(fitted_before.height - fitted_after.height),
        shared_height=shared,
    )


def normalize_files(before_path: Path, after_path: Path, width: int) -> NormalizedPair:
    with Image.open(before_path) as before_image, Image.open(after_path) as after_image:
        return normalize_pair(before_image, after_image, width)
