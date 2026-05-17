"""Garmin data fetching, incremental caching, and LLM context building.

The sync only fetches activity endpoints — the daily-wellness endpoints
(HRV, sleep, daily summary, training status, Body Battery) would return
sparse / misleading data for users who only wear the watch during runs,
so they are skipped to keep the LLM context grounded."""

import os
from datetime import date, datetime, timedelta
from typing import Callable

from garth.http import Client as GarthClient
from garminconnect import Garmin

import db  # SQLite raw-data tier (Phase 1: dual-write alongside JSON cache)
import i18n  # tag display rendering for LLM context strings


# ── Exceptions for stale-session detection ───────────────────────────────────
# When garth's OAuth2 access token expires AND auto-refresh fails (or returns
# stale data), garminconnect 0.3.3 silently returns None / empty dict for many
# endpoints instead of raising 401. Pre-fix, this cascaded into empty SQLite
# writes + "no data" reports with no user-visible error. Post-fix, these
# explicit exceptions surface the issue immediately, and the UI catches them
# to show a "reconnect Garmin" dialog.

class SessionExpiredError(Exception):
    """Raised by _client() when the loaded garth session can't be validated
    against Garmin (probe endpoint returns None / empty / non-200)."""

class EmptyDetailFetchError(Exception):
    """Raised by fetch_activity_detail() when the metric stream comes back
    empty / lacking metricDescriptors. Prevents writing garbage into JSON
    cache + SQLite (and the silent 'has_full_detail=1 with 0 metrics rows'
    bug class that follows)."""

# ── Constants ────────────────────────────────────────────────────────────────

DETAILED_DAYS  = 90    # 3 months of per-day data
LONGTERM_WEEKS = 26    # 6 months of weekly summaries

# All persistent data lives under DATA_DIR. Default = project root (backward
# compatible with local dev); in Docker, set DATA_DIR=/data and mount a volume.
DATA_DIR           = os.environ.get("DATA_DIR",
                                    os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR          = os.path.join(DATA_DIR, "cache")
SESSION_DIR        = os.path.join(DATA_DIR, ".garth_session")

# ── Lookup tables ────────────────────────────────────────────────────────────

# Set of activity-type keys we have explicit i18n entries for. Anything
# not in this set falls back to t("activity_type._unknown") via display_type().
ACTIVITY_TYPE_KEYS = {
    "running", "trail_running",
    "virtual_ride", "cycling", "indoor_cycling",
    "swimming", "open_water_swimming",
    "strength_training", "fitness_equipment",
    "walking", "hiking",
}
TE_LABEL_MAP = {
    "AEROBIC_BASE": "有氧基础", "AEROBIC_CAPACITY": "有氧能力",
    "LACTATE_THRESHOLD": "乳酸阈值", "SPEED": "速度",
    "ANAEROBIC": "无氧", "RECOVERY": "恢复训练",
}

# ── Formatters ───────────────────────────────────────────────────────────────

def format_duration(seconds) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def format_pace(speed_mps) -> str:
    if not speed_mps or speed_mps <= 0:
        return "—"
    pace_s = 1000 / speed_mps
    return f"{int(pace_s // 60)}:{int(pace_s % 60):02d}/km"


def display_type(type_key: str) -> str:
    """Locale-aware activity-type label. Falls back to the raw key (or
    'Activity' / '运动') for types we haven't catalogued yet."""
    if type_key in ACTIVITY_TYPE_KEYS:
        return i18n.t(f"activity_type.{type_key}")
    return type_key or i18n.t("activity_type._unknown")


# ── Internal helpers ─────────────────────────────────────────────────────────

def _client() -> Garmin:
    """python-garminconnect Garmin instance, but with its internal client
    swapped for a garth.http.Client loaded from our existing session.

    The library and garth maintain separate auth stacks (lib uses a "DI
    Bearer token", garth uses OAuth1+OAuth2). They both expose
    `connectapi(path, **kwargs)` with compatible signatures, so we can
    plug garth's client into the library's Garmin instance — every
    `g.<named_method>(...)` then routes through garth's auth.

    Auth stays exclusively in garmin_auth.py (garth + playwright); we never
    invoke the library's `login()`.
    """
    gc = GarthClient()
    gc.load(SESSION_DIR)            # reads .garth_session/oauth{1,2}_token.json

    g = Garmin()
    g.client = gc                   # swap; all g.<method>(...) now route through garth's auth

    # Always probe the session right after loading. garminconnect 0.3.3 returns
    # None / empty dict for many endpoints when the OAuth2 access token is
    # stale (instead of raising 401) — the probe surfaces this immediately
    # rather than letting it cascade into empty SQLite writes downstream.
    try:
        prof = g.connectapi("/userprofile-service/socialProfile")
    except Exception as e:
        raise SessionExpiredError(f"Garmin auth probe failed: {e}") from e
    if not prof or not isinstance(prof, dict) or not prof.get("displayName"):
        raise SessionExpiredError(
            "Garmin auth probe returned empty — session likely expired"
        )
    g.display_name = prof.get("displayName", "")
    g.full_name    = prof.get("fullName", g.display_name)
    return g


def _parse_activity(a: dict) -> dict:
    return {
        "activityId":              a.get("activityId"),
        "activityName":            a.get("activityName", ""),
        "activityTypeKey":         (a.get("activityType") or {}).get("typeKey", ""),
        "startTimeLocal":          a.get("startTimeLocal", ""),
        "distance":                a.get("distance"),
        "duration":                a.get("duration"),
        "averageHR":               a.get("averageHR"),
        "maxHR":                   a.get("maxHR"),
        "calories":                a.get("calories"),
        "elevationGain":           a.get("elevationGain"),
        "elevationLoss":           a.get("elevationLoss"),
        "averageSpeed":            a.get("averageSpeed"),
        "trainingEffectLabel":     a.get("trainingEffectLabel"),
        "aerobicTrainingEffect":   a.get("aerobicTrainingEffect"),
        "anaerobicTrainingEffect": a.get("anaerobicTrainingEffect"),
        "vO2MaxValue":             a.get("vO2MaxValue"),
        "activityTrainingLoad":    a.get("activityTrainingLoad"),
    }


def _parse_metrics_stream(stream: dict) -> dict:
    """Extract named time-series from the activityDetailMetrics stream."""
    descriptors = stream.get("metricDescriptors", [])
    rows        = stream.get("activityDetailMetrics", [])
    if not descriptors or not rows:
        return {}

    col = {d.get("key"): i for i, d in enumerate(descriptors)}

    def _val(metrics_row, key):
        i = col.get(key)
        if i is None or i >= len(metrics_row):
            return None
        return metrics_row[i]

    # Thin to every 5th row (~10-second resolution for most activities)
    out = {k: [] for k in ["ts", "hr", "speed", "elev", "cadence",
                            "power", "stride", "gct", "vert_osc", "vert_ratio"]}
    key_map = {
        "ts":         "directTimestamp",
        "hr":         "directHeartRate",
        "speed":      "directSpeed",
        "elev":       "directElevation",
        "cadence":    "directDoubleCadence",
        "power":      "directPower",
        "stride":     "directStrideLength",
        "gct":        "directGroundContactTime",
        "vert_osc":   "directVerticalOscillation",
        "vert_ratio": "directVerticalRatio",
    }
    for row in rows[::5]:
        m = row.get("metrics", [])
        for k, api_key in key_map.items():
            out[k].append(_val(m, api_key))
    return out


# ── Progress tracker ─────────────────────────────────────────────────────────

class _Prog:
    def __init__(self, cb: Callable | None, start: float, end: float, total: int):
        self._cb    = cb
        self._start = start
        self._end   = end
        self._total = max(1, total)
        self._done  = 0

    def tick(self, msg: str = ""):
        self._done = min(self._done + 1, self._total)
        if self._cb:
            frac = self._start + (self._end - self._start) * (self._done / self._total)
            self._cb(frac, msg)

    def done(self):
        if self._cb:
            self._cb(self._end, "")


# ── Main sync ────────────────────────────────────────────────────────────────

def sync_all(force_full: bool = False, progress: Callable | None = None) -> tuple[dict, dict]:
    """
    Incremental sync.  Returns (detailed, longterm).
    Progress callback: progress(frac 0-1, message str)
    """
    detailed = _sync_detailed(force_full, _Prog(progress, 0.0, 0.75, 1))  # placeholder total
    longterm = _sync_longterm(_Prog(progress, 0.75, 1.0, 1))
    if progress:
        progress(1.0, i18n.t("ui.sync.completed"))
    return detailed, longterm


def _sync_detailed(force_full: bool, prog_placeholder) -> dict:
    """All writes go to SQLite — activities, wellness, sync metadata.
    Returns the reconstructed legacy-shape dict via load_detailed()."""
    today    = date.today()
    cutoff   = (today - timedelta(days=DETAILED_DAYS - 1)).isoformat()

    # Determine the activity-list fetch window. Uses the most recent
    # activity start_time_local as the high-water mark for incremental sync.
    if force_full:
        start_date = today - timedelta(days=DETAILED_DAYS - 1)
    else:
        with db.connect() as _conn:
            row = _conn.execute(
                "SELECT MAX(start_time_local) FROM activities"
            ).fetchone()
        last_date_str = (row[0][:10] if row and row[0] else cutoff)
        try:
            last_date = date.fromisoformat(last_date_str)
        except Exception:
            last_date = date.fromisoformat(cutoff)
        # Always re-fetch a 2-day window to catch late uploads / today's runs
        gap_days   = max(2, (today - last_date).days + 1)
        gap_days   = min(gap_days, DETAILED_DAYS)
        start_date = today - timedelta(days=gap_days - 1)

    # Step count: profile(1) + activities(2).
    total_steps = 1 + 2
    prog = _Prog(prog_placeholder._cb, 0.0, 0.75, total_steps)

    client = _client()                       # Garmin instance; profile already populated
    display_name = client.display_name

    prog.tick(i18n.t("ui.sync.user_info"))    # _client() already fetched profile

    prog.tick(i18n.t("ui.sync.activities_list", start=start_date, end=today))
    raw_acts = client.get_activities_by_date(start_date.isoformat(),
                                              today.isoformat()) or []
    prog.tick()

    # Persist activities + sync metadata to SQLite. Each activity gets its
    # raw payload stashed in raw_summary_json so the parts we don't
    # column-explode are preserved.
    with db.connect() as _conn:
        for raw in raw_acts:
            parsed = _parse_activity(raw)
            aid = parsed.get("activityId")
            if aid is None:
                continue
            flat = dict(parsed)
            flat["startTimeGMT"] = raw.get("startTimeGMT")
            flat["maxSpeed"]     = raw.get("maxSpeed")
            db.upsert_activity_metadata(_conn, flat, raw_summary=raw)

        # Sync-metadata singleton (replaces fetched_at + display_name fields
        # that used to live at the top of garmin_data.json)
        db.set_app_metadata(_conn,
                            fetched_at=datetime.now().isoformat(),
                            display_name=display_name)

    _prefetch_run_details(raw_acts[:10])
    prog.done()
    return load_detailed() or {}


def _sync_longterm(prog: _Prog) -> dict:
    """UPSERT the 6-month activity window into SQLite + extend HRV history.
    Weekly summary is computed by db.get_weekly_summary on demand."""
    today   = date.today()
    lt_days = LONGTERM_WEEKS * 7
    start   = (today - timedelta(days=lt_days - 1)).isoformat()

    # Just the activity list (1 paged endpoint, ~2 steps).
    p = _Prog(prog._cb, 0.75, 1.0, 2)

    client = _client()

    p.tick(i18n.t("ui.sync.longterm_activities", weeks=LONGTERM_WEEKS))
    raw_acts = client.get_activities_by_date(start, today.isoformat()) or []
    p.tick()

    # UPSERT activities to SQLite. Weekly summary is computed by
    # db.get_weekly_summary on demand from the activities table.
    with db.connect() as _conn:
        for raw in raw_acts:
            parsed = _parse_activity(raw)
            aid = parsed.get("activityId")
            if aid is None:
                continue
            flat = dict(parsed)
            flat["startTimeGMT"] = raw.get("startTimeGMT")
            flat["maxSpeed"]     = raw.get("maxSpeed")
            db.upsert_activity_metadata(_conn, flat, raw_summary=raw)

        db.set_app_metadata(_conn, fetched_at=datetime.now().isoformat())

    p.done()
    return load_longterm() or {}


def _format_laps_ctx(laps: list) -> str:
    """Per-lap summary for AI context. Lap numbers preserved so the AI can
    spot interval structure (e.g. lap 1 = warmup if slow+long; alternating
    fast/slow laps = intervals + recoveries; last slow lap = cooldown)."""
    if not laps:
        return ""
    parts = []
    for i, lap in enumerate(laps, 1):
        dist  = lap.get("distance", 0) / 1000
        speed = lap.get("averageSpeed", 0)
        pace  = format_pace(speed) if speed else "—"
        hr    = lap.get("averageHR", 0)
        cad   = lap.get("averageRunCadence", 0)
        gct   = lap.get("groundContactTime", 0)
        seg = f"{i}){dist:.2f}km @{pace}"
        if hr: seg += f" HR{hr:.0f}"
        # Skip dynamics for tiny laps (likely recovery/rest, data unreliable)
        if dist >= 0.3:
            if cad and cad > 50: seg += f" {cad:.0f}spm"
            if gct:              seg += f" GCT{gct:.0f}ms"
        parts.append(seg)
    return i18n.t("lap_ctx.laps_prefix", n=len(laps)) + " | ".join(parts)


def _format_splits_ctx(splits: list) -> str:
    """Compact split-structure summary for AI coaching context."""
    if not splits:
        return ""

    by_type: dict[str, list] = {}
    for s in splits:
        by_type.setdefault(s.get("splitType", "OTHER"), []).append(s)

    def _sp(s: dict) -> str:
        spd = s.get("averageSpeed", 0)
        hr  = s.get("averageHR", 0)
        gct = s.get("groundContactTime", 0)
        cad = s.get("averageRunCadence", 0)
        out = f"@{format_pace(spd)}" if spd else ""
        if hr:  out += f" HR{hr:.0f}"
        if gct: out += f" GCT{gct:.0f}ms"
        if cad: out += f" {cad:.0f}spm"
        return out.strip()

    parts = []

    wu = by_type.get("INTERVAL_WARMUP", [{}])[0]
    if wu.get("distance"):
        parts.append(i18n.t("lap_ctx.warmup", km=wu["distance"] / 1000, sp=_sp(wu)))

    act_list = by_type.get("INTERVAL_ACTIVE", [])
    if act_list:
        a   = act_list[0]
        n   = a.get("noOfSplits", 1)
        dist = a.get("distance", 0) / 1000
        per  = dist / n if n else dist
        label = f"{n}×{per:.2f}km" if n > 1 else f"{dist:.2f}km"
        parts.append(i18n.t("lap_ctx.main", label=label, sp=_sp(a)))

    rec = by_type.get("INTERVAL_RECOVERY", [{}])[0]
    if rec.get("averageSpeed"):
        parts.append(i18n.t("lap_ctx.recovery", sp=_sp(rec)))

    cd = by_type.get("INTERVAL_COOLDOWN", [{}])[0]
    if cd.get("distance"):
        parts.append(i18n.t("lap_ctx.cooldown", km=cd["distance"] / 1000, sp=_sp(cd)))

    # Continuous run (no interval structure) — show RWD_RUN dynamics only
    if not act_list:
        rwd = by_type.get("RWD_RUN", [{}])[0]
        gct = rwd.get("groundContactTime", 0)
        cad = rwd.get("averageRunCadence", 0)
        if gct or cad:
            dyn = []
            if cad: dyn.append(f"{cad:.0f}spm")
            if gct: dyn.append(f"GCT{gct:.0f}ms")
            parts.append(" ".join(dyn))

    return i18n.t("lap_ctx.splits_prefix") + " → ".join(parts) if parts else ""


def _prefetch_run_details(acts: list) -> None:
    """Pre-fetch and cache activity details for the most recent runs/rides
    (silent). Skips activities that already have full detail in SQLite."""
    fetched = 0
    try:
        with db.connect() as _conn:
            cached_aids = {
                aid for a in acts
                if (aid := a.get("activityId"))
                and db.has_full_detail(_conn, aid)
            }
    except Exception:
        cached_aids = set()

    # The activity-list dicts come from Garmin's get_activities_by_date,
    # whose top-level structure exposes `activityType.typeKey` rather than
    # the flat `activityTypeKey` _parse_activity emits. Probe both.
    def _type_key(a: dict) -> str:
        return (a.get("activityTypeKey")
                or (a.get("activityType") or {}).get("typeKey", ""))

    for a in acts:
        if fetched >= 5:
            break
        tk = _type_key(a)
        if not ("run" in tk or "cycling" in tk or "ride" in tk):
            continue
        aid = a.get("activityId")
        if not aid or aid in cached_aids:
            continue
        try:
            fetch_activity_detail(aid)
            fetched += 1
        except Exception:
            pass


# ── Activity detail (on-demand) ───────────────────────────────────────────────

def fetch_activity_detail(activity_id: int) -> dict:
    """SQLite is the single source of truth. Fast path: if SQLite already
    has full detail for this activity, return the legacy-shaped dict via
    db.get_activity_detail. Otherwise fetch from Garmin and persist to SQLite."""
    try:
        with db.connect() as _conn:
            if db.has_full_detail(_conn, activity_id):
                cached = db.get_activity_detail(_conn, activity_id)
                if cached is not None:
                    return cached
    except Exception as e:
        # DB unreachable — fall through to fresh fetch so the UI can still work
        print(f"[db] WARN has_full_detail check failed for {activity_id}: {e}")

    client = _client()

    base = client.get_activity(activity_id) or {}

    # Bypass lib's get_activity_details — its mandatory maxpoly=4000 default
    # over-fetches the GPS polyline ~10×. Calling connectapi directly omits
    # maxPolylineSize so polyline density matches pre-migration behavior.
    #
    # maxChartSize: Garmin's response is decimated when underlying samples
    # exceed maxChartSize. We size the cap to the activity's duration so we
    # get true 1Hz for any length (verified empirically: maxChart >= total
    # samples → no decimation; Garmin caps internally at total available).
    # firstSampleIndex pagination is broken on Garmin's end (returns same
    # data regardless of offset), so we can't paginate — must fit in one call.
    # If Garmin rejects very large maxChartSize (untested above ~10000),
    # fall back to 10000 and accept ~1 sample/min for ultra activities.
    duration_s      = int((base.get("summaryDTO") or {}).get("duration") or 2000)
    maxchart_target = max(2000, duration_s + 100)        # +100 buffer
    attempts        = [maxchart_target]
    if maxchart_target > 10000:
        attempts.append(10000)                            # fallback
    stream = None
    for mc in attempts:
        try:
            stream = client.client.connectapi(
                f"/activity-service/activity/{activity_id}/details",
                params={"maxChartSize": mc, "firstSampleIndex": 0},
            )
            if stream is not None:
                if mc != maxchart_target:
                    print(f"[gd] activity {activity_id}: fell back to maxChartSize={mc} "
                          f"(Garmin rejected {maxchart_target}); will be ~1 sample/min")
                break
        except Exception:
            stream = None

    # Validate the metric stream BEFORE persisting anything. If garth's
    # token is stale, garminconnect can return None / empty / structureless
    # response — pre-validate to avoid the silent "has_full_detail=1 with 0
    # metrics rows" bug class. The canonical "real data" sentinel is the
    # presence of metricDescriptors (the channel index), without which the
    # rest of the stream is uninterpretable.
    if not stream or not isinstance(stream, dict) or not stream.get("metricDescriptors"):
        raise EmptyDetailFetchError(
            f"activity {activity_id}: empty metric stream — Garmin session "
            "likely stale, or activity ID invalid"
        )

    try:
        hr_zones = client.get_activity_hr_in_timezones(activity_id)
    except Exception:
        hr_zones = None

    # Per-lap data (NOT aggregated by type — matches Garmin Connect web view)
    try:
        splits_resp = client.get_activity_splits(activity_id)
        laps = (splits_resp or {}).get("lapDTOs", [])
    except Exception:
        laps = []

    # Weather (Phase 1 — written to SQLite only, not JSON cache)
    try:
        weather = client.get_activity_weather(activity_id)
    except Exception:
        weather = None

    # Power zones — only meaningful for cycling activities. Match on cycling-
    # specific substrings: `cycl` (cycling/indoor_cycling/gravel_cycling/
    # cyclocross), `bik` (road_biking/mountain_biking/e_bike_*), `ride`
    # (virtual_ride). NOT `virtual` — Garmin also has virtual_run.
    type_key = ((base.get("activityTypeDTO") or {}).get("typeKey")
                or (base.get("summaryDTO") or {}).get("activityTypeKey", ""))
    power_zones = None
    if type_key and any(k in type_key for k in ("cycl", "bik", "ride")):
        try:
            power_zones = client.get_activity_power_in_timezones(activity_id)
        except Exception:
            power_zones = None

    # GPS from geoPolylineDTO (cleaner than parsing metric stream)
    gps = []
    if stream:
        for pt in (stream.get("geoPolylineDTO") or {}).get("polyline", []):
            if pt.get("valid") and pt.get("lat") and pt.get("lon"):
                gps.append({"lat": pt["lat"], "lon": pt["lon"], "time": pt.get("time")})

    summary_dto = base.get("summaryDTO", {}) or {}
    splits_dto  = base.get("splitSummaries", []) or []

    # Write full 1Hz detail into SQLite — single source of truth.
    # The return value is reconstructed via db.get_activity_detail so
    # callers see the same legacy shape they
    # always have (metrics thinned to ~10s for chart/legacy consumers).
    with db.connect() as _conn:
        full_metrics_rows = db.parse_full_metrics_stream(stream)
        unknown_chans     = db.extract_unknown_channels(stream)
        # Ensure the activities metadata row exists so upsert_activity_full_detail's
        # UPDATE doesn't silently no-op. For activities clicked outside of
        # _sync_detailed's window (unusual, but happens for older trends data),
        # the row may not be there yet.
        db.upsert_activity_metadata(_conn, {
            "activityId":              activity_id,
            "activityName":            summary_dto.get("activityName") or base.get("activityName"),
            "activityTypeKey":         ((base.get("activityTypeDTO") or {}).get("typeKey")
                                        or summary_dto.get("activityType")),
            "startTimeLocal":          summary_dto.get("startTimeLocal"),
            "startTimeGMT":            summary_dto.get("startTimeGMT"),
            "distance":                summary_dto.get("distance"),
            "duration":                summary_dto.get("duration"),
            "averageHR":               summary_dto.get("averageHR"),
            "maxHR":                   summary_dto.get("maxHR"),
            "averageSpeed":            summary_dto.get("averageSpeed"),
            "maxSpeed":                summary_dto.get("maxSpeed"),
            "elevationGain":           summary_dto.get("elevationGain"),
            "elevationLoss":           summary_dto.get("elevationLoss"),
            "calories":                summary_dto.get("calories"),
            "activityTrainingLoad":    summary_dto.get("activityTrainingLoad"),
            "aerobicTrainingEffect":   summary_dto.get("aerobicTrainingEffect"),
            "anaerobicTrainingEffect": summary_dto.get("anaerobicTrainingEffect"),
            "trainingEffectLabel":     summary_dto.get("trainingEffectLabel"),
            "vO2MaxValue":             summary_dto.get("vO2MaxValue"),
        }, raw_summary=summary_dto)

        db.upsert_activity_full_detail(
            _conn, activity_id,
            summary          = summary_dto,
            laps             = laps,
            splits           = splits_dto,
            hr_zones         = (hr_zones or []) if isinstance(hr_zones, list) else [],
            power_zones      = power_zones if isinstance(power_zones, list) else None,
            weather          = weather if isinstance(weather, dict) else None,
            metrics_rows     = full_metrics_rows,
            gps_points       = gps,
            unknown_channels = unknown_chans,
        )
        detail = db.get_activity_detail(_conn, activity_id)

    # detail can't be None here — we just wrote has_full_detail=1 — but
    # fall back to assembling the dict in-memory if something pathological
    # happens (e.g. concurrent wipe). Returning the freshly-fetched data
    # avoids surfacing a None to callers.
    if detail is None:
        return {
            "fetched_at":  datetime.now().isoformat(),
            "activity_id": activity_id,
            "summary":     summary_dto,
            "splits":      splits_dto,
            "laps":        laps,
            "gps":         gps,
            "metrics":     _parse_metrics_stream(stream),
            "hr_zones":    hr_zones or [],
        }
    return detail


# ── Load helpers (SQLite-only) ──────────────────────────────────────────────
# These reconstruct the legacy JSON-shaped dicts that the rest of the app
# (sidebar / trends / coach / build_coaching_context) consumes. Sources are
# exclusively SQLite tables. Returns None when nothing has been synced yet
# ("user never imported" signal that callers check via `if not detailed`).

def load_detailed() -> dict | None:
    with db.connect() as _conn:
        meta = db.get_app_metadata(_conn)
        acts = db.get_recent_activities(_conn, days=DETAILED_DAYS)
        # No activities AND no sync metadata = nothing imported yet
        if not acts and not meta.get("fetched_at"):
            return None
        return {
            "fetched_at":      meta.get("fetched_at", ""),
            "display_name":    meta.get("display_name", ""),
            "activities":      acts,
            "daily_summaries": db.get_recent_daily_summaries(_conn, days=DETAILED_DAYS),
            "sleep":           db.get_recent_sleep(_conn, days=DETAILED_DAYS),
            "hrv":             db.get_recent_hrv(_conn, days=DETAILED_DAYS),
            "training_status": db.get_recent_training_status(_conn, days=DETAILED_DAYS),
        }


def load_longterm() -> dict | None:
    with db.connect() as _conn:
        meta  = db.get_app_metadata(_conn)
        weeks = db.get_weekly_summary(_conn, weeks=LONGTERM_WEEKS)
        if not weeks and not meta.get("fetched_at"):
            return None
        from datetime import timedelta as _td
        start = (date.today() - _td(days=LONGTERM_WEEKS * 7 - 1)).isoformat()
        return {
            "fetched_at":  meta.get("fetched_at", ""),
            "weeks":       weeks,
            "total_weeks": len(weeks),
            "start_date":  start,
        }


# ── Chat / coaching report persistence (SQLite-backed) ──────────────────────
# These wrappers delegate to db.*. Function signatures + return shapes are
# preserved so that UI callers (passed as load_fn / save_fn / clear_fn into
# render_chat) work without changes.

def save_coaching_report(report: str, horizon: str = "24h") -> None:
    with db.connect() as _conn:
        db.coaching_report_set(_conn, report, horizon)


def load_review_report(activity_id: int) -> str | None:
    """Legacy fallback hook for _hydrate_review_chat. Returns None — the
    review report lives as chat_review msg_index=0, which load_review_chat
    already returns."""
    return None


def save_review_report(activity_id: int, report: str) -> None:
    """Deprecated — the review chat (which includes the report as msg 0)
    is now the source of truth. Kept as a no-op for any leftover caller."""
    return None


def clear_review_report(activity_id: int) -> None:
    """Deprecated — see save_review_report."""
    return None


def load_overall_chat() -> dict:
    """Return {messages, summary, summary_through_idx} for the overall thread."""
    with db.connect() as _conn:
        return db.overall_chat_load(_conn)


def save_overall_chat(messages: list, summary: str, summary_through_idx: int) -> None:
    """Persist the overall thread state."""
    with db.connect() as _conn:
        db.overall_chat_replace(_conn, messages, summary, summary_through_idx)


def clear_overall_chat() -> None:
    with db.connect() as _conn:
        db.overall_chat_clear(_conn)


def load_review_chat(activity_id: int) -> dict:
    """Return {messages, summary, summary_through_idx} for one activity's review."""
    with db.connect() as _conn:
        return db.review_chat_load(_conn, activity_id)


def save_review_chat(activity_id: int, messages: list, summary: str,
                     summary_through_idx: int) -> None:
    with db.connect() as _conn:
        db.review_chat_replace(_conn, activity_id, messages, summary, summary_through_idx)


def clear_review_chat(activity_id: int) -> None:
    with db.connect() as _conn:
        db.review_chat_clear(_conn, activity_id)


# ── LLM context builder ───────────────────────────────────────────────────────

def build_coaching_context(detailed: dict, longterm: dict | None = None,
                           user_cfg: dict | None = None) -> str:
    """Build the structured Garmin payload sent to the AI coach.

    ``user_cfg`` (the user_config dict) is optional; when provided, per-
    activity manual tags and 课表/备注 entered on the review page are
    injected into each activity entry, so the home-page chat can answer
    "刚才那次跑得怎么样" with full context, not just raw numbers.
    """
    tags     = (user_cfg or {}).get("activity_tags",     {})
    comments = (user_cfg or {}).get("activity_comments", {})
    today_str = date.today().isoformat()
    lines = [i18n.t("coach_ctx.header", date=today_str)]

    # ── Recent activities ─────────────────────────────────────────────────────
    acts = detailed.get("activities", [])
    lines.append(i18n.t("coach_ctx.recent_activities_header", days=DETAILED_DAYS, n=len(acts)))
    for a in acts[:15]:
        dt = a.get("startTimeLocal", "")[:10]
        tk = a.get("activityTypeKey", "")
        dist_km = (a.get("distance") or 0) / 1000
        dur = format_duration(a.get("duration"))
        parts = []
        if "run" in tk and a.get("averageSpeed"):
            parts.append(i18n.t("coach_ctx.avg_pace", pace=format_pace(a["averageSpeed"])))
        elif a.get("averageSpeed"):
            parts.append(i18n.t("coach_ctx.avg_speed", kmh=a["averageSpeed"] * 3.6))
        eg = a.get("elevationGain")
        if eg and eg > 5:
            parts.append(f"+{eg:.0f}m")
        if a.get("averageHR") and a.get("maxHR"):
            parts.append(i18n.t("coach_ctx.hr_avg_max", avg=a["averageHR"], max=a["maxHR"]))
        ae = a.get("aerobicTrainingEffect")
        te_key = a.get("trainingEffectLabel", "")
        te = i18n.t(f"te_label.{te_key}") if te_key else ""
        if ae:
            if te and not te.startswith("te_label."):
                parts.append(i18n.t("coach_ctx.te_with_label", te=ae, label=te))
            else:
                parts.append(i18n.t("coach_ctx.te_no_label", te=ae))
        if a.get("vO2MaxValue"):
            parts.append(f"VO₂Max {a['vO2MaxValue']:.0f}")
        lines.append(f"### {dt}  {display_type(tk)} {dist_km:.1f}km  [{dur}]")
        lines.append("  " + " | ".join(parts) if parts else "  —")

        # Manual annotations from the review page (tag + workout/note).
        aid_str = str(a.get("activityId") or "")
        if aid_str:
            tag = (tags.get(aid_str) or "").strip()
            cmt = (comments.get(aid_str) or "").strip()
            if tag:
                # tag is a stable key (e.g. 'aerobic_base') after P2 migration #8;
                # render the locale-appropriate label for the LLM context.
                tag_label = i18n.t(f"tag.{tag}") if tag else ""
                lines.append(i18n.t("coach_ctx.user_tag", label=tag_label))
            if cmt:
                lines.append(i18n.t("coach_ctx.user_comment", comment=cmt))

        # Enrich with detail (running dynamics + splits) from SQLite
        aid = a.get("activityId")
        if aid:
            try:
                with db.connect() as _conn:
                    det = db.get_activity_detail(_conn, aid)
            except Exception:
                det = None
            if det:
                ds = det.get("summary", {})
                dyn = []
                if ds.get("averageRunCadence"): dyn.append(i18n.t("coach_ctx.cadence", c=ds["averageRunCadence"]))
                if ds.get("groundContactTime"):  dyn.append(i18n.t("coach_ctx.gct", gct=ds["groundContactTime"]))
                if ds.get("strideLength"):        dyn.append(i18n.t("coach_ctx.stride", s=ds["strideLength"]))
                if ds.get("verticalOscillation"): dyn.append(i18n.t("coach_ctx.vert_osc", v=ds["verticalOscillation"]))
                if ds.get("normalizedPower"):      dyn.append(i18n.t("coach_ctx.normalized_power", np=ds["normalizedPower"]))
                if dyn:
                    lines.append("  " + " | ".join(dyn))
                # Prefer per-lap data; fall back to splits-by-type
                split_line = (_format_laps_ctx(det.get("laps", []))
                              or _format_splits_ctx(det.get("splits", [])))
                if split_line:
                    lines.append(split_line)

    # ── Long-term 6-month summary ─────────────────────────────────────────────
    if longterm and longterm.get("weeks"):
        weeks = longterm["weeks"]
        lines.append(i18n.t("coach_ctx.longterm_header", n=len(weeks)))
        lines.append(i18n.t("coach_ctx.longterm_table"))
        for w in weeks[-26:]:
            lines.append(
                f"| {w['week']} | {w['runKm']} | {w['rideKm']} "
                f"| {w['activities']} | {w['weeklyLoad']} |"
            )

    return "\n".join(lines)
