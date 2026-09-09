"""Command line interface.

    sokuho-archive once            # poll once and record anything new
    sokuho-archive watch           # poll on an interval until interrupted
    sokuho-archive status          # is the archiver healthy? (exit 1 if stale)
    sokuho-archive verify          # check the archive's integrity
    sokuho-archive reindex         # rebuild the SQLite index from the event log
    sokuho-archive list/search     # read the archive back
    sokuho-archive stats/export    # summarise or extract

Exit codes are meaningful so the commands compose with cron, CI and monitoring:
0 success, 1 a real failure, 2 bad usage, 3 stale/unhealthy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import pathlib
import signal
import sys
import time
from typing import Any

from . import __version__, index as index_mod
from .fetcher import DEFAULT_USER_AGENT, PRIMARY_URL, FetchError, Fetcher
from .parser import ParseError, parse
from .store import Store, StoreLocked
from .timeutil import utc_now

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_STALE = 0, 1, 2, 3

DEFAULT_DATA_DIR = os.environ.get("SOKUHO_DATA_DIR", "data")
DEFAULT_INTERVAL = float(os.environ.get("SOKUHO_INTERVAL", "60"))
# The feed carries Cache-Control: max-age=60, so polling faster gains nothing
# and only adds load; refuse to be impolite even if asked.
MIN_INTERVAL = 30.0


def _log(message: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(f"[{utc_now().strftime('%Y-%m-%dT%H:%M:%SZ')}] {message}", flush=True)


def _store(args) -> Store:
    return Store(args.data_dir)


def _index_path(args) -> pathlib.Path:
    return pathlib.Path(args.data_dir) / "index.sqlite3"


# --------------------------------------------------------------------------
# poll
# --------------------------------------------------------------------------

def poll_once(store: Store, fetcher: Fetcher, *, quiet: bool = False,
              heartbeat_hours: float = 6.0, use_conditional: bool = True) -> int:
    """One fetch-validate-record cycle. Returns an exit code."""
    state = store.load_state()
    etag = state.get("etag") if use_conditional else None
    last_modified = state.get("last_modified") if use_conditional else None

    try:
        result = fetcher.fetch(etag=etag, last_modified=last_modified)
    except FetchError as exc:
        _log(f"FETCH FAILED ({exc.reason}): {exc.detail}", quiet=False)
        return EXIT_FAIL

    if result.not_modified:
        _log(f"304 not modified ({result.attempts} attempt(s))", quiet=quiet)
        return EXIT_OK

    try:
        flash = parse(result.body or b"")
    except ParseError as exc:
        # Deliberately *not* recorded: an error page stored as an observation
        # would be indistinguishable from NHK genuinely publishing nothing.
        _log(f"REJECTED RESPONSE ({exc.reason}): {exc.detail}", quiet=False)
        return EXIT_FAIL

    outcome = store.record(
        result.body or b"",
        flash,
        fetch_meta={"etag": result.etag, "last_modified": result.last_modified, "url": result.url},
        heartbeat_hours=heartbeat_hours,
    )
    verb = "CHANGED" if outcome.changed else "unchanged"
    _log(f"{verb}: flag={flash.flag} reports={len(flash.reports)} -- {outcome.summary()}",
         quiet=quiet and not outcome.changed)
    for report in flash.reports:
        _log(f"  [{report.category}] {report.date_raw}  {report.text}", quiet=quiet and not outcome.changed)
    return EXIT_OK


def cmd_once(args) -> int:
    store = _store(args)
    fetcher = Fetcher(args.url, user_agent=args.user_agent, timeout=args.timeout)
    try:
        with store.lock():
            return poll_once(store, fetcher, quiet=args.quiet,
                             heartbeat_hours=args.heartbeat_hours,
                             use_conditional=not args.no_conditional)
    except StoreLocked as exc:
        _log(f"skipping: {exc}")
        return EXIT_OK


def cmd_watch(args) -> int:
    interval = max(MIN_INTERVAL, args.interval)
    if interval != args.interval:
        _log(f"interval raised to {interval:g}s (the feed is cached for 60s upstream)")
    store = _store(args)
    fetcher = Fetcher(args.url, user_agent=args.user_agent, timeout=args.timeout)

    stopping = False

    def _stop(signum, _frame):
        nonlocal stopping
        stopping = True
        _log(f"received signal {signum}; finishing current cycle")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)

    max_runtime = getattr(args, "max_runtime", 0.0) or 0.0
    limit_note = f" for up to {max_runtime:g}s" if max_runtime > 0 else ""
    _log(f"watching {args.url} every {interval:g}s{limit_note} -> {args.data_dir}")
    consecutive_failures = 0
    started_at = time.monotonic()
    deadline = time.monotonic()
    try:
        with store.lock():
            while not stopping:
                try:
                    code = poll_once(store, fetcher, quiet=args.quiet,
                                     heartbeat_hours=args.heartbeat_hours,
                                     use_conditional=not args.no_conditional)
                except Exception as exc:  # never let one bad cycle kill the daemon
                    _log(f"unexpected error: {type(exc).__name__}: {exc}")
                    code = EXIT_FAIL
                consecutive_failures = consecutive_failures + 1 if code else 0
                if consecutive_failures and consecutive_failures % 10 == 0:
                    _log(f"WARNING: {consecutive_failures} consecutive failures")
                # Fixed cadence that does not drift with request latency.
                deadline += interval
                if max_runtime > 0 and deadline - started_at >= max_runtime:
                    break
                sleep_for = deadline - time.monotonic()
                if sleep_for < 0:
                    deadline = time.monotonic()
                    sleep_for = 0
                waited = 0.0
                while waited < sleep_for and not stopping:
                    time.sleep(min(1.0, sleep_for - waited))
                    waited += 1.0
    except StoreLocked as exc:
        _log(f"cannot start: {exc}")
        return EXIT_FAIL
    _log("stopped")
    return EXIT_OK


# --------------------------------------------------------------------------
# health and integrity
# --------------------------------------------------------------------------

def cmd_status(args) -> int:
    """Report health, and fail loudly when the archive has gone stale.

    Silence is the dangerous failure here: an archiver blocked by a WAF looks
    exactly like a quiet news day.  ``last_success_utc`` distinguishes them, so
    a stale value is an error, not information.
    """
    store = _store(args)
    state = store.load_state()
    if not state.get("last_success_utc"):
        print("no successful poll recorded yet")
        return EXIT_STALE

    from .timeutil import UTC
    import datetime as dt

    last = dt.datetime.strptime(state["last_success_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    age_h = (utc_now() - last).total_seconds() / 3600.0
    report = {
        "last_success_utc": state["last_success_utc"],
        "age_hours": round(age_h, 2),
        "last_change_utc": state.get("last_change_utc"),
        "flag": state.get("flag"),
        "active_events": len(state.get("active_events") or {}),
        "polls": state.get("poll_count"),
        "changes": state.get("change_count"),
        "stale_after_hours": args.stale_hours,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if age_h > args.stale_hours:
        print(f"STALE: no successful poll for {age_h:.1f}h", file=sys.stderr)
        return EXIT_STALE
    return EXIT_OK


def cmd_verify(args) -> int:
    """Check that the archive says what it claims: hashes, parses, log integrity."""
    store = _store(args)
    problems: list[str] = []
    snapshots = 0
    for path in store.iter_snapshots():
        snapshots += 1
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        stem_digest = path.stem.rsplit("-", 1)[-1]
        if not digest.startswith(stem_digest):
            problems.append(f"{path}: content hash does not match filename ({stem_digest})")
        try:
            parse(raw)
        except ParseError as exc:
            problems.append(f"{path}: does not parse ({exc.reason})")

    rows = 0
    bad_lines = 0
    seen_events: set[str] = set()
    for jsonl in sorted(store.events_dir.glob("events-*.jsonl")):
        for number, line in enumerate(jsonl.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            rows += 1
            try:
                row = json.loads(line)
            except ValueError:
                bad_lines += 1
                problems.append(f"{jsonl}:{number}: not valid JSON")
                continue
            if row.get("type") in ("appeared",):
                seen_events.add(row.get("event_id", ""))
            elif row.get("type") in ("revised", "cleared"):
                if row.get("event_id") not in seen_events:
                    problems.append(
                        f"{jsonl}:{number}: {row.get('type')} for unseen event {row.get('event_id')}"
                    )

    print(f"snapshots: {snapshots}   event rows: {rows}   distinct events: {len(seen_events)}")
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems[:50]:
            print(f"  - {problem}", file=sys.stderr)
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more", file=sys.stderr)
        return EXIT_FAIL
    print("OK: archive is internally consistent")
    return EXIT_OK


# --------------------------------------------------------------------------
# reading back
# --------------------------------------------------------------------------

def cmd_reindex(args) -> int:
    store = _store(args)
    counts = index_mod.build(store.iter_event_rows(), _index_path(args))
    print(json.dumps(counts, indent=2))
    return EXIT_OK


def _ensure_index(args) -> pathlib.Path:
    path = _index_path(args)
    if not path.exists():
        index_mod.build(_store(args).iter_event_rows(), path)
    return path


def _print_events(rows) -> None:
    if not rows:
        print("(no matching 速報)")
        return
    for row in rows:
        when = row["date_raw"] or row["first_seen_utc"] or "?"
        state = "LIVE " if row["cleared_utc"] is None else "     "
        revised = f"  (revised x{row['revision_count'] - 1})" if row["revision_count"] > 1 else ""
        print(f"{state}{when}  [cat {row['category'] or '?'}]  {row['text']}{revised}")
        if row["link"]:
            print(f"        {row['link']}")


def cmd_list(args) -> int:
    conn = index_mod.connect(_ensure_index(args))
    try:
        _print_events(index_mod.recent(conn, args.limit, active_only=args.active))
    finally:
        conn.close()
    return EXIT_OK


def cmd_search(args) -> int:
    conn = index_mod.connect(_ensure_index(args))
    try:
        _print_events(index_mod.search(conn, args.query, args.limit))
    finally:
        conn.close()
    return EXIT_OK


def cmd_stats(args) -> int:
    conn = index_mod.connect(_ensure_index(args))
    try:
        print(json.dumps(index_mod.summary(conn), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return EXIT_OK


def cmd_export(args) -> int:
    conn = index_mod.connect(_ensure_index(args))
    try:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY COALESCE(date_utc, first_seen_utc)"
        ).fetchall()
    finally:
        conn.close()
    if args.format == "json":
        payload = [dict(r) for r in rows]
        for item in payload:
            item["lines"] = json.loads(item.pop("lines_json") or "[]")
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        fields = ["event_id", "category", "date_raw", "date_utc", "text", "link",
                  "first_seen_utc", "last_seen_utc", "cleared_utc", "revision_count"]
        writer = csv.DictWriter(sys.stdout, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in fields})
    return EXIT_OK


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sokuho-archive",
        description="NHKニュース速報 (NHK breaking news) feed archiver.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="archive root (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_fetch_options(p):
        p.add_argument("--url", default=os.environ.get("SOKUHO_URL", PRIMARY_URL))
        p.add_argument("--user-agent", default=os.environ.get("SOKUHO_USER_AGENT", DEFAULT_USER_AGENT))
        p.add_argument("--timeout", type=float, default=20.0)
        p.add_argument("--heartbeat-hours", type=float, default=6.0,
                       help="record liveness this often during quiet periods (0 disables)")
        p.add_argument("--no-conditional", action="store_true",
                       help="always send a full GET instead of If-None-Match")
        p.add_argument("-q", "--quiet", action="store_true")

    p_once = sub.add_parser("once", help="poll the feed a single time")
    add_fetch_options(p_once)
    p_once.set_defaults(func=cmd_once)

    p_watch = sub.add_parser("watch", help="poll continuously until interrupted")
    add_fetch_options(p_watch)
    p_watch.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                         help=f"seconds between polls (minimum {MIN_INTERVAL:g})")
    p_watch.add_argument("--max-runtime", type=float,
                         default=float(os.environ.get("SOKUHO_MAX_RUNTIME", "0")),
                         help="stop after roughly this many seconds (0 = run forever). "
                              "Lets one scheduled CI job poll several times.")
    p_watch.set_defaults(func=cmd_watch)

    p_status = sub.add_parser("status", help="report health; exit 3 when stale")
    p_status.add_argument("--stale-hours", type=float, default=3.0)
    p_status.set_defaults(func=cmd_status)

    sub.add_parser("verify", help="check archive integrity").set_defaults(func=cmd_verify)
    sub.add_parser("reindex", help="rebuild the SQLite index").set_defaults(func=cmd_reindex)

    p_list = sub.add_parser("list", help="show recent 速報")
    p_list.add_argument("-n", "--limit", type=int, default=20)
    p_list.add_argument("--active", action="store_true", help="only flashes still live")
    p_list.set_defaults(func=cmd_list)

    p_search = sub.add_parser("search", help="search 速報 text")
    p_search.add_argument("query")
    p_search.add_argument("-n", "--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    sub.add_parser("stats", help="summarise the archive").set_defaults(func=cmd_stats)

    p_export = sub.add_parser("export", help="export all events")
    p_export.add_argument("--format", choices=("csv", "json"), default="csv")
    p_export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return EXIT_OK
    except BrokenPipeError:  # `| head` and friends
        try:
            sys.stdout.close()
        finally:
            return EXIT_OK
