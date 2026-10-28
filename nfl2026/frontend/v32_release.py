"""Immutable local V3.2 candidates; never selects, promotes or publishes a release."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,json,shutil
import pandas as pd
from nfl2026.utilities.io import sha256_file

SOURCE_RELEASE='frontend-v3-1-0aa9d638d7215e81'
PAGES={'index.html':('games','Matchups','Your weekly matchup board'),
       'players.html':('players','Players','Player props, clearly sourced'),
       'value.html':('markets','Markets','Prices with the full context'),
       'performance.html':('performance','Performance','The original forecasts, measured'),
       'model.html':('model','Model','What is ready. What is still pending.')}

def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def write(p,x):Path(p).write_text(json.dumps(x,indent=2,ensure_ascii=True,allow_nan=False,default=str),encoding='utf-8')
def filemap(path):return {str(p.relative_to(path)).replace('\\','/'):sha256_file(p) for p in path.rglob('*') if p.is_file() and p.name!='release_manifest.json'}
def digest(files):return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def audit_archived_receivers(root,out,source,payload):
    """Replay original usage bytes, never the now-advanced canonical table."""
    baseline=read(out/'protected_before.json');bindings={};by_hash={}
    for name,digest_value in baseline.items():by_hash.setdefault(digest_value,[]).append(name)
    for name,expected in payload['source_sha256'].items():
        original=root/name
        if original.is_file() and sha256_file(original)==expected:resolved=original
        else:
            choices=[root/n for n in by_hash.get(expected,[]) if (root/n).is_file()]
            if not choices:raise ValueError('Original receiver source archive unavailable: '+name)
            resolved=sorted(choices,key=lambda p:(len(str(p)),str(p)))[0]
            if sha256_file(resolved)!=expected:raise ValueError('Archived receiver source hash differs')
        bindings[name]=dict(path=str(resolved.relative_to(root)),sha256=expected,current_path_matches=resolved==original)
    weekly_name=next(n for n in bindings if n.replace('\\','/').endswith('artifacts/canonical/player_weekly.csv'))
    actual=pd.read_csv(root/bindings[weekly_name]['path'],low_memory=False).rename(columns={'targets':'target_targets','opponent_team':'opponent'})
    rid=payload['prediction_run_id'];run=root/f"predictions/{payload['season']}/week_{payload['week']:02d}"/rid;meta=read(run/'metadata.json')
    lock=root/f"locks/{payload['season']}/week_{payload['week']:02d}"/meta['lock_id']
    from nfl2026.production.core import verify_lock
    if verify_lock(lock):raise ValueError('Original receiver lock integrity failed')
    # Reproduce the original reviewed algorithm's calendar, not a newer lock
    # calendar with additional2026rows. Its coverage is explicit in the UI.
    calendar=root/'artifacts/canonical/canonical_games.csv'
    games=pd.read_csv(calendar,usecols=['game_id','season','week','game_type','home_team','away_team','kickoff_utc'],low_memory=False);games=games[games.game_type.eq('REG')]
    bindings[str(calendar.relative_to(root))]=dict(path=str(calendar.relative_to(root)),sha256=sha256_file(calendar),
        current_path_matches=True,binding_basis='ADDITIONAL_OBSERVED_ORIGINAL_ALGORITHM_CALENDAR_NOT_A_REWRITTEN_OLD_BASELINE')
    mapping=pd.concat([games.rename(columns={'home_team':'team'})[['game_id','season','week','team','kickoff_utc']],games.rename(columns={'away_team':'team'})[['game_id','season','week','team','kickoff_utc']]])
    actual=actual[actual.player_id.notna()].merge(mapping,on=['season','week','team'],validate='many_to_one')
    actual=actual[pd.to_datetime(actual.kickoff_utc,utc=True).lt(pd.Timestamp(meta['created_at_utc']))&((actual.season<payload['season'])|((actual.season==payload['season'])&(actual.week<payload['week'])))]
    players=pd.read_csv(run/'players.csv');players=players[players.position.eq('WR')].merge(pd.read_csv(run/'games.csv')[['game_id','kickoff_utc']],on='game_id',validate='many_to_one')
    from nfl2026.research.review_inputs import prior_target_means
    values=prior_target_means(players,actual[['game_id','player_id','kickoff_utc','target_targets']]);values=values[values.apply(lambda r:f'{r.game_id}|{r.player_id}|{r.team}' in payload['players'],axis=1)]
    from nfl2026.frontend.receiver_discovery import categorize
    rebuilt=categorize(values)
    if len(rebuilt)!=78:raise ValueError('Archived WR coverage differs')
    for r in rebuilt.itertuples():
        old=payload['players'][f'{r.game_id}|{r.player_id}|{r.team}']
        if old['category']!=r.category or old['target_rank']!=r.target_rank:raise ValueError('Original receiver category/rank differs')
        for name in ['prior3_targets','prior8_targets','recent_rank']:
            a=old[name];b=getattr(r,name)
            if (a is None)!=pd.isna(b) or (a is not None and abs(a-float(b))>1e-10):raise ValueError('Original receiver usage differs: '+name)
        if pd.Timestamp(old['usage_known_at_utc'])>pd.Timestamp(old['cutoff_utc']):raise ValueError('Retrospective receiver evidence')
    receipt=dict(status='PASS',receiver_rows=78,original_usage_and_categories_reproduced=True,source_bindings=bindings,
        usage_calendar_max_season=int(games.season.max()),current_year_usage_in_original_signal=False,
        limitation='Original reviewed discovery used a2022–25calendar;2026usage was not matched. Membership and values are preserved as historical signals, not current form.',
        current_global_usage_not_substituted=True,source_release_manifest_sha256=sha256_file(source/'release_manifest.json'))
    write(out/'archived_receiver_contract_audit.json',receipt)
    return receipt

def build_candidate(settings,out):
    root=settings.root;source=root/'frontend/releases'/SOURCE_RELEASE;out=Path(out).resolve();candidate=out/'candidate'
    if not out.is_relative_to(root/'reports') or candidate.exists():raise ValueError('New owned report candidate required')
    from nfl2026.frontend.v3_release import validate_release
    # Validate immutable archived presentation first; current-global usage has
    # legitimately advanced to Week8 and cannot be used to rebuild a Week7 list.
    base=validate_release(source,settings=None,require_current=False)
    receiver_audit=audit_archived_receivers(root,out,source,read(source/'data/receiver_discovery.json'))
    shutil.copytree(source,candidate)
    source_files=filemap(source);original_data={n:h for n,h in source_files.items() if n.startswith('data/')}
    current=read(source/'data/current_week.json');presentation=read(source/'data/player_presentation.json');receivers=read(source/'data/receiver_discovery.json')
    now=datetime.now(timezone.utc).isoformat();games=[];bundles={};raw_counts={};qbs={};source_identity={}
    for path in sorted((source/'data').glob('*.json')):
        if path.name!='current_week.json' and not path.name.startswith('week_'):continue
        payload=read(path);week=int(payload['metadata']['week']);bundles[str(week)]=path.name
        for g in payload['games']:
            gid=g['game_id'];runid=g.get('prediction_source_run_id') or g.get('prediction_run_id') or payload['metadata']['run_id']
            bundle=settings.prediction_root/'2026'/f'week_{week:02d}'/runid;meta=read(bundle/'metadata.json')
            from nfl2026.production.core import validate_bundle
            if not validate_bundle(bundle).passed.all():raise ValueError('Original prediction hash failure')
            original=pd.read_csv(bundle/'games.csv').set_index('game_id').loc[gid]
            if pd.Timestamp(meta['created_at_utc'])>=pd.Timestamp(g['kickoff_utc']):raise ValueError('Forecast publication is post-kickoff')
            for k in ['home_team','away_team','home_expected_points','away_expected_points','home_win_probability','away_win_probability']:
                if g[k]!=original[k] and not (isinstance(g[k],(int,float)) and abs(g[k]-float(original[k]))<1e-10):raise ValueError('Original field differs: '+gid+'/'+k)
            source_identity[str((bundle/'metadata.json').relative_to(root))]=sha256_file(bundle/'metadata.json')
            source_identity[str((bundle/'games.csv').relative_to(root))]=sha256_file(bundle/'games.csv')
            raw_counts[week]=len(pd.read_csv(bundle/'players.csv'))
            lock=root/'locks/2026'/f'week_{week:02d}'/meta['lock_id'];manifest=pd.read_csv(lock/'lock_manifest.csv')
            observed=pd.to_datetime(manifest.retrieved_at_utc,utc=True,errors='coerce').max()
            gmeta=dict(game_id=gid,season=2026,week=week,away_team=g['away_team'],home_team=g['home_team'],kickoff_utc=g['kickoff_utc'],
                prediction_run_id=runid,prediction_lock_id=meta['lock_id'],forecast_created_at_utc=meta['created_at_utc'],
                forecast_source_observed_at_utc=observed.isoformat(),public_player_count=len(g.get('players',[])))
            games.append(gmeta)
            qpath=lock/'inputs'/f'qb_status_2026_w{week:02d}.csv'
            if qpath.is_file():
                source_identity[str(qpath.relative_to(root))]=sha256_file(qpath);qb=pd.read_csv(qpath).fillna('')
                for team in [g['away_team'],g['home_team']]:
                    rows=qb[qb.game_id.eq(gid)&qb.team.eq(team)]
                    if len(rows)==1:
                        r=rows.iloc[0];published=[p for p in g.get('players',[]) if p['team']==team and p['position']=='QB' and p.get('role_status')=='EXPECTED_STARTER']
                        public=published[0] if len(published)==1 else None
                        qbs[gid+'|'+team]=dict(player_id=r.qb_player_id,player_name=r.qb_name,confidence=r.get('confidence','UNKNOWN'),
                            source_state=r.get('source_state','UNKNOWN'),medical_status=r.get('injury_status','UNKNOWN') or 'UNKNOWN',
                            expected_status=r.get('status','UNKNOWN'),starter_confirmed=str(r.get('starter_confirmed')).lower()=='true',
                            observed_at_utc=r.get('retrieved_at_utc'),known_at_utc=r.get('known_at_utc'),basis='ORIGINAL_FORECAST_QB_SOURCE',
                            published_player_id=public['player_id'] if public else None,published_player_name=public['player_name'] if public else None,
                            identity_conflict=bool(public and public['player_id']!=r.qb_player_id),gameday_active_confirmed=False)
    if len(games)!=107 or len({g['game_id'] for g in games})!=107:raise ValueError('Original historical fixture coverage differs')
    # Independent validated results are sidecars; frozen public payloads remain exact bytes.
    schedule_path=root/'data/snapshots/2026/week_08/20261027T142113Z/games.csv'
    from nfl2026.live.pipeline import normalize_schedule_kickoffs
    schedule=normalize_schedule_kickoffs(pd.read_csv(schedule_path,low_memory=False)).set_index('game_id')
    source_identity[str(schedule_path.relative_to(root))]=sha256_file(schedule_path)
    w7actual=root/'artifacts/actuals/2026/week_07/actual-dd8a5ce2c95a/team_actuals.csv';actual=pd.read_csv(w7actual).set_index('game_id')
    source_identity[str(w7actual.relative_to(root))]=sha256_file(w7actual)
    states={}
    for g in games:
        r=schedule.loc[g['game_id']]
        if any(r[k]!=g[k] for k in ['away_team','home_team']) or pd.isna(r.home_score) or pd.isna(r.away_score):raise ValueError('Final result identity incomplete')
        if g['week']==7:
            original_actual=actual.loc[g['game_id']]
            if (r.home_score,r.away_score)!=(original_actual.home_score,original_actual.away_score):raise ValueError('Week7 immutable actual differs')
        states[g['game_id']]=dict(state='FINAL',away_team=g['away_team'],home_team=g['home_team'],kickoff_utc=g['kickoff_utc'],
            actual_away_score=int(r.away_score),actual_home_score=int(r.home_score),result_source_sha256=sha256_file(w7actual if g['week']==7 else schedule_path),
            observed_at_utc='2026-10-27T14:21:13.001005+00:00',basis='VALIDATED_IMMUTABLE_FINAL_RESULT')
    readiness_path=root/'reports/week8_phase1_certification_20261027T135222Z/fixture_readiness.json';readiness=read(readiness_path)
    source_identity[str(readiness_path.relative_to(root))]=sha256_file(readiness_path)
    upcoming=[]
    for r in readiness['fixtures']:
        g=schedule.loc[r['game_id']]
        upcoming.append(dict(game_id=r['game_id'],season=2026,week=8,away_team=g.away_team,home_team=g.home_team,kickoff_utc=r['kickoff_utc'],stadium=g.stadium,
            forecast_status='NOT_CERTIFIED',players=[],home_expected_points=None,away_expected_points=None,home_win_probability=None,away_win_probability=None,
            model_margin=None,model_total=None,display_home_score=None,display_away_score=None))
        states[r['game_id']]=dict(state='PRE_KICKOFF',away_team=g.away_team,home_team=g.home_team,kickoff_utc=r['kickoff_utc'],observed_at_utc=readiness['source_observation_utc'],basis='CACHED_SCHEDULE_NOT_LIVE_FEED')
        for qb in r['quarterbacks']:
            qbs[r['game_id']+'|'+qb['team']]=dict(player_id=qb.get('qb_player_id'),player_name=qb.get('qb_name'),confidence=qb.get('confidence','UNKNOWN'),
                expected_status=qb.get('status','UNKNOWN'),medical_status=qb.get('injury_status','UNKNOWN'),source_state=qb.get('source_state','UNKNOWN'),
                identity_conflict=not qb.get('schedule_depth_agreement',False),starter_confirmed=False,gameday_active_confirmed=False,
                known_at_utc=qb.get('known_at_utc'),observed_at_utc=qb.get('retrieved_at_utc'),basis='EARLY_WEEK_EXPECTATION_NOT_A_FORECAST')
    write(candidate/'data/dashboard_context_v32.json',dict(schema='dashboard-context-v32',generated_at_utc=now,fixture_states=states,forecast_sources={g['game_id']:g for g in games},
        qbs=qbs,week8_fixtures=upcoming,week8_status='NO_CERTIFIED_OFFICIAL_FORECAST',quarter_status='NOT_CERTIFIED',passing_status='NOT_CERTIFIED',
        status_source_observed_at_utc='2026-10-27T14:21:13.001005+00:00',source_hashes=source_identity,
        no_new_availability_refresh=True,no_live_score_feed=True,receiving_signal_max_source_season=receiver_audit['usage_calendar_max_season'],
        receiving_signal_is_current_form=False,public_players=len([p for g in current['games'] for p in g['players']]),underlying_player_forecasts=raw_counts[7]))
    perf_path=root/'reports/team_phase7_20261027T185920Z/validated/official_weeks1_7_metrics.csv';perf=pd.read_csv(perf_path)
    write(candidate/'data/official_performance_v32.json',dict(schema='official-team-performance-v32',source_path=str(perf_path.relative_to(root)),source_sha256=sha256_file(perf_path),
        metrics=json.loads(perf.to_json(orient='records')),basis='ORIGINAL_OFFICIAL_PREDICTIONS_ONLY',player_cohort_evaluation_not_combined=True))
    # Current passing/quarter research is withheld; no research numbers enter this release.
    write(candidate/'data/certified_extensions_v32.json',dict(schema='certified-extensions-v32',passing=dict(status='NOT_CERTIFIED',predictions=[],reason='The passing challenger has not passed production and calibration gates.'),
        quarters=dict(status='NOT_CERTIFIED',rows=[],reason='A certified regulation-score parent and quarter allocation approval are required.'),
        value=dict(schema='certified-value-feed-v2',selections=[],actionable_count=0,reason='No current grading-compatible quote and approved calibrated probability package.')))
    # Raw captured lines are price observations, never new model/value predictions.
    from nfl2026.markets.core import map_fixtures,map_players
    from nfl2026.markets.core import validate_market_snapshot
    snapshot=root/'markets/snapshots/2026/week_07/20261025T112245Z-4cf149bf';validate_market_snapshot(snapshot,2026,7)
    quotes=pd.read_csv(snapshot/'market_quotes.csv');frame=pd.DataFrame(current['games']);players=pd.DataFrame([p for g in current['games'] for p in g['players']])
    mapped=map_players(map_fixtures(quotes,frame),players)
    mapped=mapped[mapped.fixture_mapping_status.eq('MAPPED_EXACT')].copy()
    lines={};player_prices={}
    for gid,g in mapped.groupby('game_id'):
        gm=next(x for x in current['games'] if x['game_id']==gid)
        home=g[g.market_type.eq('SPREAD')&g.selection_team.eq(gm['home_team'])];totals=g[g.market_type.eq('TOTAL')&g.side.eq('OVER')]
        lines[gid]=dict(home_handicap=float(home.line.median()) if len(home) else None,total=float(totals.line.median()) if len(totals) else None,
            bookmaker_count=int(g.bookmaker.nunique()),source_timestamp_utc=g.source_timestamp_utc.max(),retrieval_timestamp_utc=g.retrieval_timestamp_utc.max(),
            status='HISTORICAL_EXPIRED',snapshot_id=snapshot.name,quote_certification='GRADING_AND_EXECUTION_UNVERIFIED')
    props=mapped[mapped.market_type.isin(['ANYTIME_TD','RUSHING_YARDS','RECEIVING_YARDS','PASSING_YARDS'])&mapped.player_mapping_status.eq('MAPPED_EXACT')]
    for (gid,pid,market),g in props.groupby(['game_id','player_id','market_type']):
        r=g.sort_values(['bookmaker','source_timestamp_utc','quote_id'],ascending=[True,False,True]).iloc[0]
        player_prices[gid+'|'+str(pid)+'|'+market]=dict(bookmaker=r.bookmaker,line=float(r.line) if pd.notna(r.line) else None,side=r.side,decimal_odds=float(r.decimal_odds),
            source_timestamp_utc=r.source_timestamp_utc,retrieval_timestamp_utc=r.retrieval_timestamp_utc,quote_id=r.quote_id,status='HISTORICAL_EXPIRED',
            comparison='CAPTURED_PRICE_ONLY_NO_PROBABILITY_OR_EV',calibration_verified=False)
    write(candidate/'data/market_observations_v32.json',dict(schema='captured-market-lines-v32',snapshot_id=snapshot.name,snapshot_metadata_sha256=sha256_file(snapshot/'metadata.json'),
        fixtures=lines,players=player_prices,paid_requests=0,new_value_run=False,market_implied_scores_are_model_predictions=False))
    for name in ['v32.js','v32.css']:
        shutil.copy2(root/'frontend/v32_src'/name,candidate/name)
    data_hashes={str(p.relative_to(candidate)).replace('\\','/'):sha256_file(p) for p in (candidate/'data').glob('*.json')}
    data_manifest=dict(schema='dashboard-manifest-v32',frontend_version='3.2',base_release_id=base['release_id'],base_manifest_sha256=sha256_file(source/'release_manifest.json'),
        data_sha256=data_hashes,bundles=bundles,default_week=7,available_weeks=[1,2,3,4,5,6,7,8],availability_path='data/current_availability.json',
        original_data_sha256=original_data,original_prediction_run_id=current['metadata']['run_id'],historical_fixture_count=107,publication=False)
    write(candidate/'data/dashboard_manifest_v32.json',data_manifest);manifest_sha=sha256_file(candidate/'data/dashboard_manifest_v32.json')
    shell=(root/'frontend/v32_src/shell.html').read_text(encoding='utf-8')
    for filename,(page,label,title) in PAGES.items():
        nav=''.join(f'<a href="{n}"'+(' aria-current="page"' if n==filename else '')+f'>{info[1]}</a>' for n,info in PAGES.items())
        html=shell.replace('{{PAGE}}',page).replace('{{TITLE}}',title).replace('{{NAV}}',nav).replace('{{MANIFEST_SHA}}',manifest_sha)
        for asset in ['phase7.css','phase7.js','availability_overlay.js','v32.js','v32.css']:html=html.replace('{{'+asset+'}}',sha256_file(candidate/asset)[:16])
        (candidate/filename).write_text(html,encoding='utf-8')
    files=filemap(candidate);rid='frontend-v3-2-'+digest(files)[:16]
    write(candidate/'release_manifest.json',dict(schema='frontend-v32-release-v1',frontend_version='3.2',release_id=rid,content_sha256=digest(files),created_at_utc=now,
        files=files,base_release_id=base['release_id'],base_manifest_sha256=sha256_file(source/'release_manifest.json'),original_data_sha256=original_data,
        prediction_run_id=current['metadata']['run_id'],candidate_only=True,publication_authorised=False,original_forecast_replacement=False))
    write(out/'candidate_identity.json',dict(release_id=rid,path=str(candidate.relative_to(root)),manifest_sha256=sha256_file(candidate/'release_manifest.json'),
        data_manifest_sha256=manifest_sha,base_release_id=base['release_id'],published=False))
    return candidate

def validate_candidate(candidate,root):
    candidate=Path(candidate);root=Path(root);manifest=read(candidate/'release_manifest.json');files=filemap(candidate)
    if files!=manifest['files'] or digest(files)!=manifest['content_sha256'] or manifest['release_id']!='frontend-v3-2-'+digest(files)[:16]:raise ValueError('V3.2 immutable candidate drift')
    base=root/'frontend/releases'/SOURCE_RELEASE;original_data={n:h for n,h in read(base/'release_manifest.json')['files'].items() if n.startswith('data/')}
    if manifest['original_data_sha256']!=original_data or manifest['base_manifest_sha256']!=sha256_file(base/'release_manifest.json'):raise ValueError('Reviewed base identity changed')
    if manifest.get('candidate_only') is not True or manifest.get('publication_authorised') is not False or manifest.get('original_forecast_replacement') is not False:raise ValueError('Candidate authorisation contract changed')
    for n,h in manifest['original_data_sha256'].items():
        if sha256_file(candidate/n)!=h:raise ValueError('Original public forecast/data changed')
    if sha256_file(candidate/'availability_overlay.js')!=sha256_file(base/'availability_overlay.js'):raise ValueError('Reviewed availability adapter changed')
    data_manifest=read(candidate/'data/dashboard_manifest_v32.json')
    if data_manifest['original_data_sha256']!=original_data:raise ValueError('Data manifest original scope changed')
    for n,h in data_manifest['data_sha256'].items():
        if sha256_file(candidate/n)!=h:raise ValueError('Runtime data fingerprint differs')
    for n in PAGES:
        if sha256_file(candidate/'data/dashboard_manifest_v32.json') not in (candidate/n).read_text(encoding='utf-8'):raise ValueError('HTML bootstrap data identity differs')
    if any(p.suffix in {'.joblib','.parquet','.csv'} for p in candidate.rglob('*')):raise ValueError('Raw/research artifact in release')
    current=read(candidate/'data/current_week.json');players=[p for g in current['games'] for p in g['players']]
    if len(current['games'])!=14 or len(players)!=333 or len({(p['game_id'],p['player_id'],p['team']) for p in players})!=333:raise ValueError('Original population drift')
    receivers=read(candidate/'data/receiver_discovery.json')
    if len(receivers['players'])!=78:raise ValueError('78WR receiver contract changed')
    ext=read(candidate/'data/certified_extensions_v32.json')
    if ext['passing']['predictions'] or ext['quarters']['rows'] or ext['value']['selections']:raise ValueError('Unapproved extension numbers entered candidate')
    context=read(candidate/'data/dashboard_context_v32.json')
    if context['underlying_player_forecasts']!=429 or len(context['week8_fixtures'])!=14:raise ValueError('Underlying/candidate fixture scope differs')
    raw=pd.read_csv(root/'predictions/2026/week_07'/current['metadata']['run_id']/'players.csv',keep_default_na=False).set_index(['game_id','player_id','team'],verify_integrity=True)
    for p in players:
        original=raw.loc[(p['game_id'],p['player_id'],p['team'])]
        for k,v in p.items():
            if k in ['game_id','player_id','team']:continue
            expected=original[k]
            if isinstance(v,(float,int)) and not isinstance(v,bool):
                if not isinstance(expected,(float,int)) or abs(v-expected)>1e-9:raise ValueError('Original player number changed: '+k)
            elif v!=expected:raise ValueError('Original player identity/status changed: '+k)
    for n,h in context['source_hashes'].items():
        if not (root/n).is_file() or sha256_file(root/n)!=h:raise ValueError('Context source identity changed: '+n)
    schedule=pd.read_csv(root/'data/snapshots/2026/week_08/20261027T142113Z/games.csv',low_memory=False).set_index('game_id')
    if len(context['forecast_sources'])!=107 or len(context['fixture_states'])!=121:raise ValueError('Historical/current fixture scope differs')
    for gid,original in context['forecast_sources'].items():
        r=schedule.loc[gid];state=context['fixture_states'][gid]
        if state['state']!='FINAL' or any(state[k]!=r[k] for k in ['home_team','away_team']) or (state['actual_home_score'],state['actual_away_score'])!=(r.home_score,r.away_score):raise ValueError('Actual result identity differs: '+gid)
    if any(g.get('forecast_status')!='NOT_CERTIFIED' or g['players'] or any(g.get(k) is not None for k in ['home_expected_points','away_expected_points','home_win_probability','away_win_probability','model_margin','model_total']) for g in context['week8_fixtures']):raise ValueError('Uncertified Week8 numbers')
    if any(context['fixture_states'][g['game_id']]['state']!='FINAL' for g in current['games']):raise ValueError('Week7 final state missing')
    return dict(status='PASS',release_id=manifest['release_id'],fixtures=14,published_players=333,original_forecasts=429,eligible_WRs=78,historical_fixtures=107,week8_fixtures=14,
        original_payloads_byte_identical=True,passing_predictions=0,quarter_predictions=0,actionable_values=0,publication_authorised=False)

def refresh_candidate_ui(settings,out,previous):
    """A new immutable UI iteration from validated candidate data, no recapture."""
    root=settings.root;out=Path(out).resolve();previous=Path(previous).resolve();candidate=out/'candidate'
    if not out.is_relative_to(root/'reports') or not previous.is_relative_to(out) or candidate.exists():raise ValueError('New owned candidate path required')
    validate_candidate(previous,root);old=read(previous/'release_manifest.json');shutil.copytree(previous,candidate)
    for name in ['v32.js','v32.css']:shutil.copy2(root/'frontend/v32_src'/name,candidate/name)
    manifest_sha=sha256_file(candidate/'data/dashboard_manifest_v32.json');shell=(root/'frontend/v32_src/shell.html').read_text(encoding='utf-8')
    for filename,(page,label,title) in PAGES.items():
        nav=''.join(f'<a href="{n}"'+(' aria-current="page"' if n==filename else '')+f'>{info[1]}</a>' for n,info in PAGES.items())
        html=shell.replace('{{PAGE}}',page).replace('{{TITLE}}',title).replace('{{NAV}}',nav).replace('{{MANIFEST_SHA}}',manifest_sha)
        for asset in ['phase7.css','phase7.js','availability_overlay.js','v32.js','v32.css']:html=html.replace('{{'+asset+'}}',sha256_file(candidate/asset)[:16])
        (candidate/filename).write_text(html,encoding='utf-8')
    files=filemap(candidate);rid='frontend-v3-2-'+digest(files)[:16]
    write(candidate/'release_manifest.json',dict(old,release_id=rid,content_sha256=digest(files),files=files,created_at_utc=datetime.now(timezone.utc).isoformat(),prior_candidate_release_id=old['release_id']))
    write(out/'candidate_identity.json',dict(release_id=rid,path=str(candidate.relative_to(root)),manifest_sha256=sha256_file(candidate/'release_manifest.json'),data_manifest_sha256=manifest_sha,base_release_id=old['base_release_id'],published=False))
    result=validate_candidate(candidate,root);write(out/'candidate_validation.json',result);return result
