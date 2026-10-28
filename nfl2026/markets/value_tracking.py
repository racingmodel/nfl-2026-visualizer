"""Read-only performance diagnostics. Never resettles or rewrites the ledger."""
import math
import numpy as np
import pandas as pd
from nfl2026.markets.certified_value import contract_key, stamp, clean

def calibration_metrics(probabilities, outcomes):
    p = np.asarray(probabilities,dtype=float); y = np.asarray(outcomes,dtype=float)
    if len(p) != len(y) or not len(p) or not np.isfinite(p).all() or not np.isin(y,[0,1]).all() or ((p<0)|(p>1)).any():
        raise ValueError('Finite, paired binary probabilities/outcomes required')
    buckets = np.minimum((p*10).astype(int),9)
    ece = sum(np.sum(buckets==b)/len(p)*abs(float(p[buckets==b].mean()-y[buckets==b].mean())) for b in set(buckets))
    pc = np.clip(p,1e-12,1-1e-12)
    return dict(n=len(p),brier=float(np.mean((p-y)**2)),log_loss=float(-np.mean(y*np.log(pc)+(1-y)*np.log1p(-pc))),
                bias=float((p-y).mean()),ece=float(ece))

def block_roi_interval(group, repeats=1000):
    blocks = group.groupby('game_id')[['profit','stake']].sum().to_numpy()
    if len(blocks) < 2: return None
    rng = np.random.default_rng(60126)
    draws = blocks[rng.integers(0,len(blocks),(repeats,len(blocks)))].sum(axis=1)
    return np.quantile(draws[:,0]/draws[:,1],[.025,.975]).tolist()

def performance_audit(ledger, fixture_clock):
    data = ledger.copy()
    if data.ledger_entry_id.duplicated().any(): raise ValueError('Duplicate ledger identities')
    data['week'] = data.game_id.astype(str).str.extract(r'^\d{4}_(\d{2})_')[0]
    data['kickoff'] = pd.to_datetime(data.game_id.map(fixture_clock),utc=True,errors='coerce')
    data['source'] = pd.to_datetime(data.market_timestamp_utc,utc=True,errors='coerce')
    data['publication'] = pd.to_datetime(data.captured_at_utc,utc=True,errors='coerce')
    data['minutes_before_kickoff'] = (data.kickoff-data.source).dt.total_seconds()/60
    data['quote_observation_age_minutes'] = (data.publication-data.source).dt.total_seconds()/60
    # Fixed before inspection: equal pregame windows and contemporaneous captures.
    data['fair_window'] = data.minutes_before_kickoff.between(60,120) & data.quote_observation_age_minutes.between(0,15) & (data.publication<data.kickoff)
    settled = data[data.settlement_status.eq('SETTLED')].copy()
    settled['stake'] = pd.to_numeric(settled.hypothetical_stake,errors='raise')
    settled['returned'] = pd.to_numeric(settled.hypothetical_return,errors='raise')
    settled['profit'] = settled.returned-settled.stake
    if not np.isfinite(settled[['stake','returned']]).all().all() or (settled.stake<=0).any(): raise ValueError('Invalid recorded settlement amounts')
    rows = []
    for cohort, source in [('AS_RECORDED_NOT_CERTIFIED',settled),('FIXED_60_TO_120_MIN_WINDOW',settled[settled.fair_window])]:
        for dim in ['ALL','market_type','week','bookmaker','value_confidence']:
            groups = [('ALL',source)] if dim == 'ALL' else source.groupby(dim,dropna=False)
            for label,g in groups:
                if g.empty: continue
                stake = float(g.stake.sum()); profit = float(g.profit.sum())
                r = dict(cohort=cohort,dimension=dim,segment=str(label),n=len(g),games=int(g.game_id.nunique()),stake=stake,
                    profit=profit,hypothetical_roi=profit/stake,game_block_roi_ci95=block_roi_interval(g),
                    sample_guard='INSUFFICIENT_FOR_CERTIFICATION' if g.game_id.nunique()<200 else 'DIAGNOSTIC_ONLY_NO_SOURCE_CERTIFICATION')
                decisive = g[g.settlement_result.isin(['WIN','LOSS'])]
                if len(decisive): r['calibration_diagnostic_unverified_legacy_probability'] = calibration_metrics(pd.to_numeric(decisive.model_probability),decisive.settlement_result.eq('WIN').astype(int))
                rows.append(r)
    # Settlement timestamps were never stored. Do not report a fictitious
    # real-time drawdown; group complete games by kickoff as an explicit proxy.
    game_profit = settled.groupby(['kickoff','game_id'],dropna=False).profit.sum().groupby(level=0).sum().sort_index().cumsum()
    peak = game_profit.clip(lower=0).cummax()
    drawdown = float((game_profit-peak).min()) if len(game_profit) else None
    coverage=[]
    for dim in ['week','market_type','bookmaker','value_confidence']:
        for segment,g in data.groupby(dim,dropna=False):
            coverage.append(dict(dimension=dim,segment=str(segment),ledger_rows=len(g),status_counts=g.settlement_status.value_counts().to_dict()))
    return dict(ledger_rows=len(data),status_counts=data.settlement_status.value_counts().to_dict(),settled_rows=len(settled),coverage_breakdown=coverage,
        fixed_window=dict(minutes_before_kickoff=[60,120],maximum_observation_age_minutes=15,qualified_settled_rows=int(settled.fair_window.sum())),
        breakdown=rows,kickoff_block_hypothetical_drawdown_units=drawdown,
        drawdown_limitation='Kickoff-block proxy only: no contemporaneous settlement timestamps or live bankroll reconstruction',
        contemporaneous_clv_rows=0,clv_status='WITHHELD: historical ledger lacks pinned grading/executable closing-quote contracts',
        validity='Historical descriptive audit, not certified model-vs-market performance; superseded/open/blocked rows excluded from ROI'), data

def closing_line_value(entry, closing):
    """Price CLV only against a contemporaneously captured executable same contract."""
    required = ['period','overtime','stat_contract','rule_version','jurisdiction']
    if any(not clean(entry.get(k)) or not clean(closing.get(k)) for k in required): raise ValueError('Missing grading contract')
    if contract_key(entry) != contract_key(closing) or entry.get('bookmaker') != closing.get('bookmaker'): raise ValueError('CLV contract mismatch')
    kickoff = stamp(closing['kickoff_utc']); source = stamp(closing['source_timestamp_utc']); retrieval = stamp(closing['retrieval_timestamp_utc'])
    if not stamp(entry['source_timestamp_utc']) <= stamp(entry['retrieval_timestamp_utc']) < source <= retrieval < kickoff or not 0 < (kickoff-source).total_seconds() <= 300:
        raise ValueError('Not a contemporaneous pregame closing capture')
    if closing.get('executable') is not True or closing.get('quote_type','BACK') != 'BACK': raise ValueError('No executable closing back price')
    a,b = float(entry['decimal_odds']),float(closing['decimal_odds'])
    if min(a,b)<=1 or not math.isfinite(a+b): raise ValueError('Invalid CLV prices')
    return dict(price_clv=a/b-1,definition='entry_decimal_odds/closing_decimal_odds - 1',closing_quote_id=closing['quote_id'])
