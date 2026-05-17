"""TempoBuilder — for `tempo` + `threshold` tags.

Both tags share the same data analysis: tempo / threshold workouts test
**plateau stability** — can the body sustain a given intensity without HR
drift? The recovery-vs-base-style "decoupling" check is core. Tempo (LT-30)
vs threshold (LT) distinction is surfaced by the prompt, not the math —
both are evaluated with the same metrics, just at different target HRs.

Output language: context_md is emitted in **neutral English**. The LLM's
response language is steered by the per-tag prompt (P5), not the builder.

Per the meta-rule (plan: "PRIORITIZED, not EXCLUSIVE"), this builder ADDS
tempo-specific deep analysis ON TOP of DefaultBuilder's baseline.

Segmentation policy (priority cascade — applied IN THE PROMPT, not here):
  1. **User comment is ground truth.** If the user's comment describes
     workout structure (e.g. "10min WU + 25min @4:00 + 5min CD"), the LLM
     should use that to interpret the data — even if the builder's
     auto-detected main-set window doesn't match.
  2. **Manual lap structure.** If user manually lapped (detected via
     shared `is_manual_lap_structure`), each lap is treated as a candidate
     warmup / main / cooldown segment.
  3. **HR-trend fallback.** If neither of the above apply (auto-1km lap or
     single lap, no comment structure), builder identifies the longest
     contiguous Z3+ segment as the "main set candidate" and reports
     warmup / main / cooldown around it.

Builder ALWAYS provides ONE of the two data framings (lap-by-lap OR
HR-trend warmup/main/cooldown). The prompt directs the LLM to use the
user's comment as the master frame when it specifies structure.

Coaching philosophy (from plan):
- THE anti-pattern: uneven pacing within a "steady" effort. 10min @3:50 +
  10min @4:10 averages 4:00 but biological benefit is reduced — no
  sustained lactate-stress block. **Smoothness > absolute pace.**
- Aerobic decoupling within main set: HR drift > 5% front→back of main =
  aerobic base unstable at this intensity, OR fueling/heat/fatigue.
- Pace stability: CV (std/mean) of speed within main set. < 3% smooth,
  3-6% moderate, > 6% sawtooth (re-acceleration glycogen waste).
- Form-fatigue early-warning: GCT + vertical ratio drift across main set.
- Cadence is THE pre-failure signal — when fatigued runners let cadence
  drop and lengthen stride to hold pace.
"""

import sqlite3

from review_builders.base       import (
    BuildResult, ReviewBuilder, is_manual_lap_structure, lap_windows_from_db
)
from review_builders.default    import DefaultBuilder
from review_builders.primitives import (
    seg_stats as primitive_seg_stats,
    hr_drift as hr_drift_regression,
    pa_hr_split, slice_by_km, lap_pace_cv, pairwise_delta,
    fmt_pace, fmt_pace_compact,
)


class TempoBuilder(ReviewBuilder):
    name = "TempoBuilder"

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag in ("tempo", "threshold")

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep = self._tempo_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Constants ─────────────────────────────────────────────────────────────
    MIN_MAIN_SEG_SEC = 5 * 60   # main set must be ≥5min to be meaningful

    # ── Tempo deep analysis ──────────────────────────────────────────────────

    def _tempo_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
        """Hybrid output: pure math + pattern extraction + reference thresholds.
        NO verdicts (✅/⚠️/❌), NO actionable advice — those are the LLM's job.

        Layout follows the LongRun template (per-activity / lap structure /
        per-lap / per-km / structure-agnostic / tool hint) but inserts the
        tempo-specific main-set-detection branches (manual lap vs HR-trend)
        between the lap-structure detection and the per-km breakdown, because
        main-set identification is THE central question for tempo / threshold.
        """
        # 8th column is distance_cum_m so slice_by_km can use it.
        rows = conn.execute("""
            SELECT sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio,
                   stride_cm, distance_cum_m
              FROM activity_metrics
             WHERE activity_id = ?
             ORDER BY sec_offset
        """, (aid,)).fetchall()
        if not rows:
            return ""

        # Native sample interval (1s for fresh fetch, ~10s for legacy backfill)
        if len(rows) >= 3:
            deltas = sorted(rows[i+1][0] - rows[i][0] for i in range(len(rows) - 1))
            median_delta = max(1, deltas[len(deltas) // 2])
        else:
            median_delta = 1

        # Z3 ceiling (= Z2/Z3 boundary) — tempo's "in main set" detection threshold
        z3 = conn.execute("""
            SELECT zone_low_boundary FROM activity_hr_zones
             WHERE activity_id = ? AND zone_number = 3
        """, (aid,)).fetchone()
        z3_ceiling = z3[0] if z3 and z3[0] is not None else None

        # Lap structure (shared utility — robust to km + mile auto-lap)
        # lap_windows are pause-aware (use startTimeGMT, not cum duration_s)
        raw_windows  = lap_windows_from_db(conn, aid)
        distances    = [w['dist_m'] for w in raw_windows if w['dist_m']]
        looks_manual = is_manual_lap_structure(distances)
        lap_windows  = []   # [(human_lap_id, start_sec, end_sec, dist_km)]
        if looks_manual:
            for w in raw_windows:
                lap_windows.append((w['lap_id'], w['start_sec'], w['end_sec'],
                                    w['dist_m'] / 1000))

        out = [
            "## 🎯 Tempo / threshold-specific analysis",
            "",
            "_Data + derived signals + coach-consensus reference thresholds. "
            "Verdict is yours (LLM) to synthesize from the activity tag, the "
            "user's notes (**the user's notes are the most authoritative "
            "source of structure**), personal_note, and long-term memory — "
            "do not re-quote these numbers verbatim. The central indicator "
            "for tempo / threshold is **main-set identification + within-"
            "main-set smoothness**; the other sections are supporting._",
        ]

        # ── (1) Per-activity overview ────────────────────────────────────
        whole = primitive_seg_stats(rows)
        if whole:
            out += self._overview_section(whole, raw_windows)

        # ── (2) Lap structure detection ─────────────────────────────────
        out.append("")
        out.append("### Lap structure detection")
        if looks_manual and lap_windows:
            avg_lap_km = sum(d for *_, d in lap_windows[:-1]) / max(1, len(lap_windows) - 1)
            out.append(f"- {len(distances)} laps, non-final laps average "
                       f"{avg_lap_km:.1f}km → **likely manual laps**")
            out.append("- _Downstream main-set detection follows the user's "
                       "manual laps (warmup / main set / cooldown are decided "
                       "by the user's lap boundaries)_")
        elif len(distances) >= 2:
            out.append(f"- {len(distances)} laps, distances near integer km "
                       f"→ likely Garmin auto-1km laps")
            out.append("- _Downstream main-set detection falls back to HR-trend "
                       "detection (longest contiguous Z3+ window = main-set candidate)_")
        else:
            out.append("- Single lap only, no subjective user segmentation")
            out.append("- _Downstream main-set detection falls back to HR-trend "
                       "detection (longest contiguous Z3+ window = main-set candidate)_")

        # ── (3) Main-set identification + segment compare + per-lap intra ─
        if looks_manual and lap_windows:
            out.extend(self._analyze_by_laps(rows, lap_windows))
        else:
            out.extend(self._analyze_by_hr_trend(rows, z3_ceiling, median_delta))

        # ── (4) Per-km breakdown ────────────────────────────────────────
        km_buckets = slice_by_km(rows)
        if km_buckets:
            out += self._per_km_section(km_buckets)

        # ── (5) Structure-agnostic key readings ─────────────────────────
        out += self._drift_section(rows, raw_windows, km_buckets)

        # ── (6) Tool availability ───────────────────────────────────────
        out += self._tool_hint_section()

        return "\n".join(out)

    # ── New sections (aligned with LongRun template) ─────────────────────

    @staticmethod
    def _overview_section(whole: dict, raw_windows: list[dict]) -> list[str]:
        out = ["", "### Per-activity overview"]
        out.append(f"- HR: avg {whole['hr_avg']:.0f} bpm | "
                   f"p10 {whole['hr_p10']:.0f} | p50 {whole['hr_p50']:.0f} | "
                   f"p90 {whole['hr_p90']:.0f} | max {whole['hr_max']:.0f}")
        out.append(f"- Pace: avg {fmt_pace(whole['pace_s_per_km'])} | "
                   f"p10 (fast) {fmt_pace(whole['pace_p10_s_per_km'])} | "
                   f"p50 {fmt_pace(whole['pace_p50_s_per_km'])} | "
                   f"p90 (slow) {fmt_pace(whole['pace_p90_s_per_km'])}")
        mech_parts = []
        if whole.get("cadence_avg") is not None:
            mech_parts.append(f"cadence {whole['cadence_avg']:.0f}")
        if whole.get("stride_avg_m") is not None:
            mech_parts.append(f"stride {whole['stride_avg_m']:.2f}m")
        if whole.get("gct_avg_ms") is not None:
            mech_parts.append(f"GCT {whole['gct_avg_ms']:.0f}ms")
        if whole.get("vr_avg_pct") is not None:
            mech_parts.append(f"vertical ratio {whole['vr_avg_pct']:.1f}%")
        if mech_parts:
            out.append(f"- Mechanics avg: {' | '.join(mech_parts)}")

        # Lap-level pace CV — structure hint
        if raw_windows and len(raw_windows) >= 2:
            lap_paces = []
            for w in raw_windows:
                if w['dist_m'] and w['dur_s']:
                    spd = w['dist_m'] / w['dur_s']
                    if spd > 0:
                        lap_paces.append(1000 / spd)
            cv = lap_pace_cv(lap_paces)
            if cv is not None:
                fastest = min(lap_paces)
                slowest = max(lap_paces)
                spread  = slowest - fastest
                out.append(f"- Lap pace distribution: CV (coefficient of "
                           f"variation) {cv*100:.1f}% | "
                           f"fastest {fmt_pace(fastest)} → slowest {fmt_pace(slowest)} "
                           f"(spread {spread:.0f}s/km)")
                out.append(f"  _Interpretation hint (tempo lens)_: high CV / "
                           f"large spread = multi-segment structure "
                           f"(warmup + main + cooldown, or progression); "
                           f"low CV / small spread = single continuous segment.")
        return out

    @staticmethod
    def _per_km_section(km_buckets: list[dict]) -> list[str]:
        out = ["", f"### Per-km breakdown ({len(km_buckets)} km)"]
        out.append("| km | sec | pace | HR | cadence | GCT | vertical ratio | stride |")
        out.append("|---|---|---|---|---|---|---|---|")
        for b in km_buckets:
            s = b['stats']
            cells = [
                f"{b['km']}",
                f"{b['start_sec']}-{b['end_sec']}",
                fmt_pace_compact(s['pace_s_per_km']),
                f"{s['hr_avg']:.0f}",
                f"{s['cadence_avg']:.0f}"   if s.get('cadence_avg')  is not None else "—",
                f"{s['gct_avg_ms']:.0f}"    if s.get('gct_avg_ms')   is not None else "—",
                f"{s['vr_avg_pct']:.1f}"    if s.get('vr_avg_pct')   is not None else "—",
                f"{s['stride_avg_m']:.2f}"  if s.get('stride_avg_m') is not None else "—",
            ]
            out.append("| " + " | ".join(cells) + " |")
        return out

    @staticmethod
    def _drift_section(rows: list, raw_windows: list[dict],
                       km_buckets: list[dict]) -> list[str]:
        """Structure-agnostic readings — universal regardless of tempo form
        (plateau / progression / cruise). Whole-run hr_drift here is mostly
        polluted by WU/CD structure (R² typically low), but kept for
        completeness; the truer drift signal lives in per-lap-internal
        breakdown's main-set-internal HR drift slope (see above)."""
        out = ["", "### Structure-agnostic key readings (precomputed, fixed definitions)"]

        # Whole-run HR-time linear regression
        hrd = hr_drift_regression(rows)
        if hrd:
            out.append(f"- **Full-activity HR drift** (linear regression on time): "
                       f"{hrd['slope_per_min']:+.2f} bpm/min, R²={hrd['r_squared']:.2f}")
            out.append("  _(Full-activity includes warmup and cooldown, so R² "
                       "is usually low = HR is dominated by structure. The "
                       "true main-set drift signal is in the 'Internal HR-time "
                       "drift' line under each Lap N's internal breakdown above.)_")

        # Pa:HR — whole-run EF decoupling
        pa = pa_hr_split(rows)
        if pa:
            out.append(f"- **Pa:HR decoupling** (heart-rate-to-pace ratio, "
                       f"first-half EF vs second-half EF): "
                       f"{pa['decoupling_pct']:+.1f}% "
                       f"(first half HR {pa['first_half_hr']:.0f} @ {fmt_pace(pa['first_half_pace'])} → "
                       f"second half HR {pa['second_half_hr']:.0f} @ {fmt_pace(pa['second_half_pace'])})")
            out.append("  _(Whole-activity Pa:HR for structured tempo / "
                       "threshold also leans structure-driven; the meaningful "
                       "drift signal lives in the push segment's internal "
                       "breakdown. Pa:HR <2% on a plateau LT effort is the "
                       "signature of a stable plateau.)_")

        # First km vs last km
        if len(km_buckets) >= 2:
            d = pairwise_delta(km_buckets[0]['stats'], km_buckets[-1]['stats'])
            parts = []
            if d.get('hr_delta') is not None:
                parts.append(f"HR {d['hr_delta']:+.0f}bpm ({d['hr_delta_pct']:+.1f}%)")
            if d.get('pace_delta_s') is not None:
                parts.append(f"pace {d['pace_delta_s']:+.0f}s/km ({d['pace_delta_pct']:+.1f}%)")
            mech_parts = []
            if d.get('cadence_delta') is not None:
                mech_parts.append(f"cadence {d['cadence_delta']:+.0f}")
            if d.get('gct_delta') is not None:
                mech_parts.append(f"GCT {d['gct_delta']:+.0f}ms")
            if d.get('vr_delta') is not None:
                mech_parts.append(f"vertical ratio {d['vr_delta']:+.2f}pt")
            if d.get('stride_delta') is not None:
                mech_parts.append(f"stride {d['stride_delta']*100:+.0f}cm")
            line = "- **First km vs last km**: " + " | ".join(parts)
            if mech_parts:
                line += " | Mechanics: " + ", ".join(mech_parts)
            out.append(line)

        # First lap vs last lap
        if raw_windows and len(raw_windows) >= 2:
            first_seg = [r for r in rows
                         if raw_windows[0]['start_sec'] <= r[0] < raw_windows[0]['end_sec']]
            last_seg  = [r for r in rows
                         if raw_windows[-1]['start_sec'] <= r[0] < raw_windows[-1]['end_sec']]
            sf = primitive_seg_stats(first_seg)
            sl = primitive_seg_stats(last_seg)
            if sf and sl:
                d = pairwise_delta(sf, sl)
                parts = []
                if d.get('hr_delta') is not None:
                    parts.append(f"HR {d['hr_delta']:+.0f}bpm ({d['hr_delta_pct']:+.1f}%)")
                if d.get('pace_delta_s') is not None:
                    parts.append(f"pace {d['pace_delta_s']:+.0f}s/km ({d['pace_delta_pct']:+.1f}%)")
                if parts:
                    out.append("- **First lap vs last lap**: " + " | ".join(parts) +
                               " _(in tempo / threshold the first lap is "
                               "usually warmup and the last is usually "
                               "cooldown; do not read this as cardiac drift)_")
        return out

    @staticmethod
    def _tool_hint_section() -> list[str]:
        return [
            "",
            "### Tool availability",
            "- For custom slices within the main set (e.g. \"first 5 min / "
            "middle 10 min / last 5 min\" of a progression LT), or the "
            "internal stats of a single cruise-interval rep, call "
            "`get_window_stats(start, end, key_type, channels?)`.",
            "- For raw 1Hz rows over an arbitrary window (e.g. to check "
            "whether the first 2 min of the main set ramped too fast, or "
            "whether the final 30s was a sprint), call "
            "`get_raw_window_by_time` / `get_raw_window_by_distance` "
            "(existing tools).",
            "- By default, prefer the per-lap internal breakdown + per-km "
            "slices the builder provides; only call a tool when the slice "
            "granularity is insufficient.",
        ]

    # ── Branch A: manual-lap segmented analysis ─────────────────────────────

    def _analyze_by_laps(self, rows, lap_windows) -> list[str]:
        """Per-lap stats — each lap is a candidate warmup / main / cooldown / rep.
        Compute the standard tempo metrics on each lap so LLM can identify
        which lap is the actual main set (cross-ref with user comment).

        For EACH lap with duration ≥5min, also run the intra-segment
        breakdown (first/second half + drift + CV + mechanics drift). This supports multi-stage
        forms (progression tempo: Lap 2 stage + Lap 3 stage) and rep-style
        forms (cruise tempo / threshold-w/-rep: each rep gets its own internal
        readings). Previously only the highest-HR lap got intra-segment,
        which left stage/rep internals unavailable for any other lap and
        forced the LLM to call get_window_stats for each — wasteful given
        the data is already in builder reach.

        Main set HEURISTIC GUESS (highest-HR lap ≥5min) is still surfaced
        so LLM has a default anchor for plateau cases; LLM overrides via
        comment when the run is progression / cruise / multi-stage."""
        segments = []   # [(lap_id, start, end, km, stats_dict)]
        for lap_id, start, end, km in lap_windows:
            seg = [r for r in rows if start <= r[0] < end]
            s = self._seg_stats(seg)
            if s is not None:
                segments.append((lap_id, start, end, km, s))

        if not segments:
            return []

        out = ["", "### Lap-segmented comparison (using the user's manual laps)"]
        for lap_id, start, end, km, s in segments:
            extras = self._fmt_extras(s)
            tail   = f" | {extras}" if extras else ""
            out.append(f"- Lap {lap_id} (sec {int(start)}-{int(end)}, "
                       f"{int(start)//60}-{int(end)//60}min, {km:.1f}km): "
                       f"HR {s['hr']:.0f}bpm @ {self._fmt_pace(s['pace'])} | "
                       f"pace CV {s['cv_speed']*100:.1f}%{tail}")

        # Main set heuristic guess + caveat
        eligible = [s for s in segments if (s[2] - s[1]) >= self.MIN_MAIN_SEG_SEC]
        if eligible:
            main_candidate = max(eligible, key=lambda s: s[4]["hr"])
            mc_id, mc_start, mc_end, _, mc_stats = main_candidate
            out.append("")
            out.append("### Main-set candidate (highest-HR lap ≥5min — cross-check with the user's notes)")
            out.append(f"- Lap {mc_id} ({int(mc_start)//60}-{int(mc_end)//60}min): "
                       f"HR {mc_stats['hr']:.0f}bpm, {(mc_end-mc_start)//60} minutes")
            out.append("- _Interpretation_: this is only the builder's "
                       "heuristic guess. For progression / cruise / multi-stage "
                       "forms, re-identify the main set using the user's "
                       "notes as ground truth — internal readings are provided "
                       "below for every eligible lap (≥5min).")

            # Intra-segment breakdown for EVERY eligible lap (≥5min),
            # not just the main candidate. Supports progression and cruise
            # forms where multiple laps are part of the main set / rep set.
            for lap_id, lap_start, lap_end, _km, _stats in eligible:
                out.extend(self._intra_segment_breakdown(
                    rows, lap_start, lap_end, f"Lap {lap_id}"
                ))
        return out

    # ── Branch B: HR-trend main set detection ───────────────────────────────

    def _analyze_by_hr_trend(self, rows, z3_ceiling, median_delta) -> list[str]:
        """When no manual lap, find longest contiguous Z3+ window as the main
        set candidate. Report warmup / main / cooldown stats around it."""
        if z3_ceiling is None:
            # No HR zone data — can't detect main set. Skip section.
            return ["", "### Main-set detection failed",
                    "- HR-zone data is missing — cannot identify a main-set "
                    "candidate. Infer the main set from lap details and "
                    "heart-rate distribution, or follow the structure "
                    "described in the user's notes."]

        main_start, main_end = self._find_longest_z3_plus(rows, z3_ceiling, median_delta)
        total_end = rows[-1][0]

        out = ["", "### Main-set detection (HR trend: longest contiguous Z3+ segment)"]
        if main_start is None:
            out.append(f"- No sustained Z3+ window detected over the whole "
                       f"activity (>{z3_ceiling} bpm with 30s smoothing).")
            out.append("- _Interpretation_: this may have been a low-intensity "
                       "session. If the intent was tempo / threshold, the "
                       "intensity didn't reach target.")
            out.append("- _Reference_: a tempo / threshold main set should "
                       "contain a sustained Z3+ segment of ≥ 10 min.")
            return out

        main_dur_min = (main_end - main_start) // 60
        out.append(f"- Main set candidate: {main_start//60}-{main_end//60}min "
                   f"(lasts {main_dur_min} min, HR ≥ {z3_ceiling} bpm)")

        # Warmup / Main / Cooldown stats (each as one segment)
        warmup_seg = [r for r in rows if r[0] < main_start]
        main_seg   = [r for r in rows if main_start <= r[0] <= main_end]
        cooldown_seg = [r for r in rows if r[0] > main_end]

        out.append("")
        out.append("### Three-segment comparison (warmup / main / cooldown)")
        # Skip degenerate segments (≤30s) — happens when activity ends right
        # at main set with no cooldown, or starts straight in.
        sections = [
            ("warmup",   warmup_seg,   (0, main_start)),
            ("main",     main_seg,     (main_start, main_end)),
            ("cooldown", cooldown_seg, (main_end, total_end)),
        ]
        for label, seg, span in sections:
            if span[1] - span[0] < 30:
                continue   # degenerate window (e.g. activity ends with main set)
            s = self._seg_stats(seg)
            if s is None:
                out.append(f"- {label} ({span[0]//60}-{span[1]//60}min): _insufficient data_")
                continue
            extras = self._fmt_extras(s)
            tail   = f" | {extras}" if extras else ""
            out.append(f"- {label} ({span[0]//60}-{span[1]//60}min): "
                       f"HR {s['hr']:.0f}bpm @ {self._fmt_pace(s['pace'])} | "
                       f"pace CV {s['cv_speed']*100:.1f}%{tail}")

        # Detailed within-main breakdown (the most important segment)
        out.extend(self._intra_segment_breakdown(rows, main_start, main_end, "Main set"))
        return out

    # ── Within-main-set deep dive (shared by both branches) ─────────────────

    def _intra_segment_breakdown(self, rows, seg_start, seg_end, label) -> list[str]:
        """Cardiac drift + pace stability + form drift + cadence INSIDE the
        identified main set window. The richest part of tempo analysis."""
        seg = [r for r in rows if seg_start <= r[0] <= seg_end]
        valid = [r for r in seg if r[1] is not None and r[2] and r[2] > 0.5]
        if len(valid) < 20:
            return ["", f"### {label} internal breakdown",
                    "- Insufficient data, skipped."]

        mid = len(valid) // 2
        front, back = valid[:mid], valid[mid:]
        sf = self._seg_stats(front)
        sb = self._seg_stats(back)
        if not (sf and sb):
            return []

        hr_drift   = (sb["hr"]   - sf["hr"])   / sf["hr"]   * 100
        pace_drift = (sb["pace"] - sf["pace"]) / sf["pace"] * 100
        decoupling = (sf["eff"]  - sb["eff"])  / sf["eff"]  * 100

        out = ["", f"### {label} internal breakdown (first half / second half + full segment)"]

        # Cardiac drift inside main
        out.append(f"- Cardiac drift (first→second half): HR {hr_drift:+.1f}% | "
                   f"pace {pace_drift:+.1f}% | decoupling {decoupling:+.1f}%")
        out.append("  - _Threshold_: HR drift <3% = stable plateau / 3-5% = "
                   "borderline / >5% = aerobic base unstable at this "
                   "intensity (dehydration / under-fueling / heat stress / "
                   "intensity chosen too high).")

        # Main-set-internal HR-time linear regression — structure-agnostic
        # complement to halves-based decoupling. Window is intentionally
        # same-intensity (this IS the main set), so the slope reflects genuine
        # within-effort drift rather than warmup/cooldown transitions.
        hrd = hr_drift_regression(valid)
        if hrd:
            out.append(f"- Internal HR-time drift (linear regression, "
                       f"split-independent): "
                       f"{hrd['slope_per_min']:+.2f} bpm/min, R²={hrd['r_squared']:.2f}")
            out.append("  - _Threshold_: <+0.3 bpm/min = stable output / "
                       "+0.3-0.5 = borderline / >+0.5 = already at ceiling. "
                       "High R² (>0.5) = drift is reliably linear; low R² = "
                       "HR isn't drifting linearly, possibly surge/decel "
                       "alternation (cross-reference with CV).")

        # Pace stability — sawtooth detection
        full_stats = self._seg_stats(valid)
        if full_stats:
            cv_pct = full_stats["cv_speed"] * 100
            out.append(f"- Pace stability (full-segment speed CV): {cv_pct:.1f}%")
            out.append("  - _Threshold_: <3% = smooth cruise / 3-6% = "
                       "moderate fluctuation / >6% = sawtooth "
                       "(surge→decel→surge, wastes glycogen; "
                       "'smoothness > pace').")

        # Form drift: GCT, vertical ratio halves
        if sf["gct"] is not None and sb["gct"] is not None:
            out.append(f"- GCT drift: {sf['gct']:.0f} → {sb['gct']:.0f} ms "
                       f"({sb['gct']-sf['gct']:+.0f})")
        if sf["vr"] is not None and sb["vr"] is not None:
            out.append(f"- Vertical ratio drift: {sf['vr']:.1f} → {sb['vr']:.1f} % "
                       f"({sb['vr']-sf['vr']:+.2f}pt)")

        # Cadence + Stride — the pre-failure signal pair.
        # Math: cadence × stride = speed. If pace holds but cadence drops,
        # stride MUST have lengthened — it's a forced consequence, not
        # independent info. But surfacing both makes the narrative concrete
        # ("stride lengthened from 1.10m to 1.18m" is more visceral than
        # "cadence dropped 5spm").
        if sf["cad"] is not None and sb["cad"] is not None:
            cad_drift = sb["cad"] - sf["cad"]
            line = f"- Cadence drift: {sf['cad']:.0f} → {sb['cad']:.0f} spm ({cad_drift:+.0f})"
            if sf["stride_m"] is not None and sb["stride_m"] is not None:
                stride_drift_cm = (sb["stride_m"] - sf["stride_m"]) * 100
                line += (f" | Stride drift: {sf['stride_m']:.2f} → "
                         f"{sb['stride_m']:.2f} m ({stride_drift_cm:+.0f} cm)")
            out.append(line)
            out.append("  - _Threshold_: second-half cadence drop ≥3spm + "
                       "stride growth ≥5cm + pace held → forcing it with a "
                       "longer stride (the most actionable pre-failure "
                       "signal; next time, slow down 5-10s/km).")

        return out

    # ── HR-trend main set detection ─────────────────────────────────────────

    @staticmethod
    def _find_longest_z3_plus(rows, ceiling_hr, median_delta) -> tuple:
        """Find longest contiguous segment with 30s-smoothed HR ≥ ceiling.
        Returns (start_sec, end_sec) or (None, None) if no qualifying segment.

        Smoothing handles brief dips (e.g. recovery jog mid-rep) so a
        sub-second glitch doesn't break the run. Minimum-meaningful-length
        threshold: 5 minutes — anything shorter probably isn't main set."""
        hr_vals = [(r[0], r[1]) for r in rows if r[1] is not None]
        window_n = max(1, 30 // median_delta)
        if len(hr_vals) < window_n + 1:
            return None, None

        # Smooth HR with 30s rolling
        smoothed = []
        for i in range(len(hr_vals) - window_n + 1):
            avg = sum(h for _, h in hr_vals[i:i+window_n]) / window_n
            smoothed.append((hr_vals[i][0], avg))

        # Find longest contiguous run where smoothed >= ceiling
        longest_start = longest_end = None
        longest_dur   = 0
        cur_start     = None
        cur_end       = None
        for sec, h in smoothed:
            if h >= ceiling_hr:
                if cur_start is None:
                    cur_start = sec
                cur_end = sec
                cur_dur = cur_end - cur_start
                if cur_dur > longest_dur:
                    longest_dur   = cur_dur
                    longest_start = cur_start
                    longest_end   = cur_end
            else:
                cur_start = None
                cur_end   = None

        # Sanity: at least 5 minutes to count as main set
        if longest_start is None or (longest_end - longest_start) < 5 * 60:
            return None, None
        return longest_start, longest_end

    # ── Stats helpers (shared) ──────────────────────────────────────────────

    @staticmethod
    def _seg_stats(seg_rows):
        """Compute HR / pace / efficiency / speed-CV / cadence / GCT / vertical ratio /
        stride averages from a list of (sec_offset, hr, speed_mps, cadence,
        gct, vr, stride_cm) rows."""
        hrs    = [r[1] for r in seg_rows if r[1] is not None]
        spds   = [r[2] for r in seg_rows if r[2] and r[2] > 0.5]
        cads   = [r[3] for r in seg_rows if r[3] and r[3] > 50]
        gcts   = [r[4] for r in seg_rows if r[4]]
        vrs    = [r[5] for r in seg_rows if r[5] is not None]
        strds  = [r[6] for r in seg_rows if len(r) > 6 and r[6] and r[6] > 30]
        if not hrs or not spds:
            return None
        avg_hr  = sum(hrs)  / len(hrs)
        avg_spd = sum(spds) / len(spds)
        # Speed CV (std / mean) for pace-stability detection
        if len(spds) >= 2:
            var = sum((s - avg_spd) ** 2 for s in spds) / len(spds)
            cv  = (var ** 0.5) / avg_spd if avg_spd > 0 else 0
        else:
            cv = 0
        return {
            "hr":       avg_hr,
            "pace":     1000 / avg_spd,
            "eff":      avg_spd / avg_hr,
            "cv_speed": cv,
            "cad":      (sum(cads)/len(cads)) if cads else None,
            "gct":      (sum(gcts)/len(gcts)) if gcts else None,
            "vr":       (sum(vrs)/len(vrs))   if vrs  else None,
            # Stride length in METERS (cm → m for human-readable narrative)
            "stride_m": (sum(strds)/len(strds))/100 if strds else None,
        }

    @staticmethod
    def _fmt_pace(p):
        return f"{int(p//60)}:{int(p%60):02d}/km"

    @staticmethod
    def _fmt_extras(s):
        parts = []
        # Cadence + stride together — they're mathematically coupled with pace
        # (cadence × stride = speed), so showing both makes the "lengthening
        # stride to compensate for dropping cadence" pattern concrete.
        if s["cad"]      is not None: parts.append(f"cadence {s['cad']:.0f}")
        if s["stride_m"] is not None: parts.append(f"stride {s['stride_m']:.2f}m")
        if s["gct"]      is not None: parts.append(f"GCT {s['gct']:.0f}ms")
        if s["vr"]       is not None: parts.append(f"vertical ratio {s['vr']:.1f}%")
        return " | ".join(parts) if parts else ""
