"""Tolerant, hostile-input-safe parsing of the 速報 XML document.

The archive's contract is that a stored observation is a *real* observation.
An HTTP 403 WAF page, a CDN error, a captive portal and a truncated transfer
must all be rejected rather than recorded as "NHK said nothing" -- otherwise the
archive quietly grows gaps that look like genuine quiet periods.

Validation is therefore layered, cheapest and most decisive first:

1. non-empty, and within a size cap;
2. no ``<!DOCTYPE`` -- NHK never sends one, and internal entity expansion is a
   memory-amplification vector (a 450-byte document can expand to 1 MB on
   CPython 3.11, and older interpreters have no amplification limit at all);
3. well-formed XML;
4. the root element really is ``<flashNews>``.

Only step 4 distinguishes the feed from any other well-formed XML, so it is not
optional: an ``<rss>`` document parses perfectly and means nothing here.
"""

from __future__ import annotations

import dataclasses
import re
import xml.etree.ElementTree as ET

from .models import FlashNews, Report

__all__ = ["ParseError", "parse", "MAX_DOCUMENT_BYTES", "ROOT_TAG"]

ROOT_TAG = "flashNews"

# The live document is ~105 bytes empty and a few kB with several reports.
# 1 MiB is four orders of magnitude of headroom and still bounds the damage.
MAX_DOCUMENT_BYTES = 1 << 20

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_KNOWN_REPORT_ATTRS = frozenset({"category", "date", "link"})
_KNOWN_ROOT_ATTRS = frozenset({"flag", "pubDate"})


class ParseError(ValueError):
    """The bytes are not a usable 速報 document.

    ``reason`` is a short stable slug suitable for metrics and log grouping;
    the message carries the human-readable detail.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _text_of(element: ET.Element) -> str:
    """All text under an element, entities resolved, whitespace preserved.

    Only the outer edges are stripped.  Interior spacing is meaningful: NHK
    separates clauses with the ideographic space U+3000, and collapsing it would
    silently rewrite the headline.
    """
    return "".join(element.itertext()).strip("\r\n\t ")


def parse(raw: bytes, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> FlashNews:
    """Parse feed bytes into a :class:`FlashNews`, or raise :class:`ParseError`."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ParseError("not_bytes", type(raw).__name__)
    if not raw.strip():
        raise ParseError("empty_body", "response had no content")
    if len(raw) > max_bytes:
        raise ParseError("too_large", f"{len(raw)} bytes exceeds cap of {max_bytes}")
    if _DOCTYPE_RE.search(raw):
        raise ParseError("doctype_rejected", "document type declarations are not accepted")

    try:
        root = ET.fromstring(bytes(raw))
    except ET.ParseError as exc:
        raise ParseError("malformed_xml", str(exc)) from exc

    # Namespaces are not used by this feed, but tolerate one rather than
    # rejecting a document NHK could plausibly start emitting.
    tag = root.tag.rsplit("}", 1)[-1]
    if tag != ROOT_TAG:
        raise ParseError("unexpected_root", f"<{tag}> is not <{ROOT_TAG}>")

    reports = tuple(_parse_report(node) for node in root.iter() if _is_report(node))
    root_attrs = dict(root.attrib)
    return FlashNews(
        flag=(root_attrs.pop("flag", "") or "").strip(),
        pub_date_raw=(root_attrs.pop("pubDate", "") or "").strip(),
        reports=reports,
        extra_attrs={k: v for k, v in root_attrs.items() if k not in _KNOWN_ROOT_ATTRS},
    )


def _is_report(node: ET.Element) -> bool:
    return node.tag.rsplit("}", 1)[-1] == "report"


def _parse_report(node: ET.Element) -> Report:
    attrs = dict(node.attrib)
    lines: list[str] = []
    extra_children: list[str] = []
    for child in node:
        name = child.tag.rsplit("}", 1)[-1]
        if name == "line":
            lines.append(_text_of(child))
        else:
            # Keep a serialised copy of anything unrecognised so a future NHK
            # schema addition survives in the derived record too, not only in
            # the raw snapshot.
            extra_children.append(
                ET.tostring(child, encoding="unicode").strip()
            )

    # A report with no <line> children but with its own text still carries a
    # headline; losing it because the markup changed would be the worst outcome.
    if not lines:
        direct = _text_of(node)
        if direct:
            lines.append(direct)

    return Report(
        category=(attrs.pop("category", "") or "").strip(),
        date_raw=(attrs.pop("date", "") or "").strip(),
        link=(attrs.pop("link", "") or "").strip(),
        lines=tuple(lines),
        extra_attrs={k: v for k, v in attrs.items() if k not in _KNOWN_REPORT_ATTRS},
        extra_children=tuple(extra_children),
    )
