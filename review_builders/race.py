"""RaceBuilder — for the `race` tag.

Race analysis is **distance-dependent**. 5K and full marathon are completely
different physiological events — same metric thresholds don't apply.

Output language: context_md is emitted in **neutral English**. The LLM's
response language is steered by the per-tag prompt (P5), not the builder.

Distance dispatch (4 standard sub-profiles + 2 atypical buckets):

| Distance        | Sub-profile     | Standard ref | Notes                           |
|-----------------|-----------------|--------------|---------------------------------|
| 1500-7500m      | 5K              | 5000m        | VO2max+, no steady state        |
| 7500-15000m     | 10K             | 10000m       | Threshold/slightly above        |
| 15000-30000m    | Half marathon   | 21097.5m     | Highest sustainable avg output  |
| 30000-50000m    | Full marathon   | 42195m       | Aerobic + mechanical decay      |
| <1500m          | atypical-short  | —            | Mile/track race; skip drift     |
| >50000m         | atypical-long   | —            | Ultra; suggest TrailBuilder     |

Pa:HR thresholds vary by sub-profile (see _render_pa_hr_buckets). Most
notably, 5K does NOT use Pa:HR as primary verdict — VO2max-zone races have
no steady state, so 4-5% drift is normal even with good pacing.

Universal sections (run for all profiles):
- Per-km splits table
- Pacing strategy classification (positive/even/negative/blow-up)
- Final stretch (last 1km cadence vs pace coupling)
- Power consistency (silently skipped if no power_w data)

Sub-profile-specific:
- 5K/10K: start discipline (first km vs target), mid-race sawtooth, finish kick
- Half:   HR step-up at km 15-17, cruise pace stability
- Full:   5km Pa:HR table with wall threshold, mechanical collapse at km 35+
- Atypical: just universal + framing note

Lap-aware: race activities use Garmin Auto Lap (per-km) so km-based
analysis dominates. Manual laps within a race are surfaced but don't
override km segmentation; LLM cross-refs with user comment.
"""

import sqlite3

from review_builders.base    import BuildResult, ReviewBuilder, lap_windows_from_db
from review_builders.default import DefaultBuilder


class RaceBuilder(ReviewBuilder):
    name = "RaceBuilder"

    # Distance bucket boundaries (meters)
    BUCKET_5K_MIN, BUCKET_5K_MAX     = 1500,  7500
    BUCKET_10K_MAX                    = 15000
    BUCKET_HALF_MAX                   = 30000
    BUCKET_FULL_MAX                   = 50000

    # Standard reference distances (m)
    STD_5K   = 5000
    STD_10K  = 10000
    STD_HALF = 21097.5
    STD_FULL = 42195.0

    def applies_to(self, tag: str, activity_type_key: str) -> bool:
        return tag == "race"

    def build(self, activity_id: int, conn: sqlite3.Connection) -> BuildResult:
        baseline = DefaultBuilder().build(activity_id, conn).context_md
        deep = self._race_analysis(activity_id, conn)
        return BuildResult(
            context_md       = baseline + (("\n\n" + deep) if deep else ""),
            highlight_windows= [],
            builder_hash     = self.builder_hash(),
        )

    # ── Top-level dispatch ──────────────────────────────────────────────────

    def _race_analysis(self, aid: int, conn: sqlite3.Connection) -> str:
        act = conn.execute(
            "SELECT distance_m FROM activities WHERE activity_id = ?",
            (aid,)
        ).fetchone()
        if not act or not act[0]:
            return ""
        total_dist = act[0]

        rows = conn.execute("""
            SELECT sec_offset, distance_cum_m, hr, speed_mps,
                   cadence_spm, gct_ms, vert_ratio, stride_cm, power_w
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

        sub_profile = self._pick_sub_profile(total_dist)

        out = [
            "## 🎯 Race-specific analysis",
            "",
            "_Data + derived signals + coach-consensus reference thresholds. "
            "Verdict is yours (LLM) to synthesize from the activity tag, the "
            "user's notes (**race intent — PB attempt / fitness check / "
            "training substitute — is the key framing**), personal_note, and "
            "long-term memory — do not re-quote these numbers verbatim._",
        ]

        # ── (0) Distance-bucket detection ───────────────────────────────
        out.extend(self._render_distance_bucket(total_dist, sub_profile))

        # ── (1) Per-km splits (universal) ───────────────────────────────
        out.extend(self._render_per_km_splits(rows))

        # ── (2) Pa:HR buckets — skip for atypical-short ─────────────────
        if sub_profile != "atypical-short":
            out.extend(self._render_pa_hr_buckets(rows, total_dist, sub_profile))

        # ── (3) Pacing strategy classification (universal) ──────────────
        out.extend(self._render_pacing_strategy(rows))

        # ── (4) Final stretch (universal) ───────────────────────────────
        out.extend(self._render_final_stretch(rows, total_dist))

        # ── (5) Power consistency — silently skip if no data ────────────
        out.extend(self._render_power_consistency(rows, median_delta))

        # ── (6) Km-transition micro-pacing — only ≥half ─────────────────
        if sub_profile in ("Half", "Full", "atypical-long"):
            out.extend(self._render_km_transition_micro_pacing(rows, conn, aid))

        # ── (7) Sub-profile-specific section ────────────────────────────
        if sub_profile in ("5K", "10K"):
            out.extend(self._render_5k_10k_specific(rows, total_dist, sub_profile))
        elif sub_profile == "Half":
            out.extend(self._render_half_specific(rows, total_dist))
        elif sub_profile == "Full":
            out.extend(self._render_full_specific(rows, total_dist, median_delta))
        elif sub_profile == "atypical-short":
            out.append("")
            out.append("### Sub-profile note")
            out.append("- Distance <1.5km — endurance signals like cardiac "
                       "drift are weak; analysis focus: start pace + final kick.")
        elif sub_profile == "atypical-long":
            out.append("")
            out.append("### Sub-profile note")
            out.append("- Distance >50km (ultra) — different failure modes "
                       "(thermoregulation / fueling dominate); this builder "
                       "is not specifically tuned for ultras.")
            out.append("- If it was a trail race, prefer TrailBuilder. If it "
                       "was a road ultra, apply the Full marathon wall "
                       "threshold and pay extra attention to mechanical collapse.")

        # ── (8) Tool availability ───────────────────────────────────────
        out.extend(self._tool_hint_section())

        return "\n".join(out)

    @staticmethod
    def _tool_hint_section() -> list[str]:
        return [
            "",
            "### Tool availability",
            "- For aggregates over an arbitrary custom window (e.g. \"HR / "
            "pace / mechanics for the final 2km\", \"the first-km starting "
            "burst\", \"pacing changes around the halfway mark ±1km\"), "
            "call `get_window_stats(start, end, key_type, channels?)`. "
            "**The return value includes a `grade` block (avg_grade_pct, "
            "elev_gain_m, elev_loss_m, gap_pace_s_per_km)** — for "
            "rolling-hill road races (bridges, ramps, on-ramps), a sudden "
            "slowdown on a particular km isn't necessarily a fade. First "
            "check whether `avg_grade_pct` is >+2% to rule out terrain, "
            "then look at `gap_pace_s_per_km` for the real effort.",
            "- For raw 1Hz rows (e.g. to inspect the final-30s finishing "
            "kick, or HR jumps near aid stations), call "
            "`get_raw_window_by_time` / `get_raw_window_by_distance` "
            "(existing tools).",
            "- By default, prefer the builder's per-km splits / Pa:HR "
            "buckets / final-stretch sections; only call a tool when the "
            "slice granularity is insufficient.",
        ]

    def _pick_sub_profile(self, total_dist: float) -> str:
        if total_dist < self.BUCKET_5K_MIN:
            return "atypical-short"
        elif total_dist < self.BUCKET_5K_MAX:
            return "5K"
        elif total_dist < self.BUCKET_10K_MAX:
            return "10K"
        elif total_dist < self.BUCKET_HALF_MAX:
            return "Half"
        elif total_dist < self.BUCKET_FULL_MAX:
            return "Full"
        else:
            return "atypical-long"

    def _render_distance_bucket(self, total_dist, sub_profile) -> list[str]:
        out = ["", "### Distance-bucket detection"]
        if sub_profile == "5K":
            pct = (total_dist - self.STD_5K) / self.STD_5K * 100
            out.append(f"- Actual distance {total_dist/1000:.2f}km → **5K profile** ({pct:+.0f}% vs 5km standard)")
        elif sub_profile == "10K":
            pct = (total_dist - self.STD_10K) / self.STD_10K * 100
            out.append(f"- Actual distance {total_dist/1000:.2f}km → **10K profile** ({pct:+.0f}% vs 10km standard)")
        elif sub_profile == "Half":
            pct = (total_dist - self.STD_HALF) / self.STD_HALF * 100
            out.append(f"- Actual distance {total_dist/1000:.2f}km → **Half marathon profile** ({pct:+.0f}% vs 21.1km standard)")
        elif sub_profile == "Full":
            pct = (total_dist - self.STD_FULL) / self.STD_FULL * 100
            out.append(f"- Actual distance {total_dist/1000:.2f}km → **Full marathon profile** ({pct:+.0f}% vs 42.2km standard)")
        elif sub_profile == "atypical-short":
            out.append(f"- Actual distance {total_dist/1000:.2f}km → **atypical short race** (<1.5km)")
        else:
            out.append(f"- Actual distance {total_dist/1000:.2f}km → **atypical long race** (>50km)")
        return out

    # ── Per-km splits ───────────────────────────────────────────────────────

    def _per_km_splits(self, rows) -> list[dict]:
        """Compute pace + HR per completed km using distance_cum_m. Skips
        partial last km. Returns list of {km, dur_s, avg_hr}."""
        # rows = (sec_offset, distance_cum_m, hr, speed_mps, ...)
        if not rows:
            return []
        # Iterate to find sec_offset at each km boundary
        boundaries = []  # (km, sec_offset)
        next_km_m = 1000
        for r in rows:
            sec, dist = r[0], r[1]
            if dist is None:
                continue
            while dist >= next_km_m:
                boundaries.append((next_km_m // 1000, sec))
                next_km_m += 1000
        if not boundaries:
            return []

        splits = []
        # First km split = sec at 1km - first sec
        prev_sec = rows[0][0]
        for km, sec in boundaries:
            dur = sec - prev_sec
            hrs = [r[2] for r in rows if prev_sec <= r[0] < sec and r[2] is not None]
            avg_hr = sum(hrs) / len(hrs) if hrs else 0
            splits.append({'km': km, 'dur_s': dur, 'avg_hr': avg_hr})
            prev_sec = sec
        return splits

    def _render_per_km_splits(self, rows) -> list[str]:
        splits = self._per_km_splits(rows)
        if not splits:
            return ["", "### Per-km splits",
                    "- _No distance_cum_m data — per-km splits cannot be computed._"]

        out = ["", f"### Per-km splits ({len(splits)} complete km)"]
        # Find fastest + slowest km
        fastest = min(splits, key=lambda s: s['dur_s'])
        slowest = max(splits, key=lambda s: s['dur_s'])
        for s in splits:
            pace = s['dur_s']
            marker = ""
            if s['km'] == fastest['km']: marker = "  ← fastest"
            elif s['km'] == slowest['km']: marker = "  ← slowest"
            out.append(f"- km {s['km']:>2d}: {pace//60:.0f}:{pace%60:02.0f}/km @ HR {s['avg_hr']:.0f}{marker}")
        return out

    # ── Pa:HR drift across race (5km buckets, or 2.5km halves for 5K) ──────

    def _pa_hr_buckets(self, rows, total_dist, sub_profile) -> list[dict]:
        """Returns list of {bucket_label, start_km, end_km, avg_hr, avg_pace}."""
        if not rows:
            return []
        # Bucket size: 2.5km for 5K profile; 5km for everything else
        bucket_size_m = 2500 if sub_profile == "5K" else 5000

        # Build buckets aligned at bucket_size boundaries
        buckets = []
        cur_start_m = 0
        cur_end_m = bucket_size_m
        # Find sec_offset at each bucket boundary
        cur_start_sec = rows[0][0]
        last_seen_sec = rows[0][0]
        cur_hrs = []
        for r in rows:
            sec, dist, hr = r[0], r[1], r[2]
            if dist is None:
                continue
            last_seen_sec = sec
            if hr is not None:
                cur_hrs.append(hr)
            while dist >= cur_end_m and cur_end_m <= total_dist:
                # Close this bucket
                bucket_dur = sec - cur_start_sec
                if bucket_dur > 0 and cur_hrs:
                    buckets.append({
                        'start_km': cur_start_m / 1000,
                        'end_km':   cur_end_m   / 1000,
                        'dur_s':    bucket_dur,
                        'avg_hr':   sum(cur_hrs) / len(cur_hrs),
                        'avg_pace': bucket_dur * 1000 / bucket_size_m,
                    })
                cur_start_m = cur_end_m
                cur_end_m += bucket_size_m
                cur_start_sec = sec
                cur_hrs = []
        return buckets

    def _render_pa_hr_buckets(self, rows, total_dist, sub_profile) -> list[str]:
        buckets = self._pa_hr_buckets(rows, total_dist, sub_profile)
        if len(buckets) < 2:
            return ["", "### Aerobic decoupling (Pa:HR)",
                    "- _Fewer than 2 complete buckets — drift analysis skipped._"]

        bucket_label = "2.5km" if sub_profile == "5K" else "5km"
        out = ["", f"### Aerobic decoupling (Pa:HR, heart-rate-to-pace ratio @ {bucket_label} buckets)"]
        for b in buckets:
            pace = b['avg_pace']
            out.append(f"- km {b['start_km']:.1f}-{b['end_km']:.1f}: "
                       f"HR {b['avg_hr']:.0f} @ {int(pace//60)}:{int(pace%60):02d}/km")

        # Headline: first vs last bucket drift
        first, last = buckets[0], buckets[-1]
        if first['avg_hr'] > 0 and first['avg_pace'] > 0:
            eff_first = (1000 / first['avg_pace']) / first['avg_hr']
            eff_last  = (1000 / last['avg_pace']) / last['avg_hr']
            decoupling = (eff_first - eff_last) / eff_first * 100
            hr_drift   = (last['avg_hr'] - first['avg_hr']) / first['avg_hr'] * 100
            pace_drift = (last['avg_pace'] - first['avg_pace']) / first['avg_pace'] * 100
            out.append("")
            out.append(f"- **Full-race drift** (km {first['start_km']:.1f}-{first['end_km']:.1f} → "
                       f"km {last['start_km']:.1f}-{last['end_km']:.1f}): "
                       f"HR {hr_drift:+.1f}% | pace {pace_drift:+.1f}% | decoupling {decoupling:+.1f}%")

        # Sub-profile-specific threshold reference
        out.append("")
        if sub_profile == "Full":
            out.append("- _Threshold (Full marathon, km 30+ vs km 5)_: "
                       "<5% elite / 5-8% normal / **>8% wall risk** "
                       "(the single most important early-warning signal "
                       "for hitting the wall).")
        elif sub_profile == "Half":
            out.append("- _Threshold (Half marathon)_: <5% holding well / "
                       "5-8% borderline (suggests endurance base, fueling, "
                       "or cooling issues) / >8% significant decoupling.")
        elif sub_profile == "10K":
            out.append("- _Threshold (10K, dual-tier)_:")
            out.append("  - **Aerobic-efficiency lens**: <3% excellent.")
            out.append("  - **Race-overall lens**: <5% solid pacing "
                       "(includes the final-1-2km kick, which inflates the number).")
            out.append("  - If the user's notes say \"PB attempt\", apply "
                       "the latter; if \"fitness check\", apply the former.")
        elif sub_profile == "5K":
            out.append("- _Note: 5K does NOT use Pa:HR as the primary "
                       "verdict_. VO2max-zone races have essentially no "
                       "steady state — 4-5% decoupling is acceptable. The "
                       "verdict should look at start discipline + final kick.")
        else:  # atypical-long
            out.append("- _Threshold (use Full marathon as reference)_: "
                       "<5% elite / 5-8% normal / >8% wall risk; ultras "
                       "carry additional thermoregulation factors.")

        return out

    # ── Pacing strategy classification ──────────────────────────────────────

    def _render_pacing_strategy(self, rows) -> list[str]:
        # Use first half vs second half pace from raw data
        if not rows or len(rows) < 10:
            return []
        spds = [(r[0], r[3]) for r in rows if r[3] and r[3] > 0.5]
        if len(spds) < 10:
            return []
        mid_idx = len(spds) // 2
        front = spds[:mid_idx]
        back  = spds[mid_idx:]
        avg_spd_front = sum(s for _, s in front) / len(front)
        avg_spd_back  = sum(s for _, s in back) / len(back)
        pace_front = 1000 / avg_spd_front
        pace_back  = 1000 / avg_spd_back
        delta_pct = (pace_back - pace_front) / pace_front * 100

        if delta_pct < -2:
            label = "Negative split (second half faster = deliberate acceleration)"
        elif delta_pct < 2:
            label = "Even split (front and back roughly identical = textbook race pacing)"
        elif delta_pct < 5:
            label = "Slight positive split (second half slightly slower, normal race fade)"
        elif delta_pct < 15:
            label = "Positive split (second half markedly slower; pacing chosen too aggressively OR fitness limit)"
        else:
            label = "Blow-up (second half collapses >15%, clear pacing breakdown OR hit the wall)"

        out = ["", "### Pacing strategy"]
        out.append(f"- First-half avg pace: {int(pace_front//60)}:{int(pace_front%60):02d}/km")
        out.append(f"- Second-half avg pace: {int(pace_back//60)}:{int(pace_back%60):02d}/km")
        out.append(f"- Second vs first half: {delta_pct:+.1f}% → **{label}**")
        return out

    # ── Final stretch (last 1km cadence vs pace coupling) ──────────────────

    def _render_final_stretch(self, rows, total_dist) -> list[str]:
        if not rows:
            return []
        # Find sec_offset at total_dist - 1000
        target_dist = max(0, total_dist - 1000)
        kick_start_sec = None
        for r in rows:
            if r[1] is not None and r[1] >= target_dist:
                kick_start_sec = r[0]
                break
        if kick_start_sec is None:
            return []
        kick_rows = [r for r in rows if r[0] >= kick_start_sec]
        prev_rows = [r for r in rows if r[0] < kick_start_sec]
        if len(kick_rows) < 5 or len(prev_rows) < 5:
            return []

        def _avg_pace_cad(seg):
            spds = [r[3] for r in seg if r[3] and r[3] > 0.5]
            cads = [r[4] for r in seg if r[4] and r[4] > 50]
            strds = [r[7] for r in seg if r[7] and r[7] > 30]
            if not spds:
                return None
            return {
                'pace':     1000 / (sum(spds)/len(spds)),
                'cad':      (sum(cads)/len(cads)) if cads else None,
                'stride_m': (sum(strds)/len(strds))/100 if strds else None,
            }

        kick = _avg_pace_cad(kick_rows)
        race = _avg_pace_cad(prev_rows)
        if not kick or not race:
            return []

        out = ["", "### Final stretch (last-1km kick)"]
        out.append(f"- Last 1km: {int(kick['pace']//60)}:{int(kick['pace']%60):02d}/km vs "
                   f"prior-segment avg {int(race['pace']//60)}:{int(race['pace']%60):02d}/km "
                   f"({(kick['pace'] - race['pace']):+.0f}s/km)")
        if kick['cad'] is not None and race['cad'] is not None:
            out.append(f"- Last-1km cadence: {kick['cad']:.0f} spm vs prior segment {race['cad']:.0f} "
                       f"({kick['cad']-race['cad']:+.0f})")
        if kick['stride_m'] is not None and race['stride_m'] is not None:
            out.append(f"- Last-1km stride: {kick['stride_m']:.2f}m vs prior segment {race['stride_m']:.2f}m "
                       f"({(kick['stride_m']-race['stride_m'])*100:+.0f} cm)")
        out.append("- _Interpretation_: a kick should come from **rising "
                   "cadence** (neuromuscular activation), not from raw "
                   "**stride lengthening** (forcing it with a longer stride "
                   "= injury risk). Ideal: pace up + cadence up ≥3spm + "
                   "stride up <5cm; stride up without cadence up = a "
                   "muscle-it-out finish.")
        return out

    # ── Power consistency (skip if no data) ─────────────────────────────────

    def _render_power_consistency(self, rows, median_delta) -> list[str]:
        powers = [r[8] for r in rows if r[8] and r[8] > 0]
        if len(powers) < 30:
            return []  # silent skip — no power data
        avg_p = sum(powers) / len(powers)
        if avg_p <= 0:
            return []
        var = sum((p - avg_p) ** 2 for p in powers) / len(powers)
        cv = (var ** 0.5) / avg_p * 100
        out = ["", "### Power consistency"]
        out.append(f"- Full-race avg power: {avg_p:.0f}W | CV: {cv:.1f}%")
        out.append("- _Threshold_: CV <5% = iso-power cruise / "
                   "5-10% = moderate fluctuation / "
                   ">10% = surge-and-decel, wastes energy.")
        return out

    # ── Km-transition micro-pacing (≥half only) ─────────────────────────────

    def _render_km_transition_micro_pacing(self, rows, conn, aid) -> list[str]:
        """For each Garmin auto-1km lap boundary, look at first 10s of next km
        and check if pace 'jab + decel' (immature pacing-feel)."""
        lap_windows = lap_windows_from_db(conn, aid)
        if len(lap_windows) < 3:
            return []

        # Skip first lap (no prior reference) and check transitions
        out = ["", "### Km-transition micro-pacing (first 10s after each km lap-press)"]
        jab_count = 0
        smooth_count = 0
        for w in lap_windows[1:-1]:  # skip first and last (often partial)
            transition_sec = w['start_sec']
            window = [r for r in rows
                      if transition_sec <= r[0] < transition_sec + 10
                      and r[3] and r[3] > 0.5]
            if len(window) < 5:
                continue
            speeds = [r[3] for r in window]
            avg_spd = sum(speeds) / len(speeds)
            peak_spd = max(speeds)
            jab_pct = (peak_spd - avg_spd) / avg_spd * 100 if avg_spd > 0 else 0
            if jab_pct > 8:
                jab_count += 1
            else:
                smooth_count += 1

        total = jab_count + smooth_count
        if total == 0:
            return []
        jab_share = 100 * jab_count / total
        out.append(f"- {jab_count} of {total} km transitions show 'jab + decel' "
                   f"(peak > avg +8%), share {jab_share:.0f}%")
        if jab_share > 30:
            out.append("- _Interpretation_: unconscious surge after multiple "
                       "km lap-presses — pacing feel is not yet mature. At "
                       "marathon distance this pattern repeats 42 times, "
                       "with significant cumulative glycogen waste.")
        else:
            out.append("- _Interpretation_: pacing stays smooth after "
                       "lap-press — mature pacing feel.")
        return out

    # ── 5K / 10K specific ──────────────────────────────────────────────────

    def _render_5k_10k_specific(self, rows, total_dist, sub_profile) -> list[str]:
        out = ["", f"### {sub_profile} sub-profile analysis"]

        # Start discipline: first km vs avg pace
        splits = self._per_km_splits(rows)
        if not splits:
            return out
        first_km = splits[0]
        all_paces = [s['dur_s'] for s in splits]
        avg_pace = sum(all_paces) / len(all_paces)
        first_pct = (first_km['dur_s'] - avg_pace) / avg_pace * 100
        out.append(f"- Start discipline: km 1 {int(first_km['dur_s']//60)}:{int(first_km['dur_s']%60):02d}/km "
                   f"vs full-race avg {int(avg_pace//60)}:{int(avg_pace%60):02d}/km ({first_pct:+.1f}%)")
        if first_pct < -5:
            out.append("- _Interpretation_: km 1 faster than the full-race "
                       "average by >5% = **suicide start** (adrenaline-driven "
                       "opening); usually the root cause of second-half fade.")
        elif first_pct > 3:
            out.append("- _Interpretation_: km 1 slower than the full-race "
                       "average by >3% = conservative start — possibly "
                       "under-warmed-up, or a deliberate negative-split strategy.")
        else:
            out.append("- _Interpretation_: starting pace is well-judged; disciplined pacing.")

        # Mid-race smoothness — speed CV across all but first/last km
        if len(splits) >= 4:
            mid_splits = splits[1:-1]
            durs = [s['dur_s'] for s in mid_splits]
            avg = sum(durs) / len(durs)
            var = sum((d - avg)**2 for d in durs) / len(durs)
            cv = (var ** 0.5) / avg * 100 if avg > 0 else 0
            out.append(f"- Mid-race km-to-km CV: {cv:.1f}%")
            out.append("- _Threshold_: <2% perfectly consistent / 2-4% "
                       "moderate fluctuation / >4% sawtooth (surge→decel "
                       "wastes glycogen).")
        return out

    # ── Half marathon specific ──────────────────────────────────────────────

    def _render_half_specific(self, rows, total_dist) -> list[str]:
        out = ["", "### Half marathon sub-profile analysis"]

        # HR step-up detector at km 15-17
        # Compute 1-min rolling HR avg, find largest jump in km 14-18 window
        if not rows or len(rows) < 60:
            return out

        # Find sec_offset at each km marker via distance_cum_m (r[1])
        km_secs = {}
        for k in (10, 14, 15, 16, 17, 18, 20):
            target = k * 1000
            for r in rows:
                if r[1] is not None and r[1] >= target:
                    km_secs[k] = r[0]
                    break

        # For each km in [14, 15, 16, 17, 18]: avg HR (r[2]) in [km_sec, km_sec+60s]
        if 14 in km_secs and 18 in km_secs:
            jumps = []
            for k in (14, 15, 16, 17, 18):
                if k not in km_secs: continue
                start = km_secs[k]
                seg = [r[2] for r in rows if start <= r[0] < start + 60 and r[2] is not None]
                if seg:
                    jumps.append((k, sum(seg)/len(seg)))
            if len(jumps) >= 3:
                # Find biggest km-to-km jump
                max_jump_km = None
                max_jump_val = 0
                for i in range(1, len(jumps)):
                    delta = jumps[i][1] - jumps[i-1][1]
                    if delta > max_jump_val:
                        max_jump_val = delta
                        max_jump_km = jumps[i][0]
                out.append("- HR change across km 14-18 (avg of the 1 min "
                           "after each km marker): "
                           + " → ".join(f"km{k}: {h:.0f}bpm" for k, h in jumps))
                if max_jump_val >= 5:
                    out.append(f"- **HR step-up @ km {max_jump_km}** (+{max_jump_val:.0f} bpm) "
                               "= glycogen-reserve OR heat-stress threshold "
                               "hit; the earlier pace may have been chosen "
                               "too aggressively.")
                else:
                    out.append("- No significant HR step-up across km 14-18 "
                               "(linear drift is normal).")

        # Cruise pace stability — pace std-dev across mid-race kms
        splits = self._per_km_splits(rows)
        if len(splits) >= 8:
            # Skip first 2 (warmup-bias) and last 1 (kick) kms
            mid = splits[2:-1]
            durs = [s['dur_s'] for s in mid]
            avg = sum(durs) / len(durs)
            var = sum((d - avg)**2 for d in durs) / len(durs)
            cv = (var ** 0.5) / avg * 100 if avg > 0 else 0
            out.append(f"- Mid-race (km 3 to last-1) pace CV: {cv:.1f}%")
            out.append("- _Threshold_: <2% iso-pace cruise (ideal half "
                       "marathon shape) / 2-4% moderate / >4% loose pace "
                       "management.")
        return out

    # ── Full marathon specific ──────────────────────────────────────────────

    def _render_full_specific(self, rows, total_dist, median_delta) -> list[str]:
        out = ["", "### Full marathon sub-profile analysis"]

        # Mechanical collapse at km 35+: vertical ratio + GCT + cadence in last 7km vs first 7km
        if total_dist >= 36000:
            seg_last_7k_start_sec = None
            seg_first_7k_end_sec = None
            for r in rows:
                if r[1] is None: continue
                if r[1] >= total_dist - 7000 and seg_last_7k_start_sec is None:
                    seg_last_7k_start_sec = r[0]
                if r[1] >= 7000 and seg_first_7k_end_sec is None:
                    seg_first_7k_end_sec = r[0]
            if seg_first_7k_end_sec and seg_last_7k_start_sec:
                head = [r for r in rows if r[0] < seg_first_7k_end_sec]
                tail = [r for r in rows if r[0] >= seg_last_7k_start_sec]

                def mech_avgs(seg):
                    cads = [r[4] for r in seg if r[4] and r[4] > 50]
                    gcts = [r[5] for r in seg if r[5]]
                    vrs  = [r[6] for r in seg if r[6] is not None]
                    strds= [r[7] for r in seg if r[7] and r[7] > 30]
                    return {
                        'cad':      (sum(cads)/len(cads)) if cads else None,
                        'gct':      (sum(gcts)/len(gcts)) if gcts else None,
                        'vr':       (sum(vrs)/len(vrs))   if vrs  else None,
                        'stride_m': (sum(strds)/len(strds))/100 if strds else None,
                    }
                mh, mt = mech_avgs(head), mech_avgs(tail)

                out.append(f"- Mechanical-decay detection (km 0-7 vs km "
                           f"{(total_dist-7000)//1000:.0f}-{total_dist//1000:.0f})")
                if mh['cad'] is not None and mt['cad'] is not None:
                    line = f"  - Cadence: {mh['cad']:.0f} → {mt['cad']:.0f} spm ({mt['cad']-mh['cad']:+.0f})"
                    if mh['stride_m'] is not None and mt['stride_m'] is not None:
                        line += (f" | Stride: {mh['stride_m']:.2f} → {mt['stride_m']:.2f}m "
                                 f"({(mt['stride_m']-mh['stride_m'])*100:+.0f} cm)")
                    out.append(line)
                if mh['gct'] is not None and mt['gct'] is not None:
                    out.append(f"  - GCT: {mh['gct']:.0f} → {mt['gct']:.0f} ms ({mt['gct']-mh['gct']:+.0f})")
                if mh['vr'] is not None and mt['vr'] is not None:
                    out.append(f"  - Vertical ratio: {mh['vr']:.1f} → {mt['vr']:.1f}% ({mt['vr']-mh['vr']:+.2f}pt)")
                out.append("- _Threshold_: in the last 7km, cadence drop "
                           ">5spm + stride growth >5cm + GCT rise >10ms + "
                           "vertical ratio rise >0.5pt → **mechanical collapse** "
                           "(core + foot-arch failure; the \"lungs are fine "
                           "but my legs are gone\" feeling).")
        else:
            out.append("- _Note_: distance <36km, skipping the last-7km mechanical-collapse analysis.")

        # Glycogen depletion HR step at km 30
        # Compare avg HR in km 25-30 vs km 30-35
        if total_dist >= 35000:
            sec_at = {}
            for k in (25, 30, 35):
                target = k * 1000
                for r in rows:
                    if r[1] is not None and r[1] >= target:
                        sec_at[k] = r[0]
                        break
            if 25 in sec_at and 30 in sec_at and 35 in sec_at:
                pre_seg  = [r for r in rows if sec_at[25] <= r[0] < sec_at[30] and r[1] is not None]
                post_seg = [r for r in rows if sec_at[30] <= r[0] < sec_at[35] and r[1] is not None]
                pre_hrs  = [r[2] for r in pre_seg if r[2] is not None]
                post_hrs = [r[2] for r in post_seg if r[2] is not None]
                if pre_hrs and post_hrs:
                    avg_pre = sum(pre_hrs) / len(pre_hrs)
                    avg_post = sum(post_hrs) / len(post_hrs)
                    out.append(f"- Glycogen HR step (km 25-30 vs km 30-35): "
                               f"HR {avg_pre:.0f} → {avg_post:.0f} ({avg_post-avg_pre:+.1f} bpm)")
                    out.append("- _Threshold_: HR step >+5bpm with no pace "
                               "change = glycogen-depletion signal.")
        return out
