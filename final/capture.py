"""Crawling API screenshots. The JPEG is saved before the one-hour URL expires."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_TOKEN = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)
_OPENAI = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_SLACK = re.compile(r"https://hooks\.slack\.com/\S+")


class CaptureError(RuntimeError):
    pass


def redact_secrets(text: str) -> str:
    cleaned = _TOKEN.sub(r"\1REDACTED", text)
    cleaned = _OPENAI.sub("sk-REDACTED", cleaned)
    cleaned = _SLACK.sub("https://hooks.slack.com/REDACTED", cleaned)
    return cleaned


def with_body_fallback(selector: str) -> str:
    """Append ,body to each click in the chain so a missing banner is cb_status 200."""
    selector = (selector or "").strip()
    if not selector:
        return ""
    fixed = []
    for part in selector.split("|"):
        part = part.strip()
        if not part:
            continue
        if part.endswith(",body") or part.endswith(", body"):
            fixed.append(part.replace(", body", ",body"))
        else:
            fixed.append(f"{part},body")
    return "|".join(fixed)


def build_options(
    device: str,
    page_wait: int,
    scroll_interval: int,
    css_click_selector: str = "",
) -> dict[str, str]:
    """Query params for one rendered JPEG. Strings, so urlencode sends true rather than True."""
    options = {
        "screenshot": "true",
        "store": "true",
        "format": "json",
        "device": device,
        "scroll": "true",
        "scroll_interval": str(scroll_interval),
        "page_wait": str(page_wait),
    }
    selector = with_body_fallback(css_click_selector)
    if selector:
        options["css_click_selector"] = selector
    return options


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def shot_path(shots_dir: Path, url: str, device: str, when: datetime | None = None) -> Path:
    moment = when or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return Path(shots_dir) / url_hash(url) / device / f"{stamp}.jpg"


@dataclass
class CaptureFields:
    screenshot_url: str
    rid: str
    storage_url_present: bool
    cb_status: str
    page_url: str
    page_html: str
    envelope_keys: tuple[str, ...]


def rid_from_screenshot_url(url: str) -> str:
    if not url:
        return ""
    values = parse_qs(urlparse(url).query).get("rid") or []
    return values[0] if values else ""


@dataclass
class CaptureResult:
    url: str
    device: str
    path: Path
    rid: str
    cb_status: str
    envelope_keys: tuple[str, ...]
    storage_url_present: bool
    html_path: Path | None = None
    recovered_with_requests: bool = False


def _text(body) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


def _payload_from_body(body) -> dict:
    text = _text(body).strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pick(payload: dict, headers: dict, *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if value:
            return str(value)
        header = headers.get(name.lower())
        if header:
            return str(header)
    return ""


def extract_fields(response: dict) -> CaptureFields:
    """Read screenshot_url and rid from the JSON body.

    crawlbase 1.1.0 copies only original_status, cb_status, and url into
    response["headers"]. The screenshot URL and the storage rid stay on the
    raw body when format=json.
    """
    headers = {str(key).lower(): value for key, value in (response.get("headers") or {}).items()}
    payload = _payload_from_body(response.get("body"))
    blob = response.get("json")
    if isinstance(blob, dict):
        payload = {**blob, **payload}
    screenshot_url = _pick(payload, headers, "screenshot_url", "screenshotUrl")
    rid = _pick(payload, headers, "rid") or rid_from_screenshot_url(screenshot_url)
    storage_url = _pick(payload, headers, "storage_url", "storageUrl")
    shot = urlparse(screenshot_url) if screenshot_url else None
    cb_status = _pick(payload, headers, "cb_status", "pc_status") or str(response.get("status_code") or "")
    page_url = _pick(payload, headers, "url")
    page_html = payload.get("body") if isinstance(payload.get("body"), str) else ""
    return CaptureFields(
        screenshot_url=screenshot_url,
        rid=rid,
        storage_url_present=bool(storage_url) or bool(shot and shot.path == "/storage"),
        cb_status=cb_status,
        page_url=page_url,
        page_html=page_html,
        envelope_keys=tuple(sorted(str(key) for key in payload.keys())),
    )


def _status_error(fields: CaptureFields, http_status: int) -> str | None:
    if http_status >= 400:
        return f"crawl HTTP {http_status}, cb_status {fields.cb_status or 'missing'}"
    if fields.cb_status and fields.cb_status not in {"200", "None"}:
        if fields.cb_status == "595":
            return "cb_status 595: css_click_selector matched nothing"
        return f"cb_status {fields.cb_status}"
    return None


def _download(url: str, dest: Path, timeout: int) -> None:
    import requests

    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".part")
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.content
        if not data.startswith(b"\xff\xd8"):
            raise CaptureError("screenshot download was not a JPEG")
        temporary.write_bytes(data)
        temporary.replace(dest)
    except CaptureError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise CaptureError(redact_secrets(str(exc))) from exc


def _sdk_get(url: str, token: str, options: dict[str, str], timeout: int, client):
    if client is not None:
        return client.get(url, options)
    from crawlbase import CrawlingAPI

    api = CrawlingAPI({"token": token, "timeout": timeout})
    return api.get(url, options)


def _requests_get(url: str, token: str, options: dict[str, str], timeout: int) -> dict:
    import requests

    response = requests.get(
        "https://api.crawlbase.com/",
        params={"token": token, "url": url, **options},
        timeout=timeout,
        headers={"Accept-Encoding": "gzip"},
    )
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": response.content,
    }


def capture(
    url: str,
    device: str,
    *,
    token: str,
    shots_dir: Path,
    page_wait: int = 2000,
    scroll_interval: int = 10,
    css_click_selector: str = "",
    timeout: int = 120,
    client=None,
    downloader=None,
    transport: str = "sdk",
    allow_http_fallback: bool = True,
    when: datetime | None = None,
) -> CaptureResult:
    options = build_options(device, page_wait, scroll_interval, css_click_selector)
    try:
        if transport == "requests":
            response = _requests_get(url, token, options, timeout)
        else:
            response = _sdk_get(url, token, options, timeout, client)
    except CaptureError:
        raise
    except Exception as exc:
        raise CaptureError(redact_secrets(str(exc))) from exc

    fields = extract_fields(response if isinstance(response, dict) else {})
    http_status = int((response or {}).get("status_code") or 0)
    problem = _status_error(fields, http_status)
    if problem:
        raise CaptureError(problem)
    recovered = False
    if not fields.screenshot_url and transport != "requests" and allow_http_fallback and client is None:
        # The SDK call already ran. A second crawl is a second bill, so this
        # path is only the recovery when the JSON body omitted screenshot_url.
        response = _requests_get(url, token, options, timeout)
        recovered = True
        fields = extract_fields(response)
        http_status = int(response.get("status_code") or 0)
        problem = _status_error(fields, http_status)
        if problem:
            raise CaptureError(problem)
    if not fields.screenshot_url:
        raise CaptureError("screenshot_url missing from the crawl response")
    if not fields.rid:
        raise CaptureError("rid missing from the crawl response")

    dest = shot_path(shots_dir, url, device, when)
    saver = downloader or _download
    saver(fields.screenshot_url, dest, timeout)
    if not dest.is_file():
        raise CaptureError(f"JPEG was not written to {dest}")
    html_path = None
    if fields.page_html:
        html_path = dest.with_suffix(".html")
        html_path.write_text(fields.page_html, encoding="utf-8")
    return CaptureResult(
        url=url,
        device=device,
        path=dest,
        rid=fields.rid,
        cb_status=fields.cb_status or "200",
        envelope_keys=fields.envelope_keys,
        storage_url_present=fields.storage_url_present,
        html_path=html_path,
        recovered_with_requests=recovered,
    )
