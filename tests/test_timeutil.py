import datetime as dt
import unittest

from sokuho_archive import timeutil as t


class ParsePubDate(unittest.TestCase):
    def test_rfc2822_jst(self):
        self.assertEqual(t.to_utc_iso(t.parse_pub_date("Fri, 06 May 2011 21:12:00 +0900")),
                         "2011-05-06T12:12:00Z")

    def test_matches_observed_last_modified(self):
        # The live feed's pubDate and its Last-Modified header agree; this pins
        # the JST reading so a regression cannot silently shift the archive 9h.
        self.assertEqual(t.to_utc_iso(t.parse_pub_date("Wed, 09 Sep 2026 01:02:00 +0900")),
                         "2026-09-08T16:02:00Z")

    def test_wrong_weekday_still_parses(self):
        # The weekday name is redundant; a feed bug there must not lose the date.
        self.assertEqual(t.to_utc_iso(t.parse_pub_date("Xxx, 06 May 2011 21:12:00 +0900")),
                         "2011-05-06T12:12:00Z")

    def test_missing_zone_read_as_jst(self):
        self.assertEqual(t.to_utc_iso(t.parse_pub_date("06 May 2011 21:12:00")),
                         "2011-05-06T12:12:00Z")

    def test_rejects_junk(self):
        for value in ("", "   ", "garbage", None):
            self.assertIsNone(t.parse_pub_date(value), value)

    def test_locale_independent(self):
        # strptime's %a/%b would break under a non-English LC_TIME; email.utils
        # does not. Assert the behaviour rather than the implementation.
        import locale
        original = locale.setlocale(locale.LC_TIME)
        for candidate in ("ja_JP.UTF-8", "de_DE.UTF-8", "C"):
            try:
                locale.setlocale(locale.LC_TIME, candidate)
            except locale.Error:
                continue
            try:
                self.assertEqual(
                    t.to_utc_iso(t.parse_pub_date("Fri, 06 May 2011 21:12:00 +0900")),
                    "2011-05-06T12:12:00Z",
                    f"failed under LC_TIME={candidate}",
                )
            finally:
                locale.setlocale(locale.LC_TIME, original)


class ParseReportDate(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(t.to_utc_iso(t.parse_report_date("2011/05/06 18:54")),
                         "2011-05-06T09:54:00Z")

    def test_tolerates_single_digits_and_seconds(self):
        self.assertEqual(t.to_utc_iso(t.parse_report_date("2024/7/9 9:12")),
                         "2024-07-09T00:12:00Z")
        self.assertEqual(t.to_utc_iso(t.parse_report_date("2024/03/11 14:46:30")),
                         "2024-03-11T05:46:30Z")

    def test_rejects_impossible_dates(self):
        for value in ("2011/02/30 10:00", "2024/01/01 25:00", "2024/13/01 10:00"):
            self.assertIsNone(t.parse_report_date(value), value)

    def test_rejects_junk(self):
        for value in ("", None, "yesterday", "2024/07/09"):
            self.assertIsNone(t.parse_report_date(value), value)


class Rendering(unittest.TestCase):
    def test_compact_stamp_is_filename_safe(self):
        moment = dt.datetime(2026, 9, 9, 0, 40, 23, tzinfo=t.UTC)
        stamp = t.compact_stamp(moment)
        self.assertEqual(stamp, "20260909T004023Z")
        self.assertFalse(set(stamp) & set('/\\:*?"<>| '))

    def test_naive_datetime_treated_as_jst(self):
        naive = dt.datetime(2011, 5, 6, 21, 12, 0)
        self.assertEqual(t.to_utc_iso(naive), "2011-05-06T12:12:00Z")


if __name__ == "__main__":
    unittest.main()
