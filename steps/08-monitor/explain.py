"""One paragraph about visible differences, plus a change label."""

from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

LABELS = ("pricing", "hero", "banner", "other")
_INLINE_LABEL = re.compile(r"(?i)\s*LABEL:\s*(pricing|hero|banner|other)\s*$")

PROMPT = (
    "The first image is an earlier screenshot of a public page. "
    "The second image is a later screenshot of the same page. "
    "Write one factual paragraph limited to differences that are visible in the images. "
    "Do not guess why something changed, and do not describe anything off-screen. "
    "If the shots look the same, say that. "
    "End with one line in exactly this form, using one of these four words: "
    "LABEL: pricing or LABEL: hero or LABEL: banner or LABEL: other. "
    "Use pricing, hero, or banner only when that part of the page visibly changed."
)


@dataclass(frozen=True)
class Explanation:
    paragraph: str
    label: str


def parse_explanation(text: str) -> Explanation:
    label = "other"
    body = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("LABEL:"):
            raw = stripped.split(":", 1)[1].strip().lower().split()[0]
            label = raw if raw in LABELS else "other"
            continue
        inline = _INLINE_LABEL.search(stripped)
        if inline:
            label = inline.group(1).lower()
            stripped = _INLINE_LABEL.sub("", stripped).strip()
        if stripped:
            body.append(stripped)
    return Explanation(paragraph=" ".join(body).strip(), label=label)


def jpeg_bytes_for_model(path: Path, max_edge: int = 1280) -> bytes:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        rgb.thumbnail((max_edge, max_edge))
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=70)
        return buffer.getvalue()


def _data_url(payload: bytes) -> str:
    encoded = base64.standard_b64encode(payload).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def explain_change(
    before_path: Path,
    after_path: Path,
    *,
    api_key: str,
    model: str = "gpt-4o",
    client=None,
) -> Explanation:
    if client is None:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=120)
    before = _data_url(jpeg_bytes_for_model(before_path))
    after = _data_url(jpeg_bytes_for_model(after_path))
    response = client.chat.completions.create(
        model=model,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": before, "detail": "low"}},
                    {"type": "image_url", "image_url": {"url": after, "detail": "low"}},
                ],
            }
        ],
    )
    text = response.choices[0].message.content or ""
    return parse_explanation(text)
