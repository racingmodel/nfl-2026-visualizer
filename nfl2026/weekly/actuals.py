"""Append-only postgame labels with exact identities and explicit unknowns."""
import numpy as np
import pandas as pd
from nfl2026.postgame import _regular_games,_completion_mask
from nfl2026.research.quarter_phase4 import strict_quarter_labels

STATS=['passing_yards','attempts','carries','rushing_yards','targets','receptions','receiving_yards','rushing_tds','receiving_tds','passing_tds']
def final_games(schedule,season,week):
    games=_regular_games(schedule,season,week)
    if not _completion_mask(games).all():raise ValueError('Full weekly ingestion requires all expected fixtures FINAL')
    if not np.isfinite(games[['home_score','away_score']].to_numpy(float)).all():raise ValueError('Unknown final scores')
    for k in ['home_score','away_score']:
        if not (games[k]>=0).all() or not games[k].eq(games[k].round()).all():raise ValueError('Noninteger final score')
    if 'kickoff_utc' not in games:
        games['kickoff_utc']=pd.to_datetime(games.gameday.astype(str)+' '+games.gametime.fillna('13:00').astype(str)).dt.tz_localize('America/New_York').dt.tz_convert('UTC')
    return games

def normalize_player_stats(stats,games,season,week,source):
    rows=stats[pd.to_numeric(stats.season,errors='coerce').eq(season)&pd.to_numeric(stats.week,errors='coerce').eq(week)].copy()
    if 'season_type' in rows:rows=rows[rows.season_type.eq('REG')]
    if rows.empty:raise ValueError('Requested season/week has no structured stats rows')
    invalid=rows[rows.player_id.isna()|rows.player_id.astype(str).str.strip().eq('')].copy();rows=rows.drop(invalid.index)
    if rows.duplicated(['game_id','player_id','team']).any():raise ValueError('Duplicate postgame player identity')
    expected={r.game_id:{r.home_team:r.away_team,r.away_team:r.home_team} for r in games.itertuples()}
    for r in rows.itertuples():
        if r.game_id not in expected or r.team not in expected[r.game_id] or r.opponent_team!=expected[r.game_id][r.team]:raise ValueError('Player stats exact fixture/team/opponent mismatch')
    for c in STATS:
        rows[c]=pd.to_numeric(rows[c],errors='coerce') if c in rows else np.nan
        if np.isinf(rows[c]).any():raise ValueError('Infinite actual statistic is invalid')
        # Yardage may legitimately be negative. Counts cannot.
        if c not in ['passing_yards','rushing_yards','receiving_yards'] and rows[c].dropna().lt(0).any():raise ValueError('Negative player count')
    rows['anytime_offensive_tds']=rows.rushing_tds+rows.receiving_tds
    rows['anytime_offensive_td']=rows.anytime_offensive_tds.gt(0).astype(float).where(rows.anytime_offensive_tds.notna())
    rows['label_source_url']=source['url'];rows['label_source_sha256']=source['sha256'];rows['label_observed_at_utc']=source['observed_at_utc'];rows['label_source_modified_at_utc']=source.get('last_modified')
    rows['label_publication_time']='UNKNOWN_PER_ROW';rows['participation_status']='STATS_ROW_PRESENT_NOT_A_PREGAME_ACTIVE_CERTIFICATE'
    return rows,invalid

def normalize_quarters(pbp,games,source_sha256,observed_at):
    p=pbp[pbp.game_id.isin(games.game_id)].copy()
    labels,_=strict_quarter_labels(p,games)
    labels['label_source_sha256']=source_sha256;labels['label_observed_at_utc']=observed_at;labels['source_publication_time']='UNKNOWN_LOCAL_ARCHIVE'
    return labels

def snap_identity_matches(snaps,roster,games):
    """PFR-ID to GSIS joins only; names are never fuzzy matched."""
    r=roster.copy();pfr=next((c for c in ['pfr_id','pfr_player_id'] if c in r),None)
    if pfr is None:return pd.DataFrame(),dict(status='UNRESOLVED_CROSSWALK',source_rows=len(snaps),matched_rows=0,unresolved_rows=len(snaps),reason='Roster has no explicit PFR-ID crosswalk')
    s=snaps[snaps.game_id.isin(games.game_id)].copy()
    r=r[r[pfr].notna()&r.gsis_id.notna()].copy()
    mapping=r[[pfr,'gsis_id','team']].drop_duplicates()
    if mapping.duplicated([pfr,'team']).any():raise ValueError('Ambiguous PFR-GSIS roster mapping')
    s=s.merge(mapping,left_on=['pfr_player_id','team'],right_on=[pfr,'team'],how='left',validate='many_to_one');s=s.rename(columns={'gsis_id':'player_id'})
    found=s[s.player_id.notna()].copy()
    if found.duplicated(['game_id','player_id','team']).any():raise ValueError('Duplicate snap participant identity')
    return found,dict(status='EXPLICIT_ID_CROSSWALK',source_rows=len(s),matched_rows=len(found),unresolved_rows=int(s.player_id.isna().sum()))
