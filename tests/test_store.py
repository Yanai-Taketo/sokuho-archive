import json
import pathlib
import shutil
import tempfile
import unittest

from sokuho_archive.parser import parse
from sokuho_archive.store import Store, StoreLocked


def document(flag, reports=(), pub="Wed, 09 Sep 2026 10:00:00 +0900"):
    if not reports:
        return f'<?xml version="1.0" encoding="UTF-8" ?>\n<flashNews flag="{flag}" pubDate="{pub}" />\n'.encode()
    body = f'<?xml version="1.0" encoding="UTF-8" ?>\n<flashNews flag="{flag}" pubDate="{pub}">\n'
    for category, date, link, lines in reports:
        body += f'<report category="{category}" date="{date}" link="{link}">\n'
        body += "".join(f"<line>{line}</line>\n" for line in lines)
        body += "</report>\n"
    return (body + "</flashNews>\n").encode()


A = ("1", "2026/09/09 10:00", "", ["政府　対策を発表"])
A_EDITED = ("1", "2026/09/09 10:00", "https://x.invalid", ["政府　対策を正式発表"])
B = ("2", "2026/09/09 10:20", "", ["東北で地震", "津波の心配なし"])


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.store = Store(self.tmp / "data")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def record(self, raw):
        return self.store.record(raw, parse(raw), fetch_meta={}, heartbeat_hours=0)

    def rows(self):
        return list(self.store.iter_event_rows())


class SnapshotWriting(StoreTestCase):
    def test_first_observation_is_written(self):
        outcome = self.record(document("0"))
        self.assertTrue(outcome.changed)
        self.assertTrue(outcome.snapshot_path.exists())
        self.assertTrue(self.store.latest_path.exists())

    def test_identical_bytes_are_not_rewritten(self):
        raw = document("0")
        self.record(raw)
        second = self.record(raw)
        self.assertFalse(second.changed)
        self.assertEqual(len(list(self.store.iter_snapshots())), 1)

    def test_snapshot_is_byte_identical_to_the_source(self):
        raw = document("1", [A])
        outcome = self.record(raw)
        self.assertEqual(outcome.snapshot_path.read_bytes(), raw)

    def test_latest_tracks_the_newest_document(self):
        self.record(document("0"))
        raw = document("1", [A])
        self.record(raw)
        self.assertEqual(self.store.latest_path.read_bytes(), raw)

    def test_filename_encodes_the_content_hash(self):
        import hashlib
        raw = document("1", [A])
        outcome = self.record(raw)
        self.assertIn(hashlib.sha256(raw).hexdigest()[:12], outcome.snapshot_path.name)


class TransitionLog(StoreTestCase):
    def test_appearance(self):
        self.record(document("0"))
        outcome = self.record(document("1", [A]))
        self.assertEqual(len(outcome.appeared), 1)
        rows = self.rows()
        self.assertEqual([r["type"] for r in rows], ["appeared"])
        self.assertEqual(rows[0]["text"], "政府　対策を発表")

    def test_persisting_flash_is_logged_once(self):
        self.record(document("1", [A]))
        # A second flash arrives: the document changes, but A itself has not.
        self.record(document("1", [A, B]))
        types = [(r["type"], r["category"]) for r in self.rows()]
        self.assertEqual(types, [("appeared", "1"), ("appeared", "2")])

    def test_edit_is_recorded_as_a_revision_with_a_back_link(self):
        self.record(document("1", [A]))
        self.record(document("1", [A_EDITED]))
        rows = self.rows()
        self.assertEqual([r["type"] for r in rows], ["appeared", "revised"])
        self.assertEqual(rows[0]["event_id"], rows[1]["event_id"])
        self.assertEqual(rows[1]["previous_revision_id"], rows[0]["revision_id"])
        self.assertEqual(rows[1]["link"], "https://x.invalid")

    def test_disappearance_is_recorded_and_readable_alone(self):
        self.record(document("1", [A]))
        self.record(document("0"))
        cleared = self.rows()[-1]
        self.assertEqual(cleared["type"], "cleared")
        # The cleared row carries the headline, so the log reads without a join.
        self.assertEqual(cleared["text"], "政府　対策を発表")
        self.assertEqual(cleared["category"], "1")

    def test_full_lifecycle_is_recoverable(self):
        for raw in (document("1", [A]), document("1", [A, B]),
                    document("1", [A_EDITED, B]), document("1", [B]), document("0")):
            self.record(raw)
        rows = self.rows()
        self.assertEqual([r["type"] for r in rows],
                         ["appeared", "appeared", "revised", "cleared", "cleared"])
        a_id = rows[0]["event_id"]
        self.assertEqual([r["event_id"] for r in rows].count(a_id), 3)

    def test_empty_feed_logs_nothing(self):
        self.record(document("0"))
        self.assertEqual(self.rows(), [])

    def test_log_is_append_only(self):
        self.record(document("1", [A]))
        path, = list(self.store.events_dir.glob("events-*.jsonl"))
        first = path.read_text(encoding="utf-8")
        self.record(document("1", [A, B]))
        self.assertTrue(path.read_text(encoding="utf-8").startswith(first))

    def test_rows_are_valid_jsonl(self):
        self.record(document("1", [A, B]))
        path, = list(self.store.events_dir.glob("events-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_japanese_is_not_escaped_on_disk(self):
        self.record(document("1", [A]))
        path, = list(self.store.events_dir.glob("events-*.jsonl"))
        self.assertIn("政府", path.read_text(encoding="utf-8"))


class StateHandling(StoreTestCase):
    def test_corrupt_state_does_not_stop_the_archiver(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.store.state_path.write_text("{ this is not json", encoding="utf-8")
        self.assertTrue(self.record(document("1", [A])).changed)

    def test_state_records_liveness_and_counters(self):
        self.record(document("1", [A]))
        state = self.store.load_state()
        self.assertEqual(state["poll_count"], 1)
        self.assertEqual(state["change_count"], 1)
        self.assertTrue(state["last_success_utc"])
        self.assertEqual(len(state["active_events"]), 1)

    def test_cleared_events_leave_the_active_set(self):
        self.record(document("1", [A]))
        self.record(document("0"))
        self.assertEqual(self.store.load_state()["active_events"], {})

    def test_recent_revisions_are_bounded(self):
        from sokuho_archive.store import _RECENT_REVISIONS
        for minute in range(_RECENT_REVISIONS + 20):
            self.record(document("1", [("1", f"2026/09/09 {minute // 60:02d}:{minute % 60:02d}", "", ["x"])]))
        self.assertLessEqual(len(self.store.load_state()["recent_revisions"]), _RECENT_REVISIONS)


class Heartbeat(StoreTestCase):
    def test_quiet_period_eventually_records_liveness(self):
        raw = document("0")
        self.store.record(raw, parse(raw), fetch_meta={}, heartbeat_hours=6)
        # Nothing changed, but the archive has never recorded a heartbeat.
        state = self.store.load_state()
        state["heartbeat_utc"] = "2000-01-01T00:00:00Z"
        self.store.save_state(state)
        outcome = self.store.record(raw, parse(raw), fetch_meta={}, heartbeat_hours=6)
        self.assertFalse(outcome.changed)
        self.assertTrue(outcome.heartbeat)

    def test_heartbeat_can_be_disabled(self):
        raw = document("0")
        self.record(raw)
        self.assertFalse(self.record(raw).heartbeat)


class Locking(StoreTestCase):
    def test_second_holder_is_refused(self):
        with self.store.lock():
            other = Store(self.store.root)
            with self.assertRaises(StoreLocked):
                with other.lock():
                    pass

    def test_lock_is_released_after_use(self):
        with self.store.lock():
            pass
        with Store(self.store.root).lock():
            pass


class AtomicWrites(unittest.TestCase):
    def test_no_temp_files_survive_a_successful_write(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            store = Store(tmp / "data")
            raw = document("1", [A])
            store.record(raw, parse(raw), fetch_meta={}, heartbeat_hours=0)
            self.assertEqual([p.name for p in store.root.rglob(".*.tmp")], [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_readers_never_see_a_partial_file(self):
        from sokuho_archive.store import _atomic_write
        tmp = pathlib.Path(tempfile.mkdtemp())
        try:
            target = tmp / "x.xml"
            _atomic_write(target, b"first")
            self.assertEqual(target.read_bytes(), b"first")
            _atomic_write(target, b"second-and-longer")
            self.assertEqual(target.read_bytes(), b"second-and-longer")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
