"""Drill-down tools exposed to the LLM via OpenAI function-calling.

Two tool families:

**Per-activity (review chat — single activity)** — `make_tool_handlers(aid)`:
- `get_raw_window_by_time`     — fetch raw metrics for a sec_offset window
- `get_raw_window_by_distance` — fetch raw metrics for a distance_cum_m window
- `get_window_stats`           — fetch aggregated stats (avg / percentile /
                                  drift) for a window. Cheaper than raw rows.

These all bind `activity_id` server-side; LLM sees only start/end/channels.

**Cross-activity (overall chat — main page)** — `make_overall_tool_handlers()`:
- `find_activities`     — search activities by tag / name / date range
- `get_activity_report` — get full typed-builder report (markdown) for ONE aid
- `get_metric_trend`    — time series of a metric (vo2max / training_load /
                          weekly_run_km / weekly_load / avg_hr / aerobic_te)
                          over the last N days

The overall-chat tools do NOT include the 1Hz raw drill (`get_raw_window_*`)
or the within-activity `get_window_stats` — those live in the per-activity
review chat by design. Cross-activity 1Hz comparisons should redirect the
user to 🔬 复盘.

Raw tools return up to ~200 rows by auto-downsampling longer windows; the
returned JSON includes a `sampling` note so the LLM knows the granularity.
The stats tool returns one fixed-size JSON object regardless of window length.
"""

import sqlite3
from typing import Optional

import db
import web_search
from review_builders.primitives import seg_stats, hr_drift, ROW_SEC


# ── Shared tool: web search (exposed to both per-activity + overall chats) ──

_WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the live web for running-related facts. USE WHEN: the user "
            "asks about training methodology, normative comparisons, race "
            "results, gear, injury rehab protocols, or recent research you "
            "may not know about. DO NOT USE for questions answerable from the "
            "user's own data already in context, for casual chat, or for "
            "generic non-running topics. Cite sources by URL when you use a "
            "result in your reply."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query in plain natural language.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results to return (default 5, max 10).",
                },
            },
            "required": ["query"],
        },
    },
}


# ── Sampling thresholds ("medium" cap) ──────────────────────────────────────
# Goal: tool result stays around 200 rows / 6K tokens regardless of window.

ROW_CAP             = 200      # never return more than this many rows
WIN_NATIVE_MAX_S    = 200      # ≤200s window: 1Hz native (≤200 rows)
WIN_3S_AVG_MAX_S    = 600      # 200-600s window: every 3s (67-200 rows)
                               # >600s window: every 6s (≥100 rows)

# Channels exposed to the LLM. SQLite has more (lat/lon, etc.) but those
# aren't useful for narrative drill-down — keep the tool surface focused.
ALL_CHANNELS = (
    "hr", "speed", "cadence", "gct", "vr", "stride", "power", "elevation",
)
_CHANNEL_TO_COL = {
    "hr":        "hr",
    "speed":     "speed_mps",
    "cadence":   "cadence_spm",
    "gct":       "gct_ms",
    "vr":        "vert_ratio",
    "stride":    "stride_cm",
    "power":     "power_w",
    "elevation": "elevation_m",
}


# ── Public tool implementations ─────────────────────────────────────────────

def get_raw_window_by_time(
    activity_id: int,
    start_seconds: int,
    end_seconds: int,
    channels: Optional[list[str]] = None,
) -> dict:
    """Fetch 1Hz raw metrics for a [start_seconds, end_seconds) sec_offset
    window. Auto-downsamples if window > 200s."""
    if start_seconds is None or end_seconds is None:
        return {"error": "start_seconds and end_seconds are required (integers)"}
    if end_seconds <= start_seconds:
        return {"error": f"end_seconds ({end_seconds}) must be > start_seconds ({start_seconds})"}

    with db.connect() as conn:
        # Validate window is within activity range
        max_sec_row = conn.execute(
            "SELECT MAX(sec_offset) FROM activity_metrics WHERE activity_id = ?",
            (activity_id,)
        ).fetchone()
        if not max_sec_row or max_sec_row[0] is None:
            return {"error": "no metrics data for this activity (lazy-fetch may be pending)"}
        max_sec = max_sec_row[0]
        if start_seconds > max_sec:
            return {
                "error": f"start_seconds ({start_seconds}) beyond activity end ({max_sec}s)"
            }
        # Clip end to activity end (not error — natural clamp)
        end_seconds = min(end_seconds, max_sec + 1)

        return _fetch_window(conn, activity_id, start_seconds, end_seconds, channels)


def get_raw_window_by_distance(
    activity_id: int,
    start_meters: float,
    end_meters: float,
    channels: Optional[list[str]] = None,
) -> dict:
    """Fetch 1Hz raw metrics for a distance window — useful for "末 500m",
    "km 18-20", "前 5km" style queries. Internally resolves the distance
    pair into a sec_offset window via distance_cum_m, then delegates."""
    if start_meters is None or end_meters is None:
        return {"error": "start_meters and end_meters are required"}
    if end_meters <= start_meters:
        return {"error": f"end_meters ({end_meters}) must be > start_meters ({start_meters})"}

    with db.connect() as conn:
        # Check distance_cum_m availability (legacy backfilled data may lack it)
        has_dist_row = conn.execute(
            "SELECT MAX(distance_cum_m) FROM activity_metrics "
            "WHERE activity_id = ? AND distance_cum_m IS NOT NULL",
            (activity_id,)
        ).fetchone()
        if not has_dist_row or has_dist_row[0] is None:
            return {
                "error": "no distance_cum_m data for this activity — "
                         "use get_raw_window_by_time instead"
            }
        max_dist_m = has_dist_row[0]
        if start_meters > max_dist_m:
            return {
                "error": f"start_meters ({start_meters}) beyond activity total "
                         f"distance ({max_dist_m:.0f}m)"
            }

        # Find sec_offset where distance_cum_m crosses the start/end markers
        start_sec_row = conn.execute(
            "SELECT MIN(sec_offset) FROM activity_metrics "
            "WHERE activity_id = ? AND distance_cum_m >= ?",
            (activity_id, start_meters)
        ).fetchone()
        end_sec_row = conn.execute(
            "SELECT MIN(sec_offset) FROM activity_metrics "
            "WHERE activity_id = ? AND distance_cum_m >= ?",
            (activity_id, end_meters)
        ).fetchone()

        start_sec = start_sec_row[0] if start_sec_row else None
        end_sec   = end_sec_row[0]   if end_sec_row   else None
        if start_sec is None:
            return {"error": "couldn't resolve start_meters to sec_offset"}
        if end_sec is None:
            # End beyond data → clip to activity end
            end_sec_row = conn.execute(
                "SELECT MAX(sec_offset) FROM activity_metrics WHERE activity_id = ?",
                (activity_id,)
            ).fetchone()
            end_sec = end_sec_row[0] + 1

        result = _fetch_window(conn, activity_id, start_sec, end_sec, channels)
        # Annotate the resolved sec window so LLM sees the conversion
        result["resolved_from_distance"] = {
            "start_meters":  start_meters,
            "end_meters":    end_meters,
            "→ sec_offset":  f"{start_sec}-{end_sec}",
        }
        return result


# ── Aggregated stats tool ───────────────────────────────────────────────────

def get_window_stats(
    activity_id: int,
    start: float,
    end: float,
    key_type: str = "time",
) -> dict:
    """Aggregated stats for a window — uses the same primitives the alpha
    builder uses, so builder-baked and tool-fetched numbers match exactly.

    Args:
      start, end: window boundaries (seconds if key_type="time", meters if
        key_type="distance")
      key_type: "time" | "distance"

    Returns one JSON object with HR/pace/cadence/GCT/VR/stride aggregates
    (avg + p10/p50/p90 for HR/pace; avg only for force-decay metrics) plus
    HR-time linear-regression slope within the window. Independent of
    window length — no row-cap shenanigans.
    """
    if start is None or end is None:
        return {"error": "start and end are required"}
    if end <= start:
        return {"error": f"end ({end}) must be > start ({start})"}
    if key_type not in ("time", "distance"):
        return {"error": f"key_type must be 'time' or 'distance', got {key_type!r}"}

    with db.connect() as conn:
        # Resolve window to a sec_offset range
        if key_type == "time":
            start_sec, end_sec = int(start), int(end)
            max_sec_row = conn.execute(
                "SELECT MAX(sec_offset) FROM activity_metrics WHERE activity_id = ?",
                (activity_id,)
            ).fetchone()
            if not max_sec_row or max_sec_row[0] is None:
                return {"error": "no metrics data for this activity"}
            if start_sec > max_sec_row[0]:
                return {"error": f"start ({start_sec}s) beyond activity end ({max_sec_row[0]}s)"}
            end_sec = min(end_sec, max_sec_row[0] + 1)
        else:
            start_sec, end_sec, err = _resolve_distance_window(conn, activity_id, start, end)
            if err:
                return err

        # Pull rows in primitives' canonical column order + trail-relevant
        # columns (elevation / distance_cum_m for grade; grade_adj_speed for GAP).
        rows = conn.execute("""
            SELECT sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio,
                   stride_cm, elevation_m, distance_cum_m, grade_adj_speed
              FROM activity_metrics
             WHERE activity_id = ?
               AND sec_offset >= ?
               AND sec_offset <  ?
             ORDER BY sec_offset
        """, (activity_id, start_sec, end_sec)).fetchall()

        if not rows:
            return {
                "error": f"no rows in window sec {start_sec}-{end_sec}",
                "window": {"start_sec": start_sec, "end_sec": end_sec},
            }

        # seg_stats expects 7-col canonical input; trim trail cols off.
        canon_rows = [r[:7] for r in rows]
        stats = seg_stats(canon_rows)
        if not stats:
            return {
                "error": "window has no valid HR or speed samples (likely all-paused)",
                "window": {"start_sec": start_sec, "end_sec": end_sec, "rows": len(rows)},
            }

        # Within-window HR drift (regression slope) — useful for "is HR
        # drifting inside Lap3 push?" without needing raw rows.
        drift = hr_drift(canon_rows)

        grade_block = _grade_block(rows)

        return {
            "window": {
                "start_sec":  start_sec,
                "end_sec":    end_sec,
                "duration_s": end_sec - start_sec,
                "key_type":   key_type,
                **({"resolved_from_distance": {"start_m": start, "end_m": end}}
                   if key_type == "distance" else {}),
            },
            "n_samples": stats["n_samples"],
            "hr": {
                "avg":  round(stats["hr_avg"], 1),
                "p10":  round(stats["hr_p10"], 1),
                "p50":  round(stats["hr_p50"], 1),
                "p90":  round(stats["hr_p90"], 1),
                "max":  round(stats["hr_max"], 1),
            },
            "pace": {
                "avg_s_per_km": round(stats["pace_s_per_km"], 1),
                "p10_s_per_km": round(stats["pace_p10_s_per_km"], 1),
                "p50_s_per_km": round(stats["pace_p50_s_per_km"], 1),
                "p90_s_per_km": round(stats["pace_p90_s_per_km"], 1),
            },
            "mechanics": {
                "cadence_avg":  round(stats["cadence_avg"],  1) if stats["cadence_avg"]  is not None else None,
                "gct_avg_ms":   round(stats["gct_avg_ms"],   1) if stats["gct_avg_ms"]   is not None else None,
                "vr_avg_pct":   round(stats["vr_avg_pct"],   2) if stats["vr_avg_pct"]   is not None else None,
                "stride_avg_m": round(stats["stride_avg_m"], 3) if stats["stride_avg_m"] is not None else None,
            },
            "ef":         round(stats["ef"], 5),
            "hr_drift_within_window": ({
                "slope_bpm_per_min": round(drift["slope_per_min"], 3),
                "r_squared":         round(drift["r_squared"],    3),
                "n":                 drift["n"],
            } if drift else None),
            "grade": grade_block,
        }


def _grade_block(rows: list) -> Optional[dict]:
    """Compute grade context for a window: avg grade %, elevation gain/loss,
    and GAP pace (from Garmin's grade_adj_speed when present). Returns None
    if the window has no usable elevation data.

    Row layout: (sec, hr, speed_mps, cadence, gct, vr, stride, elev_m,
                 distance_cum_m, grade_adj_speed).
    """
    elev_rows = [(r[7], r[8]) for r in rows if r[7] is not None and r[8] is not None]
    if len(elev_rows) < 2:
        return None

    elev_start, elev_end = elev_rows[0][0], elev_rows[-1][0]
    dist_start, dist_end = elev_rows[0][1], elev_rows[-1][1]
    span_m = dist_end - dist_start
    if span_m < 5:
        return None

    avg_grade_pct = (elev_end - elev_start) / span_m * 100

    gain = loss = 0.0
    for (e_prev, _), (e_cur, _) in zip(elev_rows, elev_rows[1:]):
        d = e_cur - e_prev
        if d > 0: gain += d
        else:     loss -= d

    # GAP pace from Garmin's grade_adj_speed (m/s) — only counts moving samples
    gas = [r[9] for r in rows if r[9] is not None and r[9] > 0.5]
    gap_s_per_km = (1000.0 / (sum(gas) / len(gas))) if gas else None

    return {
        "avg_grade_pct":  round(avg_grade_pct, 2),
        "elev_gain_m":    round(gain, 1),
        "elev_loss_m":    round(loss, 1),
        "span_m":         round(span_m, 1),
        "gap_pace_s_per_km": round(gap_s_per_km, 1) if gap_s_per_km is not None else None,
    }


def _resolve_distance_window(conn: sqlite3.Connection, activity_id: int,
                             start_m: float, end_m: float):
    """Returns (start_sec, end_sec, None) on success or (None, None, err_dict)."""
    has_dist_row = conn.execute(
        "SELECT MAX(distance_cum_m) FROM activity_metrics "
        "WHERE activity_id = ? AND distance_cum_m IS NOT NULL",
        (activity_id,)
    ).fetchone()
    if not has_dist_row or has_dist_row[0] is None:
        return None, None, {
            "error": "no distance_cum_m data — use key_type='time' instead",
        }
    if start_m > has_dist_row[0]:
        return None, None, {
            "error": f"start_m ({start_m}) beyond activity total ({has_dist_row[0]:.0f}m)",
        }
    start_sec_row = conn.execute(
        "SELECT MIN(sec_offset) FROM activity_metrics "
        "WHERE activity_id = ? AND distance_cum_m >= ?",
        (activity_id, start_m)
    ).fetchone()
    end_sec_row = conn.execute(
        "SELECT MIN(sec_offset) FROM activity_metrics "
        "WHERE activity_id = ? AND distance_cum_m >= ?",
        (activity_id, end_m)
    ).fetchone()
    start_sec = start_sec_row[0] if start_sec_row else None
    end_sec   = end_sec_row[0]   if end_sec_row   else None
    if start_sec is None:
        return None, None, {"error": "couldn't resolve start_m to sec_offset"}
    if end_sec is None:
        max_row = conn.execute(
            "SELECT MAX(sec_offset) FROM activity_metrics WHERE activity_id = ?",
            (activity_id,)
        ).fetchone()
        end_sec = max_row[0] + 1
    return start_sec, end_sec, None


# ── Shared fetch + downsample logic ─────────────────────────────────────────

def _fetch_window(
    conn: sqlite3.Connection,
    activity_id: int,
    start_sec: int,
    end_sec: int,
    channels: Optional[list[str]],
) -> dict:
    """Returns {sampling, rows, window: {start_sec, end_sec}, data: [...]}"""
    # Validate / default channel list
    if not channels:
        channels = list(ALL_CHANNELS)
    else:
        invalid = [c for c in channels if c not in _CHANNEL_TO_COL]
        if invalid:
            return {
                "error": f"unknown channel(s): {invalid}. "
                         f"Valid: {sorted(ALL_CHANNELS)}"
            }

    # Pick downsampling step based on window length
    window_s = end_sec - start_sec
    if window_s <= WIN_NATIVE_MAX_S:
        step_s, sampling_label = 1, "1s native"
    elif window_s <= WIN_3S_AVG_MAX_S:
        step_s, sampling_label = 3, "3s avg"
    else:
        step_s, sampling_label = 6, "6s avg"

    # Build SELECT for requested channels
    cols_sql = ", ".join(_CHANNEL_TO_COL[c] for c in channels)
    rows = conn.execute(
        f"""SELECT sec_offset, {cols_sql}
              FROM activity_metrics
             WHERE activity_id = ?
               AND sec_offset >= ?
               AND sec_offset <  ?
             ORDER BY sec_offset""",
        (activity_id, start_sec, end_sec)
    ).fetchall()

    if not rows:
        return {
            "error": f"no rows in window sec {start_sec}-{end_sec}",
            "window": {"start_sec": start_sec, "end_sec": end_sec},
        }

    # Apply step downsampling: bucket rows into [start, start+step), avg
    data = []
    if step_s == 1:
        # No bucketing — return raw rows
        for r in rows:
            entry = {"sec": r[0]}
            for i, ch in enumerate(channels, start=1):
                entry[ch] = _round(r[i])
            data.append(entry)
    else:
        bucket_start = (start_sec // step_s) * step_s
        while bucket_start < end_sec:
            bucket_end = bucket_start + step_s
            in_b = [r for r in rows if bucket_start <= r[0] < bucket_end]
            if in_b:
                entry = {"sec": bucket_start}
                for i, ch in enumerate(channels, start=1):
                    vals = [r[i] for r in in_b if r[i] is not None]
                    entry[ch] = _round(sum(vals) / len(vals)) if vals else None
                data.append(entry)
            bucket_start = bucket_end

    return {
        "sampling": sampling_label,
        "rows":     len(data),
        "window":   {"start_sec": start_sec, "end_sec": end_sec, "duration_s": window_s},
        "channels": channels,
        "data":     data,
    }


def _round(v):
    """Round numeric values to keep response compact. Speed gets 2 decimals
    (m/s precision matters for pace), HR / cadence / GCT round to int."""
    if v is None:
        return None
    if isinstance(v, float):
        return round(v, 2)
    return v


# ── OpenAI-format tool schemas ──────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_raw_window_by_time",
            "description": (
                "Fetch 1Hz raw metric data (HR, speed, cadence, GCT, VR, stride, "
                "power, elevation) for a time window within the current activity. "
                "USE WHEN: user asks about specific seconds/minutes "
                "(e.g. 'HR around minute 30', 'last 60 seconds', 'tail of Lap 4'). "
                "For lap-based questions, read the sec range from the builder's "
                "lap header (e.g. 'Lap 4 (sec 2096-2284)') and pass the appropriate "
                "sub-range. Window > 200s auto-downsamples to 3s/6s avg."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_seconds": {
                        "type": "integer",
                        "description": "Start of window (sec_offset, 0 = activity start)",
                    },
                    "end_seconds": {
                        "type": "integer",
                        "description": "End of window (exclusive)",
                    },
                    "channels": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(ALL_CHANNELS),
                        },
                        "description": "Optional. Default: all channels.",
                    },
                },
                "required": ["start_seconds", "end_seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_window_stats",
            "description": (
                "Aggregated stats (HR avg/percentiles, pace avg/percentiles, "
                "cadence/GCT/VR/stride avg, HR-time drift slope within window) "
                "for an arbitrary window. USE WHEN you want summary numbers for a "
                "custom window — e.g. 'Lap3 first half vs second half' (call twice "
                "with the lap split into two), 'last 5km of push segment', or any "
                "window the user's comment defines that doesn't match builder's "
                "pre-baked per-lap / per-km slices. Numbers match builder output "
                "exactly (same primitives). Cheaper than get_raw_window_by_time "
                "when you only need aggregates, not the time series. "
                "Also returns a `grade` block (avg grade %, elevation gain/loss, "
                "GAP pace) when the window has elevation data — primary tool for "
                "trail drill-down where every pace/HR number needs grade context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "number",
                        "description": "Start of window. Seconds (sec_offset) if "
                                       "key_type='time', meters if key_type='distance'.",
                    },
                    "end": {
                        "type": "number",
                        "description": "End of window (exclusive).",
                    },
                    "key_type": {
                        "type": "string",
                        "enum": ["time", "distance"],
                        "description": "Default 'time'. Use 'distance' for km-based "
                                       "windows like 'first 5km' or 'km 18-21'.",
                    },
                },
                "required": ["start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_raw_window_by_distance",
            "description": (
                "Fetch 1Hz raw metric data for a distance window within the current "
                "activity. USE WHEN: user asks about distance markers "
                "(e.g. 'pace at km 18-20', 'final 500 m', 'first 5 km'). "
                "Internally resolves to a sec_offset window via distance_cum_m. "
                "Returns 'error' if activity lacks distance_cum_m (legacy data) — "
                "fall back to get_raw_window_by_time in that case."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_meters": {
                        "type": "number",
                        "description": "Start distance in meters from activity start",
                    },
                    "end_meters": {
                        "type": "number",
                        "description": "End distance in meters",
                    },
                    "channels": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": list(ALL_CHANNELS),
                        },
                        "description": "Optional. Default: all channels.",
                    },
                },
                "required": ["start_meters", "end_meters"],
            },
        },
    },
    _WEB_SEARCH_SCHEMA,
]


def make_tool_handlers(activity_id: int) -> dict:
    """Return {tool_name → bound handler} for the current activity. Used by
    `llm_stream_with_tools` to dispatch tool calls to Python implementations.
    activity_id is captured here, not exposed to the LLM."""
    return {
        "get_raw_window_by_time": lambda **kw:
            get_raw_window_by_time(activity_id, **kw),
        "get_raw_window_by_distance": lambda **kw:
            get_raw_window_by_distance(activity_id, **kw),
        "get_window_stats": lambda **kw:
            get_window_stats(activity_id, **kw),
        "web_search": lambda **kw: web_search.search(**kw),
    }


# ── Overall-chat tools (cross-activity) ─────────────────────────────────────
# Used by the main-page 教练分析 chat, which has access to all 90-day
# activities but no per-activity 1Hz drill (that's the review chat's job).
#
# The LLM uses these to resolve fuzzy descriptions like "上周长距离" / "去年
# 墨马" to specific activity_ids, then pull the full typed-builder report
# for each and diff them.

def find_activities(
    tag:           Optional[str] = None,
    name_contains: Optional[str] = None,
    date_from:     Optional[str] = None,
    date_to:       Optional[str] = None,
    limit:         int           = 10,
) -> dict:
    """Search activities by criteria. Returns minimal records — use
    get_activity_report(activity_id) to drill into one."""
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        return {"error": "limit must be an integer 1-50"}

    sql_parts = ["""
        SELECT a.activity_id, a.activity_name, a.activity_type_key,
               a.start_time_local, a.distance_m, a.duration_s,
               a.average_hr, a.elevation_gain_m,
               t.tag, c.comment
          FROM activities a
          LEFT JOIN user_activity_tags     t ON t.activity_id = a.activity_id
          LEFT JOIN user_activity_comments c ON c.activity_id = a.activity_id
         WHERE 1=1
    """]
    params: list = []
    if tag:
        sql_parts.append(" AND t.tag = ?")
        params.append(tag)
    if name_contains:
        sql_parts.append(" AND lower(a.activity_name) LIKE lower(?)")
        params.append(f"%{name_contains}%")
    if date_from:
        sql_parts.append(" AND date(a.start_time_local) >= date(?)")
        params.append(date_from)
    if date_to:
        sql_parts.append(" AND date(a.start_time_local) <= date(?)")
        params.append(date_to)
    sql_parts.append(" ORDER BY a.start_time_local DESC LIMIT ?")
    params.append(limit)

    with db.connect() as conn:
        rows = conn.execute("".join(sql_parts), params).fetchall()

    matches = []
    for r in rows:
        aid, name, type_key, start, dist, dur, hr, elev, atag, acomment = r
        comment_preview = ""
        if acomment:
            comment_preview = (acomment[:120] + "...") if len(acomment) > 120 else acomment
        matches.append({
            "activity_id":      aid,
            "date":             start[:10] if start else None,
            "name":             name or "",
            "tag":              atag or "",
            "comment_preview":  comment_preview,
            "type":             type_key or "",
            "distance_km":      round(dist / 1000, 2) if dist else None,
            "duration_min":     int(dur / 60) if dur else None,
            "avg_hr":           int(hr) if hr else None,
            "elevation_gain_m": int(elev) if elev else None,
        })
    return {"n_matches": len(matches), "matches": matches}


def get_activity_report(activity_id: int) -> dict:
    """Get the full typed-builder report (markdown) for ONE activity. This
    is the same content the user sees in 🔬 复盘 for that activity — use
    it to do cross-activity comparison from the overall chat (call twice
    for two activities, then diff in your reply).

    For 1Hz raw-second drill (HR at sec 1234, power at rep 4 末 5s),
    redirect the user to the 🔬 复盘 page — those tools live there."""
    try:
        activity_id = int(activity_id)
    except (TypeError, ValueError):
        return {"error": f"activity_id must be an integer, got {activity_id!r}"}

    # Lazy import to avoid touching review_builders (and user_config) until
    # this tool actually runs. Keeps review_tools import-cycle free.
    from review_builders import dispatch

    with db.connect() as conn:
        meta_row = conn.execute("""
            SELECT a.activity_name, a.activity_type_key, a.start_time_local,
                   t.tag, c.comment
              FROM activities a
              LEFT JOIN user_activity_tags     t ON t.activity_id = a.activity_id
              LEFT JOIN user_activity_comments c ON c.activity_id = a.activity_id
             WHERE a.activity_id = ?
        """, (activity_id,)).fetchone()
        if not meta_row:
            return {"error": f"activity {activity_id} not found in SQLite. "
                             "Make sure the activity_id was copied verbatim from "
                             "a find_activities() result — Garmin activity_ids "
                             "are 11-digit integers (e.g. 22826133198), do NOT "
                             "truncate or guess. If you don't have the exact id, "
                             "call find_activities() first."}
        name, type_key, start, tag, comment = meta_row

        builder = dispatch(tag or "", type_key or "")
        result  = builder.build(activity_id, conn)

    return {
        "activity_id": activity_id,
        "date":        start[:10] if start else None,
        "name":        name or "",
        "tag":         tag or "",
        "comment":     comment or "",
        "type":        type_key or "",
        "builder":     builder.name,
        "context_md":  result.context_md,
    }


# Allowed metrics for get_metric_trend. Per-activity ones are direct columns
# in the `activities` table; weekly ones are computed by ISO-week aggregation.
_PER_ACTIVITY_METRICS = {
    "vo2max":        "vo2max",
    "training_load": "training_load",
    "aerobic_te":    "aerobic_te",
    "anaerobic_te":  "anaerobic_te",
    "avg_hr":        "average_hr",
}
_WEEKLY_METRICS = ("weekly_run_km", "weekly_load")


def get_metric_trend(metric: str, days: int = 90) -> dict:
    """Cross-activity time series of a single metric over the last N days.

    Per-activity metrics (one sample per qualifying activity):
      - vo2max
      - training_load
      - aerobic_te / anaerobic_te
      - avg_hr

    Weekly aggregates (one sample per ISO week with activities):
      - weekly_run_km   (sum of distance over running-type activities)
      - weekly_load     (sum of training_load over all activities)
    """
    try:
        days = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        return {"error": "days must be an integer 1-365"}

    if metric in _PER_ACTIVITY_METRICS:
        col = _PER_ACTIVITY_METRICS[metric]
        with db.connect() as conn:
            rows = conn.execute(f"""
                SELECT activity_id, start_time_local, activity_type_key, {col}
                  FROM activities
                 WHERE {col} IS NOT NULL
                   AND date(start_time_local) >= date('now', '-{int(days)} days')
                 ORDER BY start_time_local ASC
            """).fetchall()
        samples = [
            {
                "date":         r[1][:10] if r[1] else None,
                "activity_id":  r[0],
                "type":         r[2] or "",
                "value":        round(r[3], 2) if isinstance(r[3], float) else r[3],
            }
            for r in rows
        ]
        return {"metric": metric, "days": days, "kind": "per_activity",
                "n_samples": len(samples), "samples": samples}

    if metric in _WEEKLY_METRICS:
        from collections import defaultdict
        from datetime import date as _date

        with db.connect() as conn:
            rows = conn.execute(f"""
                SELECT start_time_local, distance_m, training_load,
                       activity_type_key
                  FROM activities
                 WHERE date(start_time_local) >= date('now', '-{int(days)} days')
            """).fetchall()

        weeks: dict[str, dict] = defaultdict(lambda: {"run_km": 0.0, "load": 0.0,
                                                       "activities": 0})
        for start, dist, load, type_key in rows:
            if not start:
                continue
            try:
                d = _date.fromisoformat(start[:10])
            except ValueError:
                continue
            iso_year, iso_week, _ = d.isocalendar()
            wk = f"{iso_year}-W{iso_week:02d}"
            if "run" in (type_key or ""):
                weeks[wk]["run_km"] += (dist or 0) / 1000
            weeks[wk]["load"] += load or 0
            weeks[wk]["activities"] += 1

        samples = []
        for wk in sorted(weeks.keys()):
            val = weeks[wk]["run_km"] if metric == "weekly_run_km" else weeks[wk]["load"]
            samples.append({
                "week":       wk,
                "value":      round(val, 1),
                "activities": weeks[wk]["activities"],
            })
        return {"metric": metric, "days": days, "kind": "weekly",
                "n_samples": len(samples), "samples": samples}

    allowed = list(_PER_ACTIVITY_METRICS) + list(_WEEKLY_METRICS)
    return {"error": f"unknown metric {metric!r}. Allowed: {allowed}"}


OVERALL_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "find_activities",
            "description": (
                "Search activities by tag / name / date range. Use this to "
                "resolve fuzzy descriptions ('last week's long run', 'last "
                "year's Melbourne marathon', 'most recent intervals') to "
                "specific activity_ids. Returns minimal records — use "
                "get_activity_report(activity_id) to drill into one. Ordered "
                "by date DESC. All filters are AND-combined. Tag must be "
                "exact (one of the stable taxonomy keys). name_contains is "
                "substring + case-insensitive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Exact stable tag key, e.g. 'long_run', 'race', "
                                       "'intervals', 'hill', 'tempo', 'threshold', "
                                       "'aerobic_base', 'aerobic_recovery', 'trail'. "
                                       "Omit to match any tag.",
                    },
                    "name_contains": {
                        "type": "string",
                        "description": "Case-insensitive substring of activity name.",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD, inclusive lower bound.",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD, inclusive upper bound.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 10, capped at 50).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_activity_report",
            "description": (
                "Get the FULL typed-builder report (markdown) for ONE activity. "
                "Same content the user sees in 🔬 Review for that activity — "
                "includes per-lap table, per-km, drift readings, mechanics "
                "deltas, etc. (specific to the activity's tag: aerobic / long "
                "/ tempo / intervals / race / hill / trail). Use this for "
                "cross-activity comparison from the overall chat — call twice "
                "for two activities then diff. "
                "For 1Hz raw-second drill (HR at sec 1234, last 5 s of rep 4 power), "
                "redirect the user to 🔬 Review — those tools live there."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "activity_id": {
                        "type": "integer",
                        "description": "From find_activities() results or the "
                                       "context's recent-activities list.",
                    },
                },
                "required": ["activity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metric_trend",
            "description": (
                "Cross-activity time series of a single metric over the last N "
                "days. Per-activity metrics return one sample per qualifying "
                "activity (date, activity_id, value); weekly metrics return one "
                "sample per ISO week (week, value, activities count). "
                "Use this for trend questions the baked context doesn't surface "
                "explicitly (e.g. 'VO2max trend over 6 months', 'weekly run km "
                "ramp', 'training load progression')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "metric": {
                        "type": "string",
                        "enum": list(_PER_ACTIVITY_METRICS) + list(_WEEKLY_METRICS),
                        "description": "Metric to trend.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Lookback window in days (default 90, "
                                       "min 1, max 365).",
                    },
                },
                "required": ["metric"],
            },
        },
    },
    _WEB_SEARCH_SCHEMA,
]


def make_overall_tool_handlers() -> dict:
    """Return {tool_name → handler} for the overall (main-page) chat.
    No activity_id binding — these tools operate cross-activity."""
    return {
        "find_activities":     find_activities,
        "get_activity_report": get_activity_report,
        "get_metric_trend":    get_metric_trend,
        "web_search":          lambda **kw: web_search.search(**kw),
    }
