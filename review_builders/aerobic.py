"""AerobicBuilder — for `aerobic_recovery` + `aerobic_base` tags.

Both tags are aerobic-running activities (low-Z2 recovery to high-Z2 / low-Z3
base accumulation). The data analysis is identical for both; the recovery vs
base distinction is surfaced by the prompt, not the math. (NB: don't mistake
this for "Z2-only" — many trained-base runs technically straddle Z2/Z3 by
Garmin's auto-zones; the framework here is general aerobic-running.)

Layout follows the LongRunBuilder template (per-activity / per-lap / per-lap
internal / per-km / structure-agnostic drift) so cross-builder data shape is
uniform. ON TOP of that, AerobicBuilder keeps the aerobic-specific
**HR ceiling observation** section — for aerobic running, "did the run stay
in Z2?" is the primary discipline question, more central than cardiac drift.

Coaching philosophy (from plan):
- Most common error in aerobic running is going too fast → builder
  must surface HR ceiling breaches clearly (HR ceiling = Garmin's
  Zone 2/3 boundary, looked up from activity_hr_zones at runtime).
- Aerobic decoupling is THE base-fitness indicator. < 5% drift = base solid.
  Pace flat + HR > 10% rise = aerobic insufficient OR dehydration / under-recovery.
- Vertical ratio reveals form efficiency. People get sloppy on easy runs;
  bouncy form increases joint load. Cross-reference with tempo vertical
  ratio ideally.
- Cadence is the pre-failure signal: when fatigued runners let cadence
  drop and lengthen stride to hold pace, that's the early warning.

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
    pairwise_delta, internal_stats, fmt_pace, fmt_pace_compact,
)


class AerobicBuilder(ReviewBuilder):
    name = "AerobicBuilder"

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag in ("aerobic_recovery", "aerobic_base")

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep     = self._aerobic_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Aerobic deep analysis ────────────────────────────────────────────

    def _aerobic_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
        # Pull rows in primitives' canonical column order + distance_cum_m
        # at idx 7 (needed by slice_by_km).
        rows = conn.execute("""
            SELECT sec_offset, hr, speed_mps, cadence_spm, gct_ms, vert_ratio,
                   stride_cm, distance_cum_m
              FROM activity_metrics
             WHERE activity_id = ?
             ORDER BY sec_offset
        """, (aid,)).fetchall()
        if len(rows) < 60:
            return ""

        # Detect native sample interval (1s for fresh fetch, ~10s for legacy backfill)
        if len(rows) >= 3:
            deltas = sorted(rows[i+1][0] - rows[i][0] for i in range(len(rows) - 1))
            median_delta = max(1, deltas[len(deltas) // 2])
        else:
            median_delta = 1

        # Z3 ceiling (= Z2/Z3 boundary) — aerobic's central indicator
        z3_row = conn.execute("""
            SELECT zone_low_boundary FROM activity_hr_zones
             WHERE activity_id = ? AND zone_number = 3
        """, (aid,)).fetchone()
        z3_ceiling = z3_row[0] if z3_row and z3_row[0] is not None else None

        lap_windows = lap_windows_from_db(conn, aid)

        out = [
            "## 🎯 Aerobic-specific analysis",
            "",
            "_Data + derived signals + coach-consensus reference thresholds. "
            "Verdict is yours (LLM) to synthesize from the activity tag, user "
            "notes, personal_note, and long-term memory — do not re-quote "
            "these numbers verbatim. The central indicator for aerobic running "
            "is **HR ceiling observation** (discipline); the other sections "
            "(Pa:HR / mechanics / drift) are supporting._",
        ]

        # ── (1) Per-activity overview ────────────────────────────────────
        whole = seg_stats(rows)
        if whole:
            out += self._overview_section(whole, lap_windows)

        # ── (2) Lap structure detection ─────────────────────────────────
        out += self._lap_structure_section(lap_windows)

        # ── (3) HR ceiling observation (aerobic-specific, priority indicator) ─
        if z3_ceiling is not None:
            out += self._hr_ceiling_section(rows, z3_ceiling, median_delta, lap_windows)

        # ── (4) Per-lap slice + per-lap internal readings ────────────────
        if lap_windows:
            out += self._per_lap_section(rows, lap_windows)
            out += self._per_lap_internal_section(rows, lap_windows)

        # ── (5) Per-km slice ─────────────────────────────────────────────
        km_buckets = slice_by_km(rows)
        if km_buckets:
            out += self._per_km_section(km_buckets)

        # ── (6) Structure-agnostic drift readings ────────────────────────
        out += self._drift_section(rows, lap_windows, km_buckets)

        # ── (7) Tool usage hint ──────────────────────────────────────────
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

        # Lap-level pace CV — structure hint (steady vs pickup)
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
                out.append(f"- Lap pace distribution: CV (coefficient of variation) "
                           f"{cv*100:.1f}% | "
                           f"fastest {fmt_pace(fastest)} → slowest {fmt_pace(slowest)} "
                           f"(spread {spread:.0f}s/km)")
                out.append(f"  _Interpretation hint (aerobic lens)_: low CV + "
                           f"small spread = steady-state; markedly faster final "
                           f"laps = likely end-of-run pickup.")
        return out

    @staticmethod
    def _lap_structure_section(lap_windows: list[dict]) -> list[str]:
        out = ["", "### Lap structure detection"]
        if not lap_windows:
            out.append("- No lap data for the whole activity "
                       "(auto-lap was off and the user didn't press lap)")
            out.append("- _Downstream lap-based analysis falls back to "
                       "per-km / first-half-vs-second-half_")
            return out

        distances = [w['dist_m'] for w in lap_windows if w['dist_m']]
        if len(distances) <= 1:
            out.append(f"- {len(lap_windows)} lap(s) total, no subjective user segmentation")
            out.append("- _Downstream lap-based analysis falls back to "
                       "per-km / first-half-vs-second-half_")
            return out

        is_manual = is_manual_lap_structure(distances)
        avg_lap_km = sum(distances[:-1]) / max(1, len(distances) - 1) / 1000
        if is_manual:
            out.append(f"- {len(distances)} laps, non-final laps average "
                       f"{avg_lap_km:.1f}km → **likely manual laps**")
            out.append("- _Reading priority_: manual laps usually correspond "
                       "to subjective markers (warmup / main set / cooldown / "
                       "pace-zone switches / a final pickup pressed as its own "
                       "lap, etc.). If the user notes describe these markers, "
                       "tie the lap data to the story in the notes.")
        else:
            out.append(f"- {len(distances)} laps, distances near integer km "
                       f"→ likely Garmin auto-1km laps")
            out.append("- _Downstream lap-based analysis is equivalent to "
                       "per-km (each lap ≈ 1km)_")
        return out

    @staticmethod
    def _hr_ceiling_section(rows: list, z3_ceiling: float, median_delta: int,
                             lap_windows: list[dict]) -> list[str]:
        """Aerobic-specific: 30s rolling HR vs Z2 ceiling.
        THE central discipline indicator for aerobic. 30s rolling spans
        across lap boundaries on purpose — HR ceiling is a continuous
        physiological metric, not a structural one."""
        window_n = max(1, 30 // median_delta)
        hr_rows = [(r[0], r[1]) for r in rows if r[1] is not None]
        if len(hr_rows) < window_n:
            return []

        breach_flags = []
        for i in range(len(hr_rows) - window_n + 1):
            avg = sum(hr_rows[i+j][1] for j in range(window_n)) / window_n
            breach_flags.append((hr_rows[i][0], avg > z3_ceiling))
        if not breach_flags:
            return []

        total = len(breach_flags)
        breaches = sum(1 for _, b in breach_flags if b)
        pct_total = 100 * breaches / total

        # Longest continuous breach run (spans laps OK)
        longest_n = 0
        longest_start_sec = None
        cur_n, cur_start = 0, None
        for sec, is_b in breach_flags:
            if is_b:
                if cur_n == 0:
                    cur_start = sec
                cur_n += 1
                if cur_n > longest_n:
                    longest_n = cur_n
                    longest_start_sec = cur_start
            else:
                cur_n = 0
        longest_dur_s = longest_n * median_delta

        out = [
            "",
            f"### HR ceiling observation (30s rolling avg vs Z2 ceiling "
            f"{z3_ceiling} bpm, = the start of Z3 in this user's Garmin zones)"
        ]
        out.append(f"- Overall breach: {breaches} / {total} 30s windows ({pct_total:.0f}%)")

        # Per-lap or halves breakdown
        if lap_windows and len(lap_windows) >= 2 \
                and is_manual_lap_structure([w['dist_m'] for w in lap_windows if w['dist_m']]):
            out.append("- Per-lap breach:")
            for w in lap_windows:
                in_lap = [b for sec, b in breach_flags
                          if w['start_sec'] <= sec < w['end_sec']]
                if not in_lap:
                    continue
                lap_pct = 100 * sum(1 for b in in_lap if b) / len(in_lap)
                out.append(f"  - Lap {w['lap_id']} (sec {int(w['start_sec'])}-"
                           f"{int(w['end_sec'])}, "
                           f"{int(w['start_sec'])//60}-{int(w['end_sec'])//60}min): "
                           f"{lap_pct:.0f}%")
        else:
            mid = total // 2
            front_pct = 100 * sum(1 for _, b in breach_flags[:mid] if b) / max(1, mid)
            back_pct  = 100 * sum(1 for _, b in breach_flags[mid:] if b) / max(1, total - mid)
            out.append(f"- First-half breach: {front_pct:.0f}% | "
                       f"Second-half breach: {back_pct:.0f}%")

        if longest_n > 0:
            out.append(f"- Longest continuous breach: {longest_dur_s/60:.1f} min, "
                       f"starting at {longest_start_sec//60}min "
                       f"{longest_start_sec%60}s "
                       "(spans laps — this is a continuous physiological signal, "
                       "not a structural one)")
        out.append("- _Reference thresholds (coach consensus)_: <5% fully in Z2 / "
                   "5-20% mild overshoot / >20% heavy overshoot. For "
                   "**aerobic_recovery** apply a stricter standard (>5% is "
                   "already a problem); for **aerobic_base** an early-X-min "
                   "surge is more acceptable than persistent high-HR drift.")
        return out

    @staticmethod
    def _per_lap_section(rows: list, lap_windows: list[dict]) -> list[str]:
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
        return out

    @staticmethod
    def _per_lap_internal_section(rows: list, lap_windows: list[dict]) -> list[str]:
        """Per-lap internal: pace CV, HR drift slope/R², first-half vs
        second-half stats. Loops ALL laps (long_run convention)."""
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

            if ist['pace_cv'] is not None:
                out.append(f"- Internal pace CV (30s buckets): {ist['pace_cv']*100:.1f}%")
            else:
                out.append("- Internal pace CV: — (lap too short, < 60s)")

            if ist['hr_drift'] is not None:
                hd = ist['hr_drift']
                out.append(f"- Internal HR drift: {hd['slope_per_min']:+.2f} bpm/min, "
                           f"R²={hd['r_squared']:.2f}")
            else:
                out.append("- Internal HR drift: — (insufficient samples)")

            fh, sh = ist['first_half'], ist['second_half']
            if fh and sh:
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
                if any(d.get(k) is not None for k in
                       ('cadence_delta', 'gct_delta', 'vr_delta', 'stride_delta')):
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
        """Structure-agnostic drift — universal regardless of run shape."""
        out = ["", "### Structure-agnostic key readings (precomputed, fixed definitions)"]

        hrd = hr_drift(rows)
        if hrd:
            out.append(f"- **Full-activity HR drift** (linear regression on time): "
                       f"{hrd['slope_per_min']:+.2f} bpm/min, R²={hrd['r_squared']:.2f} "
                       f"_(high R² = stable linear drift; low R² = HR is "
                       f"dominated by lap structure / end-of-run pickup, not "
                       f"pure cardiac drift)_")
            out.append("- _Reference thresholds (coach consensus)_: "
                       "<+0.15 bpm/min = true steady-state aerobic; "
                       "+0.15-0.4 = mild cardiac drift; >+0.4 = significant drift.")

        pa = pa_hr_split(rows)
        if pa:
            out.append(f"- **Pa:HR decoupling** (heart-rate-to-pace ratio, "
                       f"first-half EF vs second-half EF): "
                       f"{pa['decoupling_pct']:+.1f}% "
                       f"(first half HR {pa['first_half_hr']:.0f} @ {fmt_pace(pa['first_half_pace'])} → "
                       f"second half HR {pa['second_half_hr']:.0f} @ {fmt_pace(pa['second_half_pace'])})")
            out.append("- _Reference thresholds (coach consensus)_: <5% good / "
                       "5-10% slight decoupling / >10% significant decoupling. "
                       "**Special pattern**: HR up >10% but pace barely changes "
                       "→ typically points to dehydration / under-recovery / "
                       "weak aerobic base.")

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
        if lap_windows and len(lap_windows) >= 2:
            first_seg = [r for r in rows
                         if lap_windows[0]['start_sec'] <= r[0] < lap_windows[0]['end_sec']]
            last_seg  = [r for r in rows
                         if lap_windows[-1]['start_sec'] <= r[0] < lap_windows[-1]['end_sec']]
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
            "- For aggregates over an arbitrary custom window (e.g. \"stats "
            "for the final 5-min pickup segment\", or a segment redefined by "
            "the user's notes), call "
            "`get_window_stats(start, end, key_type, channels?)`.",
            "- For raw 1Hz rows over an arbitrary window (e.g. to verify "
            "whether a 30s window actually breached the ceiling), call "
            "`get_raw_window_by_time` / `get_raw_window_by_distance` "
            "(existing tools).",
            "- By default, prefer the per-lap / per-km slices the builder "
            "provides; only call a tool when the slice granularity is insufficient.",
        ]
