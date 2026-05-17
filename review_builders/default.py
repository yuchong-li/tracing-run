"""DefaultBuilder — fallback context builder for `other` and `— untagged —` tags.

Properties:
- Uses full-resolution metrics (no thinning) — gives the LLM the richest
  possible aggregate stats for atypical activities
- Time-based bucketing in the timeline section (60s / 180s / 300s / 600s
  scales with activity duration to stay around 20-30 buckets)
- HR Z4+/Z5+ thresholds read from the user's actual `activity_hr_zones`
  data (not hardcoded)
- "Similar activities" comparison filtered to those BEFORE the current
  activity's date (avoids time-traveling references)
- Lap-awareness header detects manual vs auto-1km laps and steers the LLM
  to look at user comments for manual lap meaning

Output language: context_md is emitted in **neutral English**. The LLM's
response language is steered by the per-tag prompt (P5), not by the
builder. This keeps the builder code base single-source.
"""

import json
import sqlite3
from datetime import date, datetime
from typing import Any

import garmin_data as gd
import i18n
import user_config as uc

from review_builders.base import BuildResult, ReviewBuilder, is_manual_lap_structure


class DefaultBuilder(ReviewBuilder):
    name = "DefaultBuilder"

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return True

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        act     = _load_activity(conn, activity_id)
        if act is None:
            return BuildResult(context_md="", builder_hash=self.builder_hash())

        detail  = _load_detail(conn, activity_id)
        all_acts = _load_all_activities(conn)

        cfg     = uc.load()
        manual_tag = uc.get_activity_tag(cfg, activity_id)
        comment    = uc.get_activity_comment(cfg, activity_id)

        ctx_lines = _build_review_ctx(act, detail, all_acts, manual_tag, comment)
        timeline  = _build_metrics_timeline(act, detail)
        if timeline:
            ctx_lines += timeline

        return BuildResult(
            context_md=ctx_lines,
            highlight_windows=[],   # DefaultBuilder doesn't pre-bake highlights
            builder_hash=self.builder_hash(),
        )


# ── SQLite → JSON-shape adapters ──────────────────────────────────────────────
# These materialize the dict shapes the legacy logic expects (see ui/pages/coach.py
# _build_review_ctx around lines 951-1100).

def _load_activity(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    """Reconstruct the activity dict the legacy code passes as `act`.
    Field names match what _parse_activity emits (camelCase, e.g.
    activityTypeKey, averageSpeed, etc.)."""
    row = conn.execute("""
        SELECT activity_id, activity_name, activity_type_key, start_time_local,
               distance_m, duration_s, average_hr, max_hr, average_speed_mps,
               elevation_gain_m, elevation_loss_m, calories, training_load,
               aerobic_te, anaerobic_te, te_label, vo2max
          FROM activities WHERE activity_id = ?
    """, (activity_id,)).fetchone()
    if not row:
        return None
    return {
        "activityId":              row[0],
        "activityName":            row[1] or "",
        "activityTypeKey":         row[2] or "",
        "startTimeLocal":          row[3] or "",
        "distance":                row[4],
        "duration":                row[5],
        "averageHR":               row[6],
        "maxHR":                   row[7],
        "averageSpeed":            row[8],
        "elevationGain":           row[9],
        "elevationLoss":           row[10],
        "calories":                row[11],
        "activityTrainingLoad":    row[12],
        "aerobicTrainingEffect":   row[13],
        "anaerobicTrainingEffect": row[14],
        "trainingEffectLabel":     row[15] or "",
        "vO2MaxValue":             row[16],
    }


def _load_all_activities(conn: sqlite3.Connection) -> list[dict]:
    """Reconstruct the all_acts list (used for "similar activities" comparison).
    Sort matches the legacy: most-recent first."""
    rows = conn.execute("""
        SELECT activity_id, activity_type_key, start_time_local,
               distance_m, average_hr, average_speed_mps, aerobic_te
          FROM activities
         ORDER BY start_time_local DESC
    """).fetchall()
    return [
        {
            "activityId":            r[0],
            "activityTypeKey":       r[1] or "",
            "startTimeLocal":        r[2] or "",
            "distance":              r[3],
            "averageHR":             r[4],
            "averageSpeed":          r[5],
            "aerobicTrainingEffect": r[6],
        }
        for r in rows
    ]


def _load_detail(conn: sqlite3.Connection, activity_id: int) -> dict:
    """Reconstruct the detail dict (laps + splits + hr_zones + metrics)
    in the JSON cache shape so the legacy logic ports cleanly."""
    summary_row = conn.execute(
        "SELECT raw_summary_json FROM activities WHERE activity_id = ?",
        (activity_id,),
    ).fetchone()
    summary = json.loads(summary_row[0]) if summary_row and summary_row[0] else {}

    # Laps — store full dict in raw_lap_json so legacy code (which reads
    # arbitrary lap fields like averageRunCadence, groundContactTime, etc.)
    # gets the same data shape.
    lap_rows = conn.execute("""
        SELECT raw_lap_json FROM activity_laps
         WHERE activity_id = ? ORDER BY lap_index
    """, (activity_id,)).fetchall()
    laps = [json.loads(r[0]) for r in lap_rows if r[0]]

    # Splits aggregated by type
    split_rows = conn.execute("""
        SELECT raw_split_json FROM activity_splits WHERE activity_id = ?
    """, (activity_id,)).fetchall()
    splits = [json.loads(r[0]) for r in split_rows if r[0]]

    hr_zones = [
        {"zoneNumber": r[0], "secsInZone": r[1], "zoneLowBoundary": r[2]}
        for r in conn.execute("""
            SELECT zone_number, secs_in_zone, zone_low_boundary
              FROM activity_hr_zones WHERE activity_id = ? ORDER BY zone_number
        """, (activity_id,)).fetchall()
    ]

    # Metrics — assemble parallel arrays in the legacy ts/hr/speed/etc. shape.
    # If SQLite holds 1Hz data (fresh fetch), thin to ~10s; if it's already
    # at ~10s (legacy backfill), thinning is a no-op.
    metrics = _materialize_metrics(conn, activity_id)

    return {
        "summary":  summary,
        "laps":     laps,
        "splits":   splits,
        "hr_zones": hr_zones,
        "metrics":  metrics,
    }


def _materialize_metrics(conn: sqlite3.Connection, activity_id: int) -> dict:
    """Build the {sec, ts, hr, speed, elev, cadence, power, stride, gct,
    vert_osc, vert_ratio} parallel-array dict.

    No longer thinned — all sample-rate-independent stats (averages,
    percentiles, fraction-above-threshold) work fine on full 1Hz data,
    and the timeline section now uses time-based bucketing so it doesn't
    care about sample density either."""
    rows = conn.execute("""
        SELECT sec_offset, ts_ms, hr, speed_mps, elevation_m, cadence_spm,
               power_w, stride_cm, gct_ms, vert_osc_cm, vert_ratio
          FROM activity_metrics
         WHERE activity_id = ?
         ORDER BY sec_offset
    """, (activity_id,)).fetchall()
    if not rows:
        return {}

    out: dict[str, list] = {k: [] for k in
        ("sec", "ts", "hr", "speed", "elev", "cadence", "power",
         "stride", "gct", "vert_osc", "vert_ratio")}
    for r in rows:
        out["sec"].append(r[0])         # sec_offset (wall-clock from start)
        out["ts"].append(r[1])
        out["hr"].append(r[2])
        out["speed"].append(r[3])
        out["elev"].append(r[4])
        out["cadence"].append(r[5])
        out["power"].append(r[6])
        out["stride"].append(r[7])
        out["gct"].append(r[8])
        out["vert_osc"].append(r[9])
        out["vert_ratio"].append(r[10])
    return out


# ── Context-building logic ──────────────────────────────────────────────────

def _build_review_ctx(act: dict, detail: dict, all_acts: list,
                      manual_tag: str = "", comment: str = "") -> str:
    tk   = act.get("activityTypeKey", "")
    # Always render activity-type label in neutral English for the LLM context
    # regardless of the user's UI locale (output language is set by the prompt).
    typ  = i18n.t(f"activity_type.{tk}", lang="en-US") if tk else "Activity"
    dt   = act.get("startTimeLocal", "")[:10]
    name = act.get("activityName", "")
    dist = (act.get("distance") or 0) / 1000
    lines = [f"# Workout data: {dt} {typ}" + (f"  ({name})" if name else "")]
    if manual_tag:
        # manual_tag is a stable key (e.g. 'aerobic_base'); render the EN label.
        tag_en = i18n.t(f"tag.{manual_tag}", lang="en-US")
        lines.append(f"**⚑ User-tagged workout type: [{tag_en}]** "
                     "(this tag is authoritative — use it to infer training "
                     "intent; do not re-derive from splits)")
    if comment:
        lines.append(f"**📋 Workout plan / user notes:**\n{comment}")
    lines.append("## Summary")
    lines.append(f"- Distance: {dist:.2f} km | Duration: {gd.format_duration(act.get('duration'))}")
    if "run" in tk and act.get("averageSpeed"):
        lines.append(f"- Avg pace: {gd.format_pace(act['averageSpeed'])}")
    lines.append(f"- Avg HR: {act.get('averageHR')} bpm | Max HR: {act.get('maxHR')} bpm")
    if act.get("elevationGain"):
        lines.append(f"- Elev gain: +{act['elevationGain']:.0f} m | Elev loss: {act.get('elevationLoss',0):.0f} m")
    if act.get("calories"):
        lines.append(f"- Calories: {act['calories']} kcal")
    ae = act.get("aerobicTrainingEffect")
    an = act.get("anaerobicTrainingEffect")
    te = act.get("trainingEffectLabel", "")
    if ae:
        lines.append(f"- Aerobic TE (Training Effect): {ae}/5.0 | Anaerobic TE: {an or '—'}/5.0"
                     + (f" ({te})" if te else ""))
    if act.get("activityTrainingLoad"):
        lines.append(f"- Training load: {act['activityTrainingLoad']:.0f}")
    if act.get("vO2MaxValue"):
        lines.append(f"- VO₂Max: {act['vO2MaxValue']:.0f}")

    laps = detail.get("laps", [])
    if laps:
        # Lap-awareness: detect manual vs auto-1km vs single-lap
        lap_distances = [l.get("distance") for l in laps if l.get("distance")]
        if len(lap_distances) <= 1:
            lap_header = ("Lap details (single lap only — no subjective segmentation)")
        elif is_manual_lap_structure(lap_distances):
            lap_header = ("Lap details (**likely manual laps** — user's "
                          "subjective segmentation; prefer the user's "
                          "comment to interpret each lap's meaning, "
                          "e.g. \"got tired after lap 3\" / "
                          "\"lap 5 was an aid station\")")
        else:
            lap_header = ("Lap details (likely Garmin auto-1km laps — "
                          "no subjective semantics; analyze as a "
                          "pace-vs-distance progression)")
        lines.append(f"\n## {lap_header}")
        for i, lap in enumerate(laps, 1):
            l_dist = (lap.get("distance") or 0) / 1000
            l_dur  = gd.format_duration(lap.get("duration"))
            l_hr   = lap.get("averageHR")
            l_mhr  = lap.get("maxHR")
            l_cad  = lap.get("averageRunCadence")
            l_gct  = lap.get("groundContactTime")
            l_vo   = lap.get("verticalOscillation")
            l_spd  = lap.get("averageSpeed")
            parts  = [f"{l_dist:.2f}km", l_dur]
            if l_spd and l_spd > 0.5 and "run" in tk:
                parts.append(f"pace {gd.format_pace(l_spd)}")
            if l_hr:
                hr_str = f"HR {l_hr:.0f}"
                if l_mhr:
                    hr_str += f"/peak {l_mhr:.0f}bpm"
                parts.append(hr_str)
            if l_dist >= 0.3:
                if l_cad and l_cad > 50:
                    parts.append(f"cadence {l_cad:.0f}spm")
                if l_gct:
                    parts.append(f"GCT (ground contact time) {l_gct:.0f}ms"
                                 if i == 1 else f"GCT {l_gct:.0f}ms")
                if l_vo:
                    parts.append(f"vert osc (vertical oscillation) {l_vo:.1f}cm"
                                 if i == 1 else f"vert osc {l_vo:.1f}cm")
            lines.append(f"- Lap {i}: {'  |  '.join(parts)}")
        lines.append("(Use the per-lap data to identify warmup / intervals / "
                     "recovery / cooldown structure. Short slow-pace laps are "
                     "usually recovery/rest; the first long slow lap is usually "
                     "warmup; the last long slow lap is usually cooldown.)")

    hr_zones = detail.get("hr_zones", [])
    if isinstance(hr_zones, list) and hr_zones:
        lines.append("\n## HR zone distribution")
        for z in hr_zones:
            if isinstance(z, dict):
                zn   = z.get("zoneNumber", z.get("zone", "?"))
                secs = z.get("secsInZone", 0)
                pct  = z.get("zonePercentage")
                lines.append(f"- Z{zn}: {secs//60} min" + (f" ({pct:.0f}%)" if pct else ""))

    metrics  = detail.get("metrics", {})
    spd_raw  = [v for v in metrics.get("speed", []) if v and v > 0.5]
    hr_raw   = [v for v in metrics.get("hr", [])    if v and v > 50]
    cad_raw  = [v for v in metrics.get("cadence", []) if v and v > 50]
    gct_raw  = [v for v in metrics.get("gct", [])   if v]
    vo_raw   = [v for v in metrics.get("vert_osc", []) if v]

    if spd_raw and "run" in tk:
        paces = [1000 / v for v in spd_raw]
        sorted_p = sorted(paces)
        q10 = sorted_p[max(0, int(len(sorted_p) * 0.10))]
        q90 = sorted_p[min(len(sorted_p)-1, int(len(sorted_p) * 0.90))]
        lines.append("\n## Pace distribution (full-activity timeseries)")
        lines.append(f"- Fastest 10% pace range: {q10//60:.0f}:{q10%60:02.0f}—{sorted_p[0]//60:.0f}:{sorted_p[0]%60:02.0f} /km")
        lines.append(f"- Slowest 10% pace range: {sorted_p[-1]//60:.0f}:{sorted_p[-1]%60:02.0f}—{q90//60:.0f}:{q90%60:02.0f} /km")

    if hr_raw:
        # Z4/Z5 boundaries from this user's actual hr_zones (Garmin computes
        # them per-account based on max HR / LTHR). Skip if no zones data.
        z4_thresh = next(
            (z.get("zoneLowBoundary") for z in hr_zones
             if z.get("zoneNumber") == 4 and z.get("zoneLowBoundary")),
            None,
        )
        z5_thresh = next(
            (z.get("zoneLowBoundary") for z in hr_zones
             if z.get("zoneNumber") == 5 and z.get("zoneLowBoundary")),
            None,
        )
        if z4_thresh:
            pct_z4 = sum(1 for h in hr_raw if h >= z4_thresh) / len(hr_raw) * 100
            lines.append(f"- HR ≥{z4_thresh} bpm (Z4+) time fraction: {pct_z4:.0f}%")
        if z5_thresh:
            pct_z5 = sum(1 for h in hr_raw if h >= z5_thresh) / len(hr_raw) * 100
            lines.append(f"- HR ≥{z5_thresh} bpm (Z5) time fraction: {pct_z5:.0f}%")

    if cad_raw or gct_raw or vo_raw:
        lines.append("\n## Running dynamics (full-activity averages)")
        if cad_raw:
            lines.append(f"- Avg cadence: {sum(cad_raw)/len(cad_raw):.0f} spm")
        if gct_raw:
            lines.append(f"- Avg GCT (ground contact time): {sum(gct_raw)/len(gct_raw):.0f} ms")
        if vo_raw:
            lines.append(f"- Avg vert osc (vertical oscillation): {sum(vo_raw)/len(vo_raw):.1f} cm")

    # Similar activities: filter to PRIOR activities only (same type, before
    # this activity's date) — avoids time-traveling references when reviewing
    # an old activity. all_acts is sorted DESC by start_time_local already,
    # so taking [:3] gives the 3 most recent prior same-type activities.
    act_dt_str = act.get("startTimeLocal", "")
    similar = [a for a in all_acts
               if a.get("activityTypeKey") == tk
               and a.get("activityId") != act.get("activityId")
               and (a.get("startTimeLocal") or "") < act_dt_str][:3]
    if similar:
        lines.append("\n## Recent comparable workouts (3 prior same-type sessions before this date)")
        for s in similar:
            s_dt   = s.get("startTimeLocal", "")[:10]
            s_dist = (s.get("distance") or 0) / 1000
            pace_s = (f", pace {gd.format_pace(s['averageSpeed'])}"
                      if s.get("averageSpeed") and "run" in tk else "")
            lines.append(f"- {s_dt}: {s_dist:.1f} km, "
                         f"avg HR {s.get('averageHR','—')} bpm{pace_s}")

    return "\n".join(lines)


def _build_metrics_timeline(act: dict, detail: dict) -> str:
    """Time-series bucket summary for continuous (non-interval) activities.

    Bucket size scales with activity duration to keep ~20-30 segments:
      ≤30min      → 60s buckets
      30-90min    → 180s buckets (3min)
      90-180min   → 300s buckets (5min)
      >3h         → 600s buckets (10min)

    Skipped entirely if the activity has rich per-lap structure (≥3 laps
    or ≥3 meaningful splits) — those activities are better analyzed by
    typed builders (Tempo / Interval / Race etc.) using lap boundaries."""
    tk      = act.get("activityTypeKey", "")
    metrics = detail.get("metrics", {})
    secs    = metrics.get("sec", [])
    if not secs:
        return ""

    laps   = detail.get("laps", [])
    splits = detail.get("splits", [])
    has_per_lap = len(laps) >= 3
    meaningful_splits = [
        s for s in splits
        if s.get("splitType") not in (None, "RWD_STAND", "RWD_WALK")
    ]
    if has_per_lap or len(meaningful_splits) >= 3:
        return ""

    hr_list   = metrics.get("hr", [])
    spd_list  = metrics.get("speed", [])
    cad_list  = metrics.get("cadence", [])
    gct_list  = metrics.get("gct", [])
    vo_list   = metrics.get("vert_osc", [])
    elev_list = metrics.get("elev", [])
    pwr_list  = metrics.get("power", [])

    total_sec = act.get("duration") or (secs[-1] - secs[0])
    if total_sec <= 30 * 60:
        bucket_s, bucket_label = 60,  "1-min buckets"
    elif total_sec <= 90 * 60:
        bucket_s, bucket_label = 180, "3-min buckets"
    elif total_sec <= 180 * 60:
        bucket_s, bucket_label = 300, "5-min buckets"
    else:
        bucket_s, bucket_label = 600, "10-min buckets"

    lines = [f"\n## Timeline progression ({bucket_label}, continuous activity)"]
    n = len(secs)
    if n == 0:
        return ""

    # Walk samples once with a single pointer; buckets are time-aligned to
    # 0, bucket_s, 2*bucket_s, ... regardless of native sample rate.
    bucket_start = (secs[0] // bucket_s) * bucket_s
    seg = 1
    while bucket_start <= secs[-1]:
        bucket_end = bucket_start + bucket_s
        in_bucket = [i for i in range(n) if bucket_start <= secs[i] < bucket_end]
        if not in_bucket:
            bucket_start = bucket_end
            continue

        chunk_hr   = [hr_list[i]   for i in in_bucket if hr_list[i]   and hr_list[i] > 50]
        chunk_spd  = [spd_list[i]  for i in in_bucket if spd_list[i]  and spd_list[i] > 0.3]
        chunk_cad  = [cad_list[i]  for i in in_bucket if cad_list[i]  and cad_list[i] > 50]
        chunk_gct  = [gct_list[i]  for i in in_bucket if gct_list[i]  and gct_list[i] > 0]
        chunk_vo   = [vo_list[i]   for i in in_bucket if vo_list[i]   and vo_list[i] > 0]
        chunk_pwr  = [pwr_list[i]  for i in in_bucket if pwr_list[i]  and pwr_list[i] > 0]
        chunk_elev = [elev_list[i] for i in in_bucket if elev_list[i] is not None]

        t0_min = bucket_start // 60
        t1_min = bucket_end   // 60
        parts = [f"**Seg{seg}**", f"{t0_min}-{t1_min}min"]
        seg += 1

        if chunk_hr:
            parts.append(f"HR {sum(chunk_hr)/len(chunk_hr):.0f} (peak {max(chunk_hr):.0f}) bpm")
        if chunk_spd:
            avg_spd = sum(chunk_spd) / len(chunk_spd)
            if "run" in tk:
                p = 1000 / avg_spd
                parts.append(f"pace {p//60:.0f}:{p%60:02.0f}/km")
            else:
                parts.append(f"speed {avg_spd*3.6:.1f}km/h")
        if chunk_cad:
            parts.append(f"cadence {sum(chunk_cad)/len(chunk_cad):.0f}spm")
        if chunk_gct:
            parts.append(f"GCT {sum(chunk_gct)/len(chunk_gct):.0f}ms")
        if chunk_vo:
            parts.append(f"vert osc {sum(chunk_vo)/len(chunk_vo):.1f}cm")
        if chunk_pwr:
            parts.append(f"power {sum(chunk_pwr)/len(chunk_pwr):.0f}W")
        if len(chunk_elev) >= 2:
            diff = chunk_elev[-1] - chunk_elev[0]
            if abs(diff) > 3:
                parts.append(f"elev{'↑' if diff > 0 else '↓'}{abs(diff):.0f}m")

        lines.append("  ".join(parts))
        bucket_start = bucket_end
    return "\n".join(lines)
