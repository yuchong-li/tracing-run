"""Time-awareness for LLM context.

Two responsibilities:
  1. now_block()           — ready-to-append system-prompt block telling the
                             LLM what "now" is (date, weekday, time, TZ).
  2. annotate_history(msgs)— prefix user messages with their send time so
                             the LLM can reason about gaps and deltas. Assistant
                             messages are left untouched (avoid teaching the
                             model to echo timestamps in its own replies).

Both rely on the container's wall clock (`datetime.now()`), which means TZ
correctness is upstream — see docker-compose.yml `TZ` env and .env.example.
"""

from datetime import date, datetime

import i18n


# Minimum offset gap (hours) from "now" before an activity gets a UTC tag.
# Set to 2 so daylight-saving shifts (AEST +10 ↔ AEDT +11, a 1h wobble in the
# SAME city) stay untagged, while genuine travel (AU↔US is ~17h, AU↔Hawaii
# ~20h) is flagged. Without this, half a frequent-flyer's home runs would be
# mislabelled "another timezone" just because of summer time.
_TZ_TAG_MIN_DIFF = 2


def _fmt_offset(hours: int) -> str:
    """Render an integer UTC offset as a signed string: 10 → '+10', -7 → '-7'."""
    return f"+{hours}" if hours >= 0 else str(hours)


def local_utc_offset_hours(now: datetime | None = None) -> int:
    """The container wall-clock's current UTC offset in whole hours.

    This is the reference offset activity labels compare against — an activity
    logged at a different offset (US trip vs AU home) gets a TZ annotation."""
    n = now or datetime.now()
    off = n.astimezone().utcoffset()
    return round(off.total_seconds() / 3600) if off else 0


def _parse_dt(value) -> datetime | None:
    """Lenient parse for Garmin time strings. start_time_gmt comes in mixed
    shapes ('2026-05-31T20:08:05.0' and '2026-05-30 01:09:59'); fromisoformat
    on 3.11+ handles both the 'T'/space separator and the fractional seconds."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def utc_offset_hours(local_str: str | None, gmt_str: str | None) -> int | None:
    """Whole-hour UTC offset of an activity, derived from (local - gmt).

    Both fields are naive wall-clock strings in their respective zones, so
    their difference IS the offset. Returns None if either is unparseable."""
    lo = _parse_dt(local_str)
    gm = _parse_dt(gmt_str)
    if lo is None or gm is None:
        return None
    return round((lo - gm).total_seconds() / 3600)


def weekday_label(d: date) -> str:
    """Localized weekday name (周六 / Saturday) for a date — computed here in
    Python so the LLM never has to derive day-of-week from a bare ISO date."""
    return i18n.t(f"time_awareness.weekday_{d.weekday()}")


def activity_date_label(local_str: str | None, gmt_str: str | None,
                        ref_offset: int | None = None) -> str:
    """Render an activity's date as '2026-05-30（周六）', appending a
    '，UTC-7' suffix when the activity's offset differs from ref_offset
    (i.e. it was logged in a different timezone than 'now').

    Falls back to the raw 10-char date if local time can't be parsed."""
    date_str = (local_str or "")[:10]
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return date_str
    wd = weekday_label(d)
    off = utc_offset_hours(local_str, gmt_str)
    if (off is not None and ref_offset is not None
            and abs(off - ref_offset) >= _TZ_TAG_MIN_DIFF):
        return i18n.t("time_awareness.date_weekday_tz",
                      date=date_str, weekday=wd, offset=_fmt_offset(off))
    return i18n.t("time_awareness.date_weekday", date=date_str, weekday=wd)


def activity_datetime_label(local_str: str | None, gmt_str: str | None,
                            ref_offset: int | None = None) -> str:
    """Like activity_date_label but also carries the local clock time AND the
    absolute UTC timestamp, e.g.:
        2026-06-01（周一）06:08 · UTC 2026-05-31 20:08
        2026-05-30（周六）08:00（UTC-7）· UTC 2026-05-30 15:00

    The local time-of-day lets the LLM reason about morning/evening sessions;
    the UTC suffix lets it compute real elapsed gaps between any two activities
    even when their local dates collide across a timezone change. Falls back to
    the date-only label when local time is unparseable."""
    lo = _parse_dt(local_str)
    if lo is None:
        return activity_date_label(local_str, gmt_str, ref_offset)
    date_str = lo.strftime("%Y-%m-%d")
    wd = weekday_label(lo.date())
    time_str = lo.strftime("%H:%M")
    off = utc_offset_hours(local_str, gmt_str)
    if (off is not None and ref_offset is not None
            and abs(off - ref_offset) >= _TZ_TAG_MIN_DIFF):
        base = i18n.t("time_awareness.activity_dt_tz", date=date_str,
                      weekday=wd, time=time_str, offset=_fmt_offset(off))
    else:
        base = i18n.t("time_awareness.activity_dt", date=date_str,
                      weekday=wd, time=time_str)
    gm = _parse_dt(gmt_str)
    if gm is not None:
        base += i18n.t("time_awareness.utc_suffix", utc=gm.strftime("%Y-%m-%d %H:%M"))
    return base


def now_block(now: datetime | None = None) -> str:
    """Return a markdown block ready to append to a system prompt.

    Caller appends verbatim — string already starts with two newlines so it
    visually separates from the preceding block."""
    n = now or datetime.now()
    weekday = i18n.t(f"time_awareness.weekday_{n.weekday()}")
    body = i18n.t(
        "time_awareness.now_format",
        date=n.strftime("%Y-%m-%d"),
        weekday=weekday,
        time=n.strftime("%H:%M"),
        tz=n.astimezone().strftime("%Z") or "local",
        offset=_fmt_offset(local_utc_offset_hours(n)),
    )
    return i18n.t("time_awareness.now_header") + body


def annotate_history(msgs: list[dict]) -> list[dict]:
    """Return a copy of msgs with [time] prefixed onto user-role content.

    Only the role/content keys are emitted (matches what the LLM call expects).
    Messages without a parseable `ts` field pass through unchanged."""
    out: list[dict] = []
    for m in msgs:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            ts = _parse_ts(m.get("ts"))
            if ts is not None:
                content = f"[{ts.strftime('%Y-%m-%d %H:%M')}] {content}"
        out.append({"role": role, "content": content})
    return out


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
