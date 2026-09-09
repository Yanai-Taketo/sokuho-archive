import unittest

from sokuho_archive.models import IDENTITY_VERSION, FlashNews, Report


def report(category="1", date="2026/09/09 10:00", link="", lines=("見出し",)):
    return Report(category=category, date_raw=date, link=link, lines=tuple(lines))


class EventIdentity(unittest.TestCase):
    def test_stable_across_identical_observations(self):
        self.assertEqual(report().event_id, report().event_id)

    def test_survives_text_edits(self):
        """Editing a live flash's wording is a revision, not a new flash."""
        original = report(lines=("政府　対策を発表",))
        edited = report(lines=("政府　対策を正式発表",))
        self.assertEqual(original.event_id, edited.event_id)
        self.assertNotEqual(original.revision_id, edited.revision_id)

    def test_link_change_is_a_revision_not_a_new_event(self):
        self.assertEqual(report().event_id, report(link="https://x.invalid").event_id)
        self.assertNotEqual(report().revision_id, report(link="https://x.invalid").revision_id)

    def test_distinct_flashes_differ(self):
        self.assertNotEqual(report(date="2026/09/09 10:00").event_id,
                            report(date="2026/09/09 10:01").event_id)
        self.assertNotEqual(report(category="1").event_id, report(category="2").event_id)

    def test_identity_ignores_the_volatile_root_pubdate(self):
        """pubDate moves whenever NHK rewrites the file.

        If it were part of the identity, every rewrite would look like a brand
        new flash and the archive would fill with duplicates.
        """
        a = FlashNews(flag="1", pub_date_raw="Wed, 09 Sep 2026 10:00:00 +0900", reports=(report(),))
        b = FlashNews(flag="1", pub_date_raw="Wed, 09 Sep 2026 10:30:00 +0900", reports=(report(),))
        self.assertEqual(a.reports[0].event_id, b.reports[0].event_id)
        self.assertEqual(a.reports[0].revision_id, b.reports[0].revision_id)

    def test_identity_independent_of_display_joiner(self):
        """revision_id must not shift if the display join character changes."""
        multi = report(lines=("あ", "い"))
        self.assertNotIn(multi.text, multi.revision_id)
        # Two lines must not collide with their concatenation.
        self.assertNotEqual(multi.revision_id, report(lines=("あい",)).revision_id)

    def test_record_carries_identity_version(self):
        self.assertEqual(report().to_dict()["identity_version"], IDENTITY_VERSION)


class ActiveFlag(unittest.TestCase):
    def test_flag_one_is_active(self):
        self.assertTrue(FlashNews("1", "x", (report(),)).active)

    def test_flag_zero_without_reports_is_inactive(self):
        self.assertFalse(FlashNews("0", "x", ()).active)

    def test_reports_override_a_zero_flag(self):
        self.assertTrue(FlashNews("0", "x", (report(),)).active)

    def test_unexpected_flag_value_with_reports(self):
        self.assertTrue(FlashNews("2", "x", (report(),)).active)


class Serialisation(unittest.TestCase):
    def test_round_trips_through_json(self):
        import json
        payload = json.loads(json.dumps(FlashNews("1", "Wed, 09 Sep 2026 10:00:00 +0900",
                                                  (report(lines=("あ", "い")),)).to_dict(),
                                        ensure_ascii=False))
        self.assertEqual(payload["reports"][0]["lines"], ["あ", "い"])
        self.assertEqual(payload["pub_date_utc"], "2026-09-09T01:00:00Z")

    def test_omits_empty_extras(self):
        self.assertNotIn("extra_attrs", report().to_dict())


if __name__ == "__main__":
    unittest.main()
