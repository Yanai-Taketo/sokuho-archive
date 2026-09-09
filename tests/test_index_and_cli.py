import io
import json
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from sokuho_archive import index as index_mod
from sokuho_archive.cli import main
from sokuho_archive.parser import parse
from sokuho_archive.store import Store

from tests.test_store import A, A_EDITED, B, document


class ArchiveFixture(unittest.TestCase):
    """An archive holding a realistic sequence of flashes."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        store = Store(self.data)
        for raw in (document("0"), document("1", [A]), document("1", [A, B]),
                    document("1", [A_EDITED, B]), document("1", [B]), document("0")):
            store.record(raw, parse(raw), fetch_meta={}, heartbeat_hours=0)
        self.store = store
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--data-dir", str(self.data), *args])
        return code, buffer.getvalue()


class IndexBuilding(ArchiveFixture):
    def test_folds_transitions_into_one_row_per_flash(self):
        counts = index_mod.build(self.store.iter_event_rows(), self.data / "index.sqlite3")
        self.assertEqual(counts["events"], 2)
        self.assertEqual(counts["appeared"], 2)
        self.assertEqual(counts["revised"], 1)
        self.assertEqual(counts["cleared"], 2)

    def test_revision_count_reflects_edits(self):
        index_mod.build(self.store.iter_event_rows(), self.data / "index.sqlite3")
        conn = index_mod.connect(self.data / "index.sqlite3")
        try:
            rows = {r["category"]: r for r in conn.execute("SELECT * FROM events")}
            self.assertEqual(rows["1"]["revision_count"], 2)   # appeared + revised
            self.assertEqual(rows["2"]["revision_count"], 1)
            self.assertEqual(rows["1"]["text"], "政府　対策を正式発表")
            self.assertIsNotNone(rows["1"]["cleared_utc"])
        finally:
            conn.close()

    def test_rebuild_is_idempotent(self):
        first = index_mod.build(self.store.iter_event_rows(), self.data / "index.sqlite3")
        second = index_mod.build(self.store.iter_event_rows(), self.data / "index.sqlite3")
        self.assertEqual(first, second)

    def test_index_is_derived_and_disposable(self):
        path = self.data / "index.sqlite3"
        index_mod.build(self.store.iter_event_rows(), path)
        path.unlink()
        self.assertEqual(index_mod.build(self.store.iter_event_rows(), path)["events"], 2)

    def test_malformed_rows_are_skipped_not_fatal(self):
        rows = list(self.store.iter_event_rows()) + [{"type": "nonsense"}, {}]
        counts = index_mod.build(rows, self.data / "index.sqlite3")
        self.assertEqual(counts["skipped"], 2)
        self.assertEqual(counts["events"], 2)


class JapaneseSearch(ArchiveFixture):
    def setUp(self):
        super().setUp()
        index_mod.build(self.store.iter_event_rows(), self.data / "index.sqlite3")
        self.conn = index_mod.connect(self.data / "index.sqlite3")
        self.addCleanup(self.conn.close)

    def test_finds_a_substring_inside_unsegmented_japanese(self):
        """The reason this is LIKE and not FTS5.

        Japanese has no word spaces, so an FTS5 tokenizer indexes the whole
        headline as one token and a two-character query silently finds nothing.
        """
        self.assertEqual(len(index_mod.search(self.conn, "地震")), 1)
        self.assertEqual(len(index_mod.search(self.conn, "対策")), 1)

    def test_finds_text_from_a_second_line(self):
        self.assertEqual(len(index_mod.search(self.conn, "津波")), 1)

    def test_no_match_returns_empty(self):
        self.assertEqual(index_mod.search(self.conn, "存在しない見出し"), [])

    def test_wildcards_in_the_query_are_literal(self):
        # A bare "%" must not match every row.
        self.assertEqual(index_mod.search(self.conn, "%"), [])
        self.assertEqual(index_mod.search(self.conn, "_"), [])

    def test_recent_and_active_filters(self):
        self.assertEqual(len(index_mod.recent(self.conn, limit=10)), 2)
        self.assertEqual(index_mod.recent(self.conn, limit=10, active_only=True), [])

    def test_summary(self):
        summary = index_mod.summary(self.conn)
        self.assertEqual(summary["events"], 2)
        self.assertEqual(summary["still_open"], 0)
        self.assertEqual(summary["by_category"], {"1": 1, "2": 1})


class CommandLine(ArchiveFixture):
    def test_list(self):
        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("政府", out)
        self.assertIn("東北で地震", out)

    def test_search(self):
        code, out = self.run_cli("search", "地震")
        self.assertEqual(code, 0)
        self.assertIn("東北で地震", out)
        self.assertNotIn("政府", out)

    def test_stats(self):
        code, out = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["events"], 2)

    def test_export_csv_has_a_row_per_event(self):
        import csv
        code, out = self.run_cli("export", "--format", "csv")
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(out)))
        self.assertEqual(len(rows), 2)
        self.assertIn("text", rows[0])

    def test_export_json_restores_the_lines_array(self):
        code, out = self.run_cli("export", "--format", "json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        multi = [p for p in payload if p["category"] == "2"][0]
        self.assertEqual(multi["lines"], ["東北で地震", "津波の心配なし"])

    def test_verify_passes_on_a_healthy_archive(self):
        code, out = self.run_cli("verify")
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_verify_detects_a_tampered_snapshot(self):
        snapshot = next(iter(self.store.iter_snapshots()))
        snapshot.write_bytes(b'<?xml version="1.0"?><flashNews flag="0" pubDate="tampered" />')
        code, _ = self.run_cli("verify")
        self.assertEqual(code, 1)

    def test_verify_detects_a_corrupt_event_row(self):
        path = next(iter(self.store.events_dir.glob("events-*.jsonl")))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        code, _ = self.run_cli("verify")
        self.assertEqual(code, 1)

    def test_status_is_healthy_after_a_recent_poll(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertLess(json.loads(out)["age_hours"], 1)

    def test_status_reports_staleness_with_a_distinct_exit_code(self):
        """Silence must be loud: a blocked archiver looks like a quiet news day."""
        state = self.store.load_state()
        state["last_success_utc"] = "2000-01-01T00:00:00Z"
        self.store.save_state(state)
        code, _ = self.run_cli("status")
        self.assertEqual(code, 3)

    def test_status_on_an_empty_archive_is_stale_not_healthy(self):
        empty = self.tmp / "empty"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["--data-dir", str(empty), "status"]), 3)

    def test_reindex(self):
        code, out = self.run_cli("reindex")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["events"], 2)

    def test_read_commands_build_the_index_on_demand(self):
        self.assertFalse((self.data / "index.sqlite3").exists())
        self.run_cli("list")
        self.assertTrue((self.data / "index.sqlite3").exists())


class PollCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *args):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--data-dir", str(self.data), *args])
        return code, buffer.getvalue()

    def test_once_records_a_valid_document(self):
        from sokuho_archive.fetcher import FetchResult
        result = FetchResult(url="u", status=200, body=document("1", [A]), etag='"e"',
                             last_modified="lm", fetched_at="now", elapsed_ms=1, attempts=1)
        with mock.patch("sokuho_archive.fetcher.Fetcher.fetch", return_value=result):
            code, out = self.run_cli("once")
        self.assertEqual(code, 0)
        self.assertIn("CHANGED", out)
        self.assertEqual(len(list(Store(self.data).iter_snapshots())), 1)

    def test_once_refuses_to_record_an_error_page(self):
        """The archive's core invariant: a stored observation is a real one."""
        from sokuho_archive.fetcher import FetchResult
        result = FetchResult(url="u", status=200, body=b'{"error":"Waf Error"}', etag=None,
                             last_modified=None, fetched_at="now", elapsed_ms=1, attempts=1)
        with mock.patch("sokuho_archive.fetcher.Fetcher.fetch", return_value=result):
            code, _ = self.run_cli("once")
        self.assertEqual(code, 1)
        self.assertEqual(list(Store(self.data).iter_snapshots()), [])

    def test_once_reports_a_fetch_failure(self):
        from sokuho_archive.fetcher import FetchError
        with mock.patch("sokuho_archive.fetcher.Fetcher.fetch", side_effect=FetchError("network_error", "down")):
            code, _ = self.run_cli("once")
        self.assertEqual(code, 1)

    def test_once_handles_304_without_writing(self):
        from sokuho_archive.fetcher import FetchResult
        result = FetchResult(url="u", status=304, body=None, etag='"e"', last_modified="lm",
                             fetched_at="now", elapsed_ms=1, attempts=1)
        with mock.patch("sokuho_archive.fetcher.Fetcher.fetch", return_value=result):
            code, out = self.run_cli("once")
        self.assertEqual(code, 0)
        self.assertIn("304", out)
        self.assertEqual(list(Store(self.data).iter_snapshots()), [])


if __name__ == "__main__":
    unittest.main()
