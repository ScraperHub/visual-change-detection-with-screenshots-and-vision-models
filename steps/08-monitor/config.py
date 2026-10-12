"""Target list, thresholds, and capture settings."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Target:
    url: str
    devices: tuple[str, ...] = ("desktop",)
    css_click_selector: str = ""
    extract_selector: str = ""


@dataclass(frozen=True)
class Settings:
    targets: tuple[Target, ...]
    compare_width: int = 1280
    ssim_delta: float = 0.04
    area_delta: float = 0.03
    pixel_threshold: int = 24
    rotate_after_n: int = 5
    page_wait: int = 2000
    scroll_interval: int = 10
    timeout: int = 120
    shots_dir: Path = field(default_factory=lambda: Path("shots"))
    heatmaps_dir: Path = field(default_factory=lambda: Path("heatmaps"))
    alerts_dir: Path = field(default_factory=lambda: Path("alerts"))
    db_path: Path = field(default_factory=lambda: Path("monitor.sqlite"))
    openai_model: str = "gpt-4o"


def _under(base: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return base / path


def load_settings(path: Path) -> Settings:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    targets = []
    for item in data.get("targets", []):
        devices = tuple(item.get("devices") or ("desktop",))
        unknown = [device for device in devices if device not in {"desktop", "tablet", "mobile"}]
        if unknown:
            raise ValueError(f"unsupported device {unknown[0]!r} for {item.get('url')}")
        targets.append(
            Target(
                url=item["url"],
                devices=devices,
                css_click_selector=item.get("css_click_selector") or "",
                extract_selector=item.get("extract_selector") or "",
            )
        )
    if not targets:
        raise ValueError(f"no targets in {path}")
    return Settings(
        targets=tuple(targets),
        compare_width=int(data.get("compare_width", 1280)),
        ssim_delta=float(data.get("ssim_delta", 0.04)),
        area_delta=float(data.get("area_delta", 0.03)),
        pixel_threshold=int(data.get("pixel_threshold", 24)),
        rotate_after_n=int(data.get("rotate_after_n", 5)),
        page_wait=int(data.get("page_wait", 2000)),
        scroll_interval=int(data.get("scroll_interval", 10)),
        timeout=int(data.get("timeout", 120)),
        shots_dir=_under(base, str(data.get("shots_dir", "shots"))),
        heatmaps_dir=_under(base, str(data.get("heatmaps_dir", "heatmaps"))),
        alerts_dir=_under(base, str(data.get("alerts_dir", "alerts"))),
        db_path=_under(base, str(data.get("db_path", "monitor.sqlite"))),
        openai_model=str(data.get("openai_model", "gpt-4o")),
    )
