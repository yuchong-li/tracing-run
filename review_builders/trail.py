"""TrailBuilder — for the `trail` tag.

**Meta-rule (the most important framing principle in this builder)**:
Road running data is absolute. Trail running data is relative. EVERY
pace / HR observation must be interpreted against the elevation context.
A sudden HR spike + pace collapse on the road = "blew up". On trail =
could just be 30% gradient technical scrambling, totally normal.

Output language: context_md is emitted in **neutral English**. The LLM's
response language is steered by the per-tag prompt (P5), not the builder.

This builder NEVER reports pace / HR without grade context.

Sections:
  0. Trail overview (distance, duration, gain/loss, max grades)
  1. Time-by-grade-bucket (where did time go: flat / mild climb / steep / descent)
  2. GAP × Terrain (avg GAP per climb/descent/flat — same effort = same GAP)
  3. Power × Terrain (only if power_w available)
  4. Burst detection (only if power_w; surfaces spikes WITH grade context)
  5. Downhill technique (cadence + GCT + std-dev → quad-braking detection)
  6. VO across grade buckets (technical-section vs flat efficiency)
  7. Aerobic decoupling (ULTRA only: ≥3h OR ≥35km)
  8. Hydration / heat surrogate (air_temp_c trend)
  9. Manual lap summary (if user lapped at aid stations / transitions)

GAP is computed from Garmin's `directGradeAdjustedSpeed` when available,
falls back to a Minetti-2002-derived approximation when not.

Quad-braking thresholds (calibrated to user feedback):
  - Cadence < 175 spm = overstriding (knee shock + brake friction)
  - GCT > 270 ms      = heavy loading (muscle takes over from tendon)
  - GCT std-dev > 30  = loss of flow (frequent speed change kills efficiency)
"""

import sqlite3

from review_builders.base    import (
    BuildResult, ReviewBuilder, is_manual_lap_structure, lap_windows_from_db
)
from review_builders.default import DefaultBuilder


class TrailBuilder(ReviewBuilder):
    name = "TrailBuilder"

    # Grade thresholds (% slope)
    CLIMB_GRADE_PCT_MIN   = 3.0
    DESCENT_GRADE_PCT_MAX = -3.0

    # Grade smoothing window (seconds)
    GRADE_SMOOTHING_S     = 30

    # Min segment duration to count (filters GPS-noise micro-segments)
    MIN_SEGMENT_S         = 30

    # Burst detection
    BURST_POWER_MULTIPLIER = 1.20
    BURST_MIN_DURATION_S   = 2
    BURST_TOP_N            = 3

    # Quad-braking thresholds (per user calibration)
    QUAD_BRAKE_CAD_MAX     = 175
    QUAD_BRAKE_GCT_MIN     = 270
    QUAD_BRAKE_GCT_STD_MIN = 30

    # Ultra threshold (for aerobic decoupling section)
    ULTRA_DURATION_S       = 3 * 3600
    ULTRA_DISTANCE_M       = 35000

    # Intra-segment fade detection (only run on climbs/descents this long)
    SEGMENT_FADE_MIN_S     = 180

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag == "trail"

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep = self._trail_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Top-level analysis ──────────────────────────────────────────────────

    def _trail_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
        act = conn.execute("""
            SELECT distance_m, duration_s, elevation_gain_m, elevation_loss_m
              FROM activities WHERE activity_id = ?
        """, (aid,)).fetchone()
        if not act or not act[0]:
            return ""
        total_dist, total_dur, gain, loss = act
        gain = gain or 0
        loss = loss or 0

        rows = conn.execute("""
            SELECT sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio,
                   vert_osc_cm, power_w, elevation_m, distance_cum_m,
                   grade_adj_speed, air_temp_c
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

        # Compute smoothed grade per sample, then segment
        grades   = self._compute_smoothed_grades(rows, median_delta)
        segments = self._segment_by_grade(rows, grades)

        out = [
            "## 🎯 Trail-specific analysis",
            "",
            "_Data + derived signals + coach-consensus reference thresholds. "
            "**Meta-rule: trail data is relative — every pace / HR observation "
            "must be read in the context of elevation. On the road, \"HR spike "
            "+ pace collapse = blew up\"; on trail, the same pattern could "
            "just be the normal cost of a technical section.** Verdict is "
            "yours (LLM) to synthesize from the activity tag, the user's "
            "notes, personal_note, and long-term memory._",
        ]

        # 0. Trail overview
        out.extend(self._render_overview(total_dist, total_dur, gain, loss, grades))

        # 1. Time-by-grade-bucket
        out.extend(self._render_grade_buckets(rows, grades, median_delta))

        # 2. GAP × Terrain (always — GAP is core trail metric)
        out.extend(self._render_gap_terrain(rows, segments))

        # 3. Power × Elevation overlay (silently skip if no power)
        has_power = any(r[7] is not None and r[7] > 0 for r in rows)
        if has_power:
            out.extend(self._render_power_terrain(rows, segments))
            out.extend(self._render_burst_detection(rows, grades))

        # 4. Downhill technique (cadence + GCT + std-dev)
        out.extend(self._render_downhill_technique(rows, segments))

        # 5. Intra-segment fade for long climbs/descents (≥3min)
        out.extend(self._render_segment_fade(rows, grades, segments))

        # 6. VO across grade buckets (silently skip if no VO)
        out.extend(self._render_vo_grade_buckets(rows, grades))

        # 7. Aerobic decoupling (ultra only)
        if total_dur >= self.ULTRA_DURATION_S or total_dist >= self.ULTRA_DISTANCE_M:
            out.extend(self._render_ultra_decoupling(rows))

        # 8. Hydration / heat surrogate (silently skip if no temp)
        out.extend(self._render_temp_trend(rows))

        # 9. Manual lap summary (aid stations / transitions)
        lap_windows = lap_windows_from_db(conn, aid)
        if lap_windows:
            distances = [w['dist_m'] for w in lap_windows if w['dist_m']]
            if is_manual_lap_structure(distances):
                out.extend(self._render_manual_lap_summary(lap_windows, rows, grades))

        # 10. Tool availability hint
        out.extend(self._tool_hint_section())

        return "\n".join(out)

    # ── Grade computation + segmentation ────────────────────────────────────

    def _compute_smoothed_grades(self, rows, median_delta) -> list:
        """Per-sample grade (%) using a 30s window of (Δelevation / Δdistance).
        Returns None for samples lacking elevation/distance data."""
        half_window = max(1, (self.GRADE_SMOOTHING_S // 2) // median_delta)
        grades = []
        for i in range(len(rows)):
            start_i = max(0, i - half_window)
            end_i   = min(len(rows) - 1, i + half_window)
            elev_s, elev_e = rows[start_i][8], rows[end_i][8]
            dist_s, dist_e = rows[start_i][9], rows[end_i][9]
            if (elev_s is None or elev_e is None
                or dist_s is None or dist_e is None):
                grades.append(None); continue
            dd = dist_e - dist_s
            if dd < 1:
                grades.append(None); continue
            grades.append((elev_e - elev_s) / dd * 100)
        return grades

    def _classify_grade(self, g):
        if g is None:           return None
        if g >  self.CLIMB_GRADE_PCT_MIN:    return 'climb'
        if g <  self.DESCENT_GRADE_PCT_MAX:  return 'descent'
        return 'flat'

    def _segment_by_grade(self, rows, grades) -> list[dict]:
        """Group consecutive same-classified samples; filter <MIN_SEGMENT_S."""
        segments = []
        cur_kind = None
        cur_start_i = 0
        for i, g in enumerate(grades):
            kind = self._classify_grade(g)
            if kind != cur_kind:
                if cur_kind is not None and i > cur_start_i:
                    end_i = i - 1
                    dur = rows[end_i][0] - rows[cur_start_i][0]
                    if dur >= self.MIN_SEGMENT_S:
                        segments.append({
                            'kind': cur_kind,
                            'start_sec': rows[cur_start_i][0],
                            'end_sec':   rows[end_i][0],
                            'start_i':   cur_start_i,
                            'end_i':     end_i,
                        })
                cur_kind = kind
                cur_start_i = i
        # Close final segment
        if cur_kind is not None and len(rows) > cur_start_i:
            end_i = len(rows) - 1
            dur = rows[end_i][0] - rows[cur_start_i][0]
            if dur >= self.MIN_SEGMENT_S:
                segments.append({
                    'kind': cur_kind,
                    'start_sec': rows[cur_start_i][0],
                    'end_sec':   rows[end_i][0],
                    'start_i':   cur_start_i,
                    'end_i':     end_i,
                })
        return segments

    # ── GAP helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _minetti_factor(grade_pct: float) -> float:
        """Minetti et al. 2002 metabolic cost relative to flat. Used as
        fallback when Garmin's directGradeAdjustedSpeed isn't available."""
        g = grade_pct / 100
        # C(i) = 155.4·i^5 − 30.4·i^4 − 43.3·i^3 + 46.3·i^2 + 19.5·i + 3.6
        cost = (155.4 * g**5 - 30.4 * g**4 - 43.3 * g**3
                + 46.3 * g**2 + 19.5 * g + 3.6)
        return max(0.3, cost / 3.6)   # cap downhill benefit at 30% of flat cost

    def _seg_gap_pace(self, rows, seg, grades) -> tuple:
        """Returns (avg_pace_s_per_km, avg_gap_pace_s_per_km) for a segment.
        GAP comes from grade_adj_speed if available, else Minetti formula."""
        seg_rows = rows[seg['start_i']:seg['end_i'] + 1]
        seg_grades = grades[seg['start_i']:seg['end_i'] + 1]

        spds = [r[2] for r in seg_rows if r[2] and r[2] > 0.5]
        if not spds:
            return None, None
        avg_spd  = sum(spds) / len(spds)
        avg_pace = 1000 / avg_spd

        # Try Garmin's GAP first
        gas = [r[10] for r in seg_rows if r[10] and r[10] > 0.5]
        if gas:
            avg_gas  = sum(gas) / len(gas)
            avg_gap  = 1000 / avg_gas
        else:
            # Fallback: weight pace by Minetti factor per sample
            adjusted = []
            for r, g in zip(seg_rows, seg_grades):
                if r[2] and r[2] > 0.5 and g is not None:
                    factor = self._minetti_factor(g)
                    if factor > 0:
                        adjusted.append(r[2] * factor)   # speed_flat = speed_actual × cost_ratio
            if adjusted:
                avg_adj_spd = sum(adjusted) / len(adjusted)
                avg_gap     = 1000 / avg_adj_spd
            else:
                avg_gap = None

        return avg_pace, avg_gap

    @staticmethod
    def _fmt_pace(p):
        if p is None: return "—"
        return f"{int(p//60)}:{int(p%60):02d}/km"

    # ── Section 0: Overview ─────────────────────────────────────────────────

    @staticmethod
    def _fmt_duration(s: float) -> str:
        s = int(s)
        if s >= 3600:
            h, rem = divmod(s, 3600); m, sec = divmod(rem, 60)
            return f"{h}h{m:02d}m"
        m, sec = divmod(s, 60)
        return f"{m}min"

    def _render_overview(self, total_dist, total_dur, gain, loss, grades) -> list[str]:
        out = ["", "### Trail Overview"]
        out.append(f"- Distance {total_dist/1000:.2f}km | Duration {self._fmt_duration(total_dur)}")
        out.append(f"- Elev gain +{gain:.0f}m / Elev loss {loss:.0f}m | Net {gain-loss:+.0f}m")
        valid = [g for g in grades if g is not None]
        if valid:
            out.append(f"- Max grade after 30s smoothing: up {max(valid):+.1f}% / down {min(valid):+.1f}%")
        return out

    # ── Section 1: Time-by-grade-bucket ─────────────────────────────────────

    def _render_grade_buckets(self, rows, grades, median_delta) -> list[str]:
        out = ["", "### Time-by-grade-bucket (where the effort went, by terrain)"]
        buckets = [
            ('steep up >+10%',     lambda g: g > 10),
            ('mid up +5..+10%',    lambda g: 5 < g <= 10),
            ('gentle up +3..+5%',  lambda g: 3 < g <= 5),
            ('flat -3..+3%',       lambda g: -3 <= g <= 3),
            ('gentle down -5..-3%', lambda g: -5 <= g < -3),
            ('mid down -10..-5%',  lambda g: -10 <= g < -5),
            ('steep down <-10%',   lambda g: g < -10),
        ]
        counts = [0] * len(buckets)
        for g in grades:
            if g is None: continue
            for i, (_label, fn) in enumerate(buckets):
                if fn(g):
                    counts[i] += median_delta
                    break
        total = sum(counts)
        if total == 0:
            out.append("- _No grade data, skipped._")
            return out
        for (label, _), sec in zip(buckets, counts):
            if sec == 0: continue
            out.append(f"- {label}: {sec/60:.1f}min ({100*sec/total:.0f}%)")
        return out

    # ── Section 2: GAP × Terrain ────────────────────────────────────────────

    def _render_gap_terrain(self, rows, segments) -> list[str]:
        out = ["", "### GAP × Terrain (grade-adjusted pace, segmented by terrain)"]
        out.append("- _GAP = pace adjusted for grade, i.e. \"flat-equivalent "
                   "pace\". **Ideal: GAP should be similar across terrain "
                   "kinds (same effort)**; if climb GAP is much slower than "
                   "flat GAP = uphill effort didn't match the terrain, OR "
                   "the runner was on a section that shouldn't be run._")
        if not segments:
            out.append("- _No grade segments, skipped._")
            return out

        # Group segments by kind, weighted-avg GAP
        kind_data = {'climb': [], 'flat': [], 'descent': []}
        for seg in segments:
            pace, gap = self._seg_gap_pace(rows, seg, [None]*len(rows))  # don't need grades again
            dur = seg['end_sec'] - seg['start_sec']
            kind_data[seg['kind']].append({'dur': dur, 'pace': pace, 'gap': gap, 'seg': seg})

        kind_label = {'climb': 'climb', 'flat': 'flat', 'descent': 'descent'}
        for kind in ('climb', 'flat', 'descent'):
            data = kind_data[kind]
            if not data: continue
            total_dur = sum(d['dur'] for d in data)
            n_seg = len(data)
            # Duration-weighted avg pace + gap
            paces = [d for d in data if d['pace'] is not None]
            gaps  = [d for d in data if d['gap']  is not None]
            if paces:
                w_pace = sum(d['pace'] * d['dur'] for d in paces) / sum(d['dur'] for d in paces)
            else:
                w_pace = None
            if gaps:
                w_gap = sum(d['gap'] * d['dur'] for d in gaps) / sum(d['dur'] for d in gaps)
            else:
                w_gap = None
            out.append(f"- **{kind_label[kind]}** ({n_seg} segments, "
                       f"{total_dur/60:.1f}min total): "
                       f"actual {self._fmt_pace(w_pace)} | GAP {self._fmt_pace(w_gap)}")

        # Headline interpretation: GAP spread across kinds
        gap_by_kind = {}
        for kind in ('climb', 'flat', 'descent'):
            gaps = [d['gap'] for d in kind_data[kind] if d['gap'] is not None]
            if gaps:
                durs = [d['dur'] for d in kind_data[kind] if d['gap'] is not None]
                gap_by_kind[kind] = sum(g*dur for g, dur in zip(gaps, durs)) / sum(durs)
        if 'climb' in gap_by_kind and 'flat' in gap_by_kind:
            spread = gap_by_kind['climb'] - gap_by_kind['flat']
            out.append(f"- **GAP spread (climb - flat)**: {spread:+.0f}s/km")
            out.append("  - _Threshold_: |spread| <15s/km = effort distributed "
                       "evenly / 15-30s/km = moderate / >30s/km = uphill "
                       "effort didn't match the terrain (either climbed too "
                       "slow OR drifted too fast on the flats).")
        return out

    # ── Section 3: Power × Elevation ────────────────────────────────────────

    def _render_power_terrain(self, rows, segments) -> list[str]:
        out = ["", "### Power × Terrain (power, segmented by terrain)"]
        kind_data = {'climb': [], 'flat': [], 'descent': []}
        for seg in segments:
            seg_rows = rows[seg['start_i']:seg['end_i'] + 1]
            powers = [r[7] for r in seg_rows if r[7] and r[7] > 0]
            hrs    = [r[1] for r in seg_rows if r[1] is not None]
            if not powers or not hrs: continue
            kind_data[seg['kind']].append({
                'dur':    seg['end_sec'] - seg['start_sec'],
                'avg_p':  sum(powers) / len(powers),
                'avg_hr': sum(hrs) / len(hrs),
            })

        kind_label = {'climb': 'climb', 'flat': 'flat', 'descent': 'descent'}
        for kind in ('climb', 'flat', 'descent'):
            data = kind_data[kind]
            if not data: continue
            total_dur = sum(d['dur'] for d in data)
            w_p  = sum(d['avg_p']  * d['dur'] for d in data) / total_dur
            w_hr = sum(d['avg_hr'] * d['dur'] for d in data) / total_dur
            out.append(f"- **{kind_label[kind]}**: avg power {w_p:.0f}W | avg HR {w_hr:.0f}bpm")
        out.append("- _Interpretation_: high climb power is normal; **high "
                   "flat power + climb power not correspondingly higher = "
                   "effort mismatched** (flat-section jabs are wasted / climb "
                   "is being conserved).")
        return out

    # ── Section 4: Burst detection ──────────────────────────────────────────

    def _render_burst_detection(self, rows, grades) -> list[str]:
        out = ["", "### Burst detection (power-spike warning)"]
        powers = [r[7] for r in rows if r[7] and r[7] > 0]
        if len(powers) < 30:
            return []
        # Cruise = median (avoids spike contamination)
        sorted_p = sorted(powers)
        cruise_p = sorted_p[len(sorted_p) // 2]
        threshold = cruise_p * self.BURST_POWER_MULTIPLIER

        # Find runs of consecutive samples with power > threshold lasting ≥2s
        bursts = []  # [(start_sec, end_sec, peak_power, grade_at_peak)]
        cur_start = None
        cur_peak = 0
        cur_peak_sec = None
        for i, r in enumerate(rows):
            sec, p = r[0], r[7]
            if p and p > threshold:
                if cur_start is None:
                    cur_start = sec
                if p > cur_peak:
                    cur_peak = p
                    cur_peak_sec = sec
                    cur_peak_idx = i
            else:
                if cur_start is not None:
                    dur = sec - cur_start
                    if dur >= self.BURST_MIN_DURATION_S:
                        g = grades[cur_peak_idx] if cur_peak_idx < len(grades) else None
                        bursts.append({
                            'start_sec': cur_start,
                            'peak_sec':  cur_peak_sec,
                            'peak_p':    cur_peak,
                            'grade':     g,
                            'dur':       dur,
                        })
                    cur_start = None
                    cur_peak = 0

        if not bursts:
            out.append(f"- Cruise power {cruise_p:.0f}W; no sustained burst "
                       f"≥{self.BURST_MIN_DURATION_S}s (>+20%) detected.")
            return out

        out.append(f"- Cruise power {cruise_p:.0f}W (median) | "
                   f"burst threshold {threshold:.0f}W (×{self.BURST_POWER_MULTIPLIER})")
        out.append(f"- Detected **{len(bursts)} sustained bursts** "
                   f"≥{self.BURST_MIN_DURATION_S}s")
        # Top N worst bursts (by peak power)
        top = sorted(bursts, key=lambda b: -b['peak_p'])[:self.BURST_TOP_N]
        out.append(f"- Top {len(top)} most severe:")
        for b in top:
            grade_str = (f"{b['grade']:+.1f}% grade"
                         if b['grade'] is not None else "grade unknown")
            out.append(f"  - sec {b['peak_sec']} ({b['peak_sec']//60}:{b['peak_sec']%60:02d}min): "
                       f"peak {b['peak_p']:.0f}W ({grade_str}, lasted {b['dur']}s)")
        out.append("- _Interpretation_: uphill burst (grade > +5%) = effort "
                   "is naturally high, not wasted; **flat / gentle-grade burst "
                   "(grade < +3%) = short bout of glycogen waste — repeat "
                   "many times and the late-section blow-up risk goes up**.")
        return out

    # ── Section 5: Downhill technique ───────────────────────────────────────

    def _render_downhill_technique(self, rows, segments) -> list[str]:
        out = ["", "### Downhill technique (cadence + GCT + std-dev)"]
        descents = [s for s in segments if s['kind'] == 'descent']
        if not descents:
            out.append("- _No descent segments ≥30s in this activity, skipped._")
            return out
        flats = [s for s in segments if s['kind'] == 'flat']

        def seg_cad_gct(seg):
            seg_rows = rows[seg['start_i']:seg['end_i'] + 1]
            cads = [r[3] for r in seg_rows if r[3] and r[3] > 50]
            gcts = [r[4] for r in seg_rows if r[4]]
            if not cads or not gcts:
                return None
            cad_avg = sum(cads) / len(cads)
            gct_avg = sum(gcts) / len(gcts)
            gct_var = sum((g - gct_avg)**2 for g in gcts) / len(gcts)
            gct_std = gct_var ** 0.5
            return cad_avg, gct_avg, gct_std

        # Per-descent table
        out.append("- Per-descent detail:")
        any_brake = False
        for s in descents:
            stats = seg_cad_gct(s)
            if stats is None: continue
            cad, gct, gct_std = stats
            dur = s['end_sec'] - s['start_sec']
            flag = ""
            if (cad < self.QUAD_BRAKE_CAD_MAX
                and gct > self.QUAD_BRAKE_GCT_MIN
                and gct_std > self.QUAD_BRAKE_GCT_STD_MIN):
                flag = " ← **quad-braking pattern** (low cadence + long GCT + uneven rhythm)"
                any_brake = True
            out.append(f"  - sec {s['start_sec']}-{s['end_sec']} ({dur/60:.1f}min): "
                       f"cad {cad:.0f} | GCT {gct:.0f}ms | GCT std {gct_std:.0f}{flag}")

        # Compare with flat baseline
        if flats:
            flat_stats = []
            for s in flats:
                st = seg_cad_gct(s)
                if st is not None:
                    flat_stats.append(st)
            if flat_stats:
                flat_cad = sum(s[0] for s in flat_stats) / len(flat_stats)
                flat_gct = sum(s[1] for s in flat_stats) / len(flat_stats)
                out.append(f"- Flat baseline: cad {flat_cad:.0f} | GCT {flat_gct:.0f}ms")

        out.append("- _Threshold_: cadence <175 + GCT >270ms + std-dev >30 "
                   "all at once = quad-braking (overstride-and-brake pattern, "
                   "the quad destroyer — the main cause of post-trail leg "
                   "soreness).")
        out.append("- _Ideal_: descent cadence ≥ flat + 5 spm (small steps, "
                   "fast turnover) / GCT ≤ flat / rhythm steady (low std-dev).")
        return out

    # ── Section 5: Intra-segment fade (long climbs/descents) ────────────────

    def _render_segment_fade(self, rows, grades, segments) -> list[str]:
        """For climbs/descents ≥3min, compare first half vs second half:
        did HR drift up? did pace slow within the segment? Answers
        'did you fade WITHIN this climb' — the trail equivalent of per-rep
        halves analysis in intervals."""
        long_segs = [s for s in segments
                     if s['kind'] in ('climb', 'descent')
                     and (s['end_sec'] - s['start_sec']) >= self.SEGMENT_FADE_MIN_S]
        if not long_segs:
            return []

        out = ["", f"### Long-segment fade (≥{self.SEGMENT_FADE_MIN_S//60}min) — first half vs second half"]
        out.append("- _The question is \"did this segment get harder as it "
                   "went on\" — HR drifts up + pace slows = the starting "
                   "effort for this segment was wrong._")
        kind_label = {'climb': 'climb', 'descent': 'descent'}

        for s in long_segs:
            seg_rows = rows[s['start_i']:s['end_i'] + 1]
            n = len(seg_rows)
            if n < 6:
                continue
            mid = n // 2
            front, back = seg_rows[:mid], seg_rows[mid:]

            def stats(rs):
                hrs  = [r[1] for r in rs if r[1] is not None]
                spds = [r[2] for r in rs if r[2] and r[2] > 0.5]
                avg_hr   = sum(hrs)/len(hrs) if hrs else None
                avg_pace = (1000 / (sum(spds)/len(spds))) if spds else None
                return avg_hr, avg_pace

            hr_f, pace_f = stats(front)
            hr_b, pace_b = stats(back)
            if hr_f is None or hr_b is None:
                continue

            dur_min  = (s['end_sec'] - s['start_sec']) / 60
            seg_grades = [g for g in grades[s['start_i']:s['end_i'] + 1] if g is not None]
            avg_grade = sum(seg_grades) / len(seg_grades) if seg_grades else None
            grade_str = f"{avg_grade:+.1f}%" if avg_grade is not None else "—"

            hr_d = hr_b - hr_f
            parts = [
                f"sec {s['start_sec']}-{s['end_sec']} ({kind_label[s['kind']]}, "
                f"{dur_min:.1f}min, avg grade {grade_str})",
                f"HR {hr_f:.0f}→{hr_b:.0f} ({hr_d:+.0f})",
            ]
            if pace_f is not None and pace_b is not None:
                pace_d = pace_b - pace_f
                parts.append(f"pace {self._fmt_pace(pace_f)}→{self._fmt_pace(pace_b)} ({pace_d:+.0f}s/km)")
            out.append("- " + " | ".join(parts))

        out.append("- _Threshold (within-segment)_: HR drift <+3bpm per "
                   "segment = right starting effort / +3-+6 = borderline / "
                   ">+6 = went out too hot in this segment, forced to fade "
                   "in the back half. On descents, if pace gets progressively "
                   "slower AND HR rises = legs are breaking down, technique "
                   "is failing.")
        return out

    # ── Section 6: VO across grade buckets ──────────────────────────────────

    def _render_vo_grade_buckets(self, rows, grades) -> list[str]:
        # Need vert_osc_cm column (r[6])
        valid = [(r[6], g) for r, g in zip(rows, grades)
                 if r[6] is not None and g is not None]
        if not valid:
            return []
        out = ["", "### Vertical oscillation (VO) by grade bucket"]
        bucket_defs = [
            ('steep up >+10%',     lambda g: g > 10),
            ('gentle/mid up +3..+10%', lambda g: 3 < g <= 10),
            ('flat -3..+3%',       lambda g: -3 <= g <= 3),
            ('gentle/mid down -10..-3%', lambda g: -10 <= g < -3),
            ('steep down <-10%',   lambda g: g < -10),
        ]
        bucket_vos = {label: [] for label, _ in bucket_defs}
        for vo, g in valid:
            for label, fn in bucket_defs:
                if fn(g):
                    bucket_vos[label].append(vo)
                    break
        flat_vo = None
        for label, _ in bucket_defs:
            vos = bucket_vos[label]
            if not vos: continue
            avg = sum(vos) / len(vos)
            if label.startswith('flat'):
                flat_vo = avg
            out.append(f"- {label}: {avg:.1f}cm ({len(vos)} samples)")

        if flat_vo is not None:
            out.append(f"- _Interpretation_: technical sections (steep up / "
                       f"steep down) with VO significantly higher than flat "
                       f"VO ({flat_vo:.1f}) = bouncing vertically instead of "
                       f"driving forward, wasting energy. Ideal is VO that "
                       f"stays similar across buckets.")
        return out

    # ── Section 7: Aerobic decoupling (ultra only) ──────────────────────────

    def _render_ultra_decoupling(self, rows) -> list[str]:
        out = ["", "### Aerobic decoupling (ultra only — first half vs second half)"]
        valid = [r for r in rows if r[1] is not None and r[2] and r[2] > 0.5]
        if len(valid) < 30:
            return out
        mid = len(valid) // 2
        front, back = valid[:mid], valid[mid:]
        avg_hr_f  = sum(r[1] for r in front) / len(front)
        avg_hr_b  = sum(r[1] for r in back)  / len(back)
        avg_spd_f = sum(r[2] for r in front) / len(front)
        avg_spd_b = sum(r[2] for r in back)  / len(back)
        eff_f = avg_spd_f / avg_hr_f
        eff_b = avg_spd_b / avg_hr_b
        decoupling = (eff_f - eff_b) / eff_f * 100
        hr_drift   = (avg_hr_b - avg_hr_f) / avg_hr_f * 100

        out.append(f"- First half HR {avg_hr_f:.0f}bpm | Second half HR {avg_hr_b:.0f}bpm | "
                   f"HR drift {hr_drift:+.1f}% | decoupling {decoupling:+.1f}%")
        out.append("- _Threshold_: <5% elite / 5-8% normal / >8% wall risk "
                   "(in ultras, layer in thermo + fueling factors).")
        return out

    # ── Section 8: Heat surrogate ──────────────────────────────────────────

    def _render_temp_trend(self, rows) -> list[str]:
        temps = [r[11] for r in rows if r[11] is not None]
        if len(temps) < 30:
            return []
        t_min  = min(temps)
        t_max  = max(temps)
        t_avg  = sum(temps) / len(temps)
        # Front-half vs back-half (approximate trend)
        mid    = len(temps) // 2
        t_front = sum(temps[:mid]) / mid
        t_back  = sum(temps[mid:]) / (len(temps) - mid)
        out = ["", "### Hydration / Heat surrogate"]
        out.append(f"- Full-activity temp: avg {t_avg:.1f}°C (range {t_min:.1f} – {t_max:.1f})")
        if abs(t_back - t_front) > 1:
            out.append(f"- First-half avg {t_front:.1f}°C → second-half avg "
                       f"{t_back:.1f}°C ({t_back-t_front:+.1f})")
        if t_max >= 28:
            out.append(f"- _Heat-stress risk_: peak {t_max:.1f}°C ≥28 → "
                       f"second-half HR drift may be heat, not fitness.")
        return out

    # ── Section 9: Manual lap summary ──────────────────────────────────────

    def _render_manual_lap_summary(self, lap_windows, rows, grades) -> list[str]:
        out = ["", "### User-manual laps (aid station / transition / surface-change markers)"]
        out.append(f"- {len(lap_windows)} manual laps total; if the user's "
                   f"comment refers to a specific lap (e.g. \"got tired "
                   f"after lap 3\" / \"lap 5 was an aid station\"), prefer "
                   f"the comment to interpret what each lap boundary means.")
        out.append("- _Trail meta-rule: every lap's pace must be read with avg grade + GAP._")
        for w in lap_windows:
            seg_pairs = [(i, r) for i, r in enumerate(rows)
                         if w['start_sec'] <= r[0] < w['end_sec']]
            if not seg_pairs:
                continue
            seg_rows = [r for _, r in seg_pairs]
            seg_idx  = [i for i, _ in seg_pairs]

            hrs  = [r[1] for r in seg_rows if r[1] is not None]
            spds = [r[2] for r in seg_rows if r[2] and r[2] > 0.5]
            avg_hr = sum(hrs) / len(hrs) if hrs else 0
            pace = (1000 / (sum(spds)/len(spds))) if spds else None
            pace_str = self._fmt_pace(pace)

            # avg grade across this lap (from already-smoothed per-sample grades)
            seg_grades = [grades[i] for i in seg_idx if grades[i] is not None]
            avg_grade = sum(seg_grades)/len(seg_grades) if seg_grades else None
            grade_str = f"{avg_grade:+.1f}%" if avg_grade is not None else "—"

            # GAP for this lap — reuse the same logic as GAP × Terrain
            fake_seg = {'start_i': seg_idx[0], 'end_i': seg_idx[-1]}
            _, gap = self._seg_gap_pace(rows, fake_seg, grades)
            gap_str = self._fmt_pace(gap)

            dist_km = w['dist_m'] / 1000 if w['dist_m'] else 0
            dur_s = w['dur_s']
            out.append(f"  - Lap {w['lap_id']} (sec {w['start_sec']}-{w['end_sec']}): "
                       f"{dist_km:.2f}km / {int(dur_s//60)}:{int(dur_s%60):02d} / "
                       f"avg grade {grade_str} / HR {avg_hr:.0f} / pace {pace_str} / GAP {gap_str}")
        return out

    # ── Section 10: Tool availability ──────────────────────────────────────

    @staticmethod
    def _tool_hint_section() -> list[str]:
        return [
            "",
            "### Tool availability",
            "- **`get_window_stats(start, end, key_type)`** is the default "
              "drill-down tool for trail. Returns HR / pace / mechanics "
              "aggregates **plus a `grade` block (`avg_grade_pct`, "
              "`elev_gain_m`, `elev_loss_m`, `gap_pace_s_per_km`)**. One "
              "call gives you the full grade context — no need to compute "
              "it yourself. Useful for \"GAP in the second half of lap 3\", "
              "\"avg grade + HR over a particular climb\", \"elev gain on "
              "the steepest segment\".",
            "- For raw 1Hz rows (e.g. to check whether HR jumped at sec X / "
              "to inspect the pace-curve shape / to retrieve a complete "
              "metadata timeseries), call `get_raw_window_by_time` / "
              "`get_raw_window_by_distance` — pass `\"elevation\"` in "
              "channels to also get the raw elevation timeseries.",
            "- By default, prefer the builder's GAP × Terrain / long-segment "
              "fade / downhill technique / Manual lap sections; only call a "
              "tool when the slice granularity is insufficient (e.g. to "
              "analyze the internals of a single descent within a cluster, "
              "or when the user's comment refers to \"the last 200m before "
              "the summit\").",
        ]
