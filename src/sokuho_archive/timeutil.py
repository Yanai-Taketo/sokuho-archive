"""Time parsing and normalisation for the NHK 速報 feed.

The feed speaks two time formats, both in Japan Standard Time (UTC+9):

* ``flashNews@pubDate``  -- RFC 2822, e.g. ``Fri, 06 May 2011 21:12:00 +0900``
* ``report@date``        -- ``YYYY/MM/DD HH:MM`` with no zone and no seconds

Everything the archive derives is stored twice: the original string exactly as
NHK sent it, and a normalised UTC ISO-8601 rendering.  The original is the
record of what was published; the normalised form is what you sort and query on.

JST has no daylight saving, so the fixed +09:00 offset is correct for all dates.
"""

from __future__ import annotations

import datetime as _dt
import re
from email.utils import parsedate_to_datetime

__all__ = [
    "JST",
    "UTC",
    "parse_pub_date",
    "parse_report_date",
    "to_utc_iso",
    "utc_now",
    "utc_now_iso",
    "compact_stamp",
]

JST = _dt.timezone(_dt.timedelta(hours=9), "JST")
UTC = _dt.timezone.utc

# "2011/05/06 18:54" and the seconds-bearing variant, tolerating runs of spaces
# and single-digit month/day/hour in case NHK ever emits them.
_REPORT_DATE_RE = re.compile(
    r"^\s*(?P<Y>\d{4})[/-](?P<m>\d{1,2})[/-](?P<d>\d{1,2})"
    r"[\s　T]+(?P<H>\d{1,2}):(?P<M>\d{2})(?::(?P<S>\d{2}))?\s*$"
)


def utc_now() -> _dt.datetime:
    """Timezone-aware current time in UTC."""
    return _dt.datetime.now(tz=UTC)


def utc_now_iso() -> str:
    """Current UTC time as ``2026-09-09T00:40:23Z`` (second precision)."""
    return to_utc_iso(utc_now()) or ""


def compact_stamp(moment: _dt.datetime) -> str:
    """``20260909T004023Z`` -- safe for filenames on every filesystem."""
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_pub_date(value: str | None) -> _dt.datetime | None:
    """Parse ``flashNews@pubDate`` (RFC 2822). ``None`` if unparseable.

    ``email.utils`` is used rather than ``strptime`` because ``%a``/``%b`` are
    locale-dependent and would break under a non-English ``LC_TIME``.  The
    weekday name is ignored, so a feed whose day-of-week disagrees with the date
    still parses.  A value carrying no zone is read as JST, matching the feed.
    """
    if not value or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


def parse_report_date(value: str | None) -> _dt.datetime | None:
    """Parse ``report@date`` (``YYYY/MM/DD HH:MM``, implicitly JST)."""
    if not value:
        return None
    match = _REPORT_DATE_RE.match(value)
    if not match:
        return None
    part = match.groupdict()
    try:
        return _dt.datetime(
            int(part["Y"]), int(part["m"]), int(part["d"]),
            int(part["H"]), int(part["M"]), int(part["S"] or 0),
            tzinfo=JST,
        )
    except ValueError:
        # Real calendar violations such as 2011/02/30 or hour 25.
        return None


def to_utc_iso(moment: _dt.datetime | None) -> str | None:
    """Render an aware datetime as ``2011-05-06T09:54:00Z``."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=JST)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
