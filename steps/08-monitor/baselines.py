"""SQLite baselines. Rotate after an acknowledgement or after N compared runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS baselines (
    url TEXT NOT NULL,
    device TEXT NOT NULL,
    image_path TEXT NOT NULL,
    rid TEXT,
    runs_since_rotate INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (url, device)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    device TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    image_path TEXT NOT NULL,
    ssim REAL,
    changed_area REAL,
    height_delta INTEGER,
    rid TEXT,
    heatmap_path TEXT,
    narration TEXT,
    label TEXT,
    extracted TEXT,
    alerted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    url TEXT NOT NULL,
    device TEXT NOT NULL,
    payload_path TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_baseline(conn: sqlite3.Connection, url: str, device: str):
    return conn.execute(
        "SELECT url, device, image_path, rid, runs_since_rotate, updated_at "
        "FROM baselines WHERE url = ? AND device = ?",
        (url, device),
    ).fetchone()


def set_baseline(conn: sqlite3.Connection, url: str, device: str, image_path: str, rid: str) -> None:
    conn.execute(
        """
        INSERT INTO baselines (url, device, image_path, rid, runs_since_rotate, updated_at)
        VALUES (?, ?, ?, ?, 0, ?)
        ON CONFLICT(url, device) DO UPDATE SET
            image_path = excluded.image_path,
            rid = excluded.rid,
            runs_since_rotate = 0,
            updated_at = excluded.updated_at
        """,
        (url, device, image_path, rid, _now()),
    )
    conn.commit()


def record_run(
    conn: sqlite3.Connection,
    *,
    url: str,
    device: str,
    image_path: str,
    rid: str,
    ssim: float | None = None,
    changed_area: float | None = None,
    height_delta: int | None = None,
    heatmap_path: str | None = None,
    narration: str | None = None,
    label: str | None = None,
    extracted: str | None = None,
    alerted: bool = False,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO runs (
            url, device, captured_at, image_path, ssim, changed_area, height_delta,
            rid, heatmap_path, narration, label, extracted, alerted
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            url,
            device,
            _now(),
            image_path,
            ssim,
            changed_area,
            height_delta,
            rid,
            heatmap_path,
            narration,
            label,
            extracted,
            1 if alerted else 0,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def record_alert(conn: sqlite3.Connection, run_id: int, url: str, device: str, payload_path: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO alerts (run_id, url, device, payload_path, acknowledged, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (run_id, url, device, payload_path, _now()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def note_compared_run(
    conn: sqlite3.Connection,
    url: str,
    device: str,
    image_path: str,
    rid: str,
    rotate_after_n: int,
) -> bool:
    """Increment the compare counter. Rotate onto this shot when it reaches N."""
    row = get_baseline(conn, url, device)
    if row is None:
        set_baseline(conn, url, device, image_path, rid)
        return True
    count = int(row["runs_since_rotate"]) + 1
    if count >= rotate_after_n:
        set_baseline(conn, url, device, image_path, rid)
        return True
    conn.execute(
        "UPDATE baselines SET runs_since_rotate = ? WHERE url = ? AND device = ?",
        (count, url, device),
    )
    conn.commit()
    return False


def acknowledge(conn: sqlite3.Connection, alert_id: int) -> None:
    alert = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if alert is None:
        raise LookupError(f"alert {alert_id} not found")
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (alert["run_id"],)).fetchone()
    if run is None:
        raise LookupError(f"run {alert['run_id']} not found")
    conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
    conn.commit()
    set_baseline(conn, run["url"], run["device"], run["image_path"], run["rid"] or "")
