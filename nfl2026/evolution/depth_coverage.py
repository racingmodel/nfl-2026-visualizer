"""Position coverage diagnostics without changing frozen roles or inventing fallback."""
import pandas as pd


def position_coverage(raw,teams,cutoff,positions=('QB','RB','WR','TE')):
    required={'team','dt','pos_abb','gsis_id'}
    if not required.issubset(raw):raise ValueError('Structured depth identity missing')
    d=raw.copy();d['dt']=pd.to_datetime(d.dt,utc=True,errors='coerce');d=d[d.dt.le(pd.Timestamp(cutoff))]
    rows=[]
    for team in teams:
        group=d[d.team.eq(team)];latest=group.dt.max()
        for position in positions:
            p=group[group.pos_abb.eq(position)];recent=p.dt.max();count=int(p.dt.eq(latest).sum())
            rows.append(dict(team=team,position=position,latest_team_snapshot=latest,latest_position_snapshot=recent,
                rows_at_latest_team_snapshot=count,position_age_hours=float((pd.Timestamp(cutoff)-recent).total_seconds()/3600) if pd.notna(recent) else None,
                state='CURRENT_POSITION_ROWS' if count else 'BLOCKED_POSITION_GAP',prior_rows_are_automatically_eligible=False))
    return pd.DataFrame(rows)
