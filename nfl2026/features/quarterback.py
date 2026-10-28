from __future__ import annotations

import numpy as np
import pandas as pd

from nfl2026.config import Settings


def _expected_depth_qbs(settings: Settings, fixtures: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for season in (2022, 2023, 2024):
        depth = pd.read_csv(settings.legacy_root / f"{season} Stats" / f"depth_charts_{season}.csv", low_memory=False)
        depth = depth[depth["game_type"].eq("REG") & depth["position"].eq("QB") & depth["week"].notna()].copy()
        depth["week"] = depth["week"].astype(int)
        depth = depth.sort_values(["club_code", "week", "depth_team"]).drop_duplicates(["club_code", "week"])
        depth = depth.rename(columns={"club_code": "team", "gsis_id": "expected_qb_id", "full_name": "expected_qb_name"})
        depth["season"] = season
        depth["qb_source_reliability"] = "WEEKLY_ASSUMED_PREGAME"
        rows.append(depth[["season", "week", "team", "expected_qb_id", "expected_qb_name", "qb_source_reliability"]])
    season = 2025
    depth = pd.read_csv(settings.legacy_root / "2025 Stats" / "depth_charts_2025.csv", low_memory=False)
    depth = depth[depth["pos_abb"].eq("QB") & depth["gsis_id"].notna()].copy()
    depth["dt"] = pd.to_datetime(depth["dt"], utc=True)
    for fixture in fixtures[fixtures.season.eq(season)].itertuples(index=False):
        eligible = depth[(depth.team.eq(fixture.team)) & (depth.dt < fixture.kickoff_utc)]
        if eligible.empty:
            continue
        snapshot = eligible.dt.max()
        qb = eligible[eligible.dt.eq(snapshot)].sort_values("pos_rank").iloc[0]
        rows.append(pd.DataFrame([{"season": season, "week": fixture.week, "team": fixture.team, "expected_qb_id": qb.gsis_id, "expected_qb_name": qb.player_name, "qb_source_reliability": "EXACT_POINT_IN_TIME"}]))
    return pd.concat(rows, ignore_index=True).drop_duplicates(["season", "week", "team"], keep="last")


def _qb_game_history(settings: Settings) -> pd.DataFrame:
    frames = []
    columns = ["game_id", "season", "season_type", "week", "posteam", "passer_player_id", "pass_attempt", "sack", "epa", "success", "yards_gained", "interception", "qb_kneel", "qb_spike"]
    for season in settings.training_seasons:
        raw = pd.read_parquet(settings.legacy_root / f"{season} Stats" / f"pbp_{season}.parquet", columns=columns)
        raw = raw[raw.season_type.eq("REG") & raw.passer_player_id.notna() & raw.qb_kneel.fillna(0).ne(1) & raw.qb_spike.fillna(0).ne(1)].copy()
        for c in ("pass_attempt", "sack", "epa", "success", "yards_gained", "interception"):
            raw[c] = pd.to_numeric(raw[c], errors="coerce").fillna(0)
        raw["dropback"] = ((raw.pass_attempt.eq(1)) | (raw.sack.eq(1))).astype(int)
        agg = raw.groupby(["game_id", "season", "week", "posteam", "passer_player_id"], as_index=False).agg(dropbacks=("dropback","sum"), epa_sum=("epa","sum"), successes=("success","sum"), yards=("yards_gained","sum"), interceptions=("interception","sum"), sacks=("sack","sum"))
        frames.append(agg)
    return pd.concat(frames, ignore_index=True)


def augment_with_qb_features(settings: Settings, matrix: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    fixtures = pd.concat([
        matrix[["game_id","season","week","kickoff_utc","home_team"]].rename(columns={"home_team":"team"}),
        matrix[["game_id","season","week","kickoff_utc","away_team"]].rename(columns={"away_team":"team"}),
    ], ignore_index=True)
    fixtures["kickoff_utc"] = pd.to_datetime(fixtures["kickoff_utc"], utc=True)
    expected = _expected_depth_qbs(settings, fixtures)
    history = _qb_game_history(settings).merge(fixtures[["game_id","kickoff_utc"]].drop_duplicates(), on="game_id", how="left")
    history = history.sort_values(["passer_player_id","kickoff_utc","game_id"])
    for numerator, output in (("epa_sum","qb_epa_per_dropback"),("successes","qb_success_rate"),("yards","qb_yards_per_dropback"),("interceptions","qb_interception_rate"),("sacks","qb_sack_rate")):
        prior_num = history.groupby("passer_player_id")[numerator].cumsum() - history[numerator]
        prior_den = history.groupby("passer_player_id")["dropbacks"].cumsum() - history["dropbacks"]
        history[output] = prior_num / prior_den.replace(0,np.nan)
    history["effective_utc"] = history["kickoff_utc"] + pd.Timedelta(nanoseconds=1)
    rating_cols = ["qb_epa_per_dropback","qb_success_rate","qb_yards_per_dropback","qb_interception_rate","qb_sack_rate"]
    state = history[["passer_player_id","effective_utc",*rating_cols]].sort_values(["effective_utc","passer_player_id"])
    expected = expected.merge(fixtures, on=["season","week","team"], how="inner")
    parts=[]
    for qb_id, group in expected.groupby("expected_qb_id", dropna=False, sort=False):
        hist = state[state.passer_player_id.eq(qb_id)].drop(columns="passer_player_id").sort_values("effective_utc")
        group = group.sort_values("kickoff_utc")
        parts.append(pd.merge_asof(group,hist,left_on="kickoff_utc",right_on="effective_utc",direction="backward",allow_exact_matches=False) if len(hist) else group.assign(**{c:np.nan for c in rating_cols}))
    expected = pd.concat(parts,ignore_index=True)
    expected = expected.sort_values(["team","kickoff_utc"])
    expected["qb_replacement"] = expected.expected_qb_id.ne(expected.groupby("team").expected_qb_id.shift(1)).astype(float)
    league = history[rating_cols].median()
    expected[rating_cols] = expected[rating_cols].fillna(league).fillna(0)
    home = expected.rename(columns={"team":"home_team", **{c:f"home_{c}" for c in ["expected_qb_id","expected_qb_name","qb_source_reliability","qb_replacement",*rating_cols]}})
    away = expected.rename(columns={"team":"away_team", **{c:f"away_{c}" for c in ["expected_qb_id","expected_qb_name","qb_source_reliability","qb_replacement",*rating_cols]}})
    out = matrix.merge(home[["game_id","home_team",*[f"home_{c}" for c in ["expected_qb_id","expected_qb_name","qb_source_reliability","qb_replacement",*rating_cols]]]],on=["game_id","home_team"],how="left")
    out = out.merge(away[["game_id","away_team",*[f"away_{c}" for c in ["expected_qb_id","expected_qb_name","qb_source_reliability","qb_replacement",*rating_cols]]]],on=["game_id","away_team"],how="left")
    features=[f"{side}_{c}" for side in ("home","away") for c in ["qb_replacement",*rating_cols]]
    return out,features
