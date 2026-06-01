"""Pure helpers for `coach_app.py`.

`_build_date_background` returns surrounding-activities context for a date.
"""

from datetime import date, timedelta

import garmin_data as gd
import i18n
import time_awareness as ta


def _build_date_background(act: dict, det: dict) -> str:
    """Build training background centered on act's date, not today."""
    act_date_str = (act.get("startTimeLocal") or "")[:10]
    try:
        act_dt = date.fromisoformat(act_date_str)
    except ValueError:
        return ""
    ref_offset = ta.local_utc_offset_hours()
    act_label = ta.activity_date_label(
        act.get("startTimeLocal"), act.get("startTimeGMT"), ref_offset)
    days_ago = (date.today() - act_dt).days
    lines = [
        i18n.t("date_bg.header", date=act_label, days=days_ago),
        i18n.t("date_bg.note", date=act_label),
    ]
    # Surrounding activities: ±4 days, excluding the selected one
    win_start = (act_dt - timedelta(days=4)).isoformat()
    win_end   = (act_dt + timedelta(days=2)).isoformat()
    act_id    = act.get("activityId")
    surr = sorted(
        [a for a in det.get("activities", [])
         if win_start <= (a.get("startTimeLocal") or "")[:10] <= win_end
         and a.get("activityId") != act_id],
        key=lambda a: a.get("startTimeLocal", ""),
    )
    if surr:
        lines.append(i18n.t("date_bg.surrounding_header"))
        for a in surr:
            a_date = (a.get("startTimeLocal") or "")[:10]
            a_tk   = a.get("activityTypeKey", "")
            a_typ  = i18n.t(f"activity_type.{a_tk}") if a_tk else i18n.t("activity_type._unknown")
            a_dist = (a.get("distance") or 0) / 1000
            a_dur  = gd.format_duration(a.get("duration"))
            try:
                delta = (date.fromisoformat(a_date) - act_dt).days
                if delta < 0:
                    rel = i18n.t("date_bg.rel_before", n=-delta)
                else:
                    rel = i18n.t("date_bg.rel_after", n=delta)
            except ValueError:
                rel = a_date
            ap = [f"{a_dist:.1f}km", a_dur]
            if a.get("averageSpeed") and "run" in a_tk:
                ap.append(i18n.t("date_bg.avg_pace", pace=gd.format_pace(a["averageSpeed"])))
            if a.get("averageHR"):
                ap.append(i18n.t("date_bg.avg_hr", hr=a["averageHR"]))
            if a.get("aerobicTrainingEffect"):
                ap.append(i18n.t("date_bg.aerobic_te", te=a["aerobicTrainingEffect"]))
            a_label = ta.activity_date_label(
                a.get("startTimeLocal"), a.get("startTimeGMT"), ref_offset)
            lines.append(i18n.t(
                "date_bg.surrounding_line",
                date=a_label, rel=rel, typ=a_typ, stats=" | ".join(ap),
            ))
    return "\n".join(lines)


def _elapsed_minutes(ts_raw: list) -> list:
    """Convert raw UTC ms timestamps to minutes-since-start."""
    t0 = next((t for t in ts_raw if t), None)
    if t0 is None:
        return [None] * len(ts_raw)
    return [(t - t0) / 60000 if t else None for t in ts_raw]


def _pace_ticks(pace_s_values: list) -> tuple[list, list]:
    """Pick tick positions for a pace axis in seconds/km, formatted as MM:SS."""
    valid = [p for p in pace_s_values if p is not None]
    if not valid:
        return [], []
    p_min, p_max = min(valid), max(valid)
    span = p_max - p_min
    step = 15 if span < 90 else (30 if span < 240 else 60)
    lo = (int(p_min) // step) * step
    hi = (int(p_max) // step + 1) * step
    vals = list(range(lo, hi + step, step))
    text = [f"{v // 60}:{v % 60:02d}" for v in vals]
    return vals, text
