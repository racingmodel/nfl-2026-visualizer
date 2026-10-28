"""Read-only source semantics, workload diagnostics and replacement prerequisites."""
from datetime import datetime,timezone
import json

import numpy as np
import pandas as pd

from nfl2026.five_week_review import finite
from nfl2026.phase7.common import json_write
from nfl2026.research.rigorous_review import save,cluster_ci
from nfl2026.research.review_challengers import eligible_research_qbs
from nfl2026.utilities.io import sha256_file


def audit(root,output):
    players=pd.read_csv(output/'final/official_players.csv')
    import pyarrow.parquet as pq
    path=root/'NFL Fantasy/2026 Stats/pbp_2026.parquet'
    wanted=['game_id','play_id','posteam','rusher_player_id','rushing_yards','rush_attempt','qb_kneel','qb_spike','qb_scramble','sack','week','play_type','two_point_attempt']
    schema=pq.read_schema(path).names;missing=sorted(set(wanted)-set(schema))
    columns=[c for c in wanted if c in schema]
    pbp=pq.read_table(path,columns=columns,filters=[('week','<',7)]).to_pandas()
    qb_ids=set(players.loc[players.position.eq('QB'),'player_id'])
    rush=pbp[pbp.rusher_player_id.isin(qb_ids)&pbp.play_type.ne('no_play')&pbp.two_point_attempt.ne(1)].copy()
    rush['kneel']=rush.qb_kneel.eq(1)
    rush['valid_carry']=rush.rush_attempt.eq(1)&rush.qb_kneel.ne(1)&rush.qb_spike.ne(1)
    rush['scramble']=rush.qb_scramble.eq(1)&rush.valid_carry if 'qb_scramble' in rush else False
    rush['non_scramble_rush_proxy']=rush.valid_carry&~rush.scramble
    for category in ['kneel','valid_carry','scramble','non_scramble_rush_proxy']:
        rush[category+'_yards']=rush.rushing_yards.where(rush[category],0)
    cols=['kneel','valid_carry','scramble','non_scramble_rush_proxy',*[x+'_yards' for x in ['kneel','valid_carry','scramble','non_scramble_rush_proxy']]]
    summary=rush.groupby(['game_id','rusher_player_id','posteam'])[cols].sum().reset_index().rename(columns={'rusher_player_id':'player_id','posteam':'team'})
    save(output,'qb_rush_pbp_classification',summary)
    json_write(output/'rush_label_contract.json',finite({'source_sha256':sha256_file(path),'consumed_max_week':int(pbp.week.max()),
        'missing_fields':missing,'total_qb_kneels':int(rush.kneel.sum()),'total_kneel_yards':rush.kneel_yards.sum(),
        'qb_scrambles':int(rush.scramble.sum()),'non_scramble_non_kneel_proxy':int(rush.non_scramble_rush_proxy.sum()),
        'sack_plays':int(pbp.sack.eq(1).sum()),'sack_plays_with_recorded_rushing_yards':int((pbp.sack.eq(1)&pbp.rushing_yards.notna()).sum()),
        'model_canonical_policy':'nfl2026/data/canonical.py valid_carry excludes kneels and spikes; sacks are passing/dropback events',
        'bookmaker_contract':'Provider quote schema contains no kneeldown grading rule. Exact bookmaker rushing label equivalence NOT CERTIFIED; no grading change.',
        'designed_rush_qualification':'Non-scramble non-kneel is a proxy, not an authoritative designed-run play-call label',
        'leakage_policy':'Completed Week1-6 PBP for retrospective diagnostics only; no Week7 outcomes consumed'}))
    lock=root/'locks/2026/week_07/lock-2026-w07-d4022d070e6f1b48'
    features=pd.read_csv(lock/'inputs/player_pregame_universe_features.csv',low_memory=False)
    features=features[features.season.eq(2026)&features.week.eq(7)]
    rows=[]
    for position in ['QB','TE','WR']:
        b=features[features.position.eq(position)]
        for field in ['prior_carries','prior_targets','prior_rushing_yards','prior_receiving_yards','recent_snap_pct',
                      'recent_red_zone_targets','recent_team_targets','recent_yards_per_target','routes','targets_per_route',
                      'mobility_constraint','opponent_te_coverage','offensive_line_health','target_growth','air_yard_share']:
            rows.append({'position':position,'feature':field,'rows':len(b),'available':field in b,
                'non_missing':int(b[field].notna().sum()) if field in b else 0,
                'qualification':'Existing frozen feature contract; absent fields not inferred or invented'})
    save(output,'week7_position_feature_coverage',pd.DataFrame(rows))
    qbs=pd.read_csv(lock/'inputs/qb_status_2026_w07.csv')
    passed=eligible_research_qbs(qbs)
    csv=pd.read_csv(output/'final/week7_passing_challenger.csv')
    allowed=set(zip(passed.game_id,passed.team,passed.qb_player_id))
    emitted=set(zip(csv.game_id,csv.team,csv.player_id))
    if not emitted.issubset(allowed):raise ValueError('Archived passing research contains an ineligible QB')
    json_write(output/'week7_research_qb_audit.json',{'status':'PASS','expected_qbs':len(qbs),'eligible_qbs':len(passed),
        'emitted_qbs':len(csv),'starter_confirmed':int(qbs.starter_confirmed.astype(str).str.lower().eq('true').sum()),
        'rule':'Unique game/team + exact GSIS + VALID source + EXPECTED/CONFIRMED + no critical availability blocker; UNKNOWN remains UNKNOWN',
        'source_sha256':sha256_file(lock/'inputs/qb_status_2026_w07.csv')})
    matched=pd.read_csv(output/'final/paired_player_lines.csv');rows=[]
    for (position,market,cohort),b in matched.groupby(['position','market','cohort']):
        if position not in ['QB','TE']:continue
        b=b.dropna(subset=['actual_over','model_over_probability']).copy();b['season']=2026
        for bandname,band in [('ALL',pd.Series('ALL',index=b.index)),('line_band',pd.cut(b.line,[-np.inf,10,25,50,75,np.inf])),
                              ('projection_band',pd.cut(b['mean'],[-np.inf,10,25,50,75,np.inf]))]:
            for label,g in b.groupby(band,observed=True):
                delta=(g.model_over_probability-g.actual_over)**2-(g.market_over_no_vig-g.actual_over)**2
                ci=cluster_ci(g,delta)
                rows.append({'position':position,'market':market,'cohort':cohort,'segment':bandname,'band':str(label),
                    'book_lines':len(g),'unique_player_games':len(g.drop_duplicates(['game_id','player_id'])),'games':g.game_id.nunique(),
                    'mean_minus_actual':(g['mean']-g.actual).mean(),'mean_minus_line':g.mean_minus_line.mean(),
                    'model_over_probability':g.model_over_probability.mean(),'market_over_probability':g.market_over_no_vig.mean(),
                    'actual_over_rate':g.actual_over.mean(),'brier_model_minus_book':delta.mean(),'difference_ci_low':ci[0],'difference_ci_high':ci[1]})
    save(output,'position_market_calibration',pd.DataFrame(rows))
    # Descriptive research watch is kept separate from the certified primary UI.
    wr=pd.read_csv(output/'final/week7_wr_discovery.csv')
    wr['role_label']=wr.role_status.fillna('ROLE_UNCERTAIN')
    wr['watch_status']=np.where(wr.receiving_group.eq('EMERGING'),'PREGAME_GROWTH_WATCH_RESEARCH','NOT_WATCHED')
    save(output,'week7_receiver_research_watch',wr)
    inventory=[]
    for p in sorted((root/'artifacts/research/phase7/models').glob('*passing*')):
        inventory.append({'path':str(p.relative_to(root)),'sha256':sha256_file(p),'bytes':p.stat().st_size})
    save(output,'existing_passing_artifacts',pd.DataFrame(inventory))
    provider=(root/'nfl2026/markets/providers/the_odds_api.py').read_text()
    quote_count=0
    for file in (root/'markets/snapshots/2026').glob('week_*/**/market_quotes.csv'):
        quote_count+=int(pd.read_csv(file,usecols=['market_type']).market_type.eq('PASSING_YARDS').sum())
    json_write(output/'passing_market_contract.json',{'captured_passing_quotes':quote_count,'passing_market_enabled':False,
        'provider_adapter_has_passing_mapping':'PASSING_YARDS' in provider,
        'current_configured_prop_request':'player_anytime_td,player_rush_yds,player_reception_yds',
        'chargeable_markets_changed':False,'odds_api_requests':0,
        'frontend_contract':'NOT_CERTIFIED remains required by v3_release validator; model-only presentation code cannot certify a model'})


def prior_target_means(candidates,history):
    history=history.copy()
    history['time']=pd.to_datetime(history.kickoff_utc,utc=True)
    if history.duplicated(['player_id','game_id']).any():raise ValueError('Duplicate usage history')
    indexed={pid:g.sort_values('time') for pid,g in history.groupby('player_id')}
    d=candidates.copy()
    d['prior3_targets']=np.nan;d['prior8_targets']=np.nan
    for pid,indexes in d.groupby('player_id').groups.items():
        past=indexed.get(pid)
        if past is None:continue
        for i in indexes:
            before=past[past.time.lt(pd.Timestamp(d.loc[i,'kickoff_utc']))]
            if len(before):
                d.loc[i,['prior3_targets','prior8_targets']]=[before.tail(3).target_targets.mean(),before.tail(8).target_targets.mean()]
    return d


def wr_growth_audit(root,output):
    """Research recall from strictly prior target history; no invented routes/roles."""
    source=root/'artifacts/phase2/player_advanced_conditional_matrix.csv'
    history=pd.read_csv(source,usecols=['player_id','game_id','kickoff_utc','target_targets'])
    d=prior_target_means(pd.read_csv(output/'final/wr_historical_ranking.csv'),history)
    d['prior3_team_rank']=d.groupby(['game_id','team']).prior3_targets.rank(method='first',ascending=False)
    d['growth_watch']=d.prior3_targets.gt(d.prior8_targets)&d.prior3_team_rank.le(3)&d.opportunity_rank.gt(2)
    rows=[]
    for season,b in d.groupby('season'):
        emerging=b.prior8_targets.lt(5)&b.actual_targets.ge(5)
        for name,chosen in [('TOP_TWO',b.opportunity_rank.le(2)),('TOP_TWO_PLUS_GROWTH_WATCH',b.opportunity_rank.le(2)|b.growth_watch)]:
            selected=b[chosen]
            rows.append({'season':season,'selector':name,'selected_rows':len(selected),'all_rows':len(b),
                'high_opportunity_coverage':selected.actual_targets.ge(5).sum()/b.actual_targets.ge(5).sum(),
                'emerging_outcomes':int(emerging.sum()),'emerging_recall':int((emerging&chosen).sum())/emerging.sum(),
                'weak_under2_targets_fraction':selected.actual_targets.lt(2).mean(),
                'role_publication_certified':False,'promotion':False,
                'policy':'Prior3>prior8 and within-team prior3 rank<=3 adds research watch; high/emerging outcomes >=5 actual targets are diagnostic, not selector inputs'})
    save(output,'wr_growth_signal_predictions',d);save(output,'wr_growth_validation',pd.DataFrame(rows))
