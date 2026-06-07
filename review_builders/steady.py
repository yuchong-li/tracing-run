"""SteadyBuilder — for the `steady` tag (High-Z2 → mid/high-Z3 cruise).

Standalone builder (does NOT subclass AerobicBuilder) so steady's analysis can
diverge freely from aerobic's over time. It shares the common `primitives`
toolkit + the DefaultBuilder baseline (the standard metadata every report
shows), then adds steady-specific deep analysis.

Why steady gets its own builder instead of reusing AerobicBuilder:

- **Inverted HR-ceiling reading.** For an easy/aerobic run, time above the Z2
  ceiling is a discipline miss. For steady it is EXPECTED — the run is supposed
  to sit above it. So the ceiling block is framed as "did the run genuinely
  reach the cruise band?" (a LOW breach % is the warning = sagged into easy),
  not "did you stay in Z2?".
- **Lap-to-lap decoupling trend.** Steady's #1 sustainability signal is whether
  the HR cost rises lap-to-lap (efficiency factor dropping across laps), even
  when each lap is internally smooth — a number AerobicBuilder never
  precomputed. This builder emits per-lap EF + the lap-1-relative EF drift so
  the coach reads the trend directly instead of approximating it.

Steadiness is read on two independent axes the builder keeps separate:
  - within-lap   → per-lap internal pace CV  (`### Per-lap internal readings`)
  - between-lap  → per-lap pace drift + EF trend  (`### Lap-to-lap …`)

context_md is emitted in neutral English; the per-tag prompt steers the
coach's response language.
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


class SteadyBuilder(ReviewBuilder):
    name = "SteadyBuilder"

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag == "steady"

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep     = self._steady_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Steady deep analysis ─────────────────────────────────────────────

    def _steady_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
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

        # Detect native sample interval (1s fresh fetch, ~10s legacy backfill)
        if len(rows) >= 3:
            deltas = sorted(rows[i+1][0] - rows[i][0] for i in range(len(rows) - 1))
            median_delta = max(1, deltas[len(deltas) // 2])
        else:
            median_delta = 1

        # Z3 ceiling (= Z2/Z3 boundary). For steady this is the floor of the
        # cruise band — confirms the run reached the band, not a discipline cap.
        z3_row = conn.execute("""
            SELECT zone_low_boundary FROM activity_hr_zones
             WHERE activity_id = ? AND zone_number = 3
        """, (aid,)).fetchone()
        z3_ceiling = z3_row[0] if z3_row and z3_row[0] is not None else None

        lap_windows = lap_windows_from_db(conn, aid)

        out = [
            "## 🎯 Steady-specific analysis",
            "",
            "_Data + derived signals + coach-consensus reference thresholds. "
            "Verdict is yours (LLM) to synthesize from the activity tag, user "
            "notes, personal_note, and long-term memory — do not re-quote these "
            "numbers verbatim. For a STEADY / cruise run the central reads are: "
            "**sustainability** (Pa:HR decoupling + the lap-to-lap EF trend) and "
            "**steadiness** on two axes (within-lap pace CV + lap-to-lap pace "
            "drift). The HR-ceiling block CONFIRMS the run reached the cruise "
            "band — time above the Z2 ceiling is expected for steady, a LOW "
            "breach % is the warning, not a high one._",
        ]

        # ── (1) Per-activity overview ────────────────────────────────────
        whole = seg_stats(rows)
        if whole:
            out += self._overview_section(whole, lap_windows)

        # ── (2) Lap structure detection ─────────────────────────────────
        out += self._lap_structure_section(lap_windows)

        # ── (3) HR ceiling observation (steady framing) ──────────────────
        if z3_ceiling is not None:
            out += self._hr_ceiling_section(rows, z3_ceiling, median_delta, lap_windows)

        # ── (4) Per-lap slice + within-lap internal readings ─────────────
        if lap_windows:
            out += self._per_lap_section(rows, lap_windows)
            out += self._per_lap_internal_section(rows, lap_windows)
            # ── (5) Between-lap stability + decoupling trend (steady-specific)
            out += self._lap_to_lap_section(rows, lap_windows)

        # ── (6) Per-km slice ─────────────────────────────────────────────
        km_buckets = slice_by_km(rows)
        if km_buckets:
            out += self._per_km_section(km_buckets)

        # ── (7) Structure-agnostic drift readings ────────────────────────
        out += self._drift_section(rows, lap_windows, km_buckets)

        # ── (8) Tool usage hint ──────────────────────────────────────────
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

        # Lap-level pace CV — first cut at the between-lap steadiness axis
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
                out.append("  _Interpretation hint (steady lens)_: low CV + small "
                           "spread = a controlled cruise (between-lap stable); a "
                           "wide spread or laps stepping monotonically = either a "
                           "progression or drifting up — disambiguate in the "
                           "lap-to-lap section + the user's notes.")
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
            out.append("- _Reading priority (steady)_: manual laps are the user's "
                       "own segmentation. The press points are informative — in a "
                       "cruise, pressing lap mid-run usually means \"felt I could "
                       "lift a gear\" or \"this was too hot, backed off\". Whether "
                       "the body (HR / EF) supported that adjustment is direct "
                       "calibration data — tie it to the notes.")
        else:
            out.append(f"- {len(distances)} laps, distances near integer km "
                       f"→ likely Garmin auto-1km laps")
            out.append("- _Downstream lap-based analysis is equivalent to "
                       "per-km (each lap ≈ 1km)_")
        return out

    @staticmethod
    def _hr_ceiling_section(rows: list, z3_ceiling: float, median_delta: int,
                             lap_windows: list[dict]) -> list[str]:
        """30s rolling HR vs the Z2 ceiling. For steady this CONFIRMS the run
        reached the cruise band (above the ceiling is expected); a LOW breach
        % is the warning (sagged into easy). The 30s window spans lap
        boundaries on purpose — it's a continuous physiological signal."""
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
        out.append(f"- Time above the Z2 ceiling (in/above the cruise band): "
                   f"{breaches} / {total} 30s windows ({pct_total:.0f}%)")

        # Per-lap or halves breakdown
        if lap_windows and len(lap_windows) >= 2 \
                and is_manual_lap_structure([w['dist_m'] for w in lap_windows if w['dist_m']]):
            out.append("- Per-lap time above ceiling:")
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
            out.append(f"- First-half: {front_pct:.0f}% | "
                       f"Second-half: {back_pct:.0f}% above ceiling")

        if longest_n > 0:
            out.append(f"- Longest continuous stretch above ceiling: "
                       f"{longest_dur_s/60:.1f} min, "
                       f"starting at {longest_start_sec//60}min "
                       f"{longest_start_sec%60}s")

        out.append("- _Reading for STEADY (reference anchors — recalibrate to "
                   "your own data over time)_: time above the ceiling is EXPECTED "
                   "for a cruise. **>70%** = genuinely in the cruise band; "
                   "**40–70%** = partial (slow warmup or a sagging back half — "
                   "check the per-km HR progression for which); **<40%** = the "
                   "run sagged into easy and missed the steady stimulus. The "
                   "warning sign for steady is a LOW breach %, not a high one.")
        out.append("- _What this number CAN'T answer (the upper bound)_: did HR "
                   "stay inside the steady band (~mid–high Z3) or climb past it "
                   "into tempo/threshold? Judge that from the lap-to-lap EF trend "
                   "+ Pa:HR decoupling + HR-time drift, not from this %.")
        return out

    @staticmethod
    def _per_lap_section(rows: list, lap_windows: list[dict]) -> list[str]:
        distances = [w['dist_m'] for w in lap_windows if w['dist_m']]
        is_manual = is_manual_lap_structure(distances)
        kind_label = ("**likely manual laps**" if is_manual
                      else "likely auto-1km laps")
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
        """Within-lap steadiness axis: per-lap pace CV, HR drift slope/R²,
        first-half vs second-half stats. Loops ALL laps."""
        out = ["", "### Per-lap internal readings (within-lap stability + drift)"]
        out.append("_The WITHIN-lap steadiness axis: pace CV (is each lap "
                   "internally smooth or surge-and-ease?), within-lap HR drift, "
                   "first-half vs second-half. Read alongside the Lap-to-lap "
                   "section, which is the BETWEEN-lap axis. Some sub-readings may "
                   "be None for short laps — that's normal._")
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
    def _lap_to_lap_section(rows: list, lap_windows: list[dict]) -> list[str]:
        """Between-lap steadiness + decoupling trend — steady's sustainability
        signal. Per-lap EF (efficiency factor = speed/HR) and its drift vs
        lap 1: EF dropping lap-to-lap = HR cost rising = decoupling across the
        run, even when each lap is internally smooth. This is the lap-to-lap
        trend the whole-run Pa:HR (single first/second-half number) can't show."""
        if not lap_windows or len(lap_windows) < 2:
            return []
        per_lap = []
        for w in lap_windows:
            seg = [r for r in rows if w['start_sec'] <= r[0] < w['end_sec']]
            s = seg_stats(seg)
            if s:
                per_lap.append((w, s))
        if len(per_lap) < 2:
            return []

        out = ["", "### Lap-to-lap stability & decoupling trend "
                   "(BETWEEN-lap axis — steady's sustainability signal)"]
        out.append("| Lap | pace | HR | EF (m/s per bpm) | EF drift vs lap1 |")
        out.append("|---|---|---|---|---|")
        ef1 = per_lap[0][1]['ef']
        paces = []
        for w, s in per_lap:
            ef = s['ef']
            drift = (ef1 - ef) / ef1 * 100 if ef1 else 0.0
            paces.append(s['pace_s_per_km'])
            out.append("| " + " | ".join([
                f"{w['lap_id']}",
                fmt_pace_compact(s['pace_s_per_km']),
                f"{s['hr_avg']:.0f}",
                f"{ef:.4f}",
                f"{drift:+.1f}%",
            ]) + " |")

        # Pace step direction (lap1 → last)
        pace_delta = paces[-1] - paces[0]   # s/km; negative = faster
        if pace_delta <= -5:
            direction = (f"stepping FASTER ({fmt_pace_compact(paces[0])} → "
                         f"{fmt_pace_compact(paces[-1])}, {pace_delta:+.0f}s/km)")
        elif pace_delta >= 5:
            direction = (f"stepping SLOWER ({fmt_pace_compact(paces[0])} → "
                         f"{fmt_pace_compact(paces[-1])}, +{pace_delta:.0f}s/km)")
        else:
            direction = (f"flat ({fmt_pace_compact(paces[0])} → "
                         f"{fmt_pace_compact(paces[-1])}, {pace_delta:+.0f}s/km)")
        out.append(f"- Pace step direction (lap1 → last): {direction}")

        ef_last_drift = (ef1 - per_lap[-1][1]['ef']) / ef1 * 100 if ef1 else 0.0
        out.append(f"- EF drift lap1 → last: {ef_last_drift:+.1f}% "
                   "(positive = efficiency dropping = HR cost rising lap-to-lap "
                   "= decoupling across the run)")
        out.append("- _Reading for STEADY (rough anchors — recalibrate over "
                   "time)_: pace flat + EF drift small (~<3%) = held a real "
                   "steady state; pace stepping faster + EF dropping = drifting "
                   "toward tempo (or a planned progression — check the notes); "
                   "EF dropping while pace holds or sags = cost rising at/over "
                   "today's ceiling.")
        out.append("- _Caveat_: EF compared across laps in different pace zones "
                   "isn't pure cardiac drift — always read the EF column "
                   "alongside the pace column (a planned progression naturally "
                   "shifts EF).")
        return out

    @staticmethod
    def _per_km_section(km_buckets: list[dict]) -> list[str]:
        out = ["", f"### Per-km breakdown ({len(km_buckets)} km)"]
        out.append("_For single-lap runs this IS the between-lap axis: read the "
                   "per-km pace column for monotonic drift (stepping up/down) vs "
                   "flat._")
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
                       f"_(high R² = stable linear drift; low R² = HR is dominated "
                       f"by pace changes / lap structure, not pure cardiac drift)_")
            out.append("- _Reference thresholds (coach consensus)_: "
                       "<+0.15 bpm/min = true steady-state; "
                       "+0.15-0.4 = mild cardiac drift; >+0.4 = significant drift.")

        pa = pa_hr_split(rows)
        if pa:
            out.append(f"- **Pa:HR decoupling** (heart-rate-to-pace ratio, "
                       f"first-half EF vs second-half EF): "
                       f"{pa['decoupling_pct']:+.1f}% "
                       f"(first half HR {pa['first_half_hr']:.0f} @ {fmt_pace(pa['first_half_pace'])} → "
                       f"second half HR {pa['second_half_hr']:.0f} @ {fmt_pace(pa['second_half_pace'])})")
            out.append("- _Reference thresholds for STEADY (coach consensus)_: "
                       "<5% = genuinely sustainable cruise / 5-8% = borderline, "
                       "at the edge of sustainable for this duration / >8% = "
                       "functionally a threshold, the body couldn't hold the "
                       "cruise at constant cost. **Special pattern**: HR up >10% "
                       "but pace barely changes → dehydration / under-recovery. "
                       "_(This is the whole-run halves split; the Lap-to-lap "
                       "section shows the finer lap-by-lap trend.)_")

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
                               " _(caveat: laps may live in different pace zones; "
                               "read with the Lap-to-lap EF column, not as pure "
                               "cardiac drift)_")
        return out

    @staticmethod
    def _tool_hint_section() -> list[str]:
        return [
            "",
            "### Tool availability",
            "- For aggregates over an arbitrary custom window (e.g. a segment "
            "redefined by the user's notes, or one lap on its own), call "
            "`get_window_stats(start, end, key_type, channels?)`. It returns "
            "HR/pace stats, mechanics, an `ef` (efficiency factor) for the "
            "window, and the within-window HR-drift slope — note it does NOT "
            "return a decoupling % or a pace CV directly (use the `ef` across "
            "two windows, or the precomputed per-lap sections above).",
            "- For raw 1Hz rows over an arbitrary window, call "
            "`get_raw_window_by_time` / `get_raw_window_by_distance`.",
            "- By default, prefer the per-lap / per-km / lap-to-lap slices the "
            "builder already provides; only call a tool when the slice "
            "granularity is insufficient.",
        ]
