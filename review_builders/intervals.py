"""IntervalBuilder — for the `intervals` tag.

Interval workouts have **non-trivial structure** (warmup → reps with rest
between → cooldown). Builder must respect that structure: lap types
(warmup / work / rest / cooldown) have wildly different pace + HR profiles —
averaging across them produces meaningless numbers.

Output language: context_md is emitted in **neutral English**. The LLM's
response language is steered by the per-tag prompt (P5), not the builder.

Per the meta-rule (plan: "PRIORITIZED, not EXCLUSIVE"), this builder ADDS
interval-specific deep analysis ON TOP of DefaultBuilder's baseline.

Segmentation policy (priority cascade — applied IN THE PROMPT, not here):
  1. **User comment is ground truth.** Comment usually describes the planned
     workout (e.g. "3k WU + 3000m @4:10 + 90s rest + 3x (800m @3:55 + 90s
     rest) + 3k CD"). Builder gives heuristic lap classification; LLM uses
     comment to override / verify.
  2. **Lap boundaries are user-imposed** (Garmin laps follow user lap
     presses). Builder respects them; doesn't merge across.
  3. **Heuristic classification fallback** (no comment to verify against):
     uses pace + distance to label warmup/work/rest/cooldown.

Why heuristic and not Garmin's intensity_type:
  - Empirically, intensity_type is unreliable across firmware/devices
    (test activity 22765867631 has all 11 laps tagged simply "INTERVAL"
    with no INTERVAL_ACTIVE/INTERVAL_RECOVERY distinction)
  - Heuristic + comment cross-ref is more robust

Coaching philosophy (from plan + design discussion):
- Per-rep consistency > absolute pace. Rep 5 should look like rep 1.
  rep N HR ≥5bpm higher than rep 1 OR pace ≥5s/km slower = cross-rep decay.
- Time-to-consistency (start crispness) is technique, not fitness — runners
  with poor pacing-feel waste 15-20s of every rep ramping up.
- Recovery HR drop curves reflect parasympathetic activation. Early-30s
  share (30s_drop / 60s_drop) >60% = elite switch speed.
- Rest duration vs comment plan: ±10s tolerance. Don't flag 88s vs 90s
  plan as "early start" — it's normal variance.
- HRR thresholds are age-dependent: linear `Base_30 - (age-30) × 0.5` bpm.
"""

import sqlite3

from review_builders.base       import BuildResult, ReviewBuilder, lap_windows_from_db
from review_builders.default    import DefaultBuilder
from review_builders.primitives import internal_stats, pairwise_delta, fmt_pace_compact


class IntervalBuilder(ReviewBuilder):
    name = "IntervalBuilder"

    # ── Heuristic thresholds (TODO: future fitness-level adaptive) ──────────
    REST_DIST_M_MAX            = 200       # rest lap distance ceiling
    REST_PACE_S_PER_KM         = 9 * 60    # rest pace floor (slower than 9:00/km)
    NOISE_DUR_S_MAX            = 20        # very short stray lap = noise
    NOISE_DIST_M_MAX           = 50
    WARMUP_COOLDOWN_DIST_M_MIN = 1000      # warmup/cooldown must be ≥1km
    WARMUP_PACE_DELTA_S        = 30        # slower than work median by ≥30s/km
    REP_CLUSTER_TOL_PCT        = 20        # group reps if distance within ±20%
    CONSISTENCY_BAND_PCT       = 5         # ±5% of rep median pace
    CONSISTENCY_HOLD_S         = 8         # must hold band for ≥8s
    REP_INTERNAL_HALVES_MIN_S  = 60        # rep needs ≥60s for halves comparison to be meaningful

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag == "intervals"

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep = self._interval_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Interval deep analysis ───────────────────────────────────────────────

    def _interval_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
        rows = conn.execute("""
            SELECT sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio,
                   stride_cm
              FROM activity_metrics
             WHERE activity_id = ?
             ORDER BY sec_offset
        """, (aid,)).fetchall()
        if not rows:
            return ""

        if len(rows) >= 3:
            deltas = sorted(rows[i+1][0] - rows[i][0] for i in range(len(rows)-1))
            median_delta = max(1, deltas[len(deltas)//2])
        else:
            median_delta = 1

        # Pause-aware lap windows (uses startTimeGMT, not cumulative duration_s)
        lap_windows = lap_windows_from_db(conn, aid)
        if not lap_windows:
            return ""

        labeled = self._classify_laps(lap_windows)
        if not labeled:
            return ""

        out = [
            "## 🎯 Interval-specific analysis",
            "",
            "_Data + heuristic classification + coach-consensus reference "
            "thresholds. Verdict is yours (LLM) to synthesize from the "
            "activity tag, the user's notes (**the workout described in the "
            "comment is the most authoritative source of structure**), "
            "personal_note, and long-term memory. Garmin's `intensity_type` "
            "does not reliably distinguish active from recovery, so the "
            "classification below is heuristic — **always cross-check it "
            "against the user's comment**._",
        ]

        # ── (1) Auto lap classification ─────────────────────────────────
        out.extend(self._render_lap_classification(labeled))

        # ── (2) Work cluster details (reps grouped by distance) ─────────
        work_laps = [l for l in labeled if l['kind'] == 'work']
        clusters  = self._cluster_work_reps(work_laps)
        if clusters:
            out.extend(self._render_clusters(clusters, rows, median_delta))

        # ── (3) Recovery HR drop (within each rest lap) ─────────────────
        rest_laps = [l for l in labeled if l['kind'] == 'rest']
        if rest_laps:
            out.extend(self._render_recovery_hrr(rest_laps, rows))

        # ── (4) Cross-rep decay (per cluster with ≥2 reps) ──────────────
        for cluster in clusters:
            if len(cluster['reps']) >= 2:
                out.extend(self._render_cross_rep_drift(cluster, rows))

        # ── (5) Tool availability ───────────────────────────────────────
        out.extend(self._tool_hint_section())

        return "\n".join(out)

    @staticmethod
    def _tool_hint_section() -> list[str]:
        return [
            "",
            "### Tool availability",
            "- For aggregates over an arbitrary sub-window of a rep (e.g. "
            "the first 30s of a rep, to see how hot the start went; or the "
            "last 10s of the final rep, to see the finishing kick), call "
            "`get_window_stats(start, end, key_type, channels?)`.",
            "- For raw 1Hz rows (e.g. to check whether HR was still on a "
            "plateau in the first 5s of a rest), call "
            "`get_raw_window_by_time` / `get_raw_window_by_distance` "
            "(existing tools).",
            "- By default, prefer the cluster / cross-rep / HRR sections "
            "the builder provides; only call a tool when the slice "
            "granularity is insufficient.",
        ]

    # ── Lap classification heuristic ────────────────────────────────────────

    def _classify_laps(self, lap_windows) -> list[dict]:
        """Heuristic warmup/work/rest/cooldown/noise classification.

        Takes pause-aware lap_windows from base.lap_windows_from_db.
        Returns list of dicts {lap_id, kind, start_sec, end_sec, dist_m,
        dur_s, pace_s_per_km}, in chronological order. Pace is computed
        from dist_m/dur_s (moving time → real running pace, not wall-clock)."""
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

        # Compute median work-candidate pace (laps not yet tagged with reasonable dist)
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

        # Tag warmup (first eligible lap) + cooldown (last eligible lap)
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

    # ── Work-rep clustering by distance ─────────────────────────────────────

    def _cluster_work_reps(self, work_laps) -> list[dict]:
        """Group work reps by distance similarity (±20% tolerance).

        E.g., a workout with [3000m × 1, 800m × 3] gets two clusters:
        {target_dist_m: 3000, reps: [3000m_lap]} and
        {target_dist_m: 808, reps: [800m, 810m, 810m]}."""
        if not work_laps:
            return []

        # Build clusters greedily; sort by distance for stable grouping
        sorted_laps = sorted(work_laps, key=lambda l: l['dist_m'])
        clusters: list[dict] = []
        for lap in sorted_laps:
            placed = False
            for cluster in clusters:
                rep_dist = cluster['target_dist_m']
                if abs(lap['dist_m'] - rep_dist) / rep_dist <= self.REP_CLUSTER_TOL_PCT / 100:
                    cluster['reps'].append(lap)
                    # Rolling cluster center to avoid drift
                    cluster['target_dist_m'] = (
                        sum(r['dist_m'] for r in cluster['reps']) / len(cluster['reps'])
                    )
                    placed = True
                    break
            if not placed:
                clusters.append({'target_dist_m': lap['dist_m'], 'reps': [lap]})

        # Sort reps within each cluster chronologically
        for c in clusters:
            c['reps'].sort(key=lambda r: r['lap_id'])
        # Sort clusters by their first rep's chronological position
        clusters.sort(key=lambda c: c['reps'][0]['start_sec'])
        return clusters

    def _render_clusters(self, clusters, rows, median_delta) -> list[str]:
        out = []
        for ci, cluster in enumerate(clusters, 1):
            target = cluster['target_dist_m']
            n_reps = len(cluster['reps'])
            out.append("")
            out.append(f"### Work Cluster {ci}  ({n_reps} × ~{target:.0f}m)")
            for rep in cluster['reps']:
                stats = self._rep_internal_stats(rows, rep, median_delta)
                self._render_rep_line(out, rep, stats)
        return out

    # ── Per-rep internal stats (incl. time-to-consistency) ──────────────────

    def _rep_internal_stats(self, rows, rep, median_delta) -> dict | None:
        """Compute per-rep stats. Pace is from lap-level dist/dur (matches
        Garmin Connect + DefaultBuilder), NOT row-level speed_mps avg
        (which is biased low by the 0.5-2s ramp-up at lap start)."""
        seg = [r for r in rows if rep['start_sec'] <= r[0] < rep['end_sec']]
        if not seg:
            return None

        hrs   = [r[1] for r in seg if r[1] is not None]
        spds  = [r[2] for r in seg if r[2] and r[2] > 0.5]
        cads  = [r[3] for r in seg if r[3] and r[3] > 50]
        gcts  = [r[4] for r in seg if r[4]]
        vrs   = [r[5] for r in seg if r[5] is not None]
        strds = [r[6] for r in seg if r[6] and r[6] > 30]

        if not hrs or not spds:
            return None

        avg_hr  = sum(hrs)  / len(hrs)
        peak_hr = max(hrs)
        # Primary pace: lap-level dist/dur (consistent with Garmin Connect)
        avg_pace = (1000 * rep['dur_s'] / rep['dist_m']) if rep['dist_m'] > 0 else None

        # Speed CV (std / mean) — uses row-level samples since CV needs
        # variance of the actual instantaneous speeds
        avg_spd = sum(spds) / len(spds)
        if len(spds) >= 2:
            var = sum((s - avg_spd) ** 2 for s in spds) / len(spds)
            cv  = (var ** 0.5) / avg_spd if avg_spd > 0 else 0
        else:
            cv = 0

        ttc = self._time_to_consistency(seg, median_delta)

        # Internal halves + HR drift (only meaningful for reps ≥60s — halves
        # of a 400m sprint at <80s give too-sparse data; primitives return
        # mostly None for those). For long reps (800m+ class) the halves
        # comparison detects within-rep fade pattern that single-rep avg can't.
        halves_internal = None
        if rep['dur_s'] >= self.REP_INTERNAL_HALVES_MIN_S:
            halves_internal = internal_stats(seg)

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
            'halves_internal': halves_internal,
        }

    def _time_to_consistency(self, seg, median_delta) -> int | None:
        """How many seconds from rep start until smoothed pace enters the
        rep's own median ±5% band and stays there for ≥8s.

        Returns None if rep never stabilizes — typical of <30s reps or
        very erratic pacing. Lower = crisper start (technique signal)."""
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

    def _render_rep_line(self, out, rep, stats) -> None:
        sec_range = f"sec {rep['start_sec']}-{rep['end_sec']}"
        if stats is None:
            out.append(f"- Lap {rep['lap_id']} ({sec_range}, {rep['dist_m']:.0f}m, "
                       f"{rep['dur_s']}s): _insufficient data_")
            return
        pace_str = f"{int(stats['avg_pace']//60)}:{int(stats['avg_pace']%60):02d}/km"
        ttc_str  = (f"stabilized in {stats['ttc_s']}s"
                    if stats['ttc_s'] is not None else "did not stabilize")
        extras = []
        if stats['cad']      is not None: extras.append(f"cadence {stats['cad']:.0f}")
        if stats['stride_m'] is not None: extras.append(f"stride {stats['stride_m']:.2f}m")
        if stats['gct']      is not None: extras.append(f"GCT {stats['gct']:.0f}ms")
        if stats['vr']       is not None: extras.append(f"vertical ratio {stats['vr']:.1f}%")
        extras_str = " | ".join(extras)
        out.append(f"- Lap {rep['lap_id']} ({sec_range}, {rep['dist_m']:.0f}m, "
                   f"{rep['dur_s']}s @ {pace_str}): "
                   f"HR avg {stats['avg_hr']:.0f} / peak {stats['peak_hr']:.0f} | "
                   f"pace CV {stats['cv_speed']*100:.1f}% | "
                   f"{ttc_str} | {extras_str}")

        # Internal halves + HR drift sub-bullets (only for ≥60s reps where
        # internal_stats was computed). Detects within-rep fade: rep avg
        # alone can't tell "165→169 ramp-up" from "173→162 finish-then-fade" —
        # halves can.
        halves = stats.get('halves_internal')
        if halves is None:
            return
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

        # Internal HR-time drift slope + R² (linear regression on the rep)
        hrd = halves.get('hr_drift')
        if hrd:
            out.append(f"  - Internal HR-time drift: {hrd['slope_per_min']:+.2f} bpm/min, "
                       f"R²={hrd['r_squared']:.2f}")

    # ── Recovery HR drop per rest lap ───────────────────────────────────────

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
                   "applies specifically to the 60s drop, NOT to the "
                   "\"full-rest drop\"** — that depends on the actual rest "
                   "duration and has no single threshold.")
        out.append("- _Early-30s share_: >60% = parasympathetic switches in "
                   "quickly; <40% **does NOT necessarily mean slow switching** "
                   "— if HR is still flat at 178+ in the first 5-15s of the "
                   "rest (post-effort plateau), that is genuine physiology — "
                   "parasympathetic activation has a lag. The 60s drop value "
                   "itself is more reliable than the share %.")
        out.append("- _Age-linear adjustment_: baseline = "
                   "`Base_30 - (age - 30) × 0.5` bpm; if personal_note "
                   "mentions the user's age, apply the formula.")
        out.append("- _Rest duration vs the planned workout in the comment_: "
                   "±10s tolerance; 88s vs 90s is in spec, only differences "
                   ">10s count as a real early start / overrun.")
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

        # Headline: start = avg of last 5s of preceding rep; end = avg of first 5s of next rep
        start_hr = avg_hr_in_window(rest_start_t, rest_start_t + 5)
        end_hr   = avg_hr_in_window(max(rest_start_t, rest_end_t - 5), rest_end_t)
        if start_hr is None or end_hr is None:
            return None
        total_drop = start_hr - end_hr

        # Checkpoints (30s / 60s / 90s) — for cross-rep comparison + curve shape
        checkpoints: dict[int, float] = {}
        for chk in (30, 60, 90):
            if rest_end_t - rest_start_t >= chk:
                chk_hr = avg_hr_in_window(rest_start_t + chk - 2, rest_start_t + chk + 3)
                if chk_hr is not None:
                    checkpoints[chk] = start_hr - chk_hr

        # Early-30s share = 30s_drop / 60s_drop (only when both available)
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

    # ── Cross-rep drift within a cluster ────────────────────────────────────

    def _render_cross_rep_drift(self, cluster, rows) -> list[str]:
        target = cluster['target_dist_m']
        n      = len(cluster['reps'])
        out = ["", f"### Cross-rep decay (Cluster ~{target:.0f}m, {n} reps: rep 1 → rep {n})"]

        def rep_avgs(rep):
            seg   = [r for r in rows if rep['start_sec'] <= r[0] < rep['end_sec']]
            hrs   = [r[1] for r in seg if r[1] is not None]
            cads  = [r[3] for r in seg if r[3] and r[3] > 50]
            strds = [r[6] for r in seg if r[6] and r[6] > 30]
            # Pace from lap-level dist/dur (matches Garmin Connect)
            pace = (1000 * rep['dur_s'] / rep['dist_m']) if rep['dist_m'] > 0 else None
            return {
                'hr':       sum(hrs)/len(hrs) if hrs else None,
                'pace':     pace,
                'cad':      sum(cads)/len(cads) if cads else None,
                'stride_m': (sum(strds)/len(strds))/100 if strds else None,
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
        if first['cad'] and last['cad']:
            out.append(f"- Cadence: {first['cad']:.0f} → {last['cad']:.0f} spm "
                       f"({last['cad']-first['cad']:+.0f})")
        if first['stride_m'] and last['stride_m']:
            out.append(f"- Stride: {first['stride_m']:.2f} → {last['stride_m']:.2f}m "
                       f"({(last['stride_m']-first['stride_m'])*100:+.0f} cm)")
        out.append("- _Threshold_: final rep pace ≥5s/km slower than first rep "
                   "OR HR ≥5bpm higher = cross-rep decay; cadence drop ≥3spm + "
                   "stride growth ≥5cm + pace held = forcing it with a longer "
                   "stride (the most actionable pre-failure signal).")
        return out
