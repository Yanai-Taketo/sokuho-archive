"""Data model for one observation of the NHK 速報 feed.

Identity is deliberately split in two, because a flash is not immutable:

``event_id``
    The *slot* a flash occupies -- derived from ``category`` and the publisher's
    own ``date``.  It stays the same across every poll while the flash is live,
    and stays the same if NHK edits the wording in place.

``revision_id``
    The *content* -- derived from the event id plus the link and the text lines.
    A new revision under an existing event id means NHK rewrote a live flash,
    which is itself worth recording rather than silently overwriting.

Hashing over the raw ``date`` string rather than a parsed timestamp keeps
identity stable even if the date fails to parse, and keeps it independent of
the volatile ``pubDate`` on the root element (which moves whenever NHK rewrites
the file, and would otherwise make every poll look like a new event).
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any

from .timeutil import parse_pub_date, parse_report_date, to_utc_iso

__all__ = ["Report", "FlashNews", "SCHEMA_VERSION", "IDENTITY_VERSION", "LINE_JOINER"]

# Bumped when the shape of an emitted JSONL record changes.
SCHEMA_VERSION = 1
# Bumped only when the event_id/revision_id recipe changes; a bump means old and
# new ids are not comparable, so it is recorded alongside every id.
IDENTITY_VERSION = 1

# U+3000 IDEOGRAPHIC SPACE -- the separator NHK itself uses between clauses.
LINE_JOINER = "\u3000"


def _digest(*parts: str) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class Report:
    """A single ``<report>`` element: one breaking-news item."""

    category: str
    date_raw: str
    link: str
    lines: tuple[str, ...]
    # Attributes and child elements NHK may add in future, preserved verbatim so
    # that a schema change degrades to "we kept it" rather than "we dropped it".
    extra_attrs: dict[str, str] = dataclasses.field(default_factory=dict)
    extra_children: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """The lines joined into one readable headline.

        NHK separates clauses inside a single ``<line>`` with the ideographic
        space U+3000, so joining separate lines with the same character keeps
        the rendering typographically consistent with the source.  Joining with
        nothing would run words together ("...地震津波の心配はありません").

        This is a convenience for display and search only -- ``lines`` holds the
        verbatim source, and ``revision_id`` hashes the lines with a newline
        separator, so identity never depends on this choice.
        """
        return LINE_JOINER.join(self.lines)

    @property
    def date_utc(self) -> str | None:
        return to_utc_iso(parse_report_date(self.date_raw))

    @property
    def event_id(self) -> str:
        return _digest(f"v{IDENTITY_VERSION}", "event", self.category, self.date_raw)

    @property
    def revision_id(self) -> str:
        return _digest(
            f"v{IDENTITY_VERSION}", "rev", self.event_id, self.link, "\n".join(self.lines)
        )

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "event_id": self.event_id,
            "revision_id": self.revision_id,
            "identity_version": IDENTITY_VERSION,
            "category": self.category,
            "date_raw": self.date_raw,
            "date_utc": self.date_utc,
            "link": self.link,
            "lines": list(self.lines),
            "text": self.text,
        }
        if self.extra_attrs:
            record["extra_attrs"] = dict(self.extra_attrs)
        if self.extra_children:
            record["extra_children"] = list(self.extra_children)
        return record


@dataclasses.dataclass(frozen=True)
class FlashNews:
    """A parsed ``<flashNews>`` document -- the whole feed at one instant."""

    flag: str
    pub_date_raw: str
    reports: tuple[Report, ...]
    extra_attrs: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def active(self) -> bool:
        """True when NHK is currently showing a flash.

        ``flag`` is authoritative, but a document carrying reports while
        claiming ``flag="0"`` is treated as active: keeping the reports costs
        nothing and dropping them would lose data.
        """
        return self.flag == "1" or bool(self.reports)

    @property
    def pub_date_utc(self) -> str | None:
        return to_utc_iso(parse_pub_date(self.pub_date_raw))

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "flag": self.flag,
            "active": self.active,
            "pub_date_raw": self.pub_date_raw,
            "pub_date_utc": self.pub_date_utc,
            "reports": [r.to_dict() for r in self.reports],
        }
        if self.extra_attrs:
            record["extra_attrs"] = dict(self.extra_attrs)
        return record
