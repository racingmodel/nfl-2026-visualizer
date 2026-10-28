"""Nominal-roster QB passing research, not a certified expected-starter model."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from threadpoolctl import threadpool_limits

from nfl2026.phase7.common import json_write
from nfl2026.five_week_review import immutable_research_artifact
from nfl2026.utilities.io import sha256_file

PASS_FIELDS = ["pass_attempts", "passing_yards", "passing_tds"]


def aggregate_passing(pbp):
    from nfl2026.data.passing_contract import aggregate_individual_passing
    return aggregate_individual_passing(pbp)[['game_id','team','player_id',*PASS_FIELDS]]


def lag_passing(candidates, history):
    """Passing observations are outcomes; only strictly earlier games enter X."""
    out = candidates.copy().reset_index(drop=True)
    history = history[history.pass_attempts.gt(0)].copy()
    history["time"] = pd.to_datetime(history.kickoff_utc, utc=True)
    for field in PASS_FIELDS:
        out[f"lag_{field}"] = np.nan
    out["passing_games_before"] = 0
    indexed = {player: group.sort_values("time") for player, group in history.groupby("player_id")}
    for index, row in out.iterrows():
        prior = indexed.get(row.player_id)
        if prior is None:
            continue
        prior = prior[prior.time.lt(pd.Timestamp(row.kickoff_utc))]
        out.loc[index, "passing_games_before"] = len(prior)
        if len(prior):
            out.loc[index, [f"lag_{f}" for f in PASS_FIELDS]] = prior.tail(8)[PASS_FIELDS].mean().to_numpy()
    return out


def build_passing_research(settings):
    import pyarrow.parquet as parquet
    from nfl2026.data.canonical import _kickoff_utc
    from nfl2026.phase7.player_v4 import build_research_panel
    panel, player_features = build_research_panel(settings)
    panel = panel[panel.position.eq("QB")].copy()
    games = pd.read_csv(settings.artifact_root / "canonical/canonical_games.csv", low_memory=False)
    parts, source_hashes = [], {}
    cols = ["game_id", "play_id", "posteam", "passer_player_id", "passing_yards", "pass_touchdown", "pass_attempt",
            "sack", "two_point_attempt", "play_type", "complete_pass", "season_type"]
    for season in (2022, 2023, 2024, 2025):
        path = settings.root / f"NFL Fantasy/{season} Stats/pbp_{season}.parquet"
        pbp = parquet.read_table(path, columns=cols).to_pandas()
        pbp = pbp[pbp.season_type.eq("REG")]
        expected = set(games[games.season.eq(season) & games.game_type.eq("REG")].game_id)
        if set(pbp.game_id) != expected:
            raise ValueError(f"Incomplete historical passing PBP: {season}")
        part = aggregate_passing(pbp)
        part["season"] = season
        parts.append(part)
        source_hashes[str(path.relative_to(settings.root))] = sha256_file(path)
    history = pd.concat(parts, ignore_index=True).merge(games[["game_id", "kickoff_utc"]], on="game_id", validate="many_to_one")
    data = lag_passing(panel, history)
    data = data.merge(history[["game_id", "team", "player_id"] + PASS_FIELDS], on=["game_id", "team", "player_id"], how="left", validate="one_to_one")
    # Complete PBP coverage makes absence a known no-pass outcome. It is not
    # used to select an actual starter or drop backup quarterbacks.
    data[PASS_FIELDS] = data[PASS_FIELDS].fillna(0)
    features = player_features + [f"lag_{f}" for f in PASS_FIELDS] + ["passing_games_before"]
    predictions, metrics = [], []
    with threadpool_limits(2):
        for season in (2023, 2024, 2025):
            train, test = data[data.season.lt(season)], data[data.season.eq(season)]
            if pd.to_datetime(train.kickoff_utc, utc=True).max() >= pd.to_datetime(test.kickoff_utc, utc=True).min():
                raise ValueError("QB target fold entered training")
            models = {}
            out = test[["game_id", "player_id", "player_name", "team", "season", "week", "kickoff_utc"] + PASS_FIELDS].copy()
            for field in PASS_FIELDS:
                model = make_pipeline(SimpleImputer(strategy="median"), ExtraTreesRegressor(n_estimators=80, max_depth=6,
                    min_samples_leaf=10, max_features=.75, random_state=20261014, n_jobs=2))
                model.fit(train[features], train[field])
                models[field] = model
                out[f"pred_{field}"] = model.predict(test[features])
                # Strict lag-only no fitted role baseline; missing history stays
                # a fold-fitted median, not retrospective starter recognition.
                baseline = test[f"lag_{field}"].fillna(train[field].median()).to_numpy()
                error = out[f"pred_{field}"].to_numpy() - test[field].to_numpy()
                metrics.append({"season": season, "target": field, "rows": len(test), "mae": float(np.abs(error).mean()),
                    "rmse": float(np.sqrt((error**2).mean())), "bias": float(error.mean()),
                    "lag_baseline_mae": float(np.mean(np.abs(baseline-test[field].to_numpy()))), "starter_provenance_certified": False})
            out["max_training_kickoff_utc"] = pd.to_datetime(train.kickoff_utc, utc=True).max().isoformat()
            predictions.append(out)
            if season == 2025:
                immutable_research_artifact(settings.artifact_root / "research/phase7/models/qb-passing-roster-research-v1.joblib",
                    {"features": features, "models": models}, {"training_seasons": [2022, 2023, 2024],
                     "max_training_kickoff_utc": out.max_training_kickoff_utc.iloc[0], "source_hashes": source_hashes,
                     "population": "all nominal roster QBs, actual starter never used as X", "production_registered": False})
    pd.concat(predictions).to_csv(settings.artifact_root / "research/phase7/qb_passing_oos.csv", index=False)
    history.to_csv(settings.artifact_root / "research/phase7/qb_passing_history.csv", index=False)
    pd.DataFrame(metrics).to_csv(settings.report_root / "phase7_qb_passing_metrics.csv", index=False)
    # Reconcile PBP targets against the independently present box-score fields.
    reconciliation = []
    for season in (2022, 2023, 2024, 2025):
        source = settings.root / f"NFL Fantasy/{season} Stats/weekly_{season}.csv"
        weekly = pd.read_csv(source, low_memory=False).rename(columns={"attempts": "pass_attempts", "recent_team": "team"})
        if "season_type" in weekly:
            weekly = weekly[weekly.season_type.eq("REG")]
        weekly = weekly[weekly.player_id.notna() & weekly.pass_attempts.gt(0)].copy()
        scheduled = games[games.season.eq(season)]
        team_games = pd.concat([scheduled[["game_id", "week", "home_team"]].rename(columns={"home_team": "team"}),
            scheduled[["game_id", "week", "away_team"]].rename(columns={"away_team": "team"})])
        weekly = weekly.merge(team_games, on=["week", "team"], validate="many_to_one")
        shared = weekly.merge(history[history.season.eq(season)], on=["game_id", "player_id", "team"], suffixes=("_weekly", "_pbp"))
        for field in PASS_FIELDS:
            if f"{field}_weekly" not in shared:
                reconciliation.append({"season": season, "field": field, "state": "WEEKLY_TARGET_UNAVAILABLE_PBP_USED"})
                continue
            difference = shared[f"{field}_weekly"] - shared[f"{field}_pbp"]
            reconciliation.append({"season": season, "field": field, "rows": len(shared), "mismatches": int(difference.abs().gt(1e-8).sum()),
                "max_absolute_difference": float(difference.abs().max()), "state": "AUDITED_NOT_SILENTLY_ASSUMED"})
    pd.DataFrame(reconciliation).to_csv(settings.report_root / "phase7_qb_passing_target_reconciliation.csv", index=False)
    result = {"status": "RESEARCH_ONLY", "historical_QB_rows": len(data), "OOS_rows": sum(len(p) for p in predictions),
        "passing_targets_from_complete_PBP": True, "actual_starter_used_as_predictor": False,
        "promotion": False, "remaining": ["historical pregame QB role and eligibility unproven", "passing count/yardage distribution calibration pending",
            "no official QB passing forecasts emitted", "PBP/weekly target reconciliation exceptions must be reviewed"]}
    json_write(settings.report_root / "phase7_qb_passing_summary.json", result)
    return result
