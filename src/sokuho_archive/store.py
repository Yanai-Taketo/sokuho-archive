"""The archive on disk: raw snapshots, an append-only event log, and state.

Layout (all under ``data/`` by default)::

    data/
      raw/2026/09/09/sokuho-20260909T004023Z-e5f270cc56b8.xml
      events/events-2026-09.jsonl
      latest.xml
      state.json
      runtime.json

**Raw snapshots are the source of truth.**  Everything else can be rebuilt from
them with ``reindex``.  A snapshot is written only when the bytes differ from
the previous one, because NHK rewrites the document only when something
actually changes -- polling every five minutes for a year yields a few thousand
files, not a hundred thousand.

**Liveness is kept apart from archive state.**  ``state.json`` describes the
archive and is committed; it changes only when the feed changes.  ``runtime.json``
records that the archiver is alive -- every poll, including the 304s that make up
almost all of them -- and is *not* committed, because a file that changed every
five minutes would bury the real history under liveness commits.  Keeping the two
apart is what lets ``status`` answer "is it running?" without either lying during
a quiet spell or producing a commit per poll.

**The event log records transitions, not observations.**  Appending a row per
poll would be enormous and mostly redundant; instead each flash produces at most
three kinds of row over its life:

``appeared``  the first snapshot containing it
``revised``   NHK edited the text or link of a still-live flash
``cleared``   the first snapshot no longer containing it

From those three, a flash's full lifetime -- when it went up, how it changed,
when it came down -- is exactly recoverable, and the log stays append-only, so
it never rewrites history and always produces clean, reviewable git diffs.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import pathlib
import tempfile
from typing import Any, Iterator

from .models import SCHEMA_VERSION, FlashNews, Report
from .timeutil import compact_stamp, utc_now, utc_now_iso

__all__ = ["Store", "RecordOutcome", "StoreLocked"]

_STATE_VERSION = 1
# How many recent revision ids to remember, so a flash that survives several
# snapshots is not logged again each time.  Flashes are few and short-lived;
# a few hundred covers far more history than is ever needed.
_RECENT_REVISIONS = 500


class StoreLocked(RuntimeError):
    """Another archiver process holds the store lock."""


@dataclasses.dataclass
class RecordOutcome:
    """What one poll actually changed on disk."""

    changed: bool
    snapshot_path: pathlib.Path | None = None
    appeared: list[str] = dataclasses.field(default_factory=list)
    revised: list[str] = dataclasses.field(default_factory=list)
    cleared: list[str] = dataclasses.field(default_factory=list)
    heartbeat: bool = False

    @property
    def wrote_anything(self) -> bool:
        return self.changed or self.heartbeat

    def summary(self) -> str:
        if not self.wrote_anything:
            return "no change"
        bits = []
        if self.snapshot_path:
            bits.append(f"snapshot {self.snapshot_path.name}")
        for name, ids in (("appeared", self.appeared), ("revised", self.revised), ("cleared", self.cleared)):
            if ids:
                bits.append(f"{name}={len(ids)}")
        if self.heartbeat:
            bits.append("heartbeat")
        return ", ".join(bits) or "state updated"


def _atomic_write(path: pathlib.Path, payload: bytes) -> None:
    """Write bytes so that readers never observe a partial file.

    A crash between ``write`` and ``replace`` leaves the old file intact and a
    stray temp file behind, which is recoverable; a plain overwrite could leave
    a truncated snapshot that looks like a genuine short response.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(handle.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
        raise


def _append_jsonl(path: pathlib.Path, records: list[dict[str, Any]]) -> None:
    """Append records as one write, flushed to disk before returning."""
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = "".join(
        json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for r in records
    )
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())


class Store:
    """Filesystem archive rooted at ``root`` (normally ``<repo>/data``)."""

    def __init__(self, root: str | os.PathLike[str] = "data") -> None:
        self.root = pathlib.Path(root)
        self.raw_dir = self.root / "raw"
        self.events_dir = self.root / "events"
        self.state_path = self.root / "state.json"
        self.runtime_path = self.root / "runtime.json"
        self.latest_path = self.root / "latest.xml"
        self.lock_path = self.root / ".lock"

    # -- locking -----------------------------------------------------------

    @contextlib.contextmanager
    def lock(self, *, blocking: bool = False) -> Iterator[None]:
        """Exclusive advisory lock, so a cron run and a daemon cannot interleave.

        Falls back to a no-op where ``fcntl`` is unavailable (Windows); the
        archiver is still safe there for the single-process case, which is the
        only one that platform is expected to run.
        """
        try:
            import fcntl
        except ImportError:  # pragma: no cover - platform dependent
            yield
            return

        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "w", encoding="utf-8") as handle:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise StoreLocked("another archiver process holds the lock") from exc
                raise
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- state -------------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        """Read ``state.json``, tolerating absence or corruption.

        A corrupt state file must never stop the archiver: state is a cache of
        things rediscoverable from the archive, so starting from empty costs one
        redundant snapshot, whereas crashing costs every flash until someone
        notices.
        """
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("state_version", _STATE_VERSION)
        data.setdefault("active_events", {})
        data.setdefault("recent_revisions", [])
        data.setdefault("change_count", 0)
        return data

    def save_state(self, state: dict[str, Any]) -> None:
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.state_path, payload.encode("utf-8"))

    # -- runtime liveness --------------------------------------------------

    def load_runtime(self) -> dict[str, Any]:
        """Read ``runtime.json``, tolerating absence or corruption."""
        try:
            data = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("poll_count", 0)
        data.setdefault("success_count", 0)
        data.setdefault("consecutive_failures", 0)
        return data

    def note_poll(self, *, success: bool, detail: str = "") -> dict[str, Any]:
        """Record that a poll happened, whatever its outcome.

        This must be called for *every* poll -- above all for the 304s that are
        the overwhelmingly common case.  Recording liveness only when the feed
        changes would make a healthy archiver indistinguishable from a dead one
        during any quiet stretch, which is exactly the failure ``status`` exists
        to catch.
        """
        now_iso = utc_now_iso()
        runtime = self.load_runtime()
        runtime["poll_count"] = int(runtime.get("poll_count", 0)) + 1
        runtime["last_poll_utc"] = now_iso
        if success:
            runtime["success_count"] = int(runtime.get("success_count", 0)) + 1
            runtime["last_success_utc"] = now_iso
            runtime["consecutive_failures"] = 0
            runtime.pop("last_failure_detail", None)
        else:
            runtime["consecutive_failures"] = int(runtime.get("consecutive_failures", 0)) + 1
            runtime["last_failure_utc"] = now_iso
            if detail:
                runtime["last_failure_detail"] = detail
        payload = json.dumps(runtime, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(self.runtime_path, payload.encode("utf-8"))
        return runtime

    # -- paths -------------------------------------------------------------

    def snapshot_path(self, moment, digest: str) -> pathlib.Path:
        stamp = compact_stamp(moment)
        return (
            self.raw_dir
            / f"{moment.year:04d}"
            / f"{moment.month:02d}"
            / f"{moment.day:02d}"
            / f"sokuho-{stamp}-{digest[:12]}.xml"
        )

    def events_path(self, moment) -> pathlib.Path:
        return self.events_dir / f"events-{moment.year:04d}-{moment.month:02d}.jsonl"

    # -- recording ---------------------------------------------------------

    def record(
        self,
        raw: bytes,
        flash: FlashNews,
        *,
        fetch_meta: dict[str, Any] | None = None,
        heartbeat_hours: float = 6.0,
    ) -> RecordOutcome:
        """Persist one validated observation and return what changed.

        ``raw`` must be the exact bytes received and ``flash`` their parse.
        Callers pass only observations that passed validation -- an error page
        must never reach here, or it would be recorded as NHK saying nothing.
        """
        now = utc_now()
        now_iso = utc_now_iso()
        digest = hashlib.sha256(raw).hexdigest()

        state = self.load_state()
        if fetch_meta:
            state["etag"] = fetch_meta.get("etag")
            state["last_modified"] = fetch_meta.get("last_modified")
            state["source_url"] = fetch_meta.get("url")

        outcome = RecordOutcome(changed=digest != state.get("last_sha256"))

        if outcome.changed:
            outcome.snapshot_path = self._write_snapshot(raw, now, digest)
            self._log_transitions(flash, state, now_iso, outcome, digest)
            state["last_sha256"] = digest
            state["last_change_utc"] = now_iso
            state["change_count"] = int(state.get("change_count", 0)) + 1
            state["flag"] = flash.flag
            state["active"] = flash.active
            state["pub_date_raw"] = flash.pub_date_raw
            state["pub_date_utc"] = flash.pub_date_utc
            state["report_count"] = len(flash.reports)
        else:
            outcome.heartbeat = self._heartbeat_due(state, now, heartbeat_hours)
            if outcome.heartbeat:
                state["heartbeat_utc"] = now_iso

        if outcome.wrote_anything:
            self.save_state(state)
        return outcome

    def _write_snapshot(self, raw: bytes, moment, digest: str) -> pathlib.Path:
        path = self.snapshot_path(moment, digest)
        if not path.exists():
            _atomic_write(path, raw)
        # latest.xml mirrors the newest distinct snapshot so that a reader -- or
        # a git diff -- can see the current state of the feed at a glance.
        _atomic_write(self.latest_path, raw)
        return path

    def _heartbeat_due(self, state: dict[str, Any], now, hours: float) -> bool:
        """True when the archive should record that it is alive but idle.

        Without this, a long quiet stretch and a dead archiver look identical in
        the committed history.
        """
        if hours <= 0:
            return False
        marker = state.get("heartbeat_utc") or state.get("last_change_utc")
        if not marker:
            return True
        try:
            import datetime as dt

            previous = dt.datetime.strptime(marker, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc
            )
        except (TypeError, ValueError):
            return True
        return (now - previous).total_seconds() >= hours * 3600.0

    def _log_transitions(
        self,
        flash: FlashNews,
        state: dict[str, Any],
        now_iso: str,
        outcome: RecordOutcome,
        snapshot_digest: str,
    ) -> None:
        """Diff this snapshot against the previous one and log what changed."""
        previous: dict[str, str] = dict(state.get("active_events") or {})
        recent: list[str] = list(state.get("recent_revisions") or [])
        recent_set = set(recent)

        current = {r.event_id: r for r in flash.reports}
        rows: list[dict[str, Any]] = []
        snapshot_ref = f"{snapshot_digest[:12]}"

        for event_id, report in current.items():
            known_revision = previous.get(event_id)
            if known_revision == report.revision_id:
                continue  # unchanged and still live: nothing to log
            if event_id in previous or report.revision_id in recent_set:
                kind = "revised"
                outcome.revised.append(event_id)
            else:
                kind = "appeared"
                outcome.appeared.append(event_id)
            rows.append(
                self._row(kind, now_iso, snapshot_ref, flash, report, previous_revision=known_revision)
            )
            recent.append(report.revision_id)

        last_known: dict[str, dict[str, Any]] = dict(state.get("active_details") or {})
        for event_id, revision_id in previous.items():
            if event_id in current:
                continue
            outcome.cleared.append(event_id)
            row = {
                "schema_version": SCHEMA_VERSION,
                "type": "cleared",
                "recorded_utc": now_iso,
                "snapshot": snapshot_ref,
                "event_id": event_id,
                "revision_id": revision_id,
                "flag": flash.flag,
                "pub_date_raw": flash.pub_date_raw,
                "pub_date_utc": flash.pub_date_utc,
            }
            # Carry the headline into the cleared row so the log reads on its own
            # without joining back to the matching "appeared" row.
            row.update(last_known.get(event_id) or {})
            rows.append(row)

        _append_jsonl(self.events_path(utc_now()), rows)

        state["active_events"] = {eid: r.revision_id for eid, r in current.items()}
        state["active_details"] = {
            eid: {"category": r.category, "date_raw": r.date_raw, "text": r.text}
            for eid, r in current.items()
        }
        state["recent_revisions"] = recent[-_RECENT_REVISIONS:]

    @staticmethod
    def _row(
        kind: str,
        now_iso: str,
        snapshot_ref: str,
        flash: FlashNews,
        report: Report,
        *,
        previous_revision: str | None,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "type": kind,
            "recorded_utc": now_iso,
            "snapshot": snapshot_ref,
            "flag": flash.flag,
            "pub_date_raw": flash.pub_date_raw,
            "pub_date_utc": flash.pub_date_utc,
        }
        row.update(report.to_dict())
        if previous_revision:
            row["previous_revision_id"] = previous_revision
        return row

    # -- reading back ------------------------------------------------------

    def iter_event_rows(self) -> Iterator[dict[str, Any]]:
        """Yield every event row, oldest month first, skipping corrupt lines."""
        for path in sorted(self.events_dir.glob("events-*.jsonl")):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue  # a torn final line from a crash; reindex repairs it

    def iter_snapshots(self) -> Iterator[pathlib.Path]:
        """Yield raw snapshot paths in chronological (filename) order."""
        yield from sorted(self.raw_dir.rglob("sokuho-*.xml"))
