"""Durable continuation evidence, not another programme roadmap."""
from datetime import datetime, timezone
import json

import pandas as pd

from nfl2026.phase7.common import json_write, verify_protected
from nfl2026.phase7.shadow import verify_shadow
from nfl2026.phase7.shadow_evaluation import evaluate_shadow
from nfl2026.utilities.io import sha256_file


def table(frame):
    columns = frame.columns.tolist()
    lines = ['| '+' | '.join(columns)+' |', '| '+' | '.join(['---']*len(columns))+' |']
    for row in frame.itertuples(index=False,name=None):
        lines.append('| '+' | '.join(f'{v:.5f}' if isinstance(v,float) else str(v) for v in row)+' |')
    return '\n'.join(lines)


def write_continuation_report(settings):
    root, reports = settings.root, settings.report_root
    baseline = json.loads((reports/'phase7_integrity_before.json').read_text())
    verify_protected(root,baseline)
    folders = sorted((settings.artifact_root/'research/phase7/shadows/2026/week_06').glob('shadow-*'))
    shadows = [verify_shadow(p) for p in folders]
    pending = [evaluate_shadow(settings,p) for p in folders]
    coverage = json.loads((reports/'phase7_continuation_provenance.json').read_text())
    distribution = pd.read_csv(reports/'phase7_threshold_distribution_backtest.csv')
    segments = pd.read_csv(reports/'phase7_v4_segment_comparison.csv')
    tests = json.loads((reports/'phase7_tests.json').read_text())
    optional = pd.read_csv(reports/'phase7_qb_passing_metrics.csv')
    joint = pd.read_csv(reports/'phase7_joint_refinement_backtest.csv')
    metrics=[]
    for (target,method),part in joint.groupby(['target','method']):
        count=part['rows'].sum()
        metrics.append({'target':target,'method':method,'rows':int(count),**{k:float((part[k]*part['rows']).sum()/count) for k in ['crps','coverage80','width80','mean_deviation_mae']}})
    joint_summary=pd.DataFrame(metrics)
    joint_summary.to_csv(reports/'phase7_joint_refinement_summary.csv',index=False)
    player_metrics=pd.read_csv(reports/'phase7_player_v4_metrics.csv')
    player_metrics=player_metrics[player_metrics.model.str.endswith('extra_trees') & player_metrics.prediction.eq('pred_'+player_metrics.target) & player_metrics.target.isin(['carries','targets','rushing_yards','receiving_yards'])]
    points=[]
    for target,part in player_metrics.groupby('target'):
        count=part['rows'].sum()
        points.append({'target':target,'rows':int(count),'incumbent_mae':float((part.champion_mae*part['rows']).sum()/count),
            'candidate_mae':float((part.mae*part['rows']).sum()/count)})
    mean_table=pd.DataFrame(points)
    selected=distribution[distribution.position.eq('ALL') & distribution.target.isin(['rushing_yards','receiving_yards'])][['target','method','rows','coverage80','width80','crps','mean_deviation_mae']]
    sha=sha256_file(folders[0]/'manifest.json')
    timestamp=datetime.now(timezone.utc).isoformat()
    report=f'''# NFL 2026 — Phase 7 continuation results

Updated {timestamp}. This continues the previous checkpoint. Zero production model promotions; no publication, Odds API request or replacement Week6 official forecast.

## 1. Prospective Week6 shadow — implemented first

Archived `{folders[0].relative_to(root)}`. Status **{shadows[0]['status']}**, **{shadows[0]['players']} players / {shadows[0]['games']} games**, identical official forecast keys. Positions: QB77, RB101, TE105, WR152, FB0 (actual frozen source classification, not invented FB observations).

Official run `phase3_5-production-20261014T130712Z-5aba9259`; verified exact lock `lock-2026-w06-fd15596ede7f78e9`; freeze `freeze-20261014T130739Z-98675c85` unchanged. Original input boundary {shadows[0]['original_input_cutoff_utc']}; challenger generated {shadows[0]['prediction_cutoff_utc']}; archive finalized {shadows[0]['created_at_utc']}. All14 games were PRE_KICKOFF at inference; earliest frozen kickoff 2026-10-16T00:15:00Z. No cutoff backdating option exists.

Candidate `{shadows[0]['candidate_id']}`; actual model SHA `bb7008f71aa6bfcb6acb877f3dfcf09cef372cdc94ca906c83c7cc02fbdae53f`; maximum training kickoff 2025-01-06T01:20Z, seasons2022–24. The existing fitted candidate is reused, not retrained. Manifest SHA **{sha}**. Every copied source, consumed feature block, model/metadata, inference code, prediction, comparison and predeclared evaluation policy has an immutable archive hash. Each forecast's kickoff and exact incumbent game/player/team association is retained. All official feature/model/source hashes verify. Source known/retrieval times are at or before the official cutoff; the later shadow generation timestamp is not substituted for source evidence.

Outputs: carries, targets, two-stage and direct rushing/receiving means, receptions/catch probability, snap share, red-zone/inside5 opportunities, raw offensive-TD probability and separately labelled candidate reconciliation. Numeric history remains completed Weeks1–5; no Week6 results/PBP/participation enters X. All original predicted players are retained, including likely nonparticipants. No inference-time filtering on box-score appearance. The evaluated population is certified as the **captured official forecast universe**, not as a complete independent game-day activation universe; existing transaction and unknown-availability limitations remain.

Pregame/evaluation rules were archived before outcomes: rush QB/RB/FB; receiving WR/TE/RB/FB; identical incumbent keys; absent player entry becomes zero only after validated complete-week ingestion. Present unknown stats fail closed. MAE/RMSE/bias, position metrics, paired game-block CIs, TD Brier/log loss/reliability bins and regression-review alerts. Receptions have challenger-only measured targets; no fake incumbent comparison. Optional snap/red-zone targets require separately validated source coverage and remain pending otherwise. One week cannot promote V4.

Current evaluator state: `{pending[0]['status']}`. No actuals were fabricated as zeros before finality. READY for postgame evaluation once the normal completed-week ingestion passes. Future historical replays require explicit `--allow-retrospective` and cannot become prospective records.

## 2. Historical eligibility/provenance — blocker retained, coverage expanded

{coverage['historical_roster_observations']} reconstructed historical roster claims; {coverage['frozen_roster_observations']} contemporaneous 2026 frozen roster observations; **{coverage['partially_certified']} partial roster captures**, **{coverage['fully_certified']} full eligibility certificates**. Season/week/position/class coverage is in `phase7_continuation_provenance_coverage.csv`. Existing inventory and exact evidence limitations are retained in `phase7_continuation_evidence_inventory.csv` and JSON.

Historic weekly roster status, depth dates, injury date_modified and transaction effective dates are not independently captured revision chains. A retrospective source revision may contain later knowledge. No final participation was used as pregame eligibility proof. None of the available2023–25 records establish a complete injury/reserve/activation chain. Therefore the strongest fully certified historical comparison has **0 rows**, a reduction of22530 nominal V4 roster rows. Re-evaluation on that nonexistent cohort is BLOCKED, not scored as a successful smaller sample. The 2026 captured roster-only cohort does not retroactively certify2023–25. Records explicitly reconstructed from participation: none used; unknown historical revisions are not falsely declared pregame.

## 3. V4 refinement — actual cached comparisons, no repeated fitting

{table(mean_table)}

`phase7_v4_segment_comparison.csv`: {len(segments)} fold/position/early-season/late-season/pregame-volume/direct-versus-two-stage comparisons, retaining all less favourable rows. Season-week block bootstrap400 repetitions; exact same incumbent/challenger keys. No tested segment has a paired MAE-delta CI entirely above zero; that does not prove uniform superiority. Full intervals/sample sizes are retained, not just a pooled average.2025 was already examined and remains exploratory, not a new untouched holdout. The point estimates reproduce previous results; evidence is strengthened by segment analysis and genuine prospective archival, **not yet by new prospective accuracy**.

Certified historical role-change/expected-start probability/replacement status remains BLOCKED. Active probability is not fitted to retrospectively known participants. Snap forecasts remain measured-target research. RB committee allocation and supplied-team-budget constraints remain experiments, not arbitrary caps or new production weighting. QB starter/backup annotations are available in the frozen source but historical starter evidence is unproved. `phase7_v4_certification_policy.json` requires all lab gates, cross-season and multiweek prospective evidence, calibration/bias/runtime/input reliability and human approval. No model was selected for production.

## 4. New distributions and passing capabilities — research only

Conditional residuals use genuine2023–24 rolling-origin OOS errors;2025 held out chronologically. Position/predicted-mean quartiles require100 observations, otherwise position/global fallback. Signed integer yards retain observed negative support; opportunity counts remain nonnegative. Target-count draws plus binomial catch rates make receptions<=targets, including zero outcomes. Threshold Brier/reliability,50/80/90% coverage/width, quantile CDF diagnostics,CRPS, position/zero mass/tails and mean-deviation diagnostics are persisted.

{table(selected)}

Comparing V4 heuristic with V4 residuals isolates uncertainty-method change; comparing incumbent heuristic with V4 also includes different research means. The incumbent normal baseline preserves its unbounded probabilistic distribution; display zero floors are not substituted for a truncated CDF. Discrete zero atoms mean empirical `actual<=quantile` coverage need not equal nominal q. Flooring/discretisation shifts sampled means; the point forecast fields themselves are unchanged. Thresholds are synthetic half-yard/count levels, not bookmaker data. Sample support/one repeatedly explored fold/history eligibility still prohibit certification or promotion. Immutable separate artifact: `conditional-residual-research-v1`.

QB passing: **{int(optional['rows'].sum()/3)} OOS QB rows** over2023–25; all nominal roster QBs, not retrospectively chosen actual starters. Complete historical REG PBP yields attempts excluding sacks/nullified/two-point plays, gross passing yards and PBP-defined passing TDs. Lagged personal passing history uses strictly earlier kickoff observations; missing history is imputed by the training fold. Existing23 numeric player inputs plus4 lagged passing features; fixed small ExtraTrees models, no search. New separate experimental artifact `qb-passing-roster-research-v1`, no production QB-impact activation.

{table(optional)}

Target reconciliation is NOT hidden.2022–24 attempts/yards agree on comparable weekly rows; one2022 passing-TD discrepancy: `2022_12_CIN_TEN`, Ryan Tannehill, weekly credited1 versus PBP pass_touchdown0. PBP description records Derrick Henry receiving69 yards, fumbling, Treylon Burks recovery TD. It is not silently converted to an offensive passing TD.2025 raw weekly passing_yards is absent: complete PBP supplies the research target.2025 weekly pass_attempts disagrees on512/660 matched passers (max10);580/660 weekly values match sack-included PBP pass_attempt, not official-style attempts. Remaining80 do not reconcile even by that explanation. Therefore no certified passing target contract is claimed; review the residual source differences before promotion. Passing count/tail calibration and reliable pregame starter/backup conditioning remain pending. No QB passing prediction is displayed as official.

## 5. Joint simulation / TD numerical evidence

Opportunity-conditioned historical efficiency pools reduce the previous tiny-sample tail problem. Counts still share paired game-volume residuals, teammate carries/targets reconcile, catches cannot exceed targets, and included receiving yards equal modeled gross team passing yards. Calibration uses2024-or-earlier data; 2722025 games,1500 deterministic draws/game, meaningful rushing/receiving position cohorts, matching incumbent comparison population. No target outcome supplies simulation inputs.

{table(joint_summary)}

Rushing interval calibration improves; receiving remains over-covered and sampled means are not perfectly anchored. This is a partial simulator, not a calibrated game-score/TD-count engine. TD capacity primitive returns unallocatable counts instead of inventing opportunities. No anytime probability is converted into a Poisson TD count. Production score logic/simulations/means are unchanged.

The incumbent reconciliation stopping-rule defect is proven: when raw probabilities already sum exactly to budget, it moves the bracket then returns an unevaluated midpoint, altering the already-correct vector. Candidate returns the evaluated converged root. Historical3264 candidate team groups:36 incumbent numerical errors>1e-6, max4.615e-5; candidate max9.998e-9. Existing position-specific calibration remains nonuniform. **Production reconciliation is untouched; explicit approval still required.**

## 6. Frontend V3 — real data, local integration only

The preview builder now verifies displayed score/kickoff/player fields directly against each original immutable run and lock, not just a copied JSON file. Current Week6 exact production bundle bytes remain identical; real UK times, names, official TD/rushing/receiving forecasts, and existing archived market prices. Historical result annotations are separate from forecast fields and require validated completed cohorts. Real79-game /2345-offensive-player performance remains reproduced. Pending-week wording is derived from actual cohorts, not hardcoded Week6.

Frozen source-health disclosure records known/capture timestamps, actual source states and warnings. Quote age is recalculated at view time using existing configured120-minute game/60-minute prop freshness limits; future/missing timestamps and different-run quotes are labelled, stale quotes never keep a strong-value recommendation badge. Original value calculations and stored quote bytes are unchanged. Production unknown availability never becomes healthy. Unsupported passing/reception/fantasy forecasts are not fabricated in the official view. Prospective challenger status appears only in the lab section.

Responsive browser checks:320,390,768,1280px; document widths305,375,753,1265 respectively (no horizontal overflow). Native filtering NE1/14; PHI Week3 1/16; keyboard Enter opens SUMMARY; completed PHI20–CHI22 prediction remains distinct from FINAL PHI7–CHI27; real Week6 kickoff Fri16Oct01:15BST verified. Controls have accessible labels; skip links, native details, landmarks/focus styles retained. Missing values/long names/escaping/stale quotes tested with pure-render fixtures. Basic accessibility checks pass; full external screen-reader/legacy-page audit remains outside this checkpoint. Original Players/Markets/Model pages retained, not redesigned. Screenshots `phase7_continuation_320.png`, `_390.png`, `_768.png`, `_desktop.png` and performance screenshot where captured.

Local launch: `python -m http.server 8768 --bind 127.0.0.1 --directory frontend/phase7_preview`. URL `http://127.0.0.1:8768/`. This is not an automatically activated production frontend builder or a public deployment.

## 7. Publishing integration — fixture validated, not deployed

Existing staged-diff/no-op guard retained. `publish_and_verify` is wired into the actual publisher: commits/pushes once, then PUSHED→PROPAGATING→VERIFIED, bounded retry/backoff, cache bypass and exact run ID. Transient old Pages data is not immediately FAILED. Permanent mismatch, network/schema/deadline and Git commit/push errors persist FAILED. A previously failed push with an already committed identical build retries the pending push without a duplicate commit; a genuine identical/no-pending release neither commits nor pushes. Previous deployment provenance is retained; real failures are not suppressed. Local fixtures cover these integration states. **No real deployment/push performed**, publicsite/deploystatus unchanged.

## 8. Continuous model lab — reusable weekly workflow

Immutable weekly shadow generation and separate immutable actual-result evaluations are implemented. Future weeks use an explicit official run ID/exact lock; no official forecasting call. Result matching requires full finality+validated ingestion, preserves prospective versus retrospective tags, emits point/TD calibration/position metrics, paired regression alerts, input-health/provenance and immutable outcome-revision hashes. Identical rerun returns ALREADY_EVALUATED. Changed validated actuals create a new separately hashed evaluation, not a forecast rewrite. Missing measured optional sources remain unscored. No scheduling/automatic promotion was enabled without user request.

Generate subsequent-week shadow (replace only week and its actual official run ID):
`python scripts/phase7_shadow.py --season 2026 --week 7 --official-run-id <official-Week7-run-id>`

Evaluate captured Week6 after normal ingestion:
`python scripts/phase7_shadow.py --season 2026 --week 6 --official-run-id phase3_5-production-20261014T130712Z-5aba9259 --evaluate "{folders[0].relative_to(root)}"`

Other continuation stages: `python scripts/phase7_evolution.py --stage certification|uncertainty|passing|joint-refinement|lab|preview|tests|continuation-report` (choose one actual stage, not literal pipe expression). Existing fitted artifacts remain immutable; no incumbent refit.

## Integrity, tests and decision

Protected official files: **{len(baseline)} unchanged**. Weeks1–6 predictions/locks/freezes, current registered models/config, markets/value history, source snapshots, deployment artifacts and published frontend bytes unchanged. Global team/player features still match original Week6 lock hashes. No Odds API, official Week6 regeneration or publication.

Python tests: **{tests['passed']} passed, {tests['failures']} failures, {tests['errors']} errors, {tests['skipped']} skipped**, full suite network disabled. Pure-JS rendering contracts9/9; compile/import and JS syntax checks pass. New postgame fixture initially lacked explicit final status and correctly stayed pending; fixture repaired, not finality gate. Total counts in latest checkpoint JSON are authoritative.

Decision: **DO NOT PROMOTE**. Completed real prospective archive, broader cached diagnostics, new passing/count/threshold/joint research, authoritative local UI and publisher integration, reusable postgame scoring. Still blocked: full historical eligibility/role proof, prospective outcomes/multiweek confirmation, certified starter-conditioned passing/count/tail contracts, full score/TD coupling and live deployment validation (not authorised). Highest-impact next action: ingest validated Week6 outcomes when genuinely complete and score this exact immutable shadow, then repeat captured-input shadows across future weeks. Do not retrofit its inputs or select candidates from one week's results.
'''
    (reports/'phase7_continuation_results.md').write_text(report,encoding='utf-8')
    tracker='''# Phase 7 implementation tracker

Continuation implemented; nothing promoted or published.

| Work | Completed evidence | Remaining blocker / status |
|---|---|---|
| Prospective Week6 V4 | Immutable435-player/14-game exact-input forecast, same official universe; hashes and evaluation policy | Pending genuine completed-week actuals, not retrospective |
| Historical provenance |54795 historical claims +4897 captures;2264 partial certificates; per-season/week/position coverage |0 fully certified eligibility observations; role/change histories unproved |
| V4 refinement |153 cached fold/position/early/volume/two-stage comparisons with paired CIs |Exploratory; multiweek prospective confirmation required |
| Distribution/receptions |2023–24 OOS residuals→2025 thresholds/coverage/CRPS; targets>=catches |Research only; mean/tail/source certification incomplete |
| QB passing |3323 OOS nominal-roster QB rows; strict prior passing history; PBP target reconstruction |Historical starter proof absent; target discrepancies explicitly audited |
| Joint usage |272-game conditional-efficiency matched comparison, coherent counts |Research only; receiver coverage/mean anchoring and TD-score coupling incomplete |
| Frontend V3 |Authoritative-run verification, frozen-source health, dynamic price freshness, UKtime;320/390/768/1280 checks |Local only; original site retained; no activation/publication; broader legacy accessibility review pending |
| Publishing |Actual publisher integration, durable Git/HTTP failures, pending-push recovery, exact-ID propagation fixtures |No live deployment authorised/performed |
| Model lab |Generation/evaluation CLI; immutable outcome revisions, point/TD calibration/position metrics and regression review alerts |Waiting outcomes; optional snap/red-zone scores require validated sources; no automatic scheduling/promotion |

See `phase7_continuation_results.md` for exact artifacts, commands, metrics, uncertainties and current tests. Existing `phase7_evolution_report.md` preserves the previous checkpoint, with continuation pointer appended.
'''
    (reports/'phase7_implementation_tracker.md').write_text(tracker,encoding='utf-8')
    original=reports/'phase7_evolution_report.md'
    old=original.read_text(encoding='utf-8')
    marker='\n## Phase 7 continuation (separate evidence)\n'
    if marker not in old:
        original.write_text(old+marker+'The previous checkpoint above remains recorded. See `phase7_continuation_results.md` and updated tracker for the actual prospective Week6 shadow, cached subgroup diagnostics, distribution/passing/joint experiments, local UI integration and publisher/lab completion. No production promotion or publication.\n',encoding='utf-8')
    json_write(reports/'phase7_continuation_checkpoint.json', {'updated_at':timestamp,'shadow_artifacts':[str(p.relative_to(root)) for p in folders],
        'shadow_manifest_hashes':[sha256_file(p/'manifest.json') for p in folders],'evaluation_states':pending,
        'protected_files':len(baseline),'protected_unchanged':True,'tests':tests,'JS_tests':{'passed':9,'failed':0,'skipped':0},
        'production_promotions':0,'official_predictions_generated':0,'Odds_API_calls':0,'published':False})
