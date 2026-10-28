"""Source-only zero-network plan when no certified forecast exists yet."""
from pathlib import Path
import hashlib
import json
import pandas as pd
from nfl2026.markets.providers.the_odds_api import MARKET_KEYS,TheOddsApiProvider
from nfl2026.markets.certified_value import stamp

def source_only_request_plan(settings, snapshot, season, week, now):
    snapshot=Path(snapshot);schedule=snapshot/'games.csv';manifest=snapshot/'snapshot_manifest.json'
    entries=json.loads(manifest.read_text(encoding='utf-8'))
    match=[r for r in entries if r.get('source')=='schedule']
    digest=hashlib.sha256(schedule.read_bytes()).hexdigest()
    if len(match)!=1 or match[0].get('sha256')!=digest: raise ValueError('Local schedule manifest identity differs')
    source=match[0]
    if stamp(source['retrieved_at_utc'])>stamp(now): raise ValueError('Future schedule observation')
    from nfl2026.live.partial_week import classify_game_eligibility
    all_games=pd.read_csv(schedule)
    states=classify_game_eligibility(all_games,season,week,stamp(now).to_pydatetime())
    if states.empty or states.game_id.duplicated().any(): raise ValueError('Missing/ambiguous local fixture population')
    markets=list(MARKET_KEYS)
    groups=TheOddsApiProvider.estimate_cost(','.join(markets),len(settings.market_bookmakers),len(settings.market_regions.split(',')))
    fixtures=[]
    for r in states.to_dict('records'):
        cutoff=stamp(r['kickoff_utc'])-pd.Timedelta(minutes=1)
        fixtures.append(dict(game_id=r['game_id'],away_team=r['away_team'],home_team=r['home_team'],kickoff_utc=r['kickoff_utc'],
            cutoff_utc=cutoff.isoformat(),cached_schedule_state=r['eligibility_state'],provider_event_id=None,
            chargeable_request_authorized=False,comparison_status='WITHHELD_NO_CERTIFIED_PROBABILITY_OR_COMPATIBLE_QUOTE'))
    return dict(schema='source-only-market-request-plan-v2',status='ZERO_NETWORK_UNAUTHORISED_CAPTURE_PLAN',season=season,week=week,
        planned_at_utc=stamp(now).isoformat(),network_requests=0,paid_requests=0,approved_credit_budget=0,capture_authorised=False,
        prediction_run_id=None,official_value_run_id=None,forecast_gate='BLOCKED: historical QB archive and current official source certification',
        fixture_count=len(fixtures),fixtures=fixtures,markets_available_to_parser=MARKET_KEYS,enabled_markets=[],
        proposed_optional_market_keys=['team_totals','player_pass_yds'],provider_default_capture_markets_unchanged=True,
        source=dict(path=str(schedule),sha256=digest,manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),observed_at_utc=source['retrieved_at_utc']),
        bookmakers=list(settings.market_bookmakers),provider_event_ids_discovered=False,
        contingent_all_eight_event_market_credit_upper_bound=len(fixtures)*groups,
        contingent_bound_note='Informational bound for one all-eight-markets event request per fixture using existing provider estimator; not an approved budget or promised coverage. Re-plan exact offered markets and current quota after separate capped approval.',
        execution_requirements=['Separate user approval with a credit cap','Certified original/revised model and exact calibrated market probabilities',
            'Fresh independent fixture state before each paid call','Exact observed provider event mappings; no guessed IDs',
            'Current quota check and reserve','Observed grading rules, executable price and player eligibility','Append-only snapshot and source clocks'],
        local_state_limitation='Cached schedule only; no fresh football observation or pregame clearance. Runtime cutoff always closes elapsed fixtures.')
