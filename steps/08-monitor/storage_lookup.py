"""Pull the stored HTML for this render when the narration names a concrete change."""

from __future__ import annotations

import re
from html.parser import HTMLParser

CONCRETE_LABELS = {"pricing", "hero", "banner"}
_PRICE = re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?")
_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class StorageError(RuntimeError):
    pass


def needs_html(label: str) -> bool:
    return (label or "").strip().lower() in CONCRETE_LABELS


class _Text(HTMLParser):
    def __init__(self, kind: str, value: str):
        super().__init__(convert_charrefs=True)
        self.kind = kind
        self.value = value
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        matched = (
            (self.kind == "tag" and tag.lower() == self.value)
            or (self.kind == "id" and attrs.get("id") == self.value)
            or (self.kind == "class" and self.value in classes)
        )
        if self.depth:
            self.depth += 1
        elif matched:
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and data.strip():
            self.parts.append(data.strip())


def _selector_kind(selector: str) -> tuple[str, str]:
    selector = selector.strip()
    if selector.startswith("#") and " " not in selector:
        return "id", selector[1:]
    if selector.startswith(".") and " " not in selector:
        return "class", selector[1:]
    return "tag", selector.lower()


def text_for_selector(html: str, selector: str) -> str:
    if not selector or not html:
        return ""
    kind, value = _selector_kind(selector)
    parser = _Text(kind, value)
    parser.feed(html)
    return " ".join(parser.parts).strip()


def extract_value(html: str, selector: str, label: str) -> str:
    if selector:
        found = text_for_selector(html, selector)
        if found:
            return found[:500]
    if label == "pricing":
        match = _PRICE.search(html or "")
        if match:
            return match.group(0)
    title = _TITLE.search(html or "")
    if title:
        return title.group(1).strip()[:240]
    return ""


def fetch_html(token: str, rid: str, *, timeout: int = 120, client=None) -> str:
    """Storage is partitioned per token. The rid from a JS crawl is in the JS silo."""
    if client is None:
        from crawlbase import StorageAPI

        client = StorageAPI({"token": token, "timeout": timeout})
    response = client.get(rid)
    status = int(response.get("status_code") or 0)
    if status and status >= 400:
        raise StorageError(f"storage HTTP {status} for rid {rid}")
    body = response.get("body") or b""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return str(body)


def lookup_change(token: str, rid: str, selector: str, label: str, *, timeout: int = 120, client=None) -> str:
    if not needs_html(label):
        return ""
    html = fetch_html(token, rid, timeout=timeout, client=client)
    return extract_value(html, selector, label)
