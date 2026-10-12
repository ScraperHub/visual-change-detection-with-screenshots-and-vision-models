"""Write the alert next to the heatmap. Slack runs only when a webhook is set."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from capture import redact_secrets, url_hash


def alert_payload(
    *,
    url: str,
    device: str,
    ssim: float,
    changed_area: float,
    description: str,
    label: str,
    rid: str,
    heatmap: Path,
    extracted: str = "",
) -> dict:
    return {
        "url": url,
        "device": device,
        "ssim": round(float(ssim), 4),
        "changed_area": round(float(changed_area), 4),
        "description": description,
        "label": label,
        "rid": rid,
        "heatmap": str(heatmap),
        "extracted": extracted or "",
    }


def write_alert(alerts_dir: Path, payload: dict, when: datetime | None = None) -> Path:
    moment = when or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    dest = Path(alerts_dir) / url_hash(payload["url"]) / payload["device"] / f"{stamp}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def post_slack(webhook_url: str, payload: dict, poster=None) -> int:
    if not webhook_url:
        return 0
    body = {
        "text": (
            f"{payload['url']} ({payload['device']})\n"
            f"SSIM {payload['ssim']:.4f}  changed {payload['changed_area']:.4f}  rid {payload['rid']}\n"
            f"{payload.get('description') or ''}"
        )
    }
    send = poster
    if send is None:
        import requests

        send = requests.post
    try:
        response = send(webhook_url, json=body, timeout=30)
    except Exception as exc:
        raise RuntimeError(redact_secrets(str(exc))) from exc
    return int(getattr(response, "status_code", 0) or 0)
