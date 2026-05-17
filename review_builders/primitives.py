"""Pure-function calculation primitives shared across review builders + tools.

Design rule: **no judgment, only math**. Functions in this module take rows /
windows / metrics and return numbers or dicts. They never decide which window
to use, what threshold means "bad", or what comparison to make — those are
LLM-level decisions.

Same primitives are used in two places:
  1. Builders (review_builders/*) — eager pre-bake of universal aggregations
     for the initial report.
  2. Tools (review_tools.py) — LLM-callable on-demand aggregation for ad-hoc
     windows (custom segmentation driven by user comment, etc.).

Identical inputs always yield identical outputs across both paths, so the
LLM never sees a number that depends on whether builder or tool computed it.

Row tuple convention used throughout:
  (sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio, stride_cm)
  optionally extended with: power_w, distance_cum_m
Use the named accessors below to stay schema-tolerant.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional


# ── Row schema helpers ─────────────────────────────────────────────────────
# Centralized so a schema change touches one place. Builders/tools pull rows
# with whatever SELECT they want — these helpers index by position.

ROW_SEC      = 0
ROW_HR       = 1
ROW_SPEED    = 2  # m/s
ROW_CADENCE  = 3
ROW_GCT      = 4  # ms
ROW_VR       = 5  # vertical ratio (%)
ROW_STRIDE   = 6  # cm

# Filter thresholds — same across all aggregations so a "valid HR sample" means
# the same thing in builder and tool. Anchored to physiological floors to
# reject GPS/sensor noise (HR=0, speed=0 from a paused-but-not-flagged moment).
MIN_HR        = 50      # < 50 bpm = sensor dropout
MIN_SPEED_MPS = 0.5     # < 0.5 m/s = standing still / pause
MIN_CADENCE   = 50      # < 50 spm = walking / sensor drop


# ── Window aggregation ────────────────────────────────────────────────────

def seg_stats(rows: list[tuple]) -> Optional[dict]:
    """Aggregate stats over a list of rows.

    Returns None if the window has no valid HR or speed samples (window
    likely all-paused). Otherwise returns dict with avg + p10/p50/p90 for
    HR/pace, plus avg for force-decay channels.

    Pace is reported as seconds-per-km (1000 / avg_speed_mps). Stride is
    in meters (cm/100) for human-readable narrative.
    """
    hrs    = [r[ROW_HR]      for r in rows if r[ROW_HR]      and r[ROW_HR] > MIN_HR]
    spds   = [r[ROW_SPEED]   for r in rows if r[ROW_SPEED]   and r[ROW_SPEED] > MIN_SPEED_MPS]
    cads   = [r[ROW_CADENCE] for r in rows if r[ROW_CADENCE] and r[ROW_CADENCE] > MIN_CADENCE]
    gcts   = [r[ROW_GCT]     for r in rows if r[ROW_GCT]]
    vrs    = [r[ROW_VR]      for r in rows if r[ROW_VR] is not None]
    strds  = [r[ROW_STRIDE]  for r in rows if r[ROW_STRIDE] and r[ROW_STRIDE] > 30]

    if not hrs or not spds:
        return None

    hr_avg  = sum(hrs)  / len(hrs)
    spd_avg = sum(spds) / len(spds)

    return {
        "n_samples":    len(rows),
        "hr_avg":       hr_avg,
        "hr_p10":       _percentile(hrs, 0.10),
        "hr_p50":       _percentile(hrs, 0.50),
        "hr_p90":       _percentile(hrs, 0.90),
        "hr_max":       max(hrs),
        "pace_s_per_km":     1000 / spd_avg,
        "pace_p10_s_per_km": (1000 / _percentile(spds, 0.90)) if spds else None,  # fast pace = high speed
        "pace_p50_s_per_km": (1000 / _percentile(spds, 0.50)) if spds else None,
        "pace_p90_s_per_km": (1000 / _percentile(spds, 0.10)) if spds else None,  # slow pace = low speed
        "speed_mps_avg":     spd_avg,
        "ef":           spd_avg / hr_avg,        # efficiency factor (m/s per bpm)
        "cadence_avg":  (sum(cads)  / len(cads))  if cads  else None,
        "gct_avg_ms":   (sum(gcts)  / len(gcts))  if gcts  else None,
        "vr_avg_pct":   (sum(vrs)   / len(vrs))   if vrs   else None,
        "stride_avg_m": (sum(strds) / len(strds)) / 100 if strds else None,
    }


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile. q in [0, 1]."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    pos = q * (len(s) - 1)
    lo  = int(math.floor(pos))
    hi  = int(math.ceil(pos))
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (pos - lo))


# ── Drift (linear regression over time) ────────────────────────────────────

def _regress_xy(pts: list[tuple]) -> Optional[dict]:
    """OLS regression on (x, y) pairs. x in seconds; slope reported per-min.

    Returns {slope_per_min, intercept, r_squared, n} or None if <30 points
    or zero variance in x. Internal helper — callers extract (x, y) from
    rows themselves (clean, no synthetic-row trick).
    """
    if len(pts) < 30:
        return None
    n   = len(pts)
    sx  = sum(p[0] for p in pts)
    sy  = sum(p[1] for p in pts)
    sxx = sum(p[0] * p[0] for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    syy = sum(p[1] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    slope_per_sec = (n * sxy - sx * sy) / denom
    intercept     = (sy - slope_per_sec * sx) / n
    ss_tot = syy - sy * sy / n
    ss_res = sum((p[1] - (slope_per_sec * p[0] + intercept)) ** 2 for p in pts)
    r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return {
        "slope_per_min": slope_per_sec * 60,
        "intercept":     intercept,
        "r_squared":     r2,
        "n":             n,
    }


def hr_drift(rows: list[tuple]) -> Optional[dict]:
    """HR linear regression over the whole-run time axis. Slope in bpm/min.

    Structure-agnostic: doesn't care about lap boundaries or push/CD
    structure. The "true cardiac drift" complement to Pa:HR.
    """
    pts = [(r[ROW_SEC], r[ROW_HR]) for r in rows
           if r[ROW_HR] and r[ROW_HR] > MIN_HR]
    return _regress_xy(pts)


# ── Internal stability (per-lap or arbitrary window) ──────────────────────

def _pace_cv_buckets(rows: list[tuple], bucket_s: int = 30) -> Optional[float]:
    """Pace CV across fixed-size time buckets within `rows`.

    Per-1Hz-sample CV would be dominated by GPS noise; bucketing to 30s
    averages that out, giving a coaching-meaningful "did pace stay
    controlled" signal. Returns None if window has < 2 buckets of data.
    """
    if not rows:
        return None
    start = rows[0][ROW_SEC]
    end   = rows[-1][ROW_SEC]
    if end - start < bucket_s * 2:
        return None
    bucket_paces = []
    bucket_start = (start // bucket_s) * bucket_s
    while bucket_start < end:
        bucket_end = bucket_start + bucket_s
        spds = [r[ROW_SPEED] for r in rows
                if bucket_start <= r[ROW_SEC] < bucket_end
                and r[ROW_SPEED] and r[ROW_SPEED] > MIN_SPEED_MPS]
        if spds:
            bucket_paces.append(1000 / (sum(spds) / len(spds)))
        bucket_start = bucket_end
    if len(bucket_paces) < 2:
        return None
    mean = sum(bucket_paces) / len(bucket_paces)
    if mean == 0:
        return None
    var = sum((p - mean) ** 2 for p in bucket_paces) / len(bucket_paces)
    return (var ** 0.5) / mean


def internal_stats(rows: list[tuple]) -> dict:
    """All internal-stability + drift readings for a single window/lap.

    Composition of existing primitives — used by the alpha builder to emit
    per-lap internal readings without skipping any laps regardless of length.
    Sub-readings (pace_cv / hr_drift / first_half / second_half) may be None
    individually if the window is too short for that specific computation;
    callers should render gracefully ("—" for None entries).

    NEVER returns None at the top level — always returns a dict so caller
    can layout consistently across all laps.
    """
    if not rows:
        return {
            "n_samples": 0, "duration_s": 0, "pace_cv": None,
            "hr_drift": None, "first_half": None, "second_half": None,
        }
    n   = len(rows)
    mid = n // 2
    return {
        "n_samples":   n,
        "duration_s":  rows[-1][ROW_SEC] - rows[0][ROW_SEC],
        "pace_cv":     _pace_cv_buckets(rows, bucket_s=30),
        "hr_drift":    hr_drift(rows),
        "first_half":  seg_stats(rows[:mid]) if mid > 0 else None,
        "second_half": seg_stats(rows[mid:]) if n - mid > 0 else None,
    }


# ── Pa:HR (pace-normalized HR drift) ───────────────────────────────────────

def pa_hr_split(rows: list[tuple]) -> Optional[dict]:
    """Pa:HR — first-half vs second-half ratio of (pace × HR) inverse.

    Joel Friel's classic decoupling metric: split run into halves, compute
    EF (efficiency factor = avg_speed / avg_hr) for each half, return the
    decoupling % = (EF_first - EF_second) / EF_first × 100.

    > 5% decoupling = aerobic system can't sustain this pace at this duration.
    Structure-agnostic: doesn't care whether the run is steady or progression.
    """
    if len(rows) < 60:
        return None
    mid = len(rows) // 2
    s1 = seg_stats(rows[:mid])
    s2 = seg_stats(rows[mid:])
    if not (s1 and s2):
        return None
    ef1, ef2 = s1["ef"], s2["ef"]
    decoupling_pct = (ef1 - ef2) / ef1 * 100 if ef1 else 0.0
    return {
        "first_half_ef":    ef1,
        "second_half_ef":   ef2,
        "decoupling_pct":   decoupling_pct,
        "first_half_hr":    s1["hr_avg"],
        "second_half_hr":   s2["hr_avg"],
        "first_half_pace":  s1["pace_s_per_km"],
        "second_half_pace": s2["pace_s_per_km"],
    }


# ── Per-km slicing ─────────────────────────────────────────────────────────

def slice_by_km(rows: list[tuple],
                distance_cum_m_idx: int = 7) -> list[dict]:
    """Bucket rows into per-km segments using distance_cum_m column.

    Caller must have SELECT'd distance_cum_m as column index 7 (or pass
    distance_cum_m_idx). Returns list of {km, start_sec, end_sec, stats}.
    Skips kms with insufficient samples (handles partial final km).
    """
    if not rows:
        return []
    # Find starting km (handle activities not starting at 0m, though unusual)
    first_dist = next((r[distance_cum_m_idx] for r in rows
                       if r[distance_cum_m_idx] is not None), 0.0)
    last_dist  = next((r[distance_cum_m_idx] for r in reversed(rows)
                       if r[distance_cum_m_idx] is not None), 0.0)
    if last_dist <= first_dist:
        return []

    out = []
    km_start_m = (int(first_dist) // 1000) * 1000
    while km_start_m < last_dist:
        km_end_m = km_start_m + 1000
        bucket = [r for r in rows
                  if r[distance_cum_m_idx] is not None
                  and km_start_m <= r[distance_cum_m_idx] < km_end_m]
        if len(bucket) >= 5:
            stats = seg_stats(bucket)
            if stats:
                out.append({
                    "km":         km_start_m // 1000 + 1,    # 1-indexed
                    "start_m":    km_start_m,
                    "end_m":      km_end_m,
                    "start_sec":  bucket[0][ROW_SEC],
                    "end_sec":    bucket[-1][ROW_SEC],
                    "actual_m":   bucket[-1][distance_cum_m_idx] - bucket[0][distance_cum_m_idx],
                    "stats":      stats,
                })
        km_start_m = km_end_m
    return out


# ── Lap-level helpers ──────────────────────────────────────────────────────

def lap_pace_cv(lap_paces_s_per_km: list[float]) -> Optional[float]:
    """Coefficient of variation across lap paces. LLM uses this as a
    structure hint (high CV = structured run with pace zones; low CV =
    mostly steady). Pure descriptive statistic — no threshold here.

    Returns None if fewer than 2 valid laps.
    """
    valid = [p for p in lap_paces_s_per_km if p and p > 0]
    if len(valid) < 2:
        return None
    mean = sum(valid) / len(valid)
    if mean == 0:
        return None
    var = sum((p - mean) ** 2 for p in valid) / len(valid)
    return (var ** 0.5) / mean


def pairwise_delta(stats_a: dict, stats_b: dict) -> dict:
    """Compute key deltas between two seg_stats outputs.

    Reports raw deltas + percent for HR/pace; raw delta only for force-decay
    metrics (cadence, GCT, vertical ratio, stride). Useful for first-vs-last lap, first-km-
    vs-last-km, push-lap-first-half-vs-second-half — but the choice of which
    A vs B to compare is the caller's (= LLM's) judgment.
    """
    out = {}
    if "hr_avg" in stats_a and "hr_avg" in stats_b:
        out["hr_delta"]     = stats_b["hr_avg"] - stats_a["hr_avg"]
        out["hr_delta_pct"] = (stats_b["hr_avg"] - stats_a["hr_avg"]) / stats_a["hr_avg"] * 100 \
                              if stats_a["hr_avg"] else None
    if "pace_s_per_km" in stats_a and "pace_s_per_km" in stats_b:
        out["pace_delta_s"]   = stats_b["pace_s_per_km"] - stats_a["pace_s_per_km"]
        out["pace_delta_pct"] = (stats_b["pace_s_per_km"] - stats_a["pace_s_per_km"]) / stats_a["pace_s_per_km"] * 100 \
                                if stats_a["pace_s_per_km"] else None
    if "ef" in stats_a and "ef" in stats_b and stats_a["ef"]:
        out["ef_decoupling_pct"] = (stats_a["ef"] - stats_b["ef"]) / stats_a["ef"] * 100
    for k_short, k_full in (
        ("cadence", "cadence_avg"),
        ("gct",     "gct_avg_ms"),
        ("vr",      "vr_avg_pct"),
        ("stride",  "stride_avg_m"),
    ):
        a, b = stats_a.get(k_full), stats_b.get(k_full)
        if a is not None and b is not None:
            out[f"{k_short}_delta"] = b - a
    return out


# ── Formatting helpers (used by builders, exposed so tools can mirror) ────

def fmt_pace(p_s_per_km: float) -> str:
    if p_s_per_km is None or p_s_per_km <= 0:
        return "—"
    return f"{int(p_s_per_km // 60)}:{int(p_s_per_km % 60):02d}/km"


def fmt_pace_compact(p_s_per_km: float) -> str:
    """No '/km' suffix — for tight table cells."""
    if p_s_per_km is None or p_s_per_km <= 0:
        return "—"
    return f"{int(p_s_per_km // 60)}:{int(p_s_per_km % 60):02d}"
