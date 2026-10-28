from __future__ import annotations

from datetime import datetime

import pandas as pd

from nfl2026.live.providers.base import ProviderResult, utc_iso


DEPTH_URL = "https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv"


def _role(rank: object) -> str:
    value = pd.to_numeric(rank, errors="coerce")
    if pd.isna(value):
        return "DEPTH_UNKNOWN"
    if int(value) == 1:
        return "STARTER"
    if int(value) == 2:
        return "PRIMARY_BACKUP"
    return "RESERVE"


def load_depth(raw: pd.DataFrame, scheduled_teams: set[str], cutoff: datetime, retrieved_at: datetime, season: int, *, roster=None, injuries=None, transactions=None, transaction_coverage=None) -> ProviderResult:
    frame = raw.copy()
    frame["dt"] = pd.to_datetime(frame.dt, utc=True, errors="coerce")
    eligible = frame[frame.dt.le(pd.Timestamp(cutoff)) & frame.team.isin(scheduled_teams)].copy()
    if eligible.empty:
        latest = pd.NaT
        records = pd.DataFrame(columns=["season","team","player_id","player_name","position","depth_order","depth_role","depth_source","known_at_utc","retrieved_at_utc","source"])
    else:
        latest = eligible.dt.max()
        from nfl2026.live.providers.depth_selection import select_position_snapshots
        current,position_audit=select_position_snapshots(eligible,scheduled_teams,cutoff,roster=roster,injuries=injuries,transactions=transactions,transaction_coverage=transaction_coverage)
        current["source_pos_rank"] = pd.to_numeric(current.pos_rank, errors="coerce")
        if {"pos_grp_id", "pos_id", "pos_slot"}.issubset(current.columns):
            # pos_rank is an overall position ranking; each distinct depth slot is
            # a parallel lineup place (e.g. WR1/WR2/WR3), not a reserve tier.
            slot_columns = ["team", "pos_abb", "pos_grp_id", "pos_id", "pos_slot"]
            current["depth_order"] = current['snapshot_depth_order'] if 'snapshot_depth_order' in current else current.groupby(slot_columns, dropna=False)["source_pos_rank"].rank(method="first", ascending=True).astype("Int64")
            current["depth_slot"] = current["pos_grp_id"].astype(str)+":"+current["pos_id"].astype(str)+":"+current["pos_slot"].astype(str)
            current["depth_group"] = current["pos_grp"].astype(str) if "pos_grp" in current else current["pos_grp_id"].astype(str)
        else:
            current["depth_order"] = current.get('snapshot_depth_order',current["source_pos_rank"].astype("Int64"))
            current["depth_slot"] = current.get("pos_slot", pd.Series("",index=current.index)).astype(str)
            current["depth_group"] = current.get("pos_grp", pd.Series("",index=current.index)).astype(str)
        records = pd.DataFrame({
            "season": season,
            "team": current.team,
            "player_id": current.gsis_id,
            "player_name": current.player_name,
            "position": current.pos_abb,
            "depth_order": current.depth_order,
            "depth_role": current.depth_order.map(_role),
            "depth_slot": current.depth_slot,
            "depth_group": current.depth_group,
            "source_pos_rank": current.source_pos_rank.astype("Int64"),
            "depth_source": "NFLVERSE_CURRENT",
            "known_at_utc": current.dt.map(lambda value: value.isoformat()),
            "retrieved_at_utc": utc_iso(retrieved_at),
            "source": "nflverse depth_charts release",
            "snapshot_selection": current.get('snapshot_selection','LATEST_TEAM_POSITION'),
            "source_age_hours": current.get('snapshot_age_hours',0),
        }).reset_index(drop=True)
        records = records.sort_values(["team","player_id","depth_order","depth_group","depth_slot"],na_position="last")
        records = records.drop_duplicates(["team","player_id","position"],keep="first").reset_index(drop=True)
    coverage = pd.DataFrame({"team": sorted(scheduled_teams)})
    counts = records.groupby("team").size() if len(records) else pd.Series(dtype=int)
    coverage["records"] = coverage.team.map(counts).fillna(0).astype(int)
    coverage["coverage_status"] = coverage.records.gt(0).map({True:"EXACT_CURRENT_DEPTH", False:"MODEL_INFERRED_FALLBACK"})
    coverage["latest_known_at_utc"] = latest.isoformat() if pd.notna(latest) else ""
    coverage["source_age_hours"] = round((retrieved_at-latest.to_pydatetime()).total_seconds()/3600, 2) if pd.notna(latest) else ""
    exact = int(coverage.coverage_status.eq("EXACT_CURRENT_DEPTH").sum())
    status = "READY" if exact == len(scheduled_teams) else "READY_WITH_WARNINGS"
    warnings = [] if status == "READY" else [f"exact current depth {exact}/{len(scheduled_teams)}; remaining teams use model-inferred roles"]
    if len(eligible) and len(position_audit):
        issues=position_audit[position_audit.position.isin(['QB','RB','WR','TE'])&position_audit.accepted_rows.eq(0)]
        coverage['position_audit']=coverage.team.map(position_audit.groupby('team').apply(lambda g:g.to_json(orient='records'),include_groups=False))
        if len(issues):
            status='READY_WITH_WARNINGS';warnings.extend(f"{r.team}/{r.position}: {r.selection_state}" for r in issues.itertuples())
    return ProviderResult(
        name="depth", records=records, coverage=coverage, status=status,
        source_url=DEPTH_URL.format(season=season), retrieved_at_utc=utc_iso(retrieved_at),
        source_known_at_utc=latest.isoformat() if pd.notna(latest) else utc_iso(retrieved_at),
        source_known_at_basis="row-level nflverse depth snapshot dt",
        reliability="EXACT_CURRENT_DEPTH_WITH_MODEL_FALLBACK", warnings=warnings,
    )


def infer_missing_roles(players: pd.DataFrame) -> pd.DataFrame:
    """Transparent fallback: rank modelled opportunity within team/position."""
    out = players.copy()
    missing = out.depth_source.isna() | out.depth_source.eq("")
    opportunity = out.expected_carries.fillna(0) + out.expected_targets.fillna(0)
    ranks = opportunity.groupby([out.team, out.position]).rank(method="first", ascending=False).astype(int)
    out.loc[missing, "depth_order"] = ranks[missing]
    out.loc[missing, "depth_role"] = ranks[missing].map(_role)
    out.loc[missing, "depth_source"] = "MODEL_INFERRED"
    return out


def merge_offensive_depth(players: pd.DataFrame, depth: pd.DataFrame, roster: pd.DataFrame) -> pd.DataFrame:
    """Special-teams assignments cannot overwrite the offensive lineup slot.

    NFLverse roster RB includes fullbacks; use its explicit FB depth position,
    never guess from names or create one starter per broad position group.
    """
    out=players.copy()
    out['_depth_position']=out.position
    if {'gsis_id','team','depth_chart_position'}.issubset(roster):
        fb=roster[roster.depth_chart_position.eq('FB')][['gsis_id','team']].dropna()
        keys=set(map(tuple,fb.astype(str).to_numpy()))
        mask=[position=='RB' and (str(pid),str(team)) in keys for pid,team,position in zip(out.player_id,out.team,out.position)]
        out.loc[mask,'_depth_position']='FB'
    if 'position' not in depth:
        raise ValueError('Offensive depth source requires structured position identity')
    d=depth[depth.position.isin(['QB','RB','FB','WR','TE'])].sort_values('depth_order',na_position='last').drop_duplicates(['player_id','team','position'],keep='first')
    d=d[['player_id','team','position','depth_order','depth_role','depth_source']].rename(columns={'position':'_depth_position'})
    return out.merge(d,on=['player_id','team','_depth_position'],how='left',validate='many_to_one').drop(columns='_depth_position')
