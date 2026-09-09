import pathlib
import unittest

from sokuho_archive.models import LINE_JOINER
from sokuho_archive.parser import MAX_DOCUMENT_BYTES, ParseError, parse

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_bytes()


class AcceptsRealDocuments(unittest.TestCase):
    def test_live_empty_feed(self):
        flash = parse(fixture("live_flag0_20260909.xml"))
        self.assertEqual(flash.flag, "0")
        self.assertFalse(flash.active)
        self.assertEqual(flash.reports, ())
        self.assertEqual(flash.pub_date_utc, "2026-09-08T16:02:00Z")

    def test_single_report(self):
        flash = parse(fixture("flag1_single.xml"))
        self.assertTrue(flash.active)
        report, = flash.reports
        self.assertEqual(report.category, "1")
        self.assertEqual(report.date_raw, "2011/05/06 18:54")
        self.assertEqual(report.date_utc, "2011-05-06T09:54:00Z")
        self.assertIn("浜岡原発", report.text)
        # The ideographic space inside the headline is content, not formatting.
        self.assertIn("　", report.text)

    def test_multiple_lines_are_kept_separate(self):
        flash = parse(fixture("flag1_multiline.xml"))
        report, = flash.reports
        self.assertEqual(len(report.lines), 3)
        self.assertEqual(report.lines[0], "東北地方で強い地震")
        # Joined for display, never run together.
        self.assertEqual(report.text, LINE_JOINER.join(report.lines))
        self.assertNotIn("地震宮城", report.text)

    def test_multiple_reports(self):
        flash = parse(fixture("flag1_multi_report.xml"))
        self.assertEqual(len(flash.reports), 2)
        self.assertEqual([r.category for r in flash.reports], ["1", "3"])
        self.assertTrue(flash.reports[0].link.startswith("https://"))
        self.assertEqual(flash.reports[1].link, "")

    def test_byte_order_mark(self):
        self.assertEqual(parse(fixture("edge_bom.xml")).flag, "1")

    def test_entities_and_cdata_resolve(self):
        report, = parse(fixture("edge_entities.xml")).reports
        self.assertIn("A&B<C>", report.lines[0])
        self.assertIn("CDATA & <raw>", report.lines[1])

    def test_reports_win_over_a_contradictory_flag(self):
        # flag="0" while carrying reports: keep the data rather than trust the flag.
        flash = parse(fixture("edge_flag0_with_reports.xml"))
        self.assertEqual(flash.flag, "0")
        self.assertTrue(flash.active)
        self.assertEqual(len(flash.reports), 1)

    def test_unknown_fields_are_preserved(self):
        flash = parse(fixture("edge_unknown_fields.xml"))
        self.assertEqual(flash.extra_attrs, {"region": "kanto"})
        report, = flash.reports
        self.assertEqual(report.extra_attrs, {"priority": "high", "id": "abc123"})
        self.assertTrue(any("media" in child for child in report.extra_children))
        # And they survive into the emitted record.
        self.assertIn("extra_attrs", report.to_dict())


class RejectsNonDocuments(unittest.TestCase):
    def assertRejected(self, name, reason):
        with self.assertRaises(ParseError) as caught:
            parse(fixture(name))
        self.assertEqual(caught.exception.reason, reason)

    def test_empty_body(self):
        self.assertRejected("neg_empty.xml", "empty_body")

    def test_waf_json_error_page(self):
        # Observed in the wild: an interposed WAF returning JSON with its own
        # ETag. Recording it would look like NHK publishing nothing.
        self.assertRejected("neg_waf_error.json", "malformed_xml")

    def test_html_error_page(self):
        self.assertRejected("neg_html_error.html", "doctype_rejected")

    def test_truncated_transfer(self):
        self.assertRejected("neg_truncated.xml", "malformed_xml")

    def test_mislabeled_encoding(self):
        self.assertRejected("neg_mislabeled_sjis.xml", "malformed_xml")

    def test_valid_xml_that_is_not_this_feed(self):
        self.assertRejected("neg_wrong_root.xml", "unexpected_root")

    def test_billion_laughs(self):
        self.assertRejected("neg_billion_laughs.xml", "doctype_rejected")

    def test_xxe(self):
        self.assertRejected("neg_xxe.xml", "doctype_rejected")

    def test_oversized_body(self):
        with self.assertRaises(ParseError) as caught:
            parse(b"<flashNews flag='0'/>" + b" " * MAX_DOCUMENT_BYTES)
        self.assertEqual(caught.exception.reason, "too_large")

    def test_non_bytes_input(self):
        with self.assertRaises(ParseError) as caught:
            parse("<flashNews flag='0'/>")
        self.assertEqual(caught.exception.reason, "not_bytes")


class EntityExpansionIsBounded(unittest.TestCase):
    def test_doctype_gate_precedes_expansion(self):
        """A 450-byte document can expand to a megabyte; never let it start.

        CPython grew an amplification limit only recently, so relying on it
        would make safety interpreter-version dependent.
        """
        entities = '<!ENTITY a "aaaaaaaaaa">\n'
        previous = "a"
        for level in range(5):
            name = f"e{level}"
            entities += f'<!ENTITY {name} "{("&%s;" % previous) * 10}">\n'
            previous = name
        bomb = (
            f'<?xml version="1.0"?>\n<!DOCTYPE flashNews [\n{entities}]>\n'
            f'<flashNews flag="1" pubDate="x"><report category="1" date="d" link="">'
            f"<line>&{previous};</line></report></flashNews>"
        ).encode()
        self.assertLess(len(bomb), 1000)
        with self.assertRaises(ParseError) as caught:
            parse(bomb)
        self.assertEqual(caught.exception.reason, "doctype_rejected")


class Namespaces(unittest.TestCase):
    def test_namespaced_document_still_parses(self):
        raw = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<flashNews xmlns="https://example.invalid/nhk" flag="1" pubDate="x">'
            b'<report category="9" date="2024/01/01 00:00" link="">'
            b"<line>\xe3\x83\x86\xe3\x82\xb9\xe3\x83\x88</line></report></flashNews>"
        )
        flash = parse(raw)
        self.assertEqual(flash.flag, "1")
        self.assertEqual(len(flash.reports), 1)
        self.assertEqual(flash.reports[0].lines, ("テスト",))


if __name__ == "__main__":
    unittest.main()
