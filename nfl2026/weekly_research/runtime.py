"""Reuse fixed Phase14 models/providers, with no full-week-unplayed dependency."""
from pathlib import Path
from dataclasses import replace
from datetime import timedelta
from urllib.parse import urljoin,urlparse
import json,shutil,math
from .core import TEAM_SHA,PLAYER_SHA,FEATURES,ADVERSE,clock,utc,canonical,sha,digest,immutable,load

def values(row):return {f:None if row[f]!=row[f] else float(row[f]) for f in FEATURES}

def fixture_state(row,observed):
    """Explicit live/ambiguous signals outrank score presence or cached clocks."""
    import pandas as pd
    status=next((str(row.get(k)).strip().upper().replace(' ','_') for k in ['game_status','status','game_state','state'] if pd.notna(row.get(k)) and str(row.get(k)).strip()),'')
    live={'LIVE','IN_PROGRESS','INPROGRESS','HALFTIME','DELAYED','SUSPENDED'};final={'FINAL','FINAL/OT','FINAL_OT','COMPLETE','COMPLETED','CLOSED'};pre={'SCHEDULED','PRE','PREGAME','NOT_STARTED','SCHEDULED_TO_START'}
    if status in live:return 'LIVE'
    if status and status not in final|pre:return 'AMBIGUOUS'
    kick=row.get('kickoff_utc');h=row.get('home_score');a=row.get('away_score');both=pd.notna(h) and pd.notna(a);some=pd.notna(h) or pd.notna(a)
    if pd.isna(kick):return 'AMBIGUOUS'
    if status in final:return 'FINAL' if both and utc(observed)>=utc(kick) else 'AMBIGUOUS'
    if both:return 'FINAL' if utc(observed)>=utc(kick) and status not in pre else 'AMBIGUOUS'
    if some:return 'LIVE'
    return 'LIVE' if utc(observed)>=utc(kick) else 'PRE_KICKOFF'

class FixedInference:
    def __init__(self,bundle):
        self.bundle=Path(bundle);self.calls=0
        if sha(self.bundle/'models/team.joblib')!=TEAM_SHA or sha(self.bundle/'models/player.joblib')!=PLAYER_SHA:raise ValueError('Fixed model bundle changed')
        replay=self.bundle/'seed/team_replay.json'
        if not replay.exists():raise ValueError('Fixed first-issue parity replay absent')
        import joblib,pandas as pd,numpy as np
        from nfl2026.research.causal_baseline import predict_research
        records=load(replay);frame=pd.DataFrame([{f:r[f] for f in FEATURES} for r in records]);h,a,_=predict_research(joblib.load(self.bundle/'models/team.joblib'),frame)
        if not np.allclose(h,[r['expected_home'] for r in records],rtol=0,atol=1e-10) or not np.allclose(a,[r['expected_away'] for r in records],rtol=0,atol=1e-10):raise ValueError('Saved first-issue inference parity differs on this host')
    def __call__(self,game,x,players,packet,now):
        import joblib,pandas as pd,numpy as np
        from nfl2026.research.causal_baseline import predict_research
        from nfl2026.research.prospective_shadow import raw_player_predictions
        result={};self.calls+=1
        if utc(now)>=utc(game['cutoff_utc']) or game['state']!='PRE_KICKOFF':raise ValueError('Inference after cutoff forbidden')
        if x is not None:
            if set(x)!=set(FEATURES):raise ValueError('Exact causal7-feature schema required')
            model=joblib.load(self.bundle/'models/team.joblib');frame=pd.DataFrame([{f:np.nan if x[f] is None else x[f] for f in FEATURES}])
            # Genuine QB-neutral existing model: exact feature/weight identity,
            # no QB selection feature. No invented QB-specific adjustment.
            h,a,_=predict_research(model,frame);h2,a2,_=predict_research(model,frame.copy())
            if not np.array_equal(h,h2) or not np.array_equal(a,a2):raise ValueError('Nonreproducible inference')
            result.update(home_expected_points=float(h[0]),away_expected_points=float(a[0]))
        if players is not None:
            provenance=packet.get('player_input_provenance',{}).get(game['game_id'],{})
            if provenance.get('status')!='EXISTING_CONDITIONAL_PLAYER_ROLLFORWARD' or not utc(provenance['known_at_utc'])<=utc(now)<utc(game['cutoff_utc']):raise ValueError('Player feature provenance/chronology absent')
            if not all(len(str(provenance.get(k,'')))==64 for k in ['sha256','actual_manifest_sha256','feature_manifest_sha256']):raise ValueError('Player feature manifests absent')
            model=joblib.load(self.bundle/'models/player.joblib');p=pd.DataFrame(players);g=pd.DataFrame([game]);out=raw_player_predictions(model,p,g)
            result['players']=json.loads(out.to_json(orient='records',date_format='iso'));assert len(out)==len(p)
        if clock()>=utc(game['cutoff_utc']):raise ValueError('Cutoff crossed during inference; no pregame candidate')
        return result

def capture(root,bundle,season,week,*,reuse_from=None):
    """Bounded authoritative capture + existing normalizers, isolated filesystem.

    No official locks, gate changes, market calls or inference. Missing sources
    remain missing. The original whole-week staging function is not called.
    """
    import pandas as pd
    from lxml import html
    from nfl2026.config import load_settings
    from nfl2026.live.pipeline import download_live_sources,normalize_schedule_kickoffs,_roster_for_week,ASSETS
    from nfl2026.live.providers.base import fetch_bytes,parse_nflverse_timestamp
    from nfl2026.live.providers.depth import load_depth
    from nfl2026.live.providers.injuries import load_injuries,merge_official_supplement
    from nfl2026.live.providers.official_injuries import load_official_injury_tables
    from nfl2026.live.providers.club_availability import parse_report
    from nfl2026.live.providers.qb_evidence import CLUB_HOSTS,load_evidence,reconcile_quarterbacks
    from nfl2026.live.providers.quarterbacks import load_quarterbacks
    from nfl2026.live.providers.transactions import load_transactions,overlay_transactions_on_roster
    from nfl2026.live.providers.club_transactions import load_sources as load_tx
    from nfl2026.live.providers.official_roster_identity import merge_sources as merge_roster
    from nfl2026.live.availability import apply_current_sources
    root=Path(root);root.mkdir(parents=True,exist_ok=False);bundle=Path(bundle);workspace=root/'workspace';(workspace/'config').mkdir(parents=True)
    for p in (bundle/'config').glob('*.json'):shutil.copyfile(p,workspace/'config'/p.name)
    settings=replace(load_settings(bundle/'config/nfl2026.toml'),root=workspace,artifact_root=workspace/'artifacts',report_root=workspace/'reports',prediction_root=workspace/'predictions')
    if reuse_from:
        shutil.copytree(Path(reuse_from)/'workspace/data',workspace/'data');raw=next((workspace/'data/live_downloads'/str(season)).iterdir())
    else:raw=download_live_sources(settings,season,week=week)
    notes=[];http=[];availability=load(raw/'availability_sources.json');qb_sources=load(raw/'qb_evidence_sources.json')
    games=normalize_schedule_kickoffs(pd.read_csv(raw/'games.csv',encoding='utf-8',low_memory=False));current=games[games.season.eq(season)&games.week.eq(week)&games.game_type.eq('REG')];teams=sorted(set(current.home_team)|set(current.away_team));start=clock()
    for team in ([] if reuse_from else teams):
        if not any(start<utc(g.kickoff_utc)-timedelta(minutes=1) for g in current[(current.home_team.eq(team))|current.away_team.eq(team)].itertuples()):continue
        host=CLUB_HOSTS[team];host=host if host.startswith('www.') else 'www.'+host;url='https://'+host+'/news/'
        try:
            b=fetch_bytes(url,attempts=1,timeout_seconds=12);observed=clock().isoformat();name='news_'+team+'.html';(raw/name).write_bytes(b);http.append(dict(url=url,filename=name,observed_at_utc=observed,sha256=sha(raw/name)));tree=html.fromstring(b)
            links=[]
            for href in tree.xpath('//a/@href'):
                u=urljoin(url,href);label=u.lower()
                if urlparse(u).hostname==host and '/news/' in label and not urlparse(u).query and not urlparse(u).fragment and ('week-'+str(week) in label or start.strftime('%b').lower()+'-'+str(start.day)+'-'+str(start.year) in label) and any(w in label for w in ['injur','inactive','quarterback','jackson','huntley']) and u not in links:links.append(u)
            for u in links[:2]:
                b=fetch_bytes(u,attempts=1,timeout_seconds=12);observed=clock().isoformat();name='discovered_'+team+'_'+str(len(http))+'.html';(raw/name).write_bytes(b);t=html.fromstring(b)
                articles=[]
                for v in t.xpath('//script[@type="application/ld+json"]/text()'):
                    obj=json.loads(v);articles.extend(a for a in (obj if isinstance(obj,list) else obj.get('@graph',[obj])) if a.get('@type')=='NewsArticle')
                http.append(dict(url=u,filename=name,observed_at_utc=observed,sha256=sha(raw/name),article_clocks=[{k:a.get(k) for k in ['datePublished','dateModified','headline']} for a in articles]))
                f=current[current.home_team.eq(team)|current.away_team.eq(team)].iloc[0];kind='INACTIVES' if 'inactive' in u else 'FINAL_REPORT' if any('game status' in ' '.join(n.itertext()).lower() for n in t.xpath('//th')) else 'PRACTICE_REPORT'
                s=dict(season=season,week=week,game_id=f.game_id,team=team,teams=[f.away_team,f.home_team],kind=kind,url=u,filename=name,sha256=sha(raw/name),retrieved_at_utc=observed)
                if kind=='INACTIVES':availability['sources'].append(s);continue
                if any(w in u for w in ['jackson','huntley','quarterback']):qb_sources['sources'].append({**s,'kind':'CONTEXT_ONLY'})
                else:availability['sources'].append(s)
        except Exception as exc:notes.append(dict(team=team,error=str(exc)))
    # Keep original capture manifests as evidence before deriving discovered routes.
    if not (raw/'availability_sources_original.json').exists():shutil.copyfile(raw/'availability_sources.json',raw/'availability_sources_original.json')
    (raw/'availability_sources.json').write_bytes(canonical(availability));(raw/'qb_evidence_sources.json').write_bytes(canonical(qb_sources));immutable(root/'discovery_receipts.json',dict(new_discovery_requests=http,reused_from=str(reuse_from) if reuse_from else None,original_clocks_retained=True))
    now=clock();roster,_,_= _roster_for_week(pd.read_csv(raw/f'weekly_roster_{season}.csv',encoding='utf-8',low_memory=False),season,week,set(teams));roster=merge_roster(raw,roster,now)
    injuries=load_injuries(pd.read_csv(raw/f'injuries_{season}.csv',encoding='utf-8',low_memory=False),current,season,week,parse_nflverse_timestamp(raw/'injuries_timestamp.json'),now)
    try:injuries=merge_official_supplement(injuries,load_official_injury_tables((raw/'nfl_injuries.html').read_bytes(),roster,season,week,now),now)
    except ValueError as exc:notes.append(dict(source='NFL injury route',rejected=str(exc)))
    frames=[];coverage={}
    strict=[]
    for source in availability['sources']:
        if source.get('kind')=='INACTIVES':
            try:
                from .own_inactives import extract
                g=current[current.game_id.eq(source['game_id'])].iloc[0].to_dict();strict.extend(extract((raw/source['filename']).read_bytes(),source,roster,g,now))
            except (ValueError,KeyError,TypeError,IndexError) as exc:notes.append(dict(url=source['url'],reason='Strict own-list quarantine: '+str(exc)))
            continue
        try:
            event,states=parse_report((raw/source['filename']).read_bytes(),source,roster,current,now);frames.append(event)
            for key,value in states.items():
                previous=coverage.get(key,{})
                if not previous or value.get('complete_final') or not previous.get('complete_final'):coverage[key]=value
        except (ValueError,KeyError,TypeError) as exc:notes.append(dict(url=source['url'],rejected=str(exc)))
    events=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame();events=events[~events.get('availability_status',pd.Series('',index=events.index)).eq('INACTIVE')]
    injuries=apply_current_sources(injuries,events,coverage,current,now)
    meta=load(raw/'retrieval.json');pages={p.name:p.read_bytes() for p in raw.glob('nfl_transactions_*_page*.html')};categories={r['category'] for r in meta.get('official_transaction_categories',[]) if r.get('complete')}
    tx=load_transactions(pd.read_csv(raw/'trades.csv',encoding='utf-8'),season,now,parse_nflverse_timestamp(raw/'trades_timestamp.json'),now,pages,categories,load_tx(raw,season,now));roster,_=overlay_transactions_on_roster(roster,tx.records,games,cutoff=now,roster_known_at=parse_nflverse_timestamp(raw/'weekly_rosters_timestamp.json'))
    dep=load_depth(pd.read_csv(raw/f'depth_charts_{season}.csv',encoding='utf-8',low_memory=False),set(teams),now,now,season,roster=roster,injuries=injuries.records,transactions=tx.records,transaction_coverage=tx.coverage.assign(known_at_utc=tx.source_known_at_utc,retrieved_at_utc=tx.retrieved_at_utc))
    expected=reconcile_quarterbacks(current,roster,dep.records,injuries.records,week,now,load_evidence(raw,roster,games,now));qbs=load_quarterbacks(expected,roster,dep.records,week,now,injuries.records)
    out=root/'normalised';out.mkdir()
    for name,frame in [('roster',roster),('injuries',injuries.records),('qbs',qbs.records),('transactions',tx.records),('depth',dep.records)]:frame.to_csv(out/(name+'.csv'),index=False)
    # Reobserve independent schedule last: a long capture cannot use its start
    # clock to extend eligibility. Keep each original request's clock unchanged.
    b=fetch_bytes(ASSETS['games.csv'],attempts=1,timeout_seconds=20);observed=clock();(root/'independent_schedule.csv').write_bytes(b)
    games=normalize_schedule_kickoffs(pd.read_csv(root/'independent_schedule.csv',encoding='utf-8',low_memory=False));packet=packet_from_frames(games,roster,injuries.records,qbs.records,tx.records,season,week,observed,sha(root/'independent_schedule.csv'),raw);packet['evidence'].extend(strict)
    immutable(root/'packet.json',packet);immutable(root/'capture_receipt.json',dict(status='CAPTURED_NORMALISED_RESEARCH_NOT_OFFICIAL',started_at_utc=start.isoformat(),completed_at_utc=clock().isoformat(),paid_requests=0,discovery_requests=len(http),limitations=notes,files={str(p.relative_to(root)).replace('\\','/'):sha(p) for p in root.rglob('*') if p.is_file()}));return packet

def packet_from_frames(games,roster,injuries,qbs,transactions,season,week,observed,schedule_sha,raw):
    import pandas as pd
    from nfl2026.research.causal_baseline import score_history_inputs
    selected=games[games.season.eq(season)&games.week.eq(week)&games.game_type.eq('REG')];history=games[games.season.ge(2022)&games.game_type.eq('REG')].copy()
    for i,row in history.iterrows():
        if fixture_state(row,observed)!='FINAL':history.loc[i,['home_score','away_score']]=float('nan')
    x,lineage=score_history_inputs(history,asof=observed);inputs={r.game_id:values(r._asdict()) for r in x[x.game_id.isin(selected.game_id)].itertuples()};fixtures=[];evidence=[]
    raw=Path(raw);hashes={sha(p):p.name for p in raw.glob('*') if p.is_file()}
    def valid_source(row):
        h=row.get('practice_source_sha256') or row.get('source_sha256')
        return h if h in hashes else None
    for g in selected.itertuples():
        state=fixture_state(g._asdict(),observed)
        fixtures.append(dict(game_id=g.game_id,away_team=g.away_team,home_team=g.home_team,kickoff_utc=g.kickoff_utc.isoformat(),cutoff_utc=(g.kickoff_utc-pd.Timedelta(minutes=1)).isoformat(),state=state,actual_home_score=float(g.home_score) if pd.notna(g.home_score) else None,actual_away_score=float(g.away_score) if pd.notna(g.away_score) else None))
        for q in qbs[qbs.game_id.eq(g.game_id)].fillna('').to_dict('records'):
            evidence.append(dict(type='QB',game_id=g.game_id,team=q['team'],expected_qb_id=q['qb_player_id'],expected_qb_name=q['qb_name'],status='EXPECTED_STARTER' if q['source_state']=='VALID' else 'SOURCE_CONFLICT',medical_status=q.get('injury_status','UNKNOWN'),source_url=q.get('source','NFLverse depth/roster plus official reconciliation'),source_sha256=sha(raw/f'depth_charts_{season}.csv'),published_at_utc=None,modified_at_utc=None,observed_at_utc=observed.isoformat(),validation='EXACT_SOURCE_IDENTITY_VALIDATED',QB_neutral_team_model=True,starter_confirmed=False))
        for r in injuries[injuries.team.isin([g.home_team,g.away_team])].fillna('').to_dict('records'):
            status=r.get('game_status','UNKNOWN');h=valid_source(r)
            if r.get('record_type')!='PLAYER_REPORT' or r.get('source_state')!='VALID' or status not in ADVERSE|{'QUESTIONABLE','DOUBTFUL'} or status=='INACTIVE' or not h:continue
            proof=roster[roster.team.eq(r['team'])&roster.gsis_id.astype(str).eq(str(r['player_id']))]
            if len(proof)!=1 or proof.iloc[0].full_name!=r['player_name']:continue
            evidence.append(dict(type='AVAILABILITY',game_id=g.game_id,team=r['team'],player_id=r['player_id'],name=r['player_name'],status=status,source_url=r.get('practice_source_url') or r.get('source_url'),source_sha256=h,published_at_utc=r.get('practice_published_at_utc') or r.get('published_at_utc') or None,modified_at_utc=r.get('practice_modified_at_utc') or None,observed_at_utc=r.get('practice_observed_at_utc') or r.get('retrieved_at_utc') or observed.isoformat(),validation='EXACT_SOURCE_IDENTITY_VALIDATED'))
        for r in roster[roster.team.isin([g.home_team,g.away_team])].fillna('').to_dict('records'):
            proof=r.get('official_roster_identity_proof','')
            if proof:
                try:
                    p=json.loads(proof);s=p.get('official_roster_status');s='PRACTICE_SQUAD' if s=='DEV' and p.get('section')=='Practice Squad' else s
                    if s in ADVERSE and p.get('source_sha256') in hashes:evidence.append(dict(type='AVAILABILITY',game_id=g.game_id,team=r['team'],player_id=r['gsis_id'],name=r['full_name'],status=s,source_url=p['source_url'],source_sha256=p['source_sha256'],published_at_utc=None,modified_at_utc=None,observed_at_utc=p['observed_at_utc'],validation='EXACT_SOURCE_IDENTITY_VALIDATED',transaction_uncertainty=r.get('transaction_reconciliation_error','')))
                except (ValueError,KeyError,TypeError):pass
            status=r.get('current_transaction_state','');history=r.get('current_transaction_evidence') or r.get('prior_transaction_state_history','')
            if status not in ADVERSE or r.get('transaction_reconciliation_error') or not history:continue
            try:events=json.loads(history);event=next((v for v in reversed(events) if v.get('transaction_state')==status and v.get('source_sha256') in hashes),None)
            except (ValueError,TypeError):continue
            if not event:continue
            evidence.append(dict(type='AVAILABILITY',game_id=g.game_id,team=r['team'],player_id=r['gsis_id'],name=r['full_name'],status=status,source_url=event.get('source_url'),source_sha256=event['source_sha256'],published_at_utc=event.get('published_at_utc') or None,modified_at_utc=None,observed_at_utc=event.get('retrieved_at_utc') or observed.isoformat(),validation='EXACT_SOURCE_IDENTITY_VALIDATED'))
    feature_events=[(g.kickoff_utc+pd.Timedelta(hours=48)).isoformat() for g in games[games.season.eq(season)&games.week.eq(week-1)&games.game_type.eq('REG')].itertuples() if pd.notna(g.home_score) and pd.notna(g.away_score) and g.kickoff_utc+pd.Timedelta(hours=48)>observed]
    return dict(schema='weekly-source-packet-v1',season=season,week=week,observed_at_utc=observed.isoformat(),schedule_sha256=schedule_sha,model_sha256=TEAM_SHA,player_model_sha256=PLAYER_SHA,games=fixtures,team_inputs=inputs,player_inputs={},evidence=evidence,feature_contract='causal-score-history8-v1;48hour-embargo',historical_publication_certified=False,QB_neutral_validation='Exact fixed7features contain no QB identity/performance; no QB scenario adjustment. Conflict remains explicit.',raw_source_hashes=hashes,next_feature_events_utc=sorted(set(feature_events)))

def prepare_next_players(bundle,state,captured,season,week):
    """Existing stats ingestion +23-feature rollforward in a PRIVATE copy only.

    Never called on the project canonical files. Any unsupported prerequisite
    withholds player outputs without preventing independent causal team means.
    """
    import pandas as pd
    from dataclasses import replace
    from nfl2026.config import load_settings
    from nfl2026.postgame import ensure_completed_week_actuals,_ensure_rollforward,_target_inputs
    from nfl2026.live.pipeline import normalize_schedule_kickoffs
    from nfl2026.live.providers.base import fetch_bytes
    bundle=Path(bundle);state=Path(state);captured=Path(captured);workspace=state/'preparation';marker=workspace/f'prepared-{season}-w{week:02d}.json'
    if marker.exists():
        m=load(marker);path=workspace/m['path']
        if sha(path)!=m['sha256']:raise ValueError('Prepared player features changed')
        return path,m
    if not workspace.exists():shutil.copytree(bundle/'preparation',workspace)
    ext=workspace/'data/phase2_external';ext.mkdir(parents=True,exist_ok=True);shutil.copyfile(captured/'independent_schedule.csv',ext/'games.csv');shutil.copyfile(captured/'normalised/roster.csv',ext/f'weekly_roster_{season}.csv')
    games=normalize_schedule_kickoffs(pd.read_csv(ext/'games.csv',encoding='utf-8',low_memory=False));prior=games[games.season.eq(season)&games.week.eq(week-1)&games.game_type.eq('REG')]
    if prior.empty or prior[['home_score','away_score']].isna().any().any():raise ValueError('Previous week is not completely FINAL; no new player rollforward')
    r=pd.read_csv(ext/f'weekly_roster_{season}.csv',encoding='utf-8',low_memory=False)
    if not r.week.eq(week).any():raise ValueError('Exact target-week roster missing; no inherited future identity')
    raw=next((captured/'workspace/data/live_downloads'/str(season)).iterdir());snap=raw/f'snap_counts_{season}.csv'
    if snap.exists():shutil.copyfile(snap,ext/snap.name)
    pbp=workspace/'NFL Fantasy'/f'{season} Stats'/f'pbp_{season}.parquet';pbp.parent.mkdir(parents=True,exist_ok=True)
    # One bounded free PBP observation per newly completed week, never odds.
    body=fetch_bytes(f'https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet',attempts=1,timeout_seconds=90);pbp.write_bytes(body)
    settings=replace(load_settings(bundle/'config/nfl2026.toml'),root=workspace,artifact_root=workspace/'artifacts',report_root=workspace/'reports',prediction_root=workspace/'predictions',legacy_root=workspace/'NFL Fantasy')
    actual=ensure_completed_week_actuals(settings,season,week-1,expected_games=prior);target,roster=_target_inputs(settings,season,week);result=_ensure_rollforward(settings,season,week-1,week,actual,target,roster)
    features=workspace/'artifacts/phase2/player_pregame_universe_features.csv';frame=pd.read_csv(features,encoding='utf-8',low_memory=False,float_precision='round_trip');frame=frame[frame.season.eq(season)&frame.week.eq(week)];out=workspace/f'prepared-features-{season}-w{week:02d}.csv';frame.to_csv(out,index=False)
    m=dict(status='EXISTING_CONDITIONAL_PLAYER_ROLLFORWARD',path=str(out.relative_to(workspace)),sha256=sha(out),actual_manifest_sha256=sha(actual['manifest_path']),feature_manifest_sha256=sha(result['manifest_path']),known_at_utc=clock().isoformat(),rows=len(frame),missing_stat_labels_not_converted_to_zero=True,official=False);immutable(marker,m);return out,m
