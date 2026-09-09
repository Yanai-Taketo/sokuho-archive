"""A rebuildable SQLite index over the event log.

The index is deliberately *derived*: it is never the source of truth and is not
committed to git (binary blobs make terrible diffs and merge conflicts).
``sokuho-archive reindex`` recreates it from ``data/events/*.jsonl`` at any
time, so losing it costs seconds.

Its job is to answer the questions the JSONL cannot answer cheaply -- "what was
live on this date", "which flashes mention 地震", "how long did this one stay
up" -- by folding the append-only ``appeared``/``revised``/``cleared``
transitions back into one row per flash.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from typing import Any, Iterable

__all__ = ["build", "connect", "SCHEMA_SQL", "search", "recent", "summary"]

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS events (
    event_id            TEXT PRIMARY KEY,
    category            TEXT NOT NULL DEFAULT '',
    date_raw            TEXT NOT NULL DEFAULT '',
    date_utc            TEXT,
    link                TEXT NOT NULL DEFAULT '',
    text                TEXT NOT NULL DEFAULT '',
    lines_json          TEXT NOT NULL DEFAULT '[]',
    first_seen_utc      TEXT,
    last_seen_utc       TEXT,
    cleared_utc         TEXT,
    revision_count      INTEGER NOT NULL DEFAULT 1,
    last_revision_id    TEXT
);
CREATE INDEX IF NOT EXISTS events_by_date    ON events(date_utc);
CREATE INDEX IF NOT EXISTS events_by_first   ON events(first_seen_utc);
CREATE INDEX IF NOT EXISTS events_by_cleared ON events(cleared_utc);

CREATE TABLE IF NOT EXISTS revisions (
    revision_id             TEXT PRIMARY KEY,
    event_id                TEXT NOT NULL,
    type                    TEXT NOT NULL,
    recorded_utc            TEXT,
    text                    TEXT NOT NULL DEFAULT '',
    link                    TEXT NOT NULL DEFAULT '',
    lines_json              TEXT NOT NULL DEFAULT '[]',
    previous_revision_id    TEXT,
    snapshot                TEXT
);
CREATE INDEX IF NOT EXISTS revisions_by_event ON revisions(event_id, recorded_utc);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

def connect(path: str | pathlib.Path) -> sqlite3.Connection:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def build(rows: Iterable[dict[str, Any]], path: str | pathlib.Path) -> dict[str, int]:
    """Rebuild the index from event rows. Returns counts for reporting.

    Rows are folded in the order given, which is chronological because the log
    is append-only and months are read in name order.  Replaying an already
    indexed row is harmless -- every write is an upsert -- so a partial rebuild
    can simply be run again.
    """
    path = pathlib.Path(path)
    if path.exists():
        path.unlink()
    conn = connect(path)
    counts = {"appeared": 0, "revised": 0, "cleared": 0, "skipped": 0, "events": 0}
    try:
        conn.executescript(SCHEMA_SQL)
        for row in rows:
            kind = row.get("type")
            event_id = row.get("event_id")
            if not event_id or kind not in ("appeared", "revised", "cleared"):
                counts["skipped"] += 1
                continue
            counts[kind] += 1

            if kind == "cleared":
                conn.execute(
                    "UPDATE events SET cleared_utc = COALESCE(cleared_utc, ?) WHERE event_id = ?",
                    (row.get("recorded_utc"), event_id),
                )
                continue

            lines_json = json.dumps(row.get("lines") or [], ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO events (event_id, category, date_raw, date_utc, link, text,
                                    lines_json, first_seen_utc, last_seen_utc,
                                    revision_count, last_revision_id)
                VALUES (?,?,?,?,?,?,?,?,?,1,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    link             = excluded.link,
                    text             = excluded.text,
                    lines_json       = excluded.lines_json,
                    last_seen_utc    = excluded.last_seen_utc,
                    revision_count   = events.revision_count + 1,
                    last_revision_id = excluded.last_revision_id
                """,
                (
                    event_id,
                    row.get("category", ""),
                    row.get("date_raw", ""),
                    row.get("date_utc"),
                    row.get("link", ""),
                    row.get("text", ""),
                    lines_json,
                    row.get("recorded_utc"),
                    row.get("recorded_utc"),
                    row.get("revision_id"),
                ),
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO revisions
                    (revision_id, event_id, type, recorded_utc, text, link,
                     lines_json, previous_revision_id, snapshot)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.get("revision_id"),
                    event_id,
                    kind,
                    row.get("recorded_utc"),
                    row.get("text", ""),
                    row.get("link", ""),
                    lines_json,
                    row.get("previous_revision_id"),
                    row.get("snapshot"),
                ),
            )

        counts["events"] = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', '1')"
        )
        conn.commit()
    finally:
        conn.close()
    return counts


def recent(conn: sqlite3.Connection, limit: int = 20, *, active_only: bool = False):
    where = "WHERE cleared_utc IS NULL" if active_only else ""
    return conn.execute(
        f"SELECT * FROM events {where} ORDER BY COALESCE(date_utc, first_seen_utc) DESC LIMIT ?",
        (limit,),
    ).fetchall()


def search(conn: sqlite3.Connection, query: str, limit: int = 20):
    """Substring search over the headline text.

    Deliberately ``LIKE`` rather than FTS5.  Japanese is written without spaces,
    so every stock FTS5 tokenizer indexes ``東北地方で震度６強の地震`` as a single
    token and ``MATCH '地震'`` returns nothing -- silently, which is the worst
    kind of wrong.  ``trigram`` does not rescue it either, since a two-character
    query is shorter than a trigram.  At this corpus size (a few thousand short
    headlines a year) a scan is microseconds and is always right.

    ``ESCAPE`` is set so that a query containing ``%`` or ``_`` matches those
    characters literally instead of behaving as a wildcard.
    """
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return conn.execute(
        """
        SELECT * FROM events WHERE text LIKE ? ESCAPE '\\'
        ORDER BY COALESCE(date_utc, first_seen_utc) DESC LIMIT ?
        """,
        (pattern, limit),
    ).fetchall()


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS events,
               SUM(CASE WHEN cleared_utc IS NULL THEN 1 ELSE 0 END) AS still_open,
               MIN(COALESCE(date_utc, first_seen_utc)) AS earliest,
               MAX(COALESCE(date_utc, first_seen_utc)) AS latest,
               SUM(revision_count) AS revisions
        FROM events
        """
    ).fetchone()
    by_category = conn.execute(
        "SELECT category, COUNT(*) AS n FROM events GROUP BY category ORDER BY n DESC"
    ).fetchall()
    return {
        "events": row["events"] or 0,
        "still_open": row["still_open"] or 0,
        "earliest": row["earliest"],
        "latest": row["latest"],
        "revisions": row["revisions"] or 0,
        "by_category": {r["category"] or "?": r["n"] for r in by_category},
    }
