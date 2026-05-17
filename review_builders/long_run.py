"""LongRunBuilder — for the `long_run` tag.

Design principle (replaces the legacy front-15-vs-back-15 / equal-thirds
pipeline): emit ONLY descriptive data + structure-agnostic universal
readings. NO pre-baked comparisons that depend on framework choice. The
LLM picks the comparison framework based on tag + comment, and uses
get_window_stats() tool for any custom window the comment implies.

What this builder emits:
  1. Per-activity overview (avg/percentile/CV across the whole run)
  2. Per-lap slice (one row per lap — manual or auto)
  3. Per-lap internal readings (loop over all laps: pace CV, HR drift
     slope/R², first-half vs second-half stats — gives LLM the
     "same-intensity block fatigue check" without needing tool calls)
  4. Per-km slice (fine-grained enough to recompose any window via tool
     or by-eye summing — primary lens for auto-1km / 1-lap activities)
  5. Structure-agnostic drift readings: HR-time linear regression, Pa:HR
     decoupling, first-km vs last-km delta, first-lap vs last-lap delta.
     None of these depend on a window choice.

What this builder explicitly does NOT emit:
  - Front-15-vs-back-15 mechanical decay (window choice = framework choice)
  - Equal-time front/middle/back thirds (same)
  - Lap-pairwise drift summary (uses lap boundaries as comparison framework)
  - "Suspected CD lap" / "suspected push lap" labels (interpretation)

The trade-off: this builder is strictly less opinionated. The LLM has to do
more synthesis work, but it has full per-km granularity to do that
synthesis correctly, and the prompt teaches it the three frameworks
(fatigue-driven mechanical-decay screen / same-intensity block fatigue check /
end-to-end coarse screen) so it doesn't conflate them.

Coaching philosophy:
- Pa:HR + structure-agnostic HR-time drift are the long-run aerobic-stability
  signals. Whole-run Pa:HR is meaningful only for steady runs; for structured
  runs the meaningful reading is push-lap-internal HR drift (in section 3).
- Mechanical decay is only validly compared at matched intensity. Per-lap
  internal section's first-half vs second-half stats is the canonical
  same-intensity block check.
- Honor user's manual laps + comments as ground truth.

Output language: context_md is emitted in **neutral English**. The LLM's
response language is steered by the per-tag prompt (P5), not the builder.
"""

import sqlite3

from review_builders.base       import (
    BuildResult, ReviewBuilder, is_manual_lap_structure, lap_windows_from_db
)
from review_builders.default    import DefaultBuilder
from review_builders.primitives import (
    seg_stats, hr_drift, pa_hr_split, slice_by_km, lap_pace_cv,
    pairwise_delta, fmt_pace, fmt_pace_compact, internal_stats,
)


class LongRunBuilder(ReviewBuilder):
    name = "LongRunBuilder"

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag == "long_run"

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep     = self._long_run_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Long-run deep analysis ───────────────────────────────────────────

    def _long_run_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
        # Pull rows in primitives' canonical column order, plus distance_cum_m
        # at idx 7 for slice_by_km.
        rows = conn.execute("""
            SELECT sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio,
                   stride_cm, distance_cum_m
              FROM activity_metrics
             WHERE activity_id = ?
             ORDER BY sec_offset
        """, (aid,)).fetchall()
        if len(rows) < 60:
            return ""

        lap_windows = lap_windows_from_db(conn, aid)

        out = [
            "## 🎯 Long-run-specific analysis",
            "",
            "_Builder emits only data + structure-agnostic key readings — "
            "**it does NOT pre-pick windows or render verdicts**. Choosing "
            "the comparison framework is the LLM's job, based on tag + user "
            "notes + cues like the pace CV (coefficient of variation) below. "
            "When you need a window the builder did not pre-slice (e.g. "
            "first-half vs second-half within a push lap, or a segment "
            "redefined by the user's notes), call the "
            "`get_window_stats(start, end, key_type)` tool._",
        ]

        # ── (1) Per-activity overview ────────────────────────────────────
        whole = seg_stats(rows)
        if whole:
            out += self._overview_section(whole, lap_windows)

        # ── (2) Per-lap slice + per-lap internal readings ─────────────────
        if lap_windows:
            out += self._per_lap_section(rows, lap_windows)
            out += self._per_lap_internal_section(rows, lap_windows)

        # ── (3) Per-km slice ──────────────────────────────────────────────
        km_buckets = slice_by_km(rows)
        if km_buckets:
            out += self._per_km_section(km_buckets)

        # ── (4) Structure-agnostic drift readings ────────────────────────
        out += self._drift_section(rows, lap_windows, km_buckets)

        # ── (5) Tool usage hint ──────────────────────────────────────────
        out += self._tool_hint_section()

        return "\n".join(out)

    # ── Sections ─────────────────────────────────────────────────────────

    @staticmethod
    def _overview_section(whole: dict, lap_windows: list[dict]) -> list[str]:
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

        # Lap-level pace CV — descriptive structure hint, not a verdict
        if lap_windows and len(lap_windows) >= 2:
            lap_paces = []
            for w in lap_windows:
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
                out.append(f"  _Interpretation hint (LLM judges)_: low CV + "
                           f"small spread → mostly steady-state; "
                           f"high CV or spread >20s/km → structured "
                           f"(warmup / push / cooldown / progression).")
        return out

    @staticmethod
    def _per_lap_section(rows: list, lap_windows: list[dict]) -> list[str]:
        # Detect manual vs auto for label only — heuristic, not a verdict
        distances = [w['dist_m'] for w in lap_windows if w['dist_m']]
        is_manual = is_manual_lap_structure(distances)
        kind_label = ("**likely manual laps**" if is_manual
                      else "likely Garmin auto laps (km/mile)")

        out = ["", f"### Per-lap breakdown ({len(lap_windows)} laps, {kind_label})"]
        out.append("| Lap | sec | min | distance | pace | HR | cadence | GCT | vertical ratio | stride |")
        out.append("|---|---|---|---|---|---|---|---|---|---|")

        for w in lap_windows:
            seg_rows = [r for r in rows if w['start_sec'] <= r[0] < w['end_sec']]
            s = seg_stats(seg_rows) if seg_rows else None
            if not s:
                continue
            cells = [
                f"{w['lap_id']}",
                f"{int(w['start_sec'])}-{int(w['end_sec'])}",
                f"{int(w['start_sec'])//60}-{int(w['end_sec'])//60}",
                f"{w['dist_m']/1000:.2f}km",
                fmt_pace_compact(s['pace_s_per_km']),
                f"{s['hr_avg']:.0f}",
                f"{s['cadence_avg']:.0f}"   if s.get('cadence_avg')  is not None else "—",
                f"{s['gct_avg_ms']:.0f}"    if s.get('gct_avg_ms')   is not None else "—",
                f"{s['vr_avg_pct']:.1f}"    if s.get('vr_avg_pct')   is not None else "—",
                f"{s['stride_avg_m']:.2f}"  if s.get('stride_avg_m') is not None else "—",
            ]
            out.append("| " + " | ".join(cells) + " |")

        # Descriptive observation about last lap (not a verdict)
        if len(lap_windows) >= 2:
            last_lap_pace, prev_lap_pace = None, None
            for w in (lap_windows[-1], lap_windows[-2]):
                if w['dist_m'] and w['dur_s']:
                    p = 1000 * w['dur_s'] / w['dist_m']
                    if w == lap_windows[-1]:
                        last_lap_pace = p
                        last_dur = w['dur_s']
                    else:
                        prev_lap_pace = p
            if last_lap_pace and prev_lap_pace:
                pace_delta = last_lap_pace - prev_lap_pace
                out.append(f"- Final-lap descriptive observation: pace vs "
                           f"second-to-last lap "
                           f"{'+' if pace_delta >= 0 else ''}{pace_delta:.0f}s/km, "
                           f"duration {last_dur//60}:{last_dur%60:02d} "
                           f"_(factual statement; the LLM should combine "
                           f"this with the user's notes to determine whether "
                           f"this is a cooldown / final pickup / or a forced "
                           f"slowdown)_")
        return out

    @staticmethod
    def _per_lap_internal_section(rows: list, lap_windows: list[dict]) -> list[str]:
        """Per-lap internal readings — pace CV, HR drift slope/R², first-half
        vs second-half stats. Loop over ALL laps regardless of duration; the
        primitives gracefully return None for sub-readings that need more
        samples than a short lap can provide.

        Coverage logic (no data loss):
          - Manual laps: all laps emit (canonical use)
          - Auto-1km laps: all laps emit (data is mostly redundant with
            per-km section, kept for consistency — overlap accepted)
          - Single lap: emits 1 block ≈ whole-activity readings (overlaps
            with structure-agnostic section, kept for consistency)

        For auto-1km / single-lap activities, narrative structure should
        come from per-km zone identification (LLM job, see prompt) — this
        section is supplementary data, not the primary lens.
        """
        out = ["", "### Per-lap internal readings (within-lap stability + drift)"]
        out.append("_Loops all laps; computes within-lap pace CV, HR drift "
                   "slope, and first-half vs second-half stats. Some "
                   "sub-readings may be None for short laps (insufficient "
                   "data) — that's normal._")
        out.append("")
        for w in lap_windows:
            seg_rows = [r for r in rows if w['start_sec'] <= r[0] < w['end_sec']]
            ist = internal_stats(seg_rows)

            dur_s = ist['duration_s']
            dur_str = f"{dur_s//60}:{dur_s%60:02d}"
            out.append(f"**Lap {w['lap_id']}** ({dur_str}, "
                       f"{w['dist_m']/1000:.2f}km, sec {int(w['start_sec'])}-{int(w['end_sec'])}):")

            # Pace CV
            if ist['pace_cv'] is not None:
                out.append(f"- Internal pace CV (30s buckets): {ist['pace_cv']*100:.1f}%")
            else:
                out.append("- Internal pace CV: — (lap too short, < 60s)")

            # HR drift slope + R²
            if ist['hr_drift'] is not None:
                hd = ist['hr_drift']
                out.append(f"- Internal HR drift: {hd['slope_per_min']:+.2f} bpm/min, "
                           f"R²={hd['r_squared']:.2f}")
            else:
                out.append("- Internal HR drift: — (insufficient samples)")

            # First half vs second half
            fh, sh = ist['first_half'], ist['second_half']
            if fh and sh:
                # Force-decay deltas
                d = pairwise_delta(fh, sh)
                hr_d  = f"{d['hr_delta']:+.0f}"        if d.get('hr_delta')      is not None else "—"
                pc_d  = f"{d['pace_delta_s']:+.0f}s"   if d.get('pace_delta_s')  is not None else "—"
                cad_d = f"{d['cadence_delta']:+.0f}"   if d.get('cadence_delta') is not None else "—"
                gct_d = f"{d['gct_delta']:+.0f}ms"     if d.get('gct_delta')     is not None else "—"
                vr_d  = f"{d['vr_delta']:+.2f}pt"      if d.get('vr_delta')      is not None else "—"
                str_d = f"{d['stride_delta']*100:+.0f}cm" if d.get('stride_delta') is not None else "—"
                out.append(f"- First-half vs second-half: HR {fh['hr_avg']:.0f}→"
                           f"{sh['hr_avg']:.0f} ({hr_d}) | "
                           f"pace {fmt_pace_compact(fh['pace_s_per_km'])}→"
                           f"{fmt_pace_compact(sh['pace_s_per_km'])} ({pc_d})")
                if any(d.get(k) is not None for k in ('cadence_delta', 'gct_delta', 'vr_delta', 'stride_delta')):
                    out.append(f"  Mechanics: cadence {cad_d} | GCT {gct_d} "
                               f"| vertical ratio {vr_d} | stride {str_d}")
            else:
                out.append("- First-half vs second-half: — (lap too short to split)")
            out.append("")
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
    def _drift_section(rows: list, lap_windows: list[dict],
                       km_buckets: list[dict]) -> list[str]:
        """Structure-agnostic drift — these are universal regardless of run shape."""
        out = ["", "### Structure-agnostic key readings (precomputed, fixed definitions)"]

        # Whole-run HR vs time linear regression
        hrd = hr_drift(rows)
        if hrd:
            slope = hrd['slope_per_min']
            r2    = hrd['r_squared']
            out.append(f"- **Full-activity HR drift** (linear regression on time): "
                       f"{slope:+.2f} bpm/min, R²={r2:.2f} "
                       f"_(high R² = stable linear drift; low R² = HR is "
                       f"dominated by lap structure, not pure cardiac drift)_")

        # Pa:HR — first-half vs second-half EF decoupling
        pa = pa_hr_split(rows)
        if pa:
            out.append(f"- **Pa:HR decoupling** (heart-rate-to-pace ratio, "
                       f"first-half EF vs second-half EF): "
                       f"{pa['decoupling_pct']:+.1f}% "
                       f"(first half HR {pa['first_half_hr']:.0f} @ {fmt_pace(pa['first_half_pace'])} → "
                       f"second half HR {pa['second_half_hr']:.0f} @ {fmt_pace(pa['second_half_pace'])})")
            out.append("  _Reference thresholds (coach consensus)_: <5% good / "
                       "5-8% borderline / >8% the aerobic engine is overloaded.")

        # First km vs last km delta
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

        # First lap vs last lap delta
        if lap_windows and len(lap_windows) >= 2:
            first_seg = [r for r in rows if lap_windows[0]['start_sec'] <= r[0] < lap_windows[0]['end_sec']]
            last_seg  = [r for r in rows if lap_windows[-1]['start_sec'] <= r[0] < lap_windows[-1]['end_sec']]
            sf = seg_stats(first_seg)
            sl = seg_stats(last_seg)
            if sf and sl:
                d = pairwise_delta(sf, sl)
                parts = []
                if d.get('hr_delta') is not None:
                    parts.append(f"HR {d['hr_delta']:+.0f}bpm ({d['hr_delta_pct']:+.1f}%)")
                if d.get('pace_delta_s') is not None:
                    parts.append(f"pace {d['pace_delta_s']:+.0f}s/km ({d['pace_delta_pct']:+.1f}%)")
                if parts:
                    out.append("- **First lap vs last lap**: " + " | ".join(parts) +
                               " _(caveat: laps may live in different pace "
                               "zones; do not treat this directly as cardiac drift)_")
        return out

    @staticmethod
    def _tool_hint_section() -> list[str]:
        return [
            "",
            "### Tool availability",
            "- For aggregates over an arbitrary custom window (e.g. \"Lap 3 "
            "first-half vs second-half\", \"5 min after the final push\", or "
            "a segment redefined by the user's notes), call "
            "`get_window_stats(start, end, key_type, channels?)`.",
            "- For raw 1Hz rows over an arbitrary window (e.g. to check "
            "whether the final 30s was a sprint), call "
            "`get_raw_window_by_time` / `get_raw_window_by_distance` "
            "(existing tools).",
            "- By default, prefer the per-lap / per-km slices the builder "
            "provides; only call a tool when the slice granularity is insufficient.",
        ]
