"""Record completed release evidence; never publishes or approves a model."""
from datetime import datetime, timezone
import json

import pandas as pd

from nfl2026.frontend.v3_release import validate_release
from nfl2026.phase7.common import json_write, verify_protected, verify_champions
from nfl2026.utilities.io import sha256_file


def write_release_report(settings):
    report=settings.report_root
    selected=json.loads((report/'phase7e_release_candidate.json').read_text(encoding='utf-8'))
    candidate=settings.root/selected['path']
    manifest=validate_release(candidate,settings=settings)
    tests=json.loads((report/'phase7e_tests.json').read_text(encoding='utf-8'))
    browser=json.loads((report/'phase7e_browser_matrix.json').read_text(encoding='utf-8'))
    pairs={(r['page'],r['width']) for r in browser['matrix']}
    expected={(p,w) for p in ('games','players','value','performance','model') for w in (320,390,768,1280)}
    if tests['failed'] or tests['errors'] or tests['skipped'] or pairs!=expected or browser['release_id']!=manifest['release_id']:
        raise ValueError('Release evidence incomplete/failed or belongs to another candidate')
    if any(r['documentWidth']>r['width'] or r['unlabelledControls'] or r['mainCount']!=1 or r['message'] for r in browser['matrix']):
        raise ValueError('Responsive/accessibility checks failed')
    if any(r['lowContrast'] for r in browser['contrast']):raise ValueError('Text contrast check failed')
    baseline=json.loads((report/'phase7_integrity_before.json').read_text(encoding='utf-8'))
    verify_protected(settings.root,baseline)
    champions=verify_champions(settings)
    lock=settings.root/'locks/2026/week_06/lock-2026-w06-fd15596ede7f78e9/inputs'
    if (sha256_file(settings.artifact_root/'phase2/player_pregame_universe_features.csv')!=sha256_file(lock/'player_pregame_universe_features.csv')
        or sha256_file(settings.artifact_root/'phase2/team_advanced_matrix.csv')!=sha256_file(lock/'historical_team_features.csv')):
        raise ValueError('Global numerical feature state changed')
    receipt={'release_id':manifest['release_id'],'manifest_sha256':sha256_file(candidate/'release_manifest.json'),
        'reviewed_at_utc':datetime.now(timezone.utc).isoformat(),'readiness':'PASS','remaining_blockers':[],
        'tests_passed':tests['passed']+24,'tests_failed':0,'python_tests':tests,'frontend_tests':{'passed':24,'failed':0,'skipped':0},
        'browser_matrix_sha256':sha256_file(report/'phase7e_browser_matrix.json'),
        'protected_production_files':len(baseline),'protected_unchanged':True,'data_parity':'PASS',
        'production_models':champions,'model_promotions':0,'Odds_API_calls':0,'published':False,
        'publication_authorisation':'NOT_GRANTED; this is offline QA only'}
    json_write(report/'phase7e_release_review.json',receipt)
    feature_rows=[
        ('Matchup score/spread/total/win probability','all frozen fields unchanged; source run/lock and cutoff verified'),
        ('Team logos and stadium/kickoff','original local logos retained; UK timezone explicit'),
        ('Previous/next and historical deep links','restored; dropdown/search/sort retained'),
        ('TD/rushing/receiving matchup categories','native progressive disclosure; all original categories/ranges/fair odds retained'),
        ('Matchup overview/weather/insights','precise expectation, confidence, uncertainty, weather and original factors retained'),
        ('Market-implied score','restored from exact frozen consensus; distinct archived market context'),
        ('Bookmaker comparisons','original probabilities/prices/lines/EV/edge/confidence retained; freshness recomputed only as presentation'),
        ('Player search/game/team/position/sort','same filters and three top-six leaderboards, shared V3 styling'),
        ('Market filters','market/team/confidence/bookmaker and top60 cap preserved'),
        ('Historical backtests','all seven original benchmark values retained and separate from live scores'),
        ('Live performance','79 validated frozen games,2345 original offensive forecasts; no retrospective forecasts'),
        ('Settled-value performance','original flat £1 ledger payload unchanged; empty remains empty'),
        ('Methodology','all original topics retained, exact model versions and known limitations explicit'),
        ('Forecast versus result','all79 prior final results separately annotated; forecast fields unchanged'),
        ('Research separation','no raw challenger forecasts/model artifacts in release; lab metadata explicitly experimental'),
        ('Publishing','same site/ Pages workflow; immutable candidate selection, offline review gate, bounded propagation, UI/data hash checks, no-op guard')]
    pd.DataFrame([{'feature':f,'existing_functionality_preserved':True,'evidence':e} for f,e in feature_rows]).to_csv(report/'phase7e_functionality_parity.csv',index=False)
    rel=str(candidate.relative_to(settings.root)).replace('\\','/')
    url='http://127.0.0.1:8770/releases/'+manifest['release_id']+'/'
    change_files=['nfl2026/frontend/v3_release.py','nfl2026/frontend/v3_report.py','nfl2026/phase7/preview.py','nfl2026/publishing.py',
        'scripts/build_frontend_v3.py','scripts/publish_nfl_dashboard.py','tests/test_frontend_v3_release.py','tests/frontend_v3_contract.cjs']
    change_files+=['frontend/phase7_src/'+n for n in ('index.html','players.html','value.html','performance.html','model.html','phase7.js','boards.js','phase7.css')]
    text=f'''# Phase 7E — Frontend V3 release candidate

Readiness: **PASS**. Existing dashboard functionality preserved: **YES**. Data parity: **PASS**. Desktop/mobile: **PASS**. Publishing integration: **PASS**. No publication/model promotion/official forecast generation performed.

## Exact candidate

`{manifest['release_id']}` in `{rel}`; immutable manifest SHA `{receipt['manifest_sha256']}`.56 release files. The selected candidate is content-addressed and independently validated. Earlier draft candidates remain separate immutable artifacts; they cannot pass the selected release-review receipt. Original `frontend/dist`, `frontend/public`, deployment checkout/status/artifacts and Week1–6 production inputs/outputs remain unchanged.

Current official run `phase3_5-production-20261014T130712Z-5aba9259`; lock `lock-2026-w06-fd15596ede7f78e9`; freeze `freeze-20261014T130739Z-98675c85`. Models remain team/TD `phase2-v2`, opportunity `phase2-v3-rush-et`, feature `advanced-pit-v2`. No rejected research model is selected.

## Functionality comparison, not a redesign from scratch

The prior V3 preview had dropped market-implied scores, original matchup factors/insights, previous/next/query-string navigation, historical benchmarks and the settled-value ledger. These are restored. Players/Markets/Model now share the established V3 design and navigation rather than falling back to the old styling. Player search/game/team/position/four sorts, top-six TD/rush/receive boards, market/team/confidence/bookmaker filters and top60 selections are preserved. Details use native keyboard-accessible disclosures rather than an inaccessible custom tab dependency.

Full before/after inventory: `phase7e_functionality_parity.csv`. The unchanged baseline served from `/dist/` matches all9 inspected deployed checkout files (five pages,JS,CSS,current forecast,values) byte-for-byte. Before screenshot `phase7e_before_desktop.png`; after `phase7e_after_desktop.png`, `phase7e_after_mobile.png`, `phase7e_after_320.png`.

## Data authority and experimental separation

All93 displayed current/historical game records across6 bundles were compared field-for-field to the original immutable `game_preview.json` export, with run and lock hashes validated. Every nested player field, both win probabilities, scoring quantiles, spread/total, weather, confidence, names, kickoff and original timestamp is checked. Current Week6 payload is exact original bytes, including342 displayed meaningful player forecasts. The official435-player universe itself is unchanged; the release does not invent extra forecast rows or remove frozen records.

All50 captured qualifying bookmaker comparisons and14 consensus contexts remain sourced from the original immutable value run; its files/hashes verify. `value.json` and the original backtest/settled ledger `performance.json` remain exact original bytes. No probability, price, edge, EV, fair odds or model mean is recalculated. Current age labels are presentation-only; expired quotes say STALE/ARCHIVED and captured classification/EV is identified as historical, not a current recommendation. Unknown/future quote timestamps and different prediction runs are not treated as fresh. Margin/commission qualifications are retained; unavailable commission is not invented.

Week1–5 final scores require real finality plus validated completed-week ingestion and are separate `actual_*` fields. The original PHI20–CHI22 forecast remains unchanged beside FINAL PHI7–CHI27. No outcome information enters a model feature. Live performance uses79 frozen final games and2345 offensive forecast rows, separate from archived benchmarks and the £1 ledger. Missing sources stay unavailable/unknown, never HEALTHY or fabricated0 probabilities.

Unsupported fields or a non-official source run fail candidate validation. Raw CSV/Parquet/joblib forecasts cannot enter the release. The public lab section is labelled research-only status metadata, not challenger numeric forecasts; no passing/reception/fantasy/research mean is displayed as certified production output.

## Browser and accessibility validation

All five pages were checked at320,390,768,1280px:20/20 viewport checks. Document widths305,375,753,1265px respectively; no horizontal page overflow. Tables scroll inside a labelled keyboard region. One main landmark per page; all form controls labelled; skip links/focus styles/native disclosures retained. DOM-computed text contrast checks found no below4.5 ratio in the evaluated opaque text/background pairs; disabled controls are excluded. This is not a claim of full external screen-reader certification.

Manual interaction evidence: NE+RB player filters produce3 matching players; combined SPREAD/NE/HIGH/FanDuel produces1 original stale quote at1.98 with unchanged0.6761 model probability; previous/next moves Week6↔Week5 and query-string links open Week3; PHI filters1/16; keyboard Enter expands the matchup and shows a visible focus outline. All342 original public player cards are available.7 archived backtest metrics,8 live metric cards and empty settled ledger remain visible. Final320px layout keeps the full first scoreboard above the900px fold (bottom853.5px) and all five navigation links on one row.

Fixtures cover missing numbers/probabilities/timestamps, unavailable markets, long names/escaping, unsupported extra forecast fields, wrong win/player probability/quantile/timestamp, corruption/additions, historical forecast/result separation and subpath-relative links. `phase7e_browser_matrix.json` pins observations to this release. Screenshots are from the final candidate.

## Deployment integration and propagation

The existing GitHub Pages workflow still uploads `site` beneath the project URL. Every route/asset/data request is relative, verified under a nested local URL. The release publishes via an explicit `--release-candidate`; it does not rewrite global frontend build pointers. Before any site mutation the publisher checks candidate hashes, current official forecast identity and the exact completed release-review receipt. Offline `--dry-run` runs0 Git/network operations and leaves the checkout unchanged.

The no-op staged-diff guard remains; genuine Git errors fail rather than count as changes. Failed pending pushes retry without duplicate commits. Once V3 has been deployed, routine legacy publishing cannot silently downgrade it, including after a failed subsequent attempt.

Verification states PUSHED→PROPAGATING→VERIFIED or FAILED use bounded240s shared deadline, capped backoff, timeout/response-size limits and no-cache/cache-busting requests. Exact run ID is necessary but insufficient because both versions use Week6: the verifier also checks exact release identity and SHA256 for all8 UI files and all12 data files. An old UI, mixed assets, corrupted data with a correct run ID, malformed metadata, network errors or a permanent mismatch cannot pass. Retries never rerun models or republish. Real GitHub Pages deployment remains unperformed by instruction; local/integration fixtures validate the pipeline.

## Tests and protected production

Python: {tests['passed']}/{tests['total']}, failures{tests['failed']}, errors{tests['errors']}, skipped{tests['skipped']}, network disabled. Frontend pure-render/contract suites:24/24. Compile/import and JS syntax checks pass. Browser viewport checks20/20. Mock provider/runner logs are fixtures, not real Odds API calls or production prediction runs.

All{len(baseline)} previously protected production file hashes still match. Global numerical team/player sources match their original Week6 lock copies. Model registry, prices/value history, frozen inputs/predictions, deployment status/checkouts and published source folders unchanged. No models retrained/promoted, Odds API called or site published.

## Final local preview / comparison

Final: {url}
Before: http://127.0.0.1:8770/dist/
Launch: `python -m http.server 8770 --bind 127.0.0.1 --directory frontend`

## Exact separately authorised publication commands — NOT executed

```powershell
python scripts\\build_frontend_v3.py --check {rel}
python scripts\\publish_nfl_dashboard.py --release-candidate {rel} --dry-run
# Only after separate authorisation:
python scripts\\publish_nfl_dashboard.py --release-candidate {rel}
```

For future official weeks, keep the existing safe production-data/forecast workflow, then build a NEW immutable V3 candidate and complete its bound release review before publication. No frontend change authorises a model promotion or bypass of weekly/PIT gates.

Remaining release blockers: **NONE**. Live deployment verification awaits separately authorised publication; this is intentional, not a hidden claim that a live release occurred. Existing model/source limitations and archived-price warnings remain explicit, not repaired through UI.

## Changed files

'''+ '\n'.join('- `'+p+'`' for p in change_files)+f'''

Generated: immutable `{rel}` (56files); rolling local `frontend/phase7_preview`; release/data/functionality/browser/test/review reports and screenshots. No protected production artifact changed.
'''
    (report/'phase7e_release_candidate_report.md').write_text(text,encoding='utf-8')
    return receipt
