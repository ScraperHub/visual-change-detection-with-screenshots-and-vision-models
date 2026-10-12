"""One pass over the target list. Schedule it with cron or GitHub Actions.

Hourly cron, from the directory that holds this file:

    0 * * * * cd /opt/visual-monitor && .venv/bin/python monitor.py --once

Windows Task Scheduler, from code/:

    python final\\monitor.py --once
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from baselines import acknowledge, connect, get_baseline, note_compared_run, record_alert, record_run, set_baseline
from capture import CaptureError, capture, redact_secrets
from compare import compare_images
from config import Settings, load_settings
from explain import explain_change
from gate import crosses
from normalize import normalize_files
from notify import alert_payload, post_slack, write_alert
from storage_lookup import extract_value, needs_html

HERE = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class PassResult:
    url: str
    device: str
    phase: str
    rid: str = ""
    image_path: str = ""
    cb_status: str = ""
    envelope_keys: tuple[str, ...] = ()
    storage_url_present: bool = False
    recovered_with_requests: bool = False
    ssim: float | None = None
    changed_area: float | None = None
    height_delta: int | None = None
    vision: bool = False
    label: str = ""
    narration: str = ""
    storage: bool = False
    extracted: str = ""
    alert_path: str = ""
    rotated: bool = False
    slack_status: int = 0
    error: str = ""


def _public(result: PassResult) -> dict:
    data = asdict(result)
    data["envelope_keys"] = list(result.envelope_keys)
    return data


def run_once(
    settings: Settings,
    *,
    token: str,
    openai_api_key: str | None = None,
    openai_model: str | None = None,
    slack_webhook: str | None = None,
    max_vision_calls: int | None = None,
    only: list[str] | None = None,
    conn=None,
    capture_fn=None,
    explain_fn=None,
    storage_fn=None,
    slack_fn=None,
) -> list[PassResult]:
    own_conn = conn is None
    conn = conn or connect(settings.db_path)
    capture_fn = capture_fn or capture
    explain_fn = explain_fn or explain_change
    storage_fn = storage_fn or _extract_saved
    slack_fn = slack_fn or post_slack
    model = openai_model or settings.openai_model
    vision_left = max_vision_calls
    results: list[PassResult] = []
    try:
        for target in settings.targets:
            if only and not any(piece in target.url for piece in only):
                continue
            for device in target.devices:
                results.append(
                    _one(
                        settings,
                        target,
                        device,
                        token=token,
                        openai_api_key=openai_api_key,
                        openai_model=model,
                        slack_webhook=slack_webhook,
                        conn=conn,
                        capture_fn=capture_fn,
                        explain_fn=explain_fn,
                        storage_fn=storage_fn,
                        slack_fn=slack_fn,
                        vision_budget=vision_left,
                    )
                )
                if vision_left is not None and results[-1].vision:
                    vision_left -= 1
    finally:
        if own_conn:
            conn.close()
    return results


def _extract_saved(token, rid, selector, label, timeout=120, html_path=None):
    del token, rid, timeout
    if not html_path or not Path(html_path).is_file():
        return ""
    return extract_value(Path(html_path).read_text(encoding="utf-8"), selector, label)


def _one(settings, target, device, **kwargs) -> PassResult:
    try:
        return _compare_or_baseline(settings, target, device, **kwargs)
    except Exception as exc:
        return PassResult(url=target.url, device=device, phase="error", error=redact_secrets(str(exc)))


def _compare_or_baseline(
    settings,
    target,
    device,
    *,
    token,
    openai_api_key,
    openai_model,
    slack_webhook,
    conn,
    capture_fn,
    explain_fn,
    storage_fn,
    slack_fn,
    vision_budget,
) -> PassResult:
    shot = capture_fn(
        target.url,
        device,
        token=token,
        shots_dir=settings.shots_dir,
        page_wait=settings.page_wait,
        scroll_interval=settings.scroll_interval,
        css_click_selector=target.css_click_selector,
        timeout=settings.timeout,
    )
    baseline = get_baseline(conn, target.url, device)
    if baseline is None or not Path(baseline["image_path"]).is_file():
        set_baseline(conn, target.url, device, str(shot.path), shot.rid)
        record_run(conn, url=target.url, device=device, image_path=str(shot.path), rid=shot.rid)
        return PassResult(
            url=target.url,
            device=device,
            phase="baseline",
            rid=shot.rid,
            image_path=str(shot.path),
            cb_status=shot.cb_status,
            envelope_keys=shot.envelope_keys,
            storage_url_present=shot.storage_url_present,
            recovered_with_requests=shot.recovered_with_requests,
        )

    pair = normalize_files(Path(baseline["image_path"]), shot.path, settings.compare_width)
    heatmap = settings.heatmaps_dir / shot.path.relative_to(settings.shots_dir).with_suffix(".png")
    compared = compare_images(pair, heatmap, settings.pixel_threshold)
    opened = crosses(compared.ssim, compared.changed_area, settings.ssim_delta, settings.area_delta)
    narration = ""
    label = ""
    vision = False
    if opened and openai_api_key and vision_budget != 0:
        explanation = explain_fn(
            Path(baseline["image_path"]),
            shot.path,
            api_key=openai_api_key,
            model=openai_model,
        )
        narration = explanation.paragraph
        label = explanation.label
        vision = True
    elif opened and not openai_api_key:
        narration = "Visual delta crossed the threshold. Narration was not run."
        label = "other"

    extracted = ""
    storage = False
    if vision and needs_html(label):
        extracted = storage_fn(
            token,
            shot.rid,
            target.extract_selector,
            label,
            timeout=settings.timeout,
            html_path=shot.html_path,
        )
        storage = True

    alert_path = ""
    slack_status = 0
    run_id = record_run(
        conn,
        url=target.url,
        device=device,
        image_path=str(shot.path),
        rid=shot.rid,
        ssim=compared.ssim,
        changed_area=compared.changed_area,
        height_delta=compared.height_delta,
        heatmap_path=str(compared.heatmap_path),
        narration=narration,
        label=label,
        extracted=extracted,
        alerted=opened,
    )
    if opened:
        payload = alert_payload(
            url=target.url,
            device=device,
            ssim=compared.ssim,
            changed_area=compared.changed_area,
            description=narration,
            label=label or "other",
            rid=shot.rid,
            heatmap=compared.heatmap_path,
            extracted=extracted,
        )
        written = write_alert(settings.alerts_dir, payload)
        alert_path = str(written)
        record_alert(conn, run_id, target.url, device, alert_path)
        if slack_webhook:
            slack_status = slack_fn(slack_webhook, payload)

    rotated = note_compared_run(
        conn,
        target.url,
        device,
        str(shot.path),
        shot.rid,
        settings.rotate_after_n,
    )
    return PassResult(
        url=target.url,
        device=device,
        phase="compared",
        rid=shot.rid,
        image_path=str(shot.path),
        cb_status=shot.cb_status,
        envelope_keys=shot.envelope_keys,
        storage_url_present=shot.storage_url_present,
        recovered_with_requests=shot.recovered_with_requests,
        ssim=compared.ssim,
        changed_area=compared.changed_area,
        height_delta=compared.height_delta,
        vision=vision,
        label=label,
        narration=narration,
        storage=storage,
        extracted=extracted,
        alert_path=alert_path,
        rotated=rotated,
        slack_status=slack_status,
    )


def main(argv: list[str] | None = None) -> int:
    load_env_file(HERE / ".env")
    parser = argparse.ArgumentParser(description="Capture, diff, and alert on public pages.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    parser.add_argument("--ack", type=int, help="Acknowledge an alert id and rotate its baseline.")
    parser.add_argument("--config", type=Path, default=HERE / "targets.example.toml")
    parser.add_argument("--only", action="append", default=None, help="Limit the pass to URLs containing this text.")
    parser.add_argument("--max-vision-calls", type=int, default=None)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    if args.ack is not None:
        conn = connect(settings.db_path)
        try:
            acknowledge(conn, args.ack)
        finally:
            conn.close()
        print(json.dumps({"acknowledged": args.ack}))
        if not args.once:
            return 0

    if not args.once:
        parser.print_help()
        return 2

    token = os.environ.get("CRAWLBASE_JS_TOKEN", "")
    if not token:
        print("CRAWLBASE_JS_TOKEN is missing", file=sys.stderr)
        return 2
    results = run_once(
        settings,
        token=token,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        openai_model=os.environ.get("OPENAI_MODEL") or settings.openai_model,
        slack_webhook=os.environ.get("SLACK_WEBHOOK_URL") or None,
        max_vision_calls=args.max_vision_calls,
        only=args.only,
    )
    print(json.dumps([_public(item) for item in results], indent=2))
    return 1 if any(item.phase == "error" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
