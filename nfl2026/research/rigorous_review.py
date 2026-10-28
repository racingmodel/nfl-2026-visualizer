"""Matched pregame benchmarks and chronological challengers, never promotion.

Snapshots retrieved after the forecast cannot prove forecast-time availability.
Such observations remain available in an explicitly later-pregame cohort.
"""
from datetime import datetime, timezone
from pathlib import Path
import json

import numpy as np
import pandas as pd

from nfl2026.utilities.io import sha256_file
from nfl2026.five_week_review import finite
from nfl2026.phase7.common import protect, json_write, verify_protected

EXTRA = ('artifacts/research', 'artifacts/actuals', 'artifacts/canonical',
         'frontend/releases', 'data/phase2_external', 'data/postgame_downloads',
         'reports/phase75_release_review.json', 'reports/phase75_release_candidate.json')
SEED = 20261022


def extra_hashes(root):
    result = {}
    for name in EXTRA:
        p = root/name
        for f in ([p] if p.is_file() else p.rglob('*')):
            if f.is_file():
                result[str(f.relative_to(root))] = sha256_file(f)
    return result


def verify(root, output):
    verify_protected(root, json.loads((output/'protected_before.json').read_text()))
    before = json.loads((output/'extra_before.json').read_text())
    if extra_hashes(root) != before:
        raise ValueError('Existing research, history, source or release changed')
    manifest=output/'completed_manifest.json'
    if manifest.exists():
        payload=json.loads(manifest.read_text())
        for base,entries in [(output,payload['file_sha256']),(root,payload.get('evidence_sha256',{})),(root,payload.get('code_sha256',{}))]:
            for name,digest in entries.items():
                path=(base/name).resolve()
                if not path.is_relative_to(base.resolve()) or sha256_file(path)!=digest:
                    raise ValueError('Completed research artifact/code changed: '+name)
    return {'status':'PASS', 'protected_files':len(protect(root)),
            'additional_files':len(before), 'network_requests':0,
            'production_models_changed':False, 'published_site_changed':False}


def save(output, name, frame):
    frame.to_csv(output/(name+'.csv'), index=False, lineterminator='\n')


def no_vig(home_odds, away_odds):
    a, b = float(home_odds), float(away_odds)
    if not np.isfinite([a,b]).all() or min(a,b) <= 1:
        raise ValueError('Invalid paired decimal odds')
    return (1/a)/(1/a+1/b)


def implied_scores(home_handicap, total):
    """A home -3 handicap implies a +3 home expected margin."""
    h, t = float(home_handicap), float(total)
    if not np.isfinite([h,t]).all() or t < abs(h):
        raise ValueError('Invalid paired spread/total')
    return (t-h)/2, (t+h)/2


def eligible_quotes(quotes, forecast, kickoff, max_age_minutes=90):
    q = quotes.copy()
    source = pd.to_datetime(q.source_timestamp_utc, utc=True, errors='coerce')
    retrieved = pd.to_datetime(q.retrieval_timestamp_utc, utc=True, errors='coerce')
    forecast, kickoff = pd.Timestamp(forecast), pd.Timestamp(kickoff)
    valid = (source.notna() & retrieved.notna() & source.le(retrieved)
             & retrieved.lt(kickoff) & source.lt(kickoff)
             & ~q.suspended.astype(str).str.lower().isin(['true','1'])
             & pd.to_numeric(q.decimal_odds,errors='coerce').gt(1))
    q['quote_state'] = np.where(valid, 'LATER_PREGAME', 'INVALID_OR_POSTKICKOFF')
    contemporaneous = valid & retrieved.le(forecast) & source.le(forecast)
    age = (forecast-source).dt.total_seconds()/60
    q.loc[contemporaneous & age.le(max_age_minutes),'quote_state'] = 'FORECAST_TIME_FRESH'
    q.loc[contemporaneous & age.gt(max_age_minutes),'quote_state'] = 'FORECAST_TIME_STALE'
    q['forecast_age_minutes'] = age
    return q


def cluster_ci(frame, values, *, seasons=False, draws=1800):
    """Each game stays intact; optionally retain observed season composition."""
    d = pd.DataFrame({'game':frame.game_id.to_numpy(),'value':np.asarray(values,float),
                      'season':frame.season.to_numpy() if 'season' in frame else 2026})
    d = d[np.isfinite(d.value)]
    if d.empty:
        return [None,None]
    sums = d.groupby(['season','game']).value.agg(['sum','count']).reset_index()
    rng = np.random.default_rng(SEED)
    blocks = [g for _,g in sums.groupby('season')] if seasons else [sums]
    numerator, denominator = np.zeros(draws), np.zeros(draws)
    for g in blocks:
        ids = rng.integers(0,len(g),size=(draws,len(g)))
        numerator += g['sum'].to_numpy()[ids].sum(axis=1)
        denominator += g['count'].to_numpy()[ids].sum(axis=1)
    return list(np.quantile(numerator/denominator,[.025,.975]))


def assert_chronology(frame):
    if frame.empty or not pd.to_datetime(frame.max_training_kickoff_utc,utc=True).lt(
            pd.to_datetime(frame.kickoff_utc,utc=True)).all():
        raise ValueError('Training cutoff reaches the forecast fixture')


def load_official(root, output):
    from nfl2026.config import load_settings
    from nfl2026.frontend.performance import build_performance_presentation
    # Performs the existing lock/run/actual/selection/hash/parity validations.
    presentation = build_performance_presentation(load_settings(root/'config/nfl2026.toml'))
    json_write(output/'official_source_validation.json',presentation)
    games = pd.read_csv(root/'reports/five_week_live_games.csv')
    players = pd.read_csv(root/'reports/five_week_live_players.csv')
    games = games[games.week.le(5)].copy()
    players = players[players.week.le(5)&players.position.isin(['QB','RB','FB','WR','TE'])].copy()
    for field in ['carries','targets','rushing_yards','receiving_yards']:
        if players.loc[players.actual_participant.eq(True),field].isna().any():
            raise ValueError('Existing evaluated participant has unknown '+field)
        players[field]=players['actual_'+field]
    players['actuals_policy']='Reuse archived evaluated actual_* labels; absent nonparticipant zero; present unknown blocked'
    folder = root/'artifacts/actuals/2026/week_06/actual-0dbe2d1b1f67'
    choices = pd.read_csv(folder/'evaluation_run_selection.csv')
    actual_g = pd.read_csv(folder/'team_actuals.csv')
    actual_p = pd.read_csv(folder/'player_actuals.csv')
    gp, pp = [], []
    for rid, selection in choices.groupby('selected_run_id'):
        run = root/'predictions/2026/week_06'/rid
        meta = json.loads((run/'metadata.json').read_text())
        for filename,digest in meta['file_hashes'].items():
            if sha256_file(run/filename)!=digest:
                raise ValueError('Official run hash mismatch')
        for filename, parts in [('games.csv',gp),('players.csv',pp)]:
            d = pd.read_csv(run/filename)
            d = d[d.game_id.isin(selection.game_id)].copy()
            d['source_run_id']=rid;d['prediction_timestamp']=meta['cutoff_utc']
            d['season']=2026;d['week']=6;d['lock_id']=meta['lock_id']
            parts.append(d)
    six_g = pd.concat(gp).merge(actual_g[['game_id','home_score','away_score']],on='game_id',validate='one_to_one')
    six_p = pd.concat(pp)
    six_p = six_p[six_p.position.isin(['QB','RB','FB','WR','TE'])]
    keys = ['game_id','player_id','team']
    fields = ['carries','targets','rushing_yards','receiving_yards']
    actual_p = actual_p[keys+fields]
    six_p = six_p.merge(actual_p,on=keys,how='left',validate='one_to_one',indicator=True)
    # Explicit complete-archive policy: an absent nonparticipant has zero volume.
    # A present but incomplete observation is never made zero.
    for f in fields:
        if six_p.loc[six_p._merge.eq('both'),f].isna().any():
            raise ValueError('Present player has missing '+f)
        six_p[f]=six_p[f].where(six_p._merge.eq('both'),0)
    six_p['actual_participant']=six_p._merge.eq('both')
    six_p['actuals_policy']='complete validated archive; absent nonparticipant zero; present unknown blocked'
    games = pd.concat([games,six_g],ignore_index=True)
    players = pd.concat([players,six_p.drop(columns='_merge')],ignore_index=True)
    if games.game_id.duplicated().any() or players.duplicated(keys).any():
        raise ValueError('Duplicate official evaluation identities')
    if not pd.to_datetime(games.prediction_timestamp,utc=True).lt(pd.to_datetime(games.kickoff_utc,utc=True)).all():
        raise ValueError('Official prediction not pregame')
    if len(games)!=presentation['games'] or len(players)!=presentation['offensive_player_rows']:
        raise ValueError('Official population differs from validated presentation')
    save(output,'official_games',games);save(output,'official_players',players)
    return games, players


def load_quotes(root, output, games):
    from nfl2026.markets.core import map_fixtures
    parts, inventory = [], []
    for p in sorted((root/'markets/snapshots/2026').glob('week_0[1-6]/*/market_quotes.csv')):
        meta = json.loads((p.parent/'metadata.json').read_text())
        digest=sha256_file(p)
        valid_hash=meta.get('file_hashes',{}).get(p.name)==digest
        inventory.append({'snapshot':str(p.parent.relative_to(root)), 'sha256':digest,
            'hash_valid':valid_hash,'provider':meta.get('provider'),'status':meta.get('status'),
            'rows':meta.get('quote_count',meta.get('row_count'))})
        if not valid_hash:
            raise ValueError('Historical snapshot hash mismatch: '+str(p))
        q=pd.read_csv(p)
        if q.empty:continue
        keys=['home_team_name','away_team_name','kickoff_utc']
        identities=map_fixtures(q.drop_duplicates(keys),games)
        columns=keys+['game_id','canonical_kickoff_utc','fixture_mapping_status','fixture_mapping_reason']
        q=q.merge(identities[columns],on=keys,validate='many_to_one')
        q['snapshot_id']=p.parent.name;q['snapshot_path']=str(p.parent.relative_to(root))
        parts.append(q)
    quotes=pd.concat(parts,ignore_index=True)
    # Preserve different observations of a quote at different captured times.
    quotes=quotes.drop_duplicates(['quote_id','source_timestamp_utc','retrieval_timestamp_utc','decimal_odds','line'])
    labelled=[]
    for g in games.itertuples():
        block=quotes[quotes.game_id.eq(g.game_id)]
        # Same configured freshness limits as production comparison (120/60 min).
        limit=pd.Series(np.where(block.market_type.isin(['ANYTIME_TD','RUSHING_YARDS','RECEIVING_YARDS']),60,120),index=block.index)
        labelled.append(eligible_quotes(block,g.prediction_timestamp,g.kickoff_utc,limit))
    quotes=pd.concat(labelled,ignore_index=True)
    save(output,'snapshot_inventory',pd.DataFrame(inventory));save(output,'quote_observations',quotes)
    coverage=[]
    for g in games.itertuples():
        q=quotes[quotes.game_id.eq(g.game_id)]
        if q.empty:
            coverage.append({'game_id':g.game_id,'prediction_timestamp':g.prediction_timestamp,'kickoff_utc':g.kickoff_utc,
                'quote_state':'MISSING','source_reliability':'NO_ARCHIVED_PROVIDER_QUOTE'})
        for (book,market,state),block in q.groupby(['bookmaker','market_type','quote_state']):
            coverage.append({'game_id':g.game_id,'prediction_timestamp':g.prediction_timestamp,'kickoff_utc':g.kickoff_utc,
                'bookmaker':book,'market_type':market,'quote_state':state,'quote_count':len(block),
                'quote_timestamp_min':block.source_timestamp_utc.min(),'quote_timestamp_max':block.source_timestamp_utc.max(),
                'retrieved_min':block.retrieval_timestamp_utc.min(),'retrieved_max':block.retrieval_timestamp_utc.max(),
                'provider':'|'.join(sorted(block.provider.unique())), 'source_reliability':'HASH_VERIFIED_ARCHIVE; PROVIDER_TIMESTAMP',
                'contemporaneous':state=='FORECAST_TIME_FRESH'})
    save(output,'fixture_market_coverage',pd.DataFrame(coverage))
    return quotes


def paired_books(quotes, games, state):
    from nfl2026.markets.core import _team_code
    rows=[]
    for g in games.itertuples():
        q=quotes[quotes.game_id.eq(g.game_id)&quotes.quote_state.eq(state)].copy()
        # Pair only exact captured source/snapshot/event/book/market identities;
        # never stitch opposing selections from different requests or prices.
        groupkeys=['snapshot_id','provider_event_id','bookmaker','market_type','source_timestamp_utc','retrieval_timestamp_utc']
        candidates=[]
        for keys,b in q.groupby(groupkeys,dropna=False):
            market=keys[3]
            row={'game_id':g.game_id,'bookmaker':keys[2],'market':market,'source_timestamp':keys[4],
                 'retrieval_timestamp':keys[5],'snapshot_id':keys[0]}
            b=b.drop_duplicates(['selection','line','decimal_odds'])
            if market in ['MONEYLINE','SPREAD']:
                b=b.assign(code=b.selection.map(_team_code))
                h=b[b.code.eq(g.home_team)];a=b[b.code.eq(g.away_team)]
                if len(h)!=1 or len(a)!=1:continue
                if market=='MONEYLINE':row['market_home_probability']=no_vig(h.decimal_odds.iloc[0],a.decimal_odds.iloc[0])
                elif np.isclose(h.line.iloc[0],-a.line.iloc[0]):row['home_handicap']=h.line.iloc[0]
                else:continue
            elif market=='TOTAL':
                h=b[b.side.eq('OVER')];a=b[b.side.eq('UNDER')]
                if len(h)!=1 or len(a)!=1 or not np.isclose(h.line.iloc[0],a.line.iloc[0]):continue
                row['market_total']=h.line.iloc[0]
            else:continue
            candidates.append(row)
        if not candidates:continue
        c=pd.DataFrame(candidates).sort_values(['source_timestamp','retrieval_timestamp','snapshot_id'])
        c=c.drop_duplicates(['bookmaker','market'],keep='last')
        for book,b in c.groupby('bookmaker'):
            row={'game_id':g.game_id,'bookmaker':book,'cohort':state}
            for field in ['market_home_probability','home_handicap','market_total']:
                if field in b and b[field].notna().any():row[field]=b[field].dropna().iloc[0]
            # Score algebra additionally requires spread & total from same capture.
            st=b[b.market.isin(['SPREAD','TOTAL'])]
            row['score_pair_same_capture']=(len(st)==2 and st.snapshot_id.nunique()==1
                and st.retrieval_timestamp.nunique()==1 and st.source_timestamp.nunique()==1)
            rows.append(row)
    return pd.DataFrame(rows)


def metric_values(frame, probability, home, away, margin, total):
    actual_margin=frame.home_score-frame.away_score
    actual_total=frame.home_score+frame.away_score
    y=actual_margin.gt(0).astype(float)
    p=np.clip(np.asarray(probability,float),1e-8,1-1e-8)
    values={'winner_accuracy':np.where(actual_margin.ne(0),(p>.5)==y,np.nan),
        'win_brier':np.where(actual_margin.ne(0),(p-y)**2,np.nan),
        'win_log_loss':np.where(actual_margin.ne(0),-(y*np.log(p)+(1-y)*np.log1p(-p)),np.nan),
        'team_score_mae':(np.abs(home-frame.home_score)+np.abs(away-frame.away_score))/2,
        'margin_mae':np.abs(margin-actual_margin),'total_mae':np.abs(total-actual_total)}
    return values


def benchmarks(root,output,games,quotes):
    metric_rows=[];paired=[]
    for state in ['FORECAST_TIME_FRESH','FORECAST_TIME_STALE','LATER_PREGAME']:
        books=paired_books(quotes,games,state)
        if books.empty:continue
        save(output,'paired_books_'+state.lower(),books)
        # Game consensus gives each book one vote and each fixture one weight.
        numeric=[x for x in ['market_home_probability','home_handicap','market_total'] if x in books]
        consensus=books.groupby('game_id')[numeric].median().reset_index()
        if 'score_pair_same_capture' in books:
            valid=books[books.score_pair_same_capture]
            if len(valid):
                ht=valid.groupby('game_id')[['home_handicap','market_total']].median()
                consensus=consensus.drop(columns=['home_handicap','market_total'],errors='ignore').merge(ht,on='game_id',how='outer')
            else:consensus=consensus.drop(columns=['home_handicap','market_total'],errors='ignore')
        d=games.merge(consensus,on='game_id',validate='one_to_one')
        paired.append(d.assign(cohort=state))
        for family,cols in [('probability',['market_home_probability']),('scores',['home_handicap','market_total'])]:
            if not set(cols).issubset(d):continue
            b=d.dropna(subset=cols).copy()
            if b.empty:continue
            ph=(b.market_total-b.home_handicap)/2 if family=='scores' else np.full(len(b),np.nan)
            pa=(b.market_total+b.home_handicap)/2 if family=='scores' else np.full(len(b),np.nan)
            mp=b.market_home_probability if family=='probability' else np.full(len(b),np.nan)
            mm=-b.home_handicap if family=='scores' else np.full(len(b),np.nan)
            mt=b.market_total if family=='scores' else np.full(len(b),np.nan)
            mv=metric_values(b,b.home_win_probability,b.home_expected_points,b.away_expected_points,b.model_margin,b.model_total)
            bv=metric_values(b,mp,ph,pa,mm,mt)
            names=['winner_accuracy','win_brier','win_log_loss'] if family=='probability' else ['team_score_mae','margin_mae','total_mae']
            for metric in names:
                x=np.asarray(mv[metric],float);y=np.asarray(bv[metric],float);valid=np.isfinite(x)&np.isfinite(y)
                ci=cluster_ci(b[valid],x[valid]-y[valid])
                metric_rows.append({'cohort':state,'metric':metric,'games':int(valid.sum()),'model':np.mean(x[valid]),
                    'bookmaker_consensus':np.mean(y[valid]),'model_minus_book':np.mean(x[valid]-y[valid]),
                    'difference_ci_low':ci[0],'difference_ci_high':ci[1]})
            if family=='probability':
                actual=(b.home_score>b.away_score).astype(int);notie=b.home_score.ne(b.away_score)
                for metric,values in [('win_brier',np.full(len(b),.25)),('win_log_loss',np.full(len(b),np.log(2)))]:
                    metric_rows.append({'cohort':state,'metric':metric,'baseline':'50/50','games':int(notie.sum()),'baseline_value':np.mean(values[notie])})
            else:
                am=b.home_score-b.away_score;at=b.home_score+b.away_score
                for metric,pred,actual,line in [('spread_direction_accuracy',b.model_margin,am,-b.home_handicap),
                                               ('total_over_under_accuracy',b.model_total,at,b.market_total)]:
                    valid=actual.ne(line)&pred.ne(line)
                    hit=((pred>line)==(actual>line)).astype(float)
                    ci=cluster_ci(b[valid],hit[valid])
                    metric_rows.append({'cohort':state,'metric':metric,'games':int(valid.sum()),'model':hit[valid].mean(),
                        'model_ci_low':ci[0],'model_ci_high':ci[1], 'bookmaker_consensus':None,
                        'note':'pushes and model no-picks excluded; line itself has no directional prediction'})
    save(output,'matched_metrics',pd.DataFrame(metric_rows))
    save(output,'matched_fixture_consensus',pd.concat(paired,ignore_index=True) if paired else pd.DataFrame())
    # Schedule data are a separate closing benchmark with unknown quote/book times.
    schedule=pd.read_csv(root/'data/phase2_external/games.csv',low_memory=False)
    close=games.merge(schedule[['game_id','spread_line','total_line','home_moneyline','away_moneyline']],on='game_id',validate='one_to_one')
    close['evidence_class']='CLOSING_BENCHMARK; publication/book/quote times unavailable; not contemporaneous'
    # nflverse spread_line is positive when home is favoured (home expected margin).
    close['closing_home_points']=(close.total_line+close.spread_line)/2
    close['closing_away_points']=(close.total_line-close.spread_line)/2
    save(output,'closing_benchmark_fixtures',close)
    for family in ['probability','scores']:
        cols=['home_moneyline','away_moneyline'] if family=='probability' else ['spread_line','total_line']
        b=close.dropna(subset=cols).copy()
        if b.empty:continue
        def decimal(x):return np.where(x>0,1+x/100,1+100/np.abs(x))
        p=np.array([no_vig(h,a) for h,a in zip(decimal(b.home_moneyline),decimal(b.away_moneyline))]) if family=='probability' else np.full(len(b),np.nan)
        mv=metric_values(b,b.home_win_probability,b.home_expected_points,b.away_expected_points,b.model_margin,b.model_total)
        bv=metric_values(b,p,b.closing_home_points,b.closing_away_points,b.spread_line,b.total_line)
        names=['winner_accuracy','win_brier','win_log_loss'] if family=='probability' else ['team_score_mae','margin_mae','total_mae']
        for m in names:
            x,y=np.asarray(mv[m]),np.asarray(bv[m]);valid=np.isfinite(x)&np.isfinite(y)
            ci=cluster_ci(b[valid],x[valid]-y[valid])
            metric_rows.append({'cohort':'CLOSING_BENCHMARK_UNKNOWN_QUOTE_TIME','metric':m,'games':int(valid.sum()),'model':np.mean(x[valid]),
                'bookmaker_consensus':np.mean(y[valid]),'model_minus_book':np.mean(x[valid]-y[valid]),'difference_ci_low':ci[0],'difference_ci_high':ci[1]})
    save(output,'matched_metrics',pd.DataFrame(metric_rows))
    return metric_rows


def calibration(output,games):
    d=games[games.home_score.ne(games.away_score)].copy()
    d['outcome']=(d.home_score>d.away_score).astype(int)
    d['bin']=pd.cut(d.home_win_probability,np.linspace(0,1,11),include_lowest=True)
    rows=[]
    for name,b in d.groupby('bin',observed=True):
        ci=cluster_ci(b,b.outcome)
        rows.append({'bin':str(name),'games':len(b),'probability':b.home_win_probability.mean(),'outcome_rate':b.outcome.mean(),
                     'ci_low':ci[0],'ci_high':ci[1]})
    bins=pd.DataFrame(rows);save(output,'win_reliability',bins)
    base=d.outcome.mean();rel=sum(r['games']/len(d)*(r['probability']-r['outcome_rate'])**2 for r in rows)
    resolution=sum(r['games']/len(d)*(r['outcome_rate']-base)**2 for r in rows)
    brier=np.mean((d.home_win_probability-d.outcome)**2)
    json_write(output/'brier_decomposition.json',{'games':len(d),'brier':brier,'uncertainty':base*(1-base),
        'binned_reliability':rel,'binned_resolution':resolution,
        'within_bin_remainder':brier-(rel-resolution+base*(1-base)),
        'qualification':'Binned approximation; remainder retained; not an exact point-probability decomposition'})
    segment=[]
    for key,series in [('week',games.week),('expected_spread',pd.cut(games.model_margin.abs(),[0,3,7,14,np.inf],include_lowest=True)),
                        ('total_band',pd.cut(games.model_total,[0,40,45,50,60,np.inf],include_lowest=True)),
                        ('model_favourite_side',np.where(games.home_win_probability>.5,'HOME','AWAY'))]:
        for label,b in games.groupby(series,observed=True):
            v=metric_values(b,b.home_win_probability,b.home_expected_points,b.away_expected_points,b.model_margin,b.model_total)
            segment.append({'segment':key,'value':str(label),'games':len(b),**{k:np.nanmean(x) for k,x in v.items()},
                'margin_bias':(b.model_margin-b.home_score+b.away_score).mean(),'total_bias':(b.model_total-b.home_score-b.away_score).mean()})
    save(output,'team_segments',pd.DataFrame(segment))
    hit=((d.home_win_probability>.5)==d.outcome).astype(float)
    json_write(output/'live_uncertainty.json',{'winner_correct':int(hit.sum()),'winner_games':len(d),'winner_accuracy':hit.mean(),
        'game_bootstrap_95ci':cluster_ci(d,hit),'qualification':'Single live season; 816 historical replay games are a different population, not paired evidence of deterioration'})
    return bins


def ledger_audit(root,output):
    d=pd.read_csv(root/'value_runs/ledger/value_ledger.csv')
    settled=d[d.settlement_status.eq('SETTLED')].copy()
    settled['profit']=settled.hypothetical_return-settled.hypothetical_stake
    settled['quote_age_minutes']=(pd.to_datetime(settled.captured_at_utc,utc=True)-pd.to_datetime(settled.market_timestamp_utc,utc=True)).dt.total_seconds()/60
    settled['breakeven_probability']=1/settled.book_odds
    settled['clv_status']='UNAVAILABLE_NO_MATCHED_CLOSING_QUOTE'
    settled['season']=2026
    checks=[]
    for snapshot,b in settled.groupby('market_snapshot_id'):
        matches=list((root/'markets/snapshots/2026').glob('week_*/'+str(snapshot)+'/market_quotes.csv'))
        if len(matches)!=1:
            checks.append({'snapshot_id':snapshot,'rows':len(b),'state':'MISSING_OR_AMBIGUOUS_SOURCE'});continue
        original=pd.read_csv(matches[0])
        for entry in b.itertuples():
            quote=original[original.quote_id.eq(entry.quote_id)]
            valid=len(quote)==1
            if valid:
                q=quote.iloc[0]
                valid=(np.isclose(q.decimal_odds,entry.book_odds) and q.bookmaker==entry.bookmaker
                    and q.market_type==entry.market_type and (pd.isna(q.line) and pd.isna(entry.line) or np.isclose(q.line,entry.line)))
            checks.append({'snapshot_id':snapshot,'ledger_entry_id':entry.ledger_entry_id,'state':'EXACT_CAPTURED_TERMS' if valid else 'TERMS_MISMATCH','rows':1})
    save(output,'ledger_term_verification',pd.DataFrame(checks))
    save(output,'settled_selections',settled)
    save(output,'blocked_settlements',d[d.settlement_status.eq('SETTLEMENT_BLOCKED')])
    rows=[]
    for name,b in [('ALL',settled),*settled.groupby('market_type')]:
        # Bootstrap cluster returns/stakes jointly, retaining multiple selections/game.
        ci=cluster_ci(b,b.profit) if b.hypothetical_stake.nunique()==1 else [None,None]
        stake=b.hypothetical_stake.sum();scale=b.hypothetical_stake.iloc[0]
        rows.append({'market':name,'bets':len(b),'games':b.game_id.nunique(),'stake':stake,'profit':b.profit.sum(),
            'roi':b.profit.sum()/stake,'roi_ci_low':ci[0]/scale if ci[0] is not None else None,'roi_ci_high':ci[1]/scale if ci[1] is not None else None,
            'breakeven_probability_mean':b.breakeven_probability.mean(),'model_probability_mean':b.model_probability.mean(),
            'mean_edge_pp':b.edge_pp.mean(),'mean_quote_age_minutes':b.quote_age_minutes.mean(),
            'books':'|'.join(sorted(b.bookmaker.unique())), 'clv':'NOT_ESTABLISHED',
            'qualification':'Hypothetical ledger selections, not evidence of executed wagers; correlated game cluster CI'})
    save(output,'settlement_summary',pd.DataFrame(rows))
    json_write(output/'ledger_population.json',{'status_counts':d.settlement_status.value_counts().to_dict(),
        'settled':len(settled),'unique_settled_entries':settled.ledger_entry_id.nunique(),
        'policy':'No threshold optimisation; no grading overrides; blocked and superseded excluded'})
    return rows


def player_diagnostics(output,players,quotes,games):
    from nfl2026.markets.core import map_players
    rows=[];segments=[]
    for pos,actual,pred,opportunity in [('QB','rushing_yards','expected_rush_yards','expected_carries'),('TE','receiving_yards','expected_receiving_yards','expected_targets'),('WR','receiving_yards','expected_receiving_yards','expected_targets')]:
        d=players[players.position.eq(pos)].copy();d['error']=d[pred]-d[actual];d['overprediction']=(d.error>0).astype(float)
        declared=d[d.depth_role.eq('STARTER')&d.role_status.eq('EXPECTED_STARTER')]
        for label,b in [('ALL_NOMINAL_FORECASTS',d),('PROJECTED_OPPORTUNITY',d[d[opportunity].ge(1)]),
                        ('DECLARED_EXPECTED_STARTER_NOT_RETROSPECTIVELY_RECERTIFIED',declared)]:
            if b.empty:continue
            ci=cluster_ci(b,b.error)
            rows.append({'position':pos,'target':actual,'cohort':label,'rows':len(b),'games':b.game_id.nunique(),
                'bias':b.error.mean(),'bias_ci_low':ci[0],'bias_ci_high':ci[1],'mae':b.error.abs().mean(),
                'overprediction_frequency':b.overprediction.mean(),'predicted_opportunity_mean':b[opportunity].mean(),
                'actual_opportunity_mean':b['carries' if pos=='QB' else 'targets'].mean()})
        for key,s in [('projected_band',pd.cut(d[pred],[-np.inf,10,25,50,75,np.inf])),('week',d.week),
                      ('workload_archetype',pd.cut(d[opportunity],[-np.inf,1,3,6,np.inf]))]:
            for label,b in d.groupby(s,observed=True):
                segments.append({'position':pos,'segment':key,'value':str(label),'rows':len(b),'bias':b.error.mean(),'mae':b.error.abs().mean(),'overprediction_frequency':b.overprediction.mean()})
    save(output,'position_bias',pd.DataFrame(rows));save(output,'position_segments',pd.DataFrame(segments))
    # Player quotes require independently mapped game, player ID and team.
    q=quotes[quotes.quote_state.isin(['FORECAST_TIME_FRESH','LATER_PREGAME']) & quotes.market_type.isin(['RUSHING_YARDS','RECEIVING_YARDS'])].copy()
    q=map_players(q,players.drop_duplicates(['game_id','player_id','team']))
    save(output,'player_quote_matching',q)
    paired=[]
    for keys,b in q[q.player_mapping_status.eq('MAPPED_EXACT')].groupby(['game_id','player_id','player_team','bookmaker','market_type',
            'line','snapshot_id','source_timestamp_utc','retrieval_timestamp_utc','quote_state'],dropna=False):
        b=b.drop_duplicates(['side','decimal_odds'])
        over=b[b.side.eq('OVER')];under=b[b.side.eq('UNDER')]
        if len(over)!=1 or len(under)!=1:continue
        match=players[players.game_id.eq(keys[0])&players.player_id.eq(keys[1])&players.team.eq(keys[2])]
        if len(match)!=1:continue
        p=match.iloc[0];rush=keys[4]=='RUSHING_YARDS'
        actual=p['rushing_yards' if rush else 'receiving_yards'];mean=p['expected_rush_yards' if rush else 'expected_receiving_yards']
        sd=p['rush_distribution_sd' if rush else 'receiving_distribution_sd']
        from scipy.stats import norm
        probability=float(norm.sf((keys[5]-mean)/sd)) if sd>0 else np.nan
        paired.append({'game_id':keys[0],'player_id':keys[1],'team':keys[2],'bookmaker':keys[3],'market':keys[4],
            'line':keys[5],'snapshot_id':keys[6],'source_timestamp':keys[7],'retrieval_timestamp':keys[8],'cohort':keys[9],
            'position':p.position,'mean':mean,'actual':actual,'mean_minus_line':mean-keys[5],
            'over_odds':over.decimal_odds.iloc[0],'under_odds':under.decimal_odds.iloc[0],
            'market_over_no_vig':no_vig(over.decimal_odds.iloc[0],under.decimal_odds.iloc[0]),
            'model_over_probability':probability,'actual_over':float(actual>keys[5]) if actual!=keys[5] else np.nan,
            'probability_policy':'Existing production heuristic Normal survival; no candidate calibration certification'})
    matched=pd.DataFrame(paired)
    if len(matched):
        # Latest complete book pair per fixture/player/line; no quote duplication inflation.
        matched=matched.sort_values(['source_timestamp','retrieval_timestamp']).drop_duplicates(['game_id','player_id','bookmaker','market','line','cohort'],keep='last')
    save(output,'paired_player_lines',matched)
    summary=[]
    if len(matched):
        for key,b in matched.groupby(['position','market','cohort']):
            valid=b.dropna(subset=['actual_over','model_over_probability'])
            summary.append({'position':key[0],'market':key[1],'cohort':key[2],'paired_book_lines':len(b),
                'unique_player_games':len(b.drop_duplicates(['game_id','player_id'])),'mean_minus_line':b.mean_minus_line.mean(),
                'fraction_means_above_line':b.mean_minus_line.gt(0).mean(),'model_over_brier':np.mean((valid.model_over_probability-valid.actual_over)**2),
                'book_over_brier':np.mean((valid.market_over_no_vig-valid.actual_over)**2),
                'qualification':'Book-line observations correlated; line is a threshold, not an expected mean'})
    save(output,'player_line_summary',pd.DataFrame(summary))
    return rows


def plots(output,games,bins):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(1,2,figsize=(11,4))
    ax[0].plot([0,1],[0,1],ls='--',color='grey')
    ax[0].errorbar(bins.probability,bins.outcome_rate,yerr=[bins.outcome_rate-bins.ci_low,bins.ci_high-bins.outcome_rate],fmt='o')
    ax[0].set(xlabel='Original home win probability',ylabel='Observed home win rate',title='Weeks 1–6 · game bootstrap 95% CI')
    for week,b in games.groupby('week'):
        ax[1].scatter([week]*len(b),b.model_margin-b.home_score+b.away_score,alpha=.4)
    ax[1].axhline(0,color='grey');ax[1].set(xlabel='2026 week',ylabel='Home margin prediction − actual',title='Original frozen forecasts')
    fig.tight_layout();fig.savefig(output/'calibration_and_margin.png',dpi=160);plt.close(fig)


def run(root, output, *, resume=False, schedule_evidence=None):
    output=output.resolve()
    if not output.is_relative_to(root/'reports'):
        raise ValueError('Research output must be a new reports subdirectory')
    if (output/'completed_manifest.json').exists():
        raise ValueError('Completed research run is immutable; choose a new output directory')
    if (output/'intermediate_attempt_status.json').exists():
        raise ValueError('Superseded intermediate evidence cannot be resumed; choose a new output directory')
    if resume:
        verify(root,output)
    else:
        output.mkdir(parents=True,exist_ok=False)
        json_write(output/'protected_before.json',protect(root));json_write(output/'extra_before.json',extra_hashes(root))
        json_write(output/'execution.json',{'created_at_utc':datetime.now(timezone.utc).isoformat(),'network_requests':0,
            'policy':'Append-only offline research; no model registration, retraining champion, capture, or publication'})
    if schedule_evidence:
        from nfl2026.live.partial_week import classify_game_eligibility
        source=schedule_evidence.resolve()
        provenance=json.loads(source.with_suffix('.source.json').read_text())
        if sha256_file(source)!=provenance['sha256']:
            raise ValueError('Independent schedule hash mismatch')
        age=pd.Timestamp(datetime.now(timezone.utc))-pd.Timestamp(provenance['retrieved_at_utc'])
        if not pd.Timedelta(0)<=age<=pd.Timedelta(hours=1):
            raise ValueError('Independent schedule evidence is future or older than one hour')
        states=classify_game_eligibility(pd.read_csv(source,low_memory=False),2026,7,datetime.now(timezone.utc))
        save(output,'week7_independent_states',states)
        provenance['archived_source_path']=str(source.relative_to(root))
        json_write(output/'week7_state_source.json',provenance)
    games,players=load_official(root,output)
    quotes=load_quotes(root,output,games)
    metrics=benchmarks(root,output,games,quotes)
    bins=calibration(output,games)
    ledger=ledger_audit(root,output)
    bias=player_diagnostics(output,players,quotes,games)
    plots(output,games,bins)
    from nfl2026.research.review_challengers import evaluate
    challengers=evaluate(root,output,players)
    result={'games':len(games),'players':len(players),'metrics':metrics,'ledger':ledger,'bias':bias,'challengers':challengers,
        'integrity':verify(root,output)}
    json_write(output/'summary.json',finite(result))
    return finite(result)
