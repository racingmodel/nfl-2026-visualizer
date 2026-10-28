"""Local research transforms. Never register models, write forecasts or fetch odds."""
from datetime import datetime, timezone
import numpy as np
import pandas as pd


def normalized_historical_scores(frame):
    frame=frame.copy()
    for alias,canonical in [('pred_home_score','home_expected_points'),('pred_away_score','away_expected_points'),('actual_home_score','home_score'),('actual_away_score','away_score')]:
        both=frame[alias].notna()&frame[canonical].notna()
        if not np.allclose(frame.loc[both,alias],frame.loc[both,canonical]):raise ValueError('Conflicting score aliases '+alias)
        frame[alias]=frame[alias].fillna(frame[canonical])
    if frame[['pred_home_score','pred_away_score','actual_home_score','actual_away_score']].isna().any().any():raise ValueError('Missing score evidence')
    return frame


def score_pair(margin, total):
    margin, total = np.asarray(margin, float), np.asarray(total, float)
    if not np.isfinite(margin).all() or not np.isfinite(total).all() or (total < np.abs(margin)).any():
        raise ValueError('Impossible margin/total')
    return (total + margin) / 2, (total - margin) / 2


def weights(train):
    """Two independent MAE selections. Tie goes to less market influence."""
    if len(train) < 20 or train.week.nunique() < 2:
        raise ValueError('Insufficient prior chronological training')
    result = {}
    for name, model, market, actual in [
        ('margin', train.pred_home_score-train.pred_away_score, -train.home_handicap, train.actual_home_score-train.actual_away_score),
        ('total', train.pred_home_score+train.pred_away_score, train.market_total, train.actual_home_score+train.actual_away_score)]:
        grid = np.linspace(0, 1, 21)
        loss = [np.abs((1-w)*model+w*market-actual).mean() for w in grid]
        result[name] = float(grid[np.argmin(loss)])
    return result


def blend(frame, selected):
    margin = (1-selected['margin'])*(frame.pred_home_score-frame.pred_away_score)-selected['margin']*frame.home_handicap
    total = (1-selected['total'])*(frame.pred_home_score+frame.pred_away_score)+selected['total']*frame.market_total
    return score_pair(margin, total)


def score_metrics(actual_home, actual_away, home, away):
    ah, aa, h, a = [np.asarray(x, float) for x in [actual_home, actual_away, home, away]]
    valid = ah != aa
    return dict(games=len(h), team_score_mae=float(np.abs(np.r_[h-ah,a-aa]).mean()),
        margin_mae=float(np.abs((h-a)-(ah-aa)).mean()), total_mae=float(np.abs((h+a)-(ah+aa)).mean()),
        margin_bias=float(((h-a)-(ah-aa)).mean()),
        score_winner_accuracy=float(np.mean(((h-a)[valid]>0)==((ah-aa)[valid]>0))))


def quote_reason(row, cutoff, kickoff, max_age_minutes=120):
    cutoff, kickoff = pd.Timestamp(cutoff), pd.Timestamp(kickoff)
    if cutoff.tzinfo is None or kickoff.tzinfo is None:
        return 'TIMEZONE_MISSING'
    if cutoff >= kickoff-pd.Timedelta(minutes=1): return 'KICKOFF_CUTOFF_REACHED'
    if str(row.get('suspended', False)).lower() in ['true','1']: return 'SUSPENDED'
    source = pd.to_datetime(row.get('source_timestamp_utc'), utc=True, errors='coerce')
    retrieved = pd.to_datetime(row.get('retrieval_timestamp_utc'), utc=True, errors='coerce')
    if pd.isna(source) or pd.isna(retrieved): return 'SOURCE_TIMESTAMP_MISSING'
    if source > retrieved or retrieved > cutoff: return 'FUTURE_OR_CONFLICTING_TIMESTAMP'
    if (cutoff-source).total_seconds() > max_age_minutes*60: return 'STALE_OVER_120_MINUTES'
    if source >= kickoff or retrieved >= kickoff: return 'POST_KICKOFF_QUOTE'
    if not np.isfinite(float(row.get('decimal_odds', np.nan))) or float(row['decimal_odds']) <= 1: return 'INVALID_PRICE'
    return ''


def prior_observations(observations, player, team, cutoff, limit=8):
    """Conservative completed-game lag. Availability is separate from publication proof."""
    cutoff = pd.Timestamp(cutoff)
    if cutoff.tzinfo is None: raise ValueError('Naive cutoff')
    p=observations[observations.player_id.eq(player)&observations.team.eq(team)].copy()
    p=p[p.observed_kickoff.lt(cutoff-pd.Timedelta(hours=48))]
    return p.sort_values('observed_kickoff').tail(limit)


def quarter_shares(scores):
    values=np.asarray(scores,float)
    if not np.isfinite(values).all() or (values < 0).any() or values.sum() <= 0: raise ValueError('Invalid quarter shares')
    return values/values.sum()


def require_pregame(row, now):
    if row['eligibility_state']!='PRE_KICKOFF':raise ValueError(row['eligibility_state'])
    if pd.Timestamp(now)>=pd.Timestamp(row['kickoff_utc'])-pd.Timedelta(minutes=1):raise ValueError('CUT_OFF')


def player_reason(row):
    if row['roster_status']!='ACT':return 'NOT_ACTIVE_ROSTER'
    if row['role_status']!='EXPECTED_STARTER' or row['role_confidence_label']!='HIGH':return 'EXPECTED_STARTER_NOT_HIGH_CONFIDENCE'
    if row['injury_source_state']!='VALID':return 'INJURY_SOURCE_NOT_VALID'
    if row['availability_status'] not in ['AVAILABLE','UNKNOWN']:return 'UNRESOLVED_OR_UNAVAILABLE'
    if str(row['injury_status']).upper() in ['QUESTIONABLE','DOUBTFUL','OUT','IR','INACTIVE']:return 'UNRESOLVED_OR_UNAVAILABLE'
    return ''
