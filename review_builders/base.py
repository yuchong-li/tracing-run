"""Base class + helpers for ReviewBuilder."""

import ast
import hashlib
import inspect
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime


# ── Lap window resolution from absolute timestamps ──────────────────────────
# CRITICAL: don't use cumulative `lap.duration_s` to compute lap windows on
# the metrics sec_offset axis. duration_s is **moving time** (excludes
# Garmin Auto Pause). sec_offset is **wall-clock from activity start**. If
# the user paused (Auto Pause at lights, manual pause at aid station), the
# two clocks drift apart by the cumulative pause duration.
#
# Empirically observed drift on test activities: 16-614 seconds. Activities
# with small drift produce slightly wrong per-rep stats; activities with
# large drift produce completely wrong stats (rest segment mistakenly
# windowed onto a work rep, etc.).
#
# Fix: each lap's `raw_lap_json` has `startTimeGMT` (absolute wall-clock
# timestamp). Compare to activities.start_time_gmt to get the true
# sec_offset window per lap. End of lap N = start of lap N+1
# (chronological); for last lap, fall back to start + duration_s.

def _parse_iso_garmin(s: str):
    """Parse Garmin's GMT timestamps like '2026-05-04T20:11:08.0' or
    '2026-05-04T20:11:08'. Returns datetime or None."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def lap_windows_from_db(conn: sqlite3.Connection,
                        activity_id: int) -> list[dict]:
    """Return list of {lap_id, start_sec, end_sec, dist_m, dur_s} where
    start_sec / end_sec are aligned to the activity_metrics sec_offset axis.

    Uses raw_lap_json's startTimeGMT (wall-clock, includes pause time
    correctly), not cumulative duration_s. Returns empty list if any lap
    is missing startTimeGMT (caller should fall back gracefully)."""
    act_row = conn.execute(
        "SELECT start_time_gmt FROM activities WHERE activity_id = ?",
        (activity_id,)
    ).fetchone()
    if not act_row or not act_row[0]:
        return []
    act_start = _parse_iso_garmin(act_row[0])
    if act_start is None:
        return []

    rows = conn.execute("""
        SELECT lap_index, distance_m, duration_s, raw_lap_json
          FROM activity_laps
         WHERE activity_id = ?
         ORDER BY lap_index
    """, (activity_id,)).fetchall()
    if not rows:
        return []

    laps = []
    for li, dist, dur, raw_json in rows:
        if not raw_json:
            return []
        try:
            d = json.loads(raw_json)
        except Exception:
            return []
        lap_start_dt = _parse_iso_garmin(d.get("startTimeGMT", ""))
        if lap_start_dt is None:
            return []
        start_sec = int((lap_start_dt - act_start).total_seconds())
        laps.append({
            'lap_id':    li + 1,
            'start_sec': start_sec,
            'dur_s':     int(dur) if dur else 0,
            'dist_m':    float(dist) if dist else 0.0,
        })

    # End of lap N = start of lap N+1; for last lap, use start + duration_s
    # (last lap can't overlap with anything after, and dur_s won't drift if
    # the activity wasn't paused inside the last lap).
    for i, lap in enumerate(laps):
        if i + 1 < len(laps):
            lap['end_sec'] = laps[i + 1]['start_sec']
        else:
            lap['end_sec'] = lap['start_sec'] + lap['dur_s']
    return laps


# ── Shared lap-structure heuristic (used across typed builders) ─────────────
# Manual vs auto lap detection matters for many tags:
#   LongRun  — manual lap → segment by lap; auto → equal-time thirds
#   Interval — manual lap often marks rep boundaries (cross-ref intensity_type)
#   Tempo    — manual lap may delimit warmup / main set / cooldown
#   Hill     — manual lap may delimit individual reps
#   Trail    — manual lap often marks aid station / nav transition / surface change
#   Race     — every lap press changes the runner's pacing micro-behavior
# Centralizing the detection here means all builders agree on the rule and a
# fix (e.g. mile auto-lap support) lands in one place.

def is_manual_lap_structure(lap_distances: list[float]) -> bool:
    """Whether lap distances suggest user-imposed (manual) segmentation
    rather than device auto-lap (per km / per mile).

    Heuristic — based on internal consistency of non-final lap distances,
    NOT a hardcoded baseline. Auto-lap (km or mile) produces near-identical
    lap distances; manual lap produces whatever the user pressed.

    Returns False (= auto-lap or no segmentation) for:
      - <2 laps total
      - non-final laps with low variability (CV < 5%) AND mean near a
        known device unit (1000m km, or 1609m mile)
    Returns True (= manual) for:
      - high CV across non-final laps (non-uniform lap presses)
      - uniform non-final laps at a non-standard distance (e.g. user
        manually pressed lap every 5km)

    Edge case knowingly accepted: if a user manually presses lap exactly
    every 1km or every mile, this returns False (judged as auto). That
    pattern is rare and the activity-type fallback (e.g. equal-time thirds
    for long run) still produces sensible analysis.
    """
    if len(lap_distances) < 2:
        return False

    full = lap_distances[:-1]   # exclude final lap (often partial)
    if not full:
        return False

    mean = sum(full) / len(full)
    if mean <= 0:
        return False

    if len(full) >= 2:
        var = sum((d - mean) ** 2 for d in full) / len(full)
        cv  = var ** 0.5 / mean
        if cv > 0.05:
            return True   # non-uniform → user pressed lap at chosen moments

    # Low CV (or only 1 non-final lap to judge): auto only if mean is near
    # a known device unit; otherwise treat as manual (uniform but unusual).
    near_km   = abs(mean - 1000) / 1000 < 0.10
    near_mile = abs(mean - 1609) / 1609 < 0.10
    return not (near_km or near_mile)


@dataclass
class BuildResult:
    """What every builder emits.

    Attributes:
      context_md: markdown text fed to the coach LLM as a user message.
      highlight_windows: list of drill-zone hints, each a dict like
        {"label": str, "kind": str, "start_s": int, "end_s": int,
         "channels": list[str], "metric_summary": dict}
        Used by the UI to render drill-down buttons and by the LLM system
        prompt to know what segments are interesting to bring up.
      builder_hash: AST-derived signature of the builder source; bumps
        when builder logic changes so cached contexts can be invalidated.
    """
    context_md: str
    highlight_windows: list[dict] = field(default_factory=list)
    builder_hash: str = ""


class ReviewBuilder:
    """Abstract base. Subclasses implement build()."""

    name: str = "BaseBuilder"

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        """Whether this builder should handle a (tag, activity_type) combo.
        DefaultBuilder always returns True; typed builders narrow this."""
        return False

    def build(self, activity_id: int, conn) -> BuildResult:
        """Produce the BuildResult for one activity from a SQLite connection."""
        raise NotImplementedError

    @classmethod
    def builder_hash(cls) -> str:
        """AST-based hash of the builder class source. Insensitive to
        whitespace and comments — only logical changes bump the hash, so
        cached contexts won't be invalidated by reformatting."""
        try:
            src  = inspect.getsource(cls)
            tree = ast.parse(src)
            return hashlib.sha256(ast.dump(tree).encode()).hexdigest()[:16]
        except Exception:
            # Fallback if source isn't introspectable (shouldn't happen for
            # normal class defs, but covers exec'd / dynamic classes)
            return hashlib.sha256(cls.__qualname__.encode()).hexdigest()[:16]
