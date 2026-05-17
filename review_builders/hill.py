"""HillBuilder — for the `hill` tag.

Hill repeats are structurally a sub-type of intervals (warmup → uphill reps
+ rest → cooldown), so this builder borrows IntervalBuilder's full pipeline
(lap classification + work-rep clustering + HRR per rest + cross-rep drift)
and adds the hill-specific overlays:

Output language: context_md is emitted in **neutral English**. The LLM's
response language is steered by the per-tag prompt (P5), not the builder.

  1. **Grade context per rep** — every rep row carries avg_grade%, elev_gain,
     and GAP (grade-adjusted pace). Raw pace is meaningless on a climb;
     a 6:00/km on +10% is faster effort than 4:30/km on flat.

  2. **HR vs grade slope** (structure-agnostic drift reading) — bpm per
     +1% grade across the whole activity. High slope = uphill capacity is
     the bottleneck; low slope + low R² = HR is already maxed and grade
     stops being the dominant variable.

  3. **Cadence step-down detection** (per rep) — last 10% of rep vs the
     rest. >3 spm drop = elastic running has shifted to "hard stomping",
     the canonical hill-rep form-crack early signal. This shows up
     BEFORE pace fade — the most actionable signal.

  4. **Power fade within rep** (per rep, if power_w available) — first
     half avg vs second half. >5% drop in a single rep = energy already
     spent within that rep; consistent across reps = rep count picked
     too high.

  5. **Per-rep GAP** — Garmin's directGradeAdjustedSpeed when available,
     Minetti-2002 fallback otherwise.

Coaching truths that drive these signals:
  - Per-rep consistency > absolute pace. Same as intervals.
  - Power / pace dropping rep-to-rep = fast-twitch fibers cooked.
  - Cadence step-down at rep tail = elastic running collapse → next stop.
  - HR vs grade slope reveals whether uphill ability is the actual ceiling.
  - HRR thresholds are age-dependent (linear `Base_30 - (age-30)×0.5` bpm).

Heuristic lap classification + cluster thresholds inherited from
IntervalBuilder. Hill workouts typically have all reps similar distance
(unlike intervals which can mix 3000m + 800m), so most workouts will
produce a single cluster.
"""

import sqlite3

from review_builders.base       import BuildResult, ReviewBuilder, lap_windows_from_db
from review_builders.default    import DefaultBuilder
from review_builders.primitives import (
    fmt_pace, fmt_pace_compact, hr_drift, internal_stats, pa_hr_split,
    pairwise_delta,
)


class HillBuilder(ReviewBuilder):
    name = "HillBuilder"

    # ── Heuristic thresholds (inherited from IntervalBuilder) ──────────────
    REST_DIST_M_MAX            = 200       # rest lap distance ceiling
    REST_PACE_S_PER_KM         = 9 * 60    # rest pace floor (slower than 9:00/km)
    NOISE_DUR_S_MAX            = 20
    NOISE_DIST_M_MAX           = 50
    WARMUP_COOLDOWN_DIST_M_MIN = 1000      # warmup/cooldown must be ≥1km
    WARMUP_PACE_DELTA_S        = 30        # slower than work median by ≥30s/km
    REP_CLUSTER_TOL_PCT        = 20
    CONSISTENCY_BAND_PCT       = 5
    CONSISTENCY_HOLD_S         = 8
    REP_INTERNAL_HALVES_MIN_S  = 60

    # ── Hill-specific thresholds ───────────────────────────────────────────
    GRADE_SMOOTHING_S          = 30        # window for per-sample grade %
    STEP_DOWN_TAIL_FRAC        = 0.10      # last 10% of rep
    STEP_DOWN_MIN_REP_S        = 60        # skip step-down on shorter reps
    STEP_DOWN_FLAG_SPM         = 3         # ≥3 spm drop is the actionable signal

    # Within-lap uphill segment detection — finds the actual "push" portion
    # of a lap when the user's manual lap includes both uphill + walk-back.
    # If isolated uphill segment is < UPHILL_SEG_LAP_FRAC_MAX of the lap,
    # the per-lap averages are diluted by non-push content and we surface
    # the uphill-only stats as a sub-bullet.
    UPHILL_GRADE_PCT_MIN       = 3.0       # min grade to count as "uphill push"
    UPHILL_MIN_DURATION_S      = 20        # min uphill segment duration to report
    UPHILL_SEG_LAP_FRAC_MAX    = 0.85      # only show if uphill < 85% of lap
                                           # (else it's basically the whole lap)

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag == "hill"

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep = self._hill_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Hill deep analysis ───────────────────────────────────────────────────

    def _hill_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
        # Broadest column set hill needs: primitives' canonical 7 + distance_cum_m
        # + elevation_m + grade_adj_speed + power_w. Indices below assume this order.
        rows = conn.execute("""
            SELECT sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio,
                   stride_cm, distance_cum_m, elevation_m, grade_adj_speed,
                   power_w
              FROM activity_metrics
             WHERE activity_id = ?
             ORDER BY sec_offset
        """, (aid,)).fetchall()
        if not rows:
            return ""

        # Canonical 8-column subset for primitives that don't need elev/power
        canonical = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]

        if len(rows) >= 3:
            deltas = sorted(rows[i+1][0] - rows[i][0] for i in range(len(rows)-1))
            median_delta = max(1, deltas[len(deltas)//2])
        else:
            median_delta = 1

        lap_windows = lap_windows_from_db(conn, aid)
        if not lap_windows:
            return ""

        labeled = self._classify_laps(lap_windows)
        if not labeled:
            return ""

        # Smoothed grade per sample (None where elev/dist unavailable)
        grades = self._compute_smoothed_grades(rows, median_delta)

        out = [
            "## ⛰️ Hill-specific analysis",
            "",
            "_Data + heuristic classification + coach-consensus reference "
            "thresholds. Verdict is yours (LLM) to synthesize from the "
            "activity tag, the user's notes (**the workout described in the "
            "comment is the most authoritative source of structure**), "
            "personal_note, and long-term memory. Hill is a sub-type of "
            "intervals (warmup → uphill reps + rest → cooldown). **Raw pace "
            "must always be read in the context of grade** — 6:00/km on +10% "
            "is a much fiercer effort than 4:30/km on flat. Every rep below "
            "carries avg_grade% + GAP (grade-adjusted pace)._",
        ]

        # ── (1) Auto lap classification ─────────────────────────────────
        out.extend(self._render_lap_classification(labeled))

        # ── (2) Work cluster details (each rep has grade + GAP + power) ─
        work_laps = [l for l in labeled if l['kind'] == 'work']
        clusters  = self._cluster_work_reps(work_laps)
        if clusters:
            out.extend(self._render_clusters(clusters, rows, grades, median_delta))

        # ── (3) Recovery HR drop (within each rest lap) ─────────────────
        rest_laps = [l for l in labeled if l['kind'] == 'rest']
        if rest_laps:
            out.extend(self._render_recovery_hrr(rest_laps, rows))

        # ── (4) Cross-rep decay (per cluster with ≥2 reps) ──────────────
        for cluster in clusters:
            if len(cluster['reps']) >= 2:
                out.extend(self._render_cross_rep_drift(cluster, rows))

        # ── (5) Structure-agnostic readings (HR-vs-grade slope + HR drift + Pa:HR) ─
        out.extend(self._render_drift_section(canonical, rows, grades))

        # ── (6) Tool availability ───────────────────────────────────────
        out.extend(self._tool_hint_section())

        return "\n".join(out)

    # ── Grade computation (mirrors TrailBuilder) ─────────────────────────────

    def _compute_smoothed_grades(self, rows, median_delta) -> list:
        """Per-sample grade % using a centered window of (Δelev / Δdist).
        Returns None per-sample when elev/dist data unavailable."""
        half_window = max(1, (self.GRADE_SMOOTHING_S // 2) // median_delta)
        grades = []
        for i in range(len(rows)):
            start_i = max(0, i - half_window)
            end_i   = min(len(rows) - 1, i + half_window)
            elev_s, elev_e = rows[start_i][8], rows[end_i][8]
            dist_s, dist_e = rows[start_i][7], rows[end_i][7]
            if (elev_s is None or elev_e is None
                or dist_s is None or dist_e is None):
                grades.append(None); continue
            dd = dist_e - dist_s
            if dd < 1:
                grades.append(None); continue
            grades.append((elev_e - elev_s) / dd * 100)
        return grades

    @staticmethod
    def _minetti_factor(grade_pct: float) -> float:
        """Minetti et al. 2002 metabolic cost relative to flat. Fallback
        when Garmin's directGradeAdjustedSpeed isn't recorded."""
        g = grade_pct / 100
        cost = (155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3
                + 46.3 * g**2 + 19.5 * g + 3.6)
        return max(0.3, cost / 3.6)

    def _find_uphill_segment(self, rows, grades, start_sec, end_sec) -> dict | None:
        """Find the longest contiguous uphill (≥UPHILL_GRADE_PCT_MIN) segment
        within [start_sec, end_sec). Returns None if no qualifying segment
        OR the segment covers >UPHILL_SEG_LAP_FRAC_MAX of the lap (in which
        case the lap is basically all uphill — no point splitting it out).

        Returns: {start_sec, end_sec, dur_s, dist_m, hr_avg, gap_s_per_km,
        avg_grade_pct, elev_gain_m, power_w}.

        Used to surface the "actual push portion" of a lap when the user
        manually lapped "uphill rep + walk back down" — the whole-lap GAP
        + HR averages get diluted by the non-push tail and miss the real
        rep effort signal.
        """
        # Walk through samples, find contiguous runs of grade >= threshold
        seg_rows = [(i, r) for i, r in enumerate(rows)
                    if start_sec <= r[0] < end_sec]
        if len(seg_rows) < 4:
            return None

        runs: list[tuple[int, int]] = []   # (start_idx_in_rows, end_idx_in_rows)
        cur_start = None
        for global_i, _ in seg_rows:
            g = grades[global_i] if global_i < len(grades) else None
            if g is not None and g >= self.UPHILL_GRADE_PCT_MIN:
                if cur_start is None:
                    cur_start = global_i
            else:
                if cur_start is not None:
                    runs.append((cur_start, global_i - 1))
                    cur_start = None
        if cur_start is not None:
            runs.append((cur_start, seg_rows[-1][0]))

        # Pick longest by duration
        if not runs:
            return None
        runs_with_dur = [(s, e, rows[e][0] - rows[s][0]) for s, e in runs]
        runs_with_dur.sort(key=lambda x: x[2], reverse=True)
        s_idx, e_idx, dur = runs_with_dur[0]
        if dur < self.UPHILL_MIN_DURATION_S:
            return None

        # If the uphill basically IS the whole lap, no point separating
        lap_dur = end_sec - start_sec
        if dur / lap_dur > self.UPHILL_SEG_LAP_FRAC_MAX:
            return None

        # Compute uphill-segment stats
        uphill_start_sec = rows[s_idx][0]
        uphill_end_sec   = rows[e_idx][0] + 1
        seg = rows[s_idx:e_idx + 1]
        hrs   = [r[1] for r in seg if r[1] is not None]
        spds  = [r[2] for r in seg if r[2] and r[2] > 0.5]
        powers = [r[10] for r in seg if r[10] is not None]

        ctx = self._grade_context_for_window(rows, grades, uphill_start_sec, uphill_end_sec)
        # Distance from cumulative distance column
        d_first = next((r[7] for r in seg if r[7] is not None), None)
        d_last  = next((r[7] for r in reversed(seg) if r[7] is not None), None)
        dist_m  = (d_last - d_first) if (d_first is not None and d_last is not None) else None

        return {
            "start_sec":     uphill_start_sec,
            "end_sec":       uphill_end_sec,
            "dur_s":         int(uphill_end_sec - uphill_start_sec),
            "dist_m":        dist_m,
            "hr_avg":        (sum(hrs) / len(hrs)) if hrs else None,
            "gap_s_per_km":  ctx.get("gap_s_per_km"),
            "avg_grade_pct": ctx.get("avg_grade_pct"),
            "elev_gain_m":   ctx.get("elev_gain_m"),
            "power_w":       (sum(powers) / len(powers)) if powers else None,
        }

    def _grade_context_for_window(self, rows, grades, start_sec, end_sec) -> dict:
        """Aggregate grade context for a [start_sec, end_sec) window.
        Returns avg_grade_pct, max_grade_pct, elev_gain_m, elev_loss_m, gap_s_per_km.
        Values are None when data unavailable."""
        seg = [(i, r) for i, r in enumerate(rows) if start_sec <= r[0] < end_sec]
        if not seg:
            return {}
        idxs = [i for i, _ in seg]
        seg_grades = [grades[i] for i in idxs if grades[i] is not None]

        elevs = [r[8] for _, r in seg if r[8] is not None]
        if len(elevs) >= 2:
            gain = sum(max(0, elevs[i+1] - elevs[i]) for i in range(len(elevs) - 1))
            loss = sum(max(0, elevs[i] - elevs[i+1]) for i in range(len(elevs) - 1))
        else:
            gain = loss = None

        avg_grade = (sum(seg_grades) / len(seg_grades)) if seg_grades else None
        max_grade = max(seg_grades) if seg_grades else None

        # GAP — Garmin's grade_adj_speed first; Minetti fallback
        gas = [r[9] for _, r in seg if r[9] and r[9] > 0.5]
        if gas:
            avg_gas = sum(gas) / len(gas)
            gap_s   = 1000 / avg_gas if avg_gas > 0 else None
        else:
            adjusted = []
            for i, r in seg:
                g = grades[i]
                if r[2] and r[2] > 0.5 and g is not None:
                    f = self._minetti_factor(g)
                    if f > 0:
                        adjusted.append(r[2] / f)   # speed_flat_equivalent
            if adjusted:
                avg_adj = sum(adjusted) / len(adjusted)
                gap_s   = 1000 / avg_adj if avg_adj > 0 else None
            else:
                gap_s = None

        return {
            "avg_grade_pct": avg_grade,
            "max_grade_pct": max_grade,
            "elev_gain_m":   gain,
            "elev_loss_m":   loss,
            "gap_s_per_km":  gap_s,
        }

    @staticmethod
    def _tool_hint_section() -> list[str]:
        return [
            "",
            "### Tool availability",
            "- **`get_window_stats(start, end, key_type)`** — the preferred "
            "aggregation tool for hill workouts. Returns HR / pace / "
            "mechanics averages + percentiles **plus a `grade` block "
            "(`avg_grade_pct`, `elev_gain_m`, `elev_loss_m`, "
            "`gap_pace_s_per_km`)**. One call gives you the full grade "
            "context — well suited to questions like \"power in the first "
            "30s of rep N\", \"cadence + GCT in the last 15s of a rep\", "
            "\"is HR still on a plateau in the first 5-15s of a rest\", "
            "or \"compare same-grade segments across reps\". "
            "`key_type='time'` indexes by seconds, `key_type='distance'` "
            "by meters.",
            "- `get_raw_window_by_time(start_seconds, end_seconds, "
            "channels?)` — 1Hz raw rows. Add `\"elevation\"` to channels "
            "to get the elevation timeseries. Use this for **timeseries** "
            "questions (\"did HR jump at sec X?\", \"what is the power "
            "curve shape?\"); for averages use `get_window_stats`.",
            "- `get_raw_window_by_distance(start_meters, end_meters, "
            "channels?)` — same as above but indexed by distance.",
            "- By default, prefer the cluster / cross-rep / HRR sections "
            "the builder provides; only call a tool when the slice "
            "granularity is insufficient OR you need extra grade context.",
        ]

    # ── Lap classification heuristic (lifted from IntervalBuilder) ──────────

    def _classify_laps(self, lap_windows) -> list[dict]:
        """Heuristic warmup/work/rest/cooldown/noise classification.

        Returns list of dicts with {lap_id, kind, start_sec, end_sec,
        dist_m, dur_s, pace_s_per_km}, in chronological order.
        """
        items = []
        for lap in lap_windows:
            dur  = lap['dur_s']
            dist = lap['dist_m']
            pace = (1000 * dur / dist) if dist > 0 and dur > 0 else None
            items.append({
                'lap_id':        lap['lap_id'],
                'start_sec':     lap['start_sec'],
                'end_sec':       lap['end_sec'],
                'dist_m':        dist,
                'dur_s':         dur,
                'pace_s_per_km': pace,
                'kind':          None,
            })
        if not items:
            return []

        # Pre-tag noise + rest
        for it in items:
            if it['dur_s'] < self.NOISE_DUR_S_MAX and it['dist_m'] < self.NOISE_DIST_M_MAX:
                it['kind'] = 'noise'
            elif (it['dist_m'] < self.REST_DIST_M_MAX
                  and it['pace_s_per_km'] is not None
                  and it['pace_s_per_km'] > self.REST_PACE_S_PER_KM):
                it['kind'] = 'rest'

        # Median work-candidate pace
        work_pace_candidates = [
            it['pace_s_per_km'] for it in items
            if it['kind'] is None
            and it['dist_m'] >= self.REST_DIST_M_MAX
            and it['pace_s_per_km'] is not None
        ]
        median_work_pace = None
        if work_pace_candidates:
            sorted_p = sorted(work_pace_candidates)
            median_work_pace = sorted_p[len(sorted_p) // 2]

        # Warmup + cooldown
        unmarked = [it for it in items if it['kind'] is None]
        if unmarked and median_work_pace is not None:
            first = unmarked[0]
            if (first['dist_m'] >= self.WARMUP_COOLDOWN_DIST_M_MIN
                and first['pace_s_per_km'] is not None
                and first['pace_s_per_km'] > median_work_pace + self.WARMUP_PACE_DELTA_S):
                first['kind'] = 'warmup'

            last = unmarked[-1]
            if (last is not first
                and last['dist_m'] >= self.WARMUP_COOLDOWN_DIST_M_MIN
                and last['pace_s_per_km'] is not None
                and last['pace_s_per_km'] > median_work_pace + self.WARMUP_PACE_DELTA_S):
                last['kind'] = 'cooldown'

        # Everything still unmarked → work
        for it in items:
            if it['kind'] is None:
                it['kind'] = 'work'
        return items

    def _render_lap_classification(self, labeled) -> list[str]:
        out = ["", "### Lap auto-classification (heuristic — cross-check with the user's comment)"]
        out.append("- _Rules_: rest = dist <200m AND pace >9:00/km; "
                   "warmup / cooldown = first or last lap with dist >1km AND "
                   "pace slower than the work median by >30s/km; "
                   "noise = dur <20s AND dist <50m; everything else = work.")
        out.append("")
        for it in labeled:
            pace_str = (f"{int(it['pace_s_per_km']//60)}:{int(it['pace_s_per_km']%60):02d}/km"
                        if it['pace_s_per_km'] is not None else "—")
            out.append(f"- Lap {it['lap_id']}  "
                       f"(sec {it['start_sec']}-{it['end_sec']}, "
                       f"{it['start_sec']//60}-{it['end_sec']//60}min, "
                       f"{it['dist_m']:.0f}m, {it['dur_s']}s, {pace_str})  "
                       f"→  **{it['kind']}**")
        return out

    # ── Work-rep clustering ─────────────────────────────────────────────────

    def _cluster_work_reps(self, work_laps) -> list[dict]:
        """Group work reps by distance similarity (±20%)."""
        if not work_laps:
            return []
        sorted_laps = sorted(work_laps, key=lambda l: l['dist_m'])
        clusters: list[dict] = []
        for lap in sorted_laps:
            placed = False
            for cluster in clusters:
                rep_dist = cluster['target_dist_m']
                if abs(lap['dist_m'] - rep_dist) / rep_dist <= self.REP_CLUSTER_TOL_PCT / 100:
                    cluster['reps'].append(lap)
                    cluster['target_dist_m'] = (
                        sum(r['dist_m'] for r in cluster['reps']) / len(cluster['reps'])
                    )
                    placed = True
                    break
            if not placed:
                clusters.append({'target_dist_m': lap['dist_m'], 'reps': [lap]})
        for c in clusters:
            c['reps'].sort(key=lambda r: r['lap_id'])
        clusters.sort(key=lambda c: c['reps'][0]['start_sec'])
        return clusters

    def _render_clusters(self, clusters, rows, grades, median_delta) -> list[str]:
        out = []
        for ci, cluster in enumerate(clusters, 1):
            target = cluster['target_dist_m']
            n_reps = len(cluster['reps'])
            out.append("")
            out.append(f"### Work Cluster {ci}  ({n_reps} × ~{target:.0f}m)")
            for rep in cluster['reps']:
                stats = self._rep_internal_stats(rows, rep, median_delta)
                grade_ctx = self._grade_context_for_window(
                    rows, grades, rep['start_sec'], rep['end_sec'])
                self._render_rep_block(out, rep, stats, grade_ctx, rows, grades)
        return out

    # ── Per-rep internal stats (incl. time-to-consistency) ─────────────────

    def _rep_internal_stats(self, rows, rep, median_delta) -> dict | None:
        seg = [r for r in rows if rep['start_sec'] <= r[0] < rep['end_sec']]
        if not seg:
            return None
        hrs   = [r[1] for r in seg if r[1] is not None]
        spds  = [r[2] for r in seg if r[2] and r[2] > 0.5]
        cads  = [r[3] for r in seg if r[3] and r[3] > 50]
        gcts  = [r[4] for r in seg if r[4]]
        vrs   = [r[5] for r in seg if r[5] is not None]
        strds = [r[6] for r in seg if r[6] and r[6] > 30]
        powers = [r[10] for r in seg if r[10] is not None]

        if not hrs or not spds:
            return None

        avg_hr  = sum(hrs)  / len(hrs)
        peak_hr = max(hrs)
        avg_pace = (1000 * rep['dur_s'] / rep['dist_m']) if rep['dist_m'] > 0 else None

        avg_spd = sum(spds) / len(spds)
        if len(spds) >= 2:
            var = sum((s - avg_spd) ** 2 for s in spds) / len(spds)
            cv  = (var ** 0.5) / avg_spd if avg_spd > 0 else 0
        else:
            cv = 0

        ttc = self._time_to_consistency(seg, median_delta)

        # Internal halves + HR drift only meaningful for ≥60s reps
        halves_internal = None
        if rep['dur_s'] >= self.REP_INTERNAL_HALVES_MIN_S:
            # Pass canonical 8-col subset to internal_stats
            seg_canonical = [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
                             for r in seg]
            halves_internal = internal_stats(seg_canonical)

        return {
            'avg_hr':          avg_hr,
            'peak_hr':         peak_hr,
            'avg_pace':        avg_pace,
            'cv_speed':        cv,
            'ttc_s':           ttc,
            'cad':             (sum(cads)/len(cads)) if cads else None,
            'gct':             (sum(gcts)/len(gcts)) if gcts else None,
            'vr':              (sum(vrs)/len(vrs))   if vrs  else None,
            'stride_m':        (sum(strds)/len(strds))/100 if strds else None,
            'avg_power_w':     (sum(powers)/len(powers)) if powers else None,
            'halves_internal': halves_internal,
        }

    def _time_to_consistency(self, seg, median_delta) -> int | None:
        spd_rows = [(r[0], r[2]) for r in seg if r[2] and r[2] > 0.5]
        if len(spd_rows) < 4:
            return None
        speeds = [s for _, s in spd_rows]
        sorted_s = sorted(speeds)
        median_s = sorted_s[len(sorted_s) // 2]
        band_low  = median_s * (1 - self.CONSISTENCY_BAND_PCT / 100)
        band_high = median_s * (1 + self.CONSISTENCY_BAND_PCT / 100)
        hold_n = max(1, self.CONSISTENCY_HOLD_S // median_delta)
        if hold_n > len(spd_rows):
            return None
        rep_start = seg[0][0]
        for i in range(len(spd_rows) - hold_n + 1):
            window = spd_rows[i:i + hold_n]
            if all(band_low <= s <= band_high for _, s in window):
                return int(spd_rows[i][0] - rep_start)
        return None

    def _render_rep_block(self, out, rep, stats, grade_ctx, rows, grades=None) -> None:
        sec_range = f"sec {rep['start_sec']}-{rep['end_sec']}"
        if stats is None:
            out.append(f"- Lap {rep['lap_id']} ({sec_range}, {rep['dist_m']:.0f}m, "
                       f"{rep['dur_s']}s): _insufficient data_")
            return
        pace_str = f"{int(stats['avg_pace']//60)}:{int(stats['avg_pace']%60):02d}/km"
        ttc_str  = (f"stabilized in {stats['ttc_s']}s"
                    if stats['ttc_s'] is not None else "did not stabilize")

        # Grade headline — most distinctive thing about a hill rep
        grade_bits = []
        if grade_ctx.get('avg_grade_pct') is not None:
            grade_bits.append(f"grade {grade_ctx['avg_grade_pct']:+.1f}%")
        if grade_ctx.get('elev_gain_m') is not None:
            grade_bits.append(f"+{grade_ctx['elev_gain_m']:.0f}m")
        if grade_ctx.get('gap_s_per_km') is not None:
            grade_bits.append(f"GAP {fmt_pace_compact(grade_ctx['gap_s_per_km'])}")
        grade_str = " | ".join(grade_bits) if grade_bits else "no grade data"

        extras = []
        if stats['cad']         is not None: extras.append(f"cadence {stats['cad']:.0f}")
        if stats['stride_m']    is not None: extras.append(f"stride {stats['stride_m']:.2f}m")
        if stats['gct']         is not None: extras.append(f"GCT {stats['gct']:.0f}ms")
        if stats['vr']          is not None: extras.append(f"vertical ratio {stats['vr']:.1f}%")
        if stats['avg_power_w'] is not None: extras.append(f"power {stats['avg_power_w']:.0f}W")
        extras_str = " | ".join(extras)

        out.append(f"- **Lap {rep['lap_id']}** ({sec_range}, {rep['dist_m']:.0f}m, "
                   f"{rep['dur_s']}s @ {pace_str}; **{grade_str}**): "
                   f"HR avg {stats['avg_hr']:.0f} / peak {stats['peak_hr']:.0f} | "
                   f"CV {stats['cv_speed']*100:.1f}% | "
                   f"{ttc_str} | {extras_str}")

        # Within-lap uphill push segment — surfaces the actual rep effort
        # when the lap includes a walk-back-down (or any non-push tail).
        # If no significant uphill OR uphill ≈ whole lap → skip (nothing
        # to add over the whole-lap stats).
        if grades is not None:
            uphill = self._find_uphill_segment(rows, grades,
                                               rep['start_sec'], rep['end_sec'])
            if uphill is not None:
                bits = [f"sec {uphill['start_sec']}-{uphill['end_sec']}"]
                if uphill['dist_m']: bits.append(f"{uphill['dist_m']:.0f}m")
                bits.append(f"{uphill['dur_s']}s")
                if uphill['avg_grade_pct'] is not None:
                    bits.append(f"grade {uphill['avg_grade_pct']:+.1f}%")
                if uphill['elev_gain_m'] is not None:
                    bits.append(f"+{uphill['elev_gain_m']:.0f}m")
                head = " | ".join(bits)
                stat_bits = []
                if uphill['hr_avg'] is not None:
                    stat_bits.append(f"HR {uphill['hr_avg']:.0f}")
                if uphill['gap_s_per_km'] is not None:
                    stat_bits.append(f"GAP {fmt_pace_compact(uphill['gap_s_per_km'])}")
                if uphill['power_w'] is not None:
                    stat_bits.append(f"power {uphill['power_w']:.0f}W")
                stat_str = " | ".join(stat_bits)
                out.append(f"  - **🎯 Uphill push segment (auto-detected)**: "
                           f"{head} → {stat_str} _(the whole lap includes "
                           f"non-uphill content → this segment is the actual "
                           f"rep effort; compare it against the lap-overall "
                           f"GAP/HR to judge whether the lap framing is "
                           f"appropriate)_")

        # Internal halves + HR drift sub-bullets (≥60s reps only)
        halves = stats.get('halves_internal')
        if halves:
            fh, sh = halves.get('first_half'), halves.get('second_half')
            if fh and sh:
                d = pairwise_delta(fh, sh)
                parts = []
                if d.get('hr_delta') is not None:
                    parts.append(f"HR {fh['hr_avg']:.0f}→{sh['hr_avg']:.0f} ({d['hr_delta']:+.0f})")
                if d.get('pace_delta_s') is not None:
                    parts.append(f"pace {fmt_pace_compact(fh['pace_s_per_km'])}→"
                                 f"{fmt_pace_compact(sh['pace_s_per_km'])} ({d['pace_delta_s']:+.0f}s)")
                mech_bits = []
                if d.get('cadence_delta') is not None:
                    mech_bits.append(f"cadence {d['cadence_delta']:+.0f}")
                if d.get('gct_delta') is not None:
                    mech_bits.append(f"GCT {d['gct_delta']:+.0f}ms")
                if d.get('vr_delta') is not None:
                    mech_bits.append(f"vertical ratio {d['vr_delta']:+.2f}pt")
                if d.get('stride_delta') is not None:
                    mech_bits.append(f"stride {d['stride_delta']*100:+.0f}cm")
                line = "  - Internal first-half vs second-half: " + " | ".join(parts)
                if mech_bits:
                    line += " | Mechanics: " + ", ".join(mech_bits)
                out.append(line)

            hrd = halves.get('hr_drift')
            if hrd:
                out.append(f"  - Internal HR-time drift: {hrd['slope_per_min']:+.2f} bpm/min, "
                           f"R²={hrd['r_squared']:.2f}")

        # Cadence step-down (hill-specific late-rep form crack)
        sd = self._cadence_step_down(rows, rep['start_sec'], rep['end_sec'])
        if sd is not None:
            tail_pct = sd['tail_frac'] * 100
            if sd['step_down_spm'] >= self.STEP_DOWN_FLAG_SPM:
                out.append(f"  - ⚠️ **Final-segment cadence step-down**: "
                           f"last {tail_pct:.0f}% averages "
                           f"{sd['tail_avg']:.0f} vs leading segment "
                           f"{sd['lead_avg']:.0f} "
                           f"(-{sd['step_down_spm']:.0f}spm) "
                           f"_(>{self.STEP_DOWN_FLAG_SPM} spm = elasticity is "
                           f"gone, runner has shifted to hard stomping; in "
                           f"hill workouts this is the canonical failure "
                           f"signal — usually appears BEFORE pace breaks down)_")
            else:
                out.append(f"  - Final-segment cadence: last {tail_pct:.0f}% "
                           f"{sd['tail_avg']:.0f} vs leading "
                           f"{sd['lead_avg']:.0f} "
                           f"({-sd['step_down_spm']:+.0f}spm, no step-down).")

        # Power fade (≥60s reps only)
        pwr = self._power_fade(rows, rep['start_sec'], rep['end_sec'])
        if pwr is not None:
            out.append(f"  - Internal power: first half {pwr['first_half_w']:.0f}W → "
                       f"second half {pwr['second_half_w']:.0f}W "
                       f"({pwr['delta_pct']:+.1f}%) "
                       f"_(second-half drop >5% = energy already spent within "
                       f"the rep; recurring across reps = rep count picked too high)_")

    @staticmethod
    def _cadence_step_down(rows, start_sec, end_sec) -> dict | None:
        seg = [r for r in rows if start_sec <= r[0] < end_sec and r[3] is not None]
        if len(seg) < 30:
            return None
        dur = seg[-1][0] - seg[0][0]
        if dur < HillBuilder.STEP_DOWN_MIN_REP_S:
            return None
        tail_cutoff = seg[-1][0] - dur * HillBuilder.STEP_DOWN_TAIL_FRAC
        tail = [r[3] for r in seg if r[0] >= tail_cutoff]
        lead = [r[3] for r in seg if r[0] <  tail_cutoff]
        if not tail or not lead:
            return None
        tail_avg = sum(tail) / len(tail)
        lead_avg = sum(lead) / len(lead)
        return {
            "tail_frac":     HillBuilder.STEP_DOWN_TAIL_FRAC,
            "tail_avg":      tail_avg,
            "lead_avg":      lead_avg,
            "step_down_spm": lead_avg - tail_avg,
        }

    @staticmethod
    def _power_fade(rows, start_sec, end_sec) -> dict | None:
        seg = [r for r in rows if start_sec <= r[0] < end_sec and r[10] is not None]
        if len(seg) < 30:
            return None
        dur = seg[-1][0] - seg[0][0]
        if dur < HillBuilder.STEP_DOWN_MIN_REP_S:
            return None
        mid_sec = (seg[0][0] + seg[-1][0]) / 2
        first  = [r[10] for r in seg if r[0] <  mid_sec]
        second = [r[10] for r in seg if r[0] >= mid_sec]
        if not first or not second:
            return None
        fa = sum(first) / len(first)
        sa = sum(second) / len(second)
        return {
            "first_half_w":  fa,
            "second_half_w": sa,
            "delta_pct":     ((sa - fa) / fa) * 100 if fa > 0 else 0,
        }

    # ── Recovery HR drop per rest lap (lifted from IntervalBuilder) ────────

    def _render_recovery_hrr(self, rest_laps, rows) -> list[str]:
        out = ["", "### Recovery HR Drop (within-rest-lap recovery curve)"]
        for rest in rest_laps:
            hrr = self._hrr_for_rest(rows, rest)
            if hrr is None:
                continue
            self._render_hrr_block(out, rest, hrr)
        out.append("")
        out.append("- _Reference thresholds (30-year-old active runner, "
                   "**60s drop**)_: <15bpm = severely insufficient / 20-30 = "
                   "standard / >35 = elite. **Note: the 35bpm threshold "
                   "applies specifically to the 60s drop**; on hills, the "
                   "rest is often a walk-down, so the 60s drop tends to be "
                   "larger than for flat intervals.")
        out.append("- _Early-30s share_: >60% = parasympathetic switches in "
                   "quickly; <40% **does NOT necessarily mean slow switching** "
                   "— if HR is still on a plateau in the first 5-15s of the "
                   "rest (post-effort lag), a low share % is normal.")
        out.append("- _Age-linear adjustment_: baseline = "
                   "`Base_30 - (age - 30) × 0.5` bpm; if personal_note "
                   "mentions the user's age, apply the formula.")
        out.append("- _Rest duration vs the planned workout in the comment_: "
                   "±10s tolerance; only differences >10s count as a real "
                   "early start / overrun.")
        return out

    def _hrr_for_rest(self, rows, rest) -> dict | None:
        seg = [r for r in rows if rest['start_sec'] <= r[0] < rest['end_sec']]
        hrs = [(r[0], r[1]) for r in seg if r[1] is not None]
        if len(hrs) < 4:
            return None
        rest_start_t = rest['start_sec']
        rest_end_t   = rest['end_sec']

        def avg_hr_in_window(t_start, t_end):
            vals = [h for t, h in hrs if t_start <= t < t_end]
            return sum(vals) / len(vals) if vals else None

        start_hr = avg_hr_in_window(rest_start_t, rest_start_t + 5)
        end_hr   = avg_hr_in_window(max(rest_start_t, rest_end_t - 5), rest_end_t)
        if start_hr is None or end_hr is None:
            return None
        total_drop = start_hr - end_hr
        checkpoints: dict[int, float] = {}
        for chk in (30, 60, 90):
            if rest_end_t - rest_start_t >= chk:
                chk_hr = avg_hr_in_window(rest_start_t + chk - 2, rest_start_t + chk + 3)
                if chk_hr is not None:
                    checkpoints[chk] = start_hr - chk_hr
        early_share = None
        if 30 in checkpoints and 60 in checkpoints and checkpoints[60] > 0:
            early_share = 100 * checkpoints[30] / checkpoints[60]
        return {
            'duration_s':      int(rest_end_t - rest_start_t),
            'start_hr':        start_hr,
            'end_hr':          end_hr,
            'total_drop':      total_drop,
            'checkpoints':     checkpoints,
            'early_share_pct': early_share,
        }

    def _render_hrr_block(self, out, rest, hrr) -> None:
        out.append("")
        out.append(f"- Rest Lap {rest['lap_id']} (sec {rest['start_sec']}-{rest['end_sec']}, "
                   f"actual {hrr['duration_s']}s)")
        out.append(f"  - Start HR (avg of last 5s of preceding rep): {hrr['start_hr']:.0f}")
        out.append(f"  - End HR (= avg of first 5s of the next rep): {hrr['end_hr']:.0f}")
        out.append(f"  - Full-rest drop: -{hrr['total_drop']:.0f} bpm")
        chk_strs = [f"-{hrr['checkpoints'][chk]:.0f} within {chk}s"
                    for chk in (30, 60, 90) if chk in hrr['checkpoints']]
        if chk_strs:
            out.append(f"  - Checkpoints: {' | '.join(chk_strs)}")
        if hrr['early_share_pct'] is not None:
            out.append(f"  - Early-30s share: {hrr['early_share_pct']:.0f}% "
                       f"(first 30s as a fraction of the 60s total drop)")

    # ── Cross-rep drift within cluster (hill: also surface power decay) ────

    def _render_cross_rep_drift(self, cluster, rows) -> list[str]:
        target = cluster['target_dist_m']
        n      = len(cluster['reps'])
        out = ["", f"### Cross-rep decay (Cluster ~{target:.0f}m, {n} reps: rep 1 → rep {n})"]

        def rep_avgs(rep):
            seg   = [r for r in rows if rep['start_sec'] <= r[0] < rep['end_sec']]
            hrs   = [r[1] for r in seg if r[1] is not None]
            cads  = [r[3] for r in seg if r[3] and r[3] > 50]
            strds = [r[6] for r in seg if r[6] and r[6] > 30]
            pwr   = [r[10] for r in seg if r[10] is not None]
            pace = (1000 * rep['dur_s'] / rep['dist_m']) if rep['dist_m'] > 0 else None
            return {
                'hr':       sum(hrs)/len(hrs) if hrs else None,
                'pace':     pace,
                'cad':      sum(cads)/len(cads) if cads else None,
                'stride_m': (sum(strds)/len(strds))/100 if strds else None,
                'power_w':  sum(pwr)/len(pwr) if pwr else None,
            }

        first = rep_avgs(cluster['reps'][0])
        last  = rep_avgs(cluster['reps'][-1])
        first_id = cluster['reps'][0]['lap_id']
        last_id  = cluster['reps'][-1]['lap_id']

        if first['hr'] and last['hr']:
            d = last['hr'] - first['hr']
            out.append(f"- HR: rep {first_id} {first['hr']:.0f} → rep {last_id} {last['hr']:.0f} ({d:+.0f} bpm)")
        if first['pace'] and last['pace']:
            d = last['pace'] - first['pace']
            out.append(f"- Pace: rep {first_id} {int(first['pace']//60)}:{int(first['pace']%60):02d} → "
                       f"rep {last_id} {int(last['pace']//60)}:{int(last['pace']%60):02d}/km "
                       f"({d:+.0f}s/km)")
        if first['power_w'] and last['power_w']:
            d_pct = (last['power_w'] - first['power_w']) / first['power_w'] * 100
            out.append(f"- **Power**: rep {first_id} {first['power_w']:.0f}W → rep {last_id} "
                       f"{last['power_w']:.0f}W ({d_pct:+.1f}%) "
                       f"_(on hills, power decay >10% is the firm \"stop now\" signal)_")
        if first['cad'] and last['cad']:
            out.append(f"- Cadence: {first['cad']:.0f} → {last['cad']:.0f} spm "
                       f"({last['cad']-first['cad']:+.0f})")
        if first['stride_m'] and last['stride_m']:
            out.append(f"- Stride: {first['stride_m']:.2f} → {last['stride_m']:.2f}m "
                       f"({(last['stride_m']-first['stride_m'])*100:+.0f} cm)")
        out.append("- _Threshold_: final rep pace ≥5s/km slower than first rep "
                   "OR HR ≥5bpm higher = cross-rep decay; power decay >10% = "
                   "stop now; cadence drop ≥3spm + stride growth ≥5cm + pace "
                   "held = forcing it with a longer stride (the most "
                   "actionable pre-failure signal).")
        return out

    # ── Structure-agnostic drift readings (hill-specific: HR vs grade) ─────

    def _render_drift_section(self, canonical_rows, rows, grades) -> list[str]:
        out = ["", "### Structure-agnostic key readings (precomputed, fixed definitions)"]

        # Whole-activity HR vs time
        hrd = hr_drift(canonical_rows)
        if hrd:
            out.append(f"- **Full-activity HR drift** (linear regression on time): "
                       f"{hrd['slope_per_min']:+.2f} bpm/min, R²={hrd['r_squared']:.2f} "
                       f"_(high R² = true linear cardiac drift; low R² = HR "
                       f"is dominated by the rep structure, which is normal)_")

        # HR vs grade slope — hill-specific capacity hint
        hg = self._hr_grade_slope(rows, grades)
        if hg is not None:
            out.append(f"- **HR vs grade slope**: {hg['slope_bpm_per_pct']:+.1f} bpm per +1% grade, "
                       f"R²={hg['r_squared']:.2f}, n={hg['n']} samples "
                       f"_(steep slope = HR climbs hard for each +1% grade → "
                       f"uphill ability is the bottleneck; shallow slope with "
                       f"low R² = HR is already maxed by intensity, grade is "
                       f"no longer the dominant variable)_")

        # Pa:HR — for hill, raw pace is biased; flag this in the explanation
        pa = pa_hr_split(canonical_rows)
        if pa:
            out.append(f"- **Pa:HR decoupling** (heart-rate-to-pace ratio, "
                       f"first-half EF vs second-half EF, raw pace): "
                       f"{pa['decoupling_pct']:+.1f}% "
                       f"(first half HR {pa['first_half_hr']:.0f} @ {fmt_pace(pa['first_half_pace'])} → "
                       f"second half HR {pa['second_half_hr']:.0f} @ {fmt_pace(pa['second_half_pace'])}) "
                       f"_(on hills, raw pace is polluted by grade — read "
                       f"this number for trend only; the meaningful EF "
                       f"comparison uses each rep's GAP, see the Cluster "
                       f"sections above)_")
        return out

    @staticmethod
    def _hr_grade_slope(rows, grades) -> dict | None:
        """Linear regression HR ~ grade across all (HR, grade) pairs.
        Returns slope (bpm per +1% grade) + R². Skip if grade range < 2%
        — flat run, slope would be meaningless."""
        pairs = []
        for r, g in zip(rows, grades):
            if r[1] is not None and g is not None:
                pairs.append((g, r[1]))
        if len(pairs) < 30:
            return None
        gs = [p[0] for p in pairs]
        if max(gs) - min(gs) < 2.0:
            return None
        n = len(pairs)
        mean_x = sum(p[0] for p in pairs) / n
        mean_y = sum(p[1] for p in pairs) / n
        ss_xy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pairs)
        ss_xx = sum((p[0] - mean_x) ** 2 for p in pairs)
        if ss_xx == 0:
            return None
        slope = ss_xy / ss_xx
        intercept = mean_y - slope * mean_x
        ss_res = sum((p[1] - (intercept + slope * p[0])) ** 2 for p in pairs)
        ss_tot = sum((p[1] - mean_y) ** 2 for p in pairs)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        return {
            "slope_bpm_per_pct": slope,
            "r_squared":         r2,
            "n":                 n,
        }
