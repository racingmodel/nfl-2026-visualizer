"""Durable, deliberately non-optimistic Phase 7 implementation checkpoint."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import pandas as pd
import numpy as np

from nfl2026.phase7.common import json_write, verify_protected
from nfl2026.utilities.io import sha256_file


PHASES = [
    ("7A Data foundation", "IMPLEMENTED; CERTIFICATION PARTIAL", "evidence ledger, source/feature registry, NGS adapter, frozen source proof", "unit + real-data audits", "infrastructure only; historical eligibility BLOCKED"),
    ("7B Player V4", "TESTED EXPERIMENTAL", "two opportunity/efficiency candidates; snap/reception/red-zone/goal-line targets; position TD", "2023–25 OOS; identical-cohort comparisons", "NO"),
    ("7C Distribution engine", "PARTIAL RESEARCH PROTOTYPE", "paired OOS game environment; coherent carries/targets/catches/yards; TD capacity primitive", "272 held-out games; structural + proper scores", "NO; score/TD coupling and calibration incomplete"),
    ("7D New markets", "PARTIAL IMPLEMENTATION", "reception/red-zone research outputs; threshold/fantasy utilities; explicit market contracts", "unit tests + reception OOS", "NO new certified market"),
    ("7E Frontend V3", "LOCAL PREVIEW", "matchups/performance redesign; original legacy pages retained; result annotations", "desktop/mobile + navigation/filter/detail checks", "NO; isolated preview, source integration/approval pending"),
    ("7F Continuous Model Lab", "PARTIAL IMPLEMENTATION", "immutable separate candidate registry; exact-input shadow archive; frozen weekly evaluation", "registry/shadow gates + 79-game reproduction", "research infrastructure only; full monitoring automation pending"),
    ("7G Publishing", "IMPLEMENTED, NOT DEPLOYED", "bounded propagation verifier; PUSHED/PROPAGATING/VERIFIED/FAILED; existing no-op push guard retained", "delay/cache/network/mismatch/deadline fixtures", "code-ready; no live publishing validation performed"),
]


def table(frame):
    def cell(value):
        if isinstance(value, (float, np.floating)):
            return f"{value:.4f}" if np.isfinite(value) else "—"
        return str(value).replace("|", "/").replace("\n", " ")
    return "| " + " | ".join(frame.columns) + " |\n|" + "|".join("---" for _ in frame.columns) + "|\n" + "\n".join(
        "| " + " | ".join(cell(v) for v in row) + " |" for row in frame.itertuples(index=False, name=None))


def write_reports(settings):
    report = settings.report_root
    baseline = json.loads((report / "phase7_integrity_before.json").read_text())
    verify_protected(settings.root, baseline)
    foundation = json.loads((report / "phase7_foundation.json").read_text())
    tests = json.loads((report / "phase7_tests.json").read_text()) if (report / "phase7_tests.json").exists() else {"passed":0,"executed":0}
    metrics = pd.read_csv(report / "phase7_player_v4_metrics.csv")
    selected = metrics[metrics.model.str.endswith("extra_trees") & metrics.prediction.isin([
        "pred_carries", "pred_targets", "pred_rushing_yards", "direct_rushing_yards", "pred_receiving_yards", "direct_receiving_yards"])]
    comparisons = []
    for (target, prediction), part in selected.groupby(["target", "prediction"]):
        comparisons.append({"Metric/cohort":target + " / " + prediction, "Rows":int(part.rows.sum()),
            "Incumbent replay MAE":float(np.average(part.champion_mae, weights=part.rows)),
            "V4 candidate MAE":float(np.average(part.mae, weights=part.rows))})
    compare = pd.DataFrame(comparisons)
    compare.to_csv(report / "phase7_comparable_player_metrics.csv", index=False)
    td = pd.read_csv(report / "phase7_td_reconciliation_metrics.csv")
    td = td[td.model.str.endswith("extra_trees")]
    td_rows = []
    for position, group in td.groupby("position"):
        row = {"Position": position}
        for method, part in group.groupby("method"):
            row[method + " Brier"] = float(np.average(part.brier, weights=part.rows))
        td_rows.append(row)
    td_table = pd.DataFrame(td_rows)
    budgets = pd.read_csv(report / "phase7_td_reconciliation_budgets.csv")
    live = pd.read_csv(report / "phase7_official_performance.csv")
    sources = pd.read_csv(report / "phase7_advanced_source_audit.csv")
    simulation = json.loads((report / "phase7_joint_simulation.json").read_text())
    lab = json.loads((report / "phase7_lab.json").read_text())
    preview = json.loads((report / "phase7_preview_manifest.json").read_text())
    # The protected run's own exact archives also prove the global numerical
    # source bytes did not change relative to the already frozen Week6 state.
    lock = settings.root / "locks/2026/week_06/lock-2026-w06-fd15596ede7f78e9/inputs"
    unchanged_features = all(sha256_file(settings.artifact_root / "phase2" / a)==sha256_file(lock / b)
        for a,b in [("player_pregame_universe_features.csv","player_pregame_universe_features.csv"),
                    ("team_advanced_matrix.csv","historical_team_features.csv")])
    manifest = {"checkpoint_at_utc":datetime.now(timezone.utc).isoformat(), "phase_status":PHASES,
        "protected_files":len(baseline), "protected_artifacts_unchanged":True,
        "global_features_match_frozen_week6_input_bytes":unchanged_features,
        "new_models_promoted":0,"official_predictions_generated":0,"odds_api_calls":0,"published":False,
        "tests":tests,"research_artifact_hashes":{str(p.relative_to(settings.root)):sha256_file(p)
            for p in (settings.artifact_root / "research/phase7/models").glob("*") if p.is_file()},
        "research_input_hashes":{str(p.relative_to(settings.root)):sha256_file(p) for p in [
            settings.artifact_root / "phase2/player_pregame_universe_features.csv",
            settings.artifact_root / "phase2/player_advanced_conditional_matrix.csv",
            settings.artifact_root / "canonical/player_weekly.csv"]},
        "preview_source_run_id":preview["source_run_id"],
        "promotions_blocked_by":["uncertified historical eligible population", "uncertified role/starter change timing",
            "no clean untouched new confirmation cohort", "joint score/TD/certified uncertainty incomplete"]}
    json_write(report / "phase7_checkpoint.json", manifest)
    phases = table(pd.DataFrame(PHASES, columns=["Phase","Status","Implemented","Tested","Production-ready"]))
    md = f"""# NFL 2026 — Phase 7 evolution report

Checkpoint: {manifest['checkpoint_at_utc']}. This is a tested, resumable implementation checkpoint, **not completion of all Phase 7 capabilities**. Nothing was promoted or published.

## Executive summary

Implemented evidence-led provenance, six immutable Player V4 fold artifacts (two candidate architectures × three historical folds), a coherent opportunity-allocation challenger, explicit output/fantasy contracts, a locally tested V3 matchup/performance preview, an isolated champion/challenger registry with shadow input archival, and propagation-aware publishing verification. Meaningful historical improvements are research signals only.

## Phase status

{phases}

## 7A — Data foundation and evidence

`phase7_source_inventory.csv` covers real local source classes across 2022–2026, including legacy depth/injury paths and Parquet metadata. `phase7_feature_registry.csv` extends the 113 team + 23 player-feature audit with actual registered model users/hashes, known-at rules, missingness and explicit provenance qualifications. It does not fabricate a single universal publication timestamp for a derived feature.

Reconstructed {foundation['rows']:,} offensive weekly roster observations from roster files and scheduled game/team associations, **without using final box-score participation to select players**. Historical weekly labels are retrospective membership claims, not independently certified pregame eligibility. `{foundation['certified_eligibility_rows']}` historical full-eligibility certificates were fabricated: none. Missing availability/role remains UNKNOWN. Team/status conflicts are retained for inspection, not resolved using future performance. The legacy membership file's fixture-derived timestamps are not trusted as publication evidence.

Separately recovered {foundation['captured_2026_roster_observations']:,} 2026 roster observations from original immutable locks; {foundation['certified_2026_roster_observations']:,} meet the strict captured-source timestamp/hash test. This certifies a **roster observation at its forecast cutoff**, not universal injury/transaction/game-day activation completeness. Original source warnings remain. It does not retroactively certify 2022–2025 training populations.

Existing Week6 exact feature archiving remains intact: original input bytes and actual registered model contracts are already present in new production locks. Global team/player source files still match the frozen Week6 input bytes: {unchanged_features}. No old lock was repaired or rewritten.

### Additional data actually acquired

Three public nflverse NGS compressed CSV assets were downloaded into `data/research/phase7/20261014T142440733449Z/`, with SHA256, URL, retrieval timestamps, schemas and season coverage. Raw data span 2016–2026. Training research here does not use any 2026 NGS outcomes. A tested adapter excludes week=0 season summaries and POST rows, and uses strictly earlier weeks/seasons; missing coverage is not zero efficiency. Historical release/revision latency is still unproved, so no NGS feature is a production dependency. Current source terms are not equated with the software's MIT licence; no NGS dataset is redistributed in the preview.

FTN charting was investigated, not downloaded in this checkpoint. Public charting is CC-BY-SA 4.0 with FTN Data via nflverse attribution; source documentation describes postgame charting. Participation 2023+ is published after postseason, so existing historical participation files cannot silently become recent in-season pregame features. Routes/pressure/box counts still need their own legitimate source/timing/coverage/ablation work.

Sources: [weekly rosters](https://nflreadr.nflverse.com/reference/load_rosters_weekly.html), [NGS](https://nflreadr.nflverse.com/reference/load_nextgen_stats.html), [current NGS assets](https://github.com/nflverse/nflverse-data/releases/expanded_assets/nextgen_stats), [FTN charting and attribution](https://nflreadr.nflverse.com/reference/load_ftn_charting.html), [publication schedule](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html), [underlying data terms](https://nflverse.nflverse.com/).

## 7B — Player V4 experiment

Two preregistered small architectures: regularised linear and ExtraTrees opportunity forecasts; direct yardage compared with weighted conditional efficiency × predicted opportunities; binomial catch-rate/reception forecasts; snap-share predictions scored only on measured target rows; red-zone and inside-5 opportunity forecasts. No all-history cold-start medians are used: repaired rolling features and fold-fitted imputers preserve numeric chronology. No injury, retrospectively known starter, depth promotion or market predictor enters these candidates.

Training seasons precede the scored 2023/2024/2025 season; training maximum kickoff is recorded. Each candidate scores 22,530 nominal roster rows. These are **WEEKLY_ASSUMED_PREGAME**, not certified populations. The cohort itself prevents promotion even when error improves. Active probability, historical role-change conditioning, and certified QB passing forecasts remain blocked/not implemented. QB-specific anytime **offensive** TD uses lagged rushing/goal-line inputs, not passing TD targets or a fixed player cap. Designed-run/scramble/sneak-specific targets remain outstanding.

### Comparable historical point metrics

ExtraTrees candidate versus the previously repaired incumbent architecture replay, same game/player/team/season/week keys and position cohorts. Rushing: QB/RB/FB; receiving/target metrics: non-QB offensive rows. Direct and two-stage variants are both shown, not cherry-picked as a certified champion.

{table(compare)}

Full per-fold metrics, bias/RMSE and season-week block-bootstrap delta intervals: `phase7_player_v4_metrics.csv`. Snap-share includes QBs where measured; red-zone carries use the rushing cohort, not WR/TE zeros. Team-budget allocation is an experimental variant using a supplied prior-team-volume budget; zero weights remain unallocated, and contradictory budgets block.

`phase7_td_reconciliation_metrics.csv` compares raw/reconciled incumbent and new position classifiers on identical rows. The scoring-volume budget remains unchanged. A numerical audit found the incumbent helper can stop on a converged root but then return a different, unevaluated bracket midpoint. An isolated candidate-only converged-root repair is tested; the production helper remains untouched. `phase7_td_reconciliation_budgets.csv` records both algorithms' sum errors. Reconciliation may damage individual calibration; it is reported, not assumed beneficial. Production TD remains phase2-v2. No new individual probability is presented as certified.

{table(td_table)}

Across {len(budgets)} candidate team/fold/architecture groups, {int(budgets.incumbent_algorithm_sum_error.abs().gt(1e-6).sum())} incumbent-algorithm results miss their requested sum by >1e-6; maximum {budgets.incumbent_algorithm_sum_error.abs().max():.8f}. Candidate-only converged-root maximum error {budgets.candidate_sum_error.abs().max():.10f}. These are numerical diagnostics, not evidence of a profitable TD edge. Position-specific TD is not uniformly better, and reconciliation effects remain explicit.

2025 was already examined in prior experiments. It is a time-ordered evaluation fold, **not a newly untouched holdout**. Multiple candidate comparisons and uncertified participation require genuinely new captured-pregame shadow evidence before any selection/promotion. 2026 Weeks1–5 are monitoring holdout data, not hyperparameter-selection data.

## 7C — Joint challenger

Different mechanism from the failed score-postprocessing attempts: paired 2023–24 OOS **team opportunity residuals** drive a shared game environment, with multinomial teammate allocation and binomial catches. Historical empirical game-level efficiency allows negative yardage. Team gross passing yards equal receiving-yard sums within the included population; net passing yards, sacks and untargeted passes are explicitly excluded, not falsely equated. No score-generation cosmetic adjustment was attempted.

Evaluated {simulation['games']} 2025 games, {simulation['draws_per_game']} draws per game, {simulation['seconds']:.1f}s simulation runtime. Structural consistency: {simulation['structural_consistency']}. CRPS/80% interval widths/coverage and mean-deviation diagnostics are in `phase7_joint_simulation_backtest.csv`. These metrics currently average all included offensive candidate rows, including zero-opportunity roles; they are not comparable to the primary meaningful-yardage incumbent cohort without a further matched-cohort analysis. No claim of better uncertainty is made.

A capacity-constrained TD allocator is unit-tested but **not wired to fabricated TD-count probabilities**. TD/scoring coupling, team-score distributions, empirical threshold certification, per-player mean anchoring, position/opportunity calibration and incumbent probabilistic comparisons remain incomplete. The challenger is NOT promoted.

Efficiency pools are not yet opportunity-conditioned; small-opportunity historical game averages may inflate high-volume tails. Yardage draws are continuous approximations. Tail plausibility and opportunity-conditional calibration are outstanding, even when arithmetic/CRPS diagnostics pass.

## 7D — Outputs and fantasy

New OOS research outputs: receptions, red-zone/goal-line opportunity and snap share. Existing targets/carries remain untouched production outputs. Typed contracts document target, eligible population, inputs, metrics, version and status for requested markets. Passing yards/TD counts, first TD, interceptions/sacks, 2+ TD, certified fantasy floors/ceilings and replacement-adjusted rankings are **not production-ready**.

Tested offensive-player scoring utilities support explicit standard/half-PPR/PPR weights, including passing TD, interceptions, lost fumbles, passing/rushing/receiving two-point conversions and special-teams TD. These are stated presets, not an assertion of every platform's bonus/kicker/defense rules. Missing categories fail by default; explicitly requested partial totals are labelled PARTIAL. Quantiles and threshold probabilities require actual finite draw vectors, not an unrelated point mean. No complete fantasy prediction or fabricated 2+ TD probability is displayed.

## 7E — Frontend V3 preview

Local preview: `frontend/phase7_preview/index.html` (loopback server port8768). New matchups retain AWAY SCORE @ HOME SCORE, UK-local kickoff, stadium, winner, spread/total, confidence, leading player angle, progressive native details, ranges, cutoff/source run and explicit availability. Model headline remains existing rounding. No hidden new correct-score or uncertainty model.

Historical FINAL scores are added **only to preview copies** from validated completed-week cohorts. Every original prediction field/player forecast/cutoff remains copied unchanged; global published data is protected. Published historical bundles lacked these presentation annotations. A no-store fetch prevents stale local data after rebuilds. Team abbreviations filter exactly (PHI no longer accidentally matches Dolphins).

Performance shows 79 frozen final games, 2,345 offensive player forecasts, sample counts, weekly and cumulative metrics, limitations and the isolated candidate lab. Week6 is not evaluated before validated actuals. Players/Markets/Model legacy pages and assets are retained; they are not fully redesigned yet. Market values are archived, never invented or treated as live; paired-price margin/commission qualification remains the existing market engine rather than a new unsupported overlay.

Desktop1280 and mobile390/320 viewport checks; no document horizontal overflow, filtering, historical navigation, detail expansion, PHI–CHI final result and performance values checked. Screenshots: `phase7_desktop.jpg`, `phase7_mobile.jpg`, `phase7_performance.jpg` where captured. Keyboard-native controls/skip links/focus indicators/labelled table region included; full screen-reader/automated accessibility audit and all legacy-page visual testing are outstanding. No unsupported what-if inference is enabled.

## 7F — Model lab and weekly evaluation

Separate immutable research registry tracks six artifacts with cutoff, schema, hashes, periods, metrics and failed promotion gates. No production registry write. Every required gate must be literal True; PARTIAL/missing cannot pass. Even all gates passing only means eligible for approval, never automatic activation.

Shadow archival requires pre-kickoff cutoff, earlier training, exact input bytes/hashes, independently captured hash-bound source evidence and known-at/retrieval timestamps. The actual challenger artifact hash/training provenance is checked; target/actual/bookmaker fields are blocked. Timestamp strings alone cannot certify a shadow input. No new live shadow or official forecasts were generated. New adapters must supply legitimate evidence chains, not invented capture metadata.

Frozen weekly evaluation discovers official week directories, validates original selected run/lock hashes and pregame timestamps, requires completed-week ingestion, then scores the same cohorts. It does not regenerate forecasts. Current transaction warning is read from the actual frozen manifest, not hard-coded as READY. Full scheduled monitoring, robust shift/calibration alerts, betting-return analysis and automatic weekly health delivery remain outstanding. Existing production source/PIT gates remain authority.

### Reproduced live benchmark (not compared to historical candidates as if same cohort)

{table(live[live.segment.eq('ALL')][['rows','team_score_mae','margin_mae','total_mae','winner_accuracy','win_brier','td_brier','rush_mae','receiving_mae']])}

## 7G — Propagation safety

Publisher now records PUSHED separately, then PROPAGATING/VERIFIED/FAILED with observed/expected run IDs and attempt history. Bounded240s verification, capped backoff and per-request timeout, cache-busting nonce and no-cache headers. Permanent mismatch, network/schema failure and late responses remain failures. Existing staged Git diff avoids pushing identical content. Retry only fetches public JSON, never reruns models or republishes.

The legacy Week1 report consumer accepts both historical PUBLISHED and the new VERIFIED terminal status. Existing deployment records were not rewritten.

No actual publishing invocation occurred; tests use fixtures. A larger one-command weekly orchestrator was investigated but **not implemented**: preserve native refresh → preparation/check → explicit independent production → separate market comparison → approved publishing gates. Next week's production must still wait for current-week finality/actuals/PBP. Never bundle bookmaker acquisition into independent prediction readiness.

## Safety and validation

Protected files: {len(baseline)}; all Week1–6 predictions, locks, freezes, model/production registries, market/value history, source snapshots, published frontend and deployment artifacts unchanged. Week6 official run/freeze verified against actual registered artifacts. Models remain team/TD phase2-v2, player phase2-v3-rush-et, advanced-pit-v2. New trained objects live only under `artifacts/research/phase7/models`.

Tests: {tests['passed']}/{tests['executed']}; network disabled in full suite. Compile checks performed. The first direct focused invocation hit the Windows sandbox default temp-directory permissions; tests now use a workspace temporary directory and the broader run passes. Mock provider log messages are fixtures, not Odds API calls.

No Odds API, deployment, official Week6 prediction, or unsupported promotion. Public site unchanged. Production expected means, score display, carries/targets and TD outputs remain unchanged.

## Remaining blockers and exact next actions

1. **Historical eligibility certification:** independently dated active/reserve/transaction/game-day evidence with auditable identity and revision chains; current captured roster observations alone do not certify full availability. Do not backfill participation as eligibility.
2. **New validation cohort:** prospective shadow inputs with full provenance before kickoff, then frozen evaluation after results. Historical2025 has been repeatedly explored.
3. **V4 validation:** expanded per-position/volume/early-season/role-change comparisons, TD reconciliation calibration and trustworthy team-volume predictor before production selection.
4. **Joint engine:** matched-population incumbent proper-score/threshold tests, mean-preservation diagnostics, validated count/scoring coupling and missing stat categories. No arbitrary count conversion from anytime TD.
5. **NGS/FTN:** quantify prior-week availability/revision latency, source missingness and incremental usefulness. FTN/routes/pressure not implemented in model inputs.
6. **Frontend:** source-level integration, legacy-page design/accessibility regression, full market freshness/commission presentation and approval. Preview is not deployable production merely because it renders.
7. **Lab/operations:** finish data/shift/calibration health automation and explicit stage orchestration, without bypassing existing weekly gates.

### Resume commands (no production or publishing)

```powershell
python scripts\\phase7_evolution.py --stage foundation
python scripts\\phase7_evolution.py --stage players
python scripts\\phase7_evolution.py --stage simulation
python scripts\\phase7_evolution.py --stage outputs
python scripts\\phase7_evolution.py --stage lab
python scripts\\phase7_evolution.py --stage preview
python scripts\\phase7_evolution.py --stage tests
python scripts\\phase7_evolution.py --stage report
```

`players` reuses immutable trained artifacts/OOS rows and recomputes reporting diagnostics; no incumbent refit. `sources` intentionally makes public NGS download requests into a NEW isolated archive, and is not called by `all`. The production integrity baseline deliberately fails if outside work changes protected published state; after a future authorised production update, begin a new audited research epoch rather than deleting protection records to hide changes.

## Next deployment

No model promotion approved. The publisher retry improvement can be used during a separately authorised future deployment. Frontend and model research require their own remaining gates and approval before entering a NEW future-week run. Existing Week1–6 artifacts remain immutable forever.
"""
    (report / "phase7_evolution_report.md").write_text(md, encoding="utf-8")
    tracker = f"""# Phase 7 implementation tracker

Updated: {manifest['checkpoint_at_utc']}. Durable checkpoint; not all phases complete.

{phases}

## Completed and tested implementation

- Provenance/source/feature registry + historical/frozen roster observation ledgers.
- Public NGS downloader + strictly lagged adapter; source timing limitations retained.
- Six isolated V4 fold artifacts and OOS candidate/reconciliation diagnostics.
- Joint opportunity prototype and historical structural/proper-score research.
- Output/fantasy utilities with explicit incomplete/uncertified contracts.
- Local matchup/performance preview, original forecast plus validated final-result annotations.
- Research registry/shadow archive/official frozen evaluation; no activation.
- Bounded publishing verifier and failure-state persistence; not run live.

## Tested candidate, not validated upgrade

V4 ExtraTrees improves exploratory point metrics. Shared-volume simulation passes arithmetic, not probability certification. Historical eligibility, role provenance and untouched confirmation remain blocked. Approved production promotions: **0**.

## Not started / blocked

Certified active probability and role replacements; QB passing/starter model; designed-run/sneak-specific TD; full TD-score coupling; calibrated new threshold/fantasy markets; complete source-integrated V3 frontend; automated weekly lab health/scenario inference; unified operations command.

## Exact next implementation

Implement prospective source-evidence capture/validation for complete eligibility (including transaction warnings), then shadow-evaluate V4 on genuinely new games. Use the report's resume commands for the existing isolated code. Do not generate another official Week6 forecast or publish.

Tests at checkpoint: {tests['passed']}/{tests['executed']}. Protected production file hashes: {len(baseline)} unchanged.
"""
    (report / "phase7_implementation_tracker.md").write_text(tracker, encoding="utf-8")
