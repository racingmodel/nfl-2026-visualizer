"""Point-in-time phase2-v2 team-form roll-forward."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from nfl2026.features.team_advanced import TEAM_METRICS, aggregate_team_pbp, _pbp_path
from nfl2026.utilities.io import sha256_file
from nfl2026.live.providers.base import fetch_bytes

FORM_METRICS = TEAM_METRICS + [f"def_{name}" for name in TEAM_METRICS if name not in {"points_for", "points_against"}]
MATCHUP_METRICS = ("epa_per_play", "pass_epa", "rush_epa", "success_rate", "explosive_rate", "turnover_rate")
PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet"


class TeamRollforwardError(RuntimeError):
    """Required completed-game source is absent, incomplete or post-cutoff."""


def frame_hash(frame: pd.DataFrame, keys: tuple[str, ...] = ("season", "week", "game_id", "team")) -> str:
    order = [key for key in keys if key in frame]
    data = frame.sort_values(order, kind="mergesort") if order else frame
    return hashlib.sha256(data.to_csv(index=False, na_rep="", float_format="%.12g", lineterminator="\n").encode()).hexdigest()


def build_online_form(completed_games: pd.DataFrame, pbp: pd.DataFrame,
                      targets: pd.DataFrame, window: int = 8) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reproduce the historical lagged last-eight team-game averages.

    No new prior/shrinkage is introduced. The caller must supply final games only.
    The entire target week is excluded even if some of its games have ended.
    """
    if window < 1:
        raise TeamRollforwardError("History window must be positive")
    required_games = {"game_id", "season", "week", "kickoff_utc", "home_team", "away_team", "home_score", "away_score"}
    if not required_games.issubset(completed_games):
        raise TeamRollforwardError(f"Game source lacks {sorted(required_games-set(completed_games))}")
    games = completed_games.copy()
    games["kickoff_utc"] = pd.to_datetime(games.kickoff_utc, utc=True, errors="coerce")
    if games.game_id.isna().any() or games.game_id.duplicated().any() or games.kickoff_utc.isna().any():
        raise TeamRollforwardError("Completed games have missing/duplicate ID or invalid kickoff")
    for col in ("home_score", "away_score"):
        games[col] = pd.to_numeric(games[col], errors="coerce")
    if games[["home_score", "away_score"]].isna().any().any():
        raise TeamRollforwardError("Non-final game in completed-game source")
    target = targets.copy()
    needed = {"game_id", "season", "week", "kickoff_utc", "home_team", "away_team"}
    if not needed.issubset(target) or target.game_id.isna().any() or target.game_id.duplicated().any():
        raise TeamRollforwardError("Target games missing fields or duplicated")
    target["kickoff_utc"] = pd.to_datetime(target.kickoff_utc, utc=True, errors="coerce")
    if target.kickoff_utc.isna().any():
        raise TeamRollforwardError("Target kickoff unavailable")
    for row in target.itertuples(index=False):
        if ((games.season.eq(row.season) & games.week.ge(row.week)) |
                games.kickoff_utc.ge(row.kickoff_utc)).any():
            raise TeamRollforwardError(f"Current/future outcome supplied for {row.game_id}")
    keys = ["game_id", "season", "week", "team", "opponent"]
    pbp_metrics = [m for m in TEAM_METRICS if m not in {"points_for", "points_against"}]
    if not (set(keys) | set(pbp_metrics)).issubset(pbp):
        raise TeamRollforwardError(f"PBP aggregate lacks {sorted((set(keys)|set(pbp_metrics))-set(pbp))}")
    relevant = pbp[pbp.game_id.isin(games.game_id)].copy()
    if relevant.duplicated(keys).any():
        raise TeamRollforwardError("Duplicate PBP team-game rows")
    home = games[list(required_games)].rename(columns={"home_team":"team", "away_team":"opponent", "home_score":"points_for", "away_score":"points_against"})
    away = games[list(required_games)].rename(columns={"away_team":"team", "home_team":"opponent", "away_score":"points_for", "home_score":"points_against"})
    source = pd.concat([home, away], ignore_index=True)
    if source.duplicated(["game_id", "team"]).any():
        raise TeamRollforwardError("Duplicate game/team identity")
    source = source.merge(relevant.drop(columns=[c for c in ("points_for", "points_against") if c in relevant]),
                          on=keys, how="left", validate="one_to_one", indicator=True)
    if source._merge.ne("both").any() or source.plays.isna().any():
        missing = source.loc[source._merge.ne("both") | source.plays.isna(), ["game_id", "team"]]
        raise TeamRollforwardError(f"Completed-game PBP missing: {missing.to_dict('records')[:8]}")
    defense = relevant[["game_id", "opponent", *pbp_metrics]].rename(columns={"opponent":"team", **{m:f"def_{m}" for m in pbp_metrics}})
    source = source.merge(defense, on=["game_id", "team"], how="left", validate="one_to_one")
    source = source.sort_values(["team", "kickoff_utc", "game_id"], kind="mergesort")
    rows, diagnostics = [], []
    for game in target.itertuples(index=False):
        out = {key:getattr(game,key) for key in ("game_id", "season", "week", "kickoff_utc", "home_team", "away_team")}
        for side, team in (("home", game.home_team), ("away", game.away_team)):
            history = source[source.team.eq(team) & source.kickoff_utc.lt(game.kickoff_utc) &
                             ((source.season.lt(game.season)) | (source.season.eq(game.season) & source.week.lt(game.week)))]
            recent = history.tail(window)
            for metric in FORM_METRICS:
                out[f"{side}_pregame_{metric}"] = float(pd.to_numeric(recent[metric], errors="coerce").mean()) if len(recent) else np.nan
            current = history[history.season.eq(game.season)]
            out[f"{side}_season_games_before"] = float(len(current))
            diagnostics.append({"game_id":game.game_id, "season":game.season, "target_week":game.week,
                                "team":team, "completed_games_incorporated":len(current),
                                "latest_incorporated_game":str(current.iloc[-1].game_id) if len(current) else "",
                                "current_season_sample_size":len(current),
                                "previous_season_games_in_window":int(recent.season.eq(game.season-1).sum()),
                                "prior_contribution":"last-eight cross-season games; no weighted shrinkage"})
        for metric in MATCHUP_METRICS:
            out[f"home_matchup_{metric}"] = out[f"home_pregame_{metric}"] - out[f"away_pregame_def_{metric}"]
            out[f"away_matchup_{metric}"] = out[f"away_pregame_{metric}"] - out[f"home_pregame_def_{metric}"]
        rows.append(out)
    return pd.DataFrame(rows), pd.DataFrame(diagnostics)


def current_pbp(settings, season: int, required_game_ids: set[str] | None = None,
                allow_download: bool = False) -> tuple[pd.DataFrame, Path, str]:
    path = _pbp_path(settings, season)
    required_game_ids = required_game_ids or set()
    def inspect() -> pd.DataFrame | None:
        if not path.exists():
            return None
        result = aggregate_team_pbp(settings, seasons=(season,))
        coverage = result[result.game_id.isin(required_game_ids)].groupby("game_id").team.nunique()
        if required_game_ids - set(coverage[coverage.eq(2)].index):
            return None
        return result
    data = inspect()
    if data is None and allow_download:
        try:
            body = fetch_bytes(PBP_URL.format(season=season), attempts=2, timeout_seconds=60)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temp.write_bytes(body)
            # The source is a current-season release, but only completed games
            # below target week are aggregated into the prediction state.
            pd.read_parquet(temp, columns=["game_id", "season", "week"]).head(1)
            temp.replace(path)
            data = inspect()
        except Exception as exc:
            raise TeamRollforwardError(f"Current-season PBP refresh failed: {exc}") from exc
    if data is None:
        raise TeamRollforwardError(f"Current-season PBP absent/incomplete for final games: {path}; no stale fallback")
    return data, path, sha256_file(path)


def elo_for_targets(completed_games: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    """Phase 2 historical Elo: 1500 opening, +45 home, K=20, simultaneous kickoffs."""
    games = completed_games.copy()
    games["kickoff_utc"] = pd.to_datetime(games.kickoff_utc, utc=True)
    target = targets.copy()
    target["kickoff_utc"] = pd.to_datetime(target.kickoff_utc, utc=True)
    events = pd.concat([games.assign(_target=False), target.assign(_target=True)], ignore_index=True)
    events = events.sort_values(["kickoff_utc", "game_id"], kind="mergesort")
    elo = defaultdict(lambda: 1500.0)
    rows = []
    for _, block in events.groupby("kickoff_utc", sort=True):
        for game in block.to_dict("records"):
            if game["_target"]:
                rows.append({"game_id":game["game_id"], "home_elo":elo[game["home_team"]], "away_elo":elo[game["away_team"]]})
        for game in block.to_dict("records"):
            if game["_target"]:
                continue
            expected = 1 / (1 + 10 ** (-(elo[game["home_team"]] + 45 - elo[game["away_team"]]) / 400))
            actual = 1.0 if game["home_score"] > game["away_score"] else (0.5 if game["home_score"] == game["away_score"] else 0.0)
            change = 20 * (actual - expected)
            elo[game["home_team"]] += change
            elo[game["away_team"]] -= change
    return pd.DataFrame(rows)


def state_paths(settings, season: int, week: int) -> tuple[Path, Path, Path]:
    root = settings.artifact_root / "phase2" / "team_rollforward" / str(season)
    return (root / f"week_{week:02d}.csv", root / f"week_{week:02d}.json",
            root / f"week_{week:02d}_diagnostics.csv")


def _kickoffs(games: pd.DataFrame) -> pd.DataFrame:
    out = games.copy()
    if "kickoff_utc" not in out:
        local = pd.to_datetime(out.gameday.astype(str) + " " + out.gametime.fillna("13:00"), errors="coerce")
        out["kickoff_utc"] = local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")
    out["kickoff_utc"] = pd.to_datetime(out.kickoff_utc, utc=True, errors="coerce")
    if out.kickoff_utc.isna().any():
        raise TeamRollforwardError("Schedule kickoff missing or ambiguous")
    return out


def ensure_team_state(settings, season: int, week: int, check_only: bool = False) -> dict:
    """Create/reuse a source-hashed Week 4+ state after every previous week is final.

    This intentionally does not download PBP. Missing current-season PBP is a
    hard block, rather than a 2025-form fallback mistaken for current form.
    """
    from nfl2026.postgame import _completion_mask, _official_week_frozen

    state_path, manifest_path, diagnostic_path = state_paths(settings, season, week)
    schedule_path = settings.root / "data" / "phase2_external" / "games.csv"
    schedule = pd.read_csv(schedule_path, low_memory=False)
    current = schedule[pd.to_numeric(schedule.season, errors="coerce").eq(season) &
                       schedule.game_type.astype(str).str.upper().eq("REG")].copy()
    target = _kickoffs(current[pd.to_numeric(current.week, errors="coerce").eq(week)].copy())
    if target.empty or target.game_id.duplicated().any():
        raise TeamRollforwardError("Target fixtures missing or duplicated")
    if target[["home_score", "away_score"]].notna().any().any():
        raise TeamRollforwardError("Target week already contains a result")
    prior = _kickoffs(current[pd.to_numeric(current.week, errors="coerce").lt(week)].copy())
    if week > 1:
        expected_weeks = set(range(1, week))
        if set(pd.to_numeric(prior.week, errors="coerce").astype(int)) != expected_weeks:
            raise TeamRollforwardError("Previous season weeks missing from schedule")
        for prior_week, block in prior.groupby("week"):
            final = _completion_mask(block)
            if not final.all():
                raise TeamRollforwardError(f"Previous week incomplete: {season} W{int(prior_week):02d} {int(final.sum())}/{len(block)} final")
    historical_path = settings.artifact_root / "phase2" / "team_advanced_matrix.csv"
    history = pd.read_csv(historical_path, low_memory=False)
    history = history[pd.to_numeric(history.season, errors="coerce").lt(season)].copy()
    historical_games = history[["game_id", "season", "week", "kickoff_utc", "home_team", "away_team",
                                "target_home_score", "target_away_score"]].rename(columns={"target_home_score":"home_score", "target_away_score":"away_score"})
    complete = pd.concat([historical_games, prior[list(historical_games.columns)]], ignore_index=True)
    historical_pbp_path = settings.artifact_root / "phase2" / "team_game_efficiency.csv"
    historical_pbp = pd.read_csv(historical_pbp_path, low_memory=False)
    if week > 1:
        new_pbp, current_pbp_path, current_pbp_hash = current_pbp(
            settings, season, set(prior.game_id.astype(str)), allow_download=not check_only)
        pbp = pd.concat([historical_pbp, new_pbp], ignore_index=True)
    else:
        current_pbp_path, current_pbp_hash = None, "NOT_REQUIRED_WEEK_1"
        pbp = historical_pbp
    form, diagnostic = build_online_form(complete, pbp, target, settings.phase2_team_history_window)
    elo = elo_for_targets(complete, target)
    form = form.merge(elo, on="game_id", validate="one_to_one")
    form["elo_difference"] = form.home_elo - form.away_elo
    form["home_advantage"] = 1.0  # Exact historical phase2-v2 semantics, including neutral sites.
    form = form.sort_values("game_id").reset_index(drop=True)
    # Hash only football facts consumed by this state. nflverse's schedule also
    # carries market columns; bookmaker prices must not influence provenance.
    game_source_columns=[c for c in ("game_id","season","week","kickoff_utc","home_team","away_team",
                                    "home_score","away_score","result","total","game_status","result_status","status","game_state") if c in prior]
    expected = {"season":season, "target_week":week, "latest_completed_source_week":week-1,
                "source_games_sha256":frame_hash(prior[game_source_columns]), "source_pbp_sha256":current_pbp_hash,
                "historical_games_sha256":sha256_file(historical_path),
                "historical_pbp_sha256":sha256_file(historical_pbp_path),
                "row_count":len(form), "feature_version":settings.phase2_feature_version,
                "feature_schema_sha256":hashlib.sha256("|".join(form.columns).encode()).hexdigest(),
                "feature_values_sha256":frame_hash(form), "prior_season_used":season-1,
                "shrinkage_configuration":f"last_{settings.phase2_team_history_window}_team_games_cross_season_unweighted"}
    if manifest_path.exists() and state_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if all(old.get(k) == v for k, v in expected.items()) and old.get("state_file_sha256") == sha256_file(state_path):
                return {"status":"ALREADY_CURRENT", "state_path":state_path, "manifest_path":manifest_path,
                        "rows":len(form)}
        except (OSError, ValueError):
            pass
    if check_only:
        return {"status":"NEEDS_ROLL_FORWARD", "state_path":state_path, "manifest_path":manifest_path,
                "rows":len(form)}
    if _official_week_frozen(settings, season, week):
        raise TeamRollforwardError(f"Week {week} official prediction is frozen; team state cannot change")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_name(f".{state_path.name}.{uuid.uuid4().hex}.tmp")
    form.to_csv(temp, index=False, lineterminator="\n")
    temp.replace(state_path)
    diagnostic["feature_generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    diagnostic["source_games_sha256"] = expected["source_games_sha256"]
    diagnostic["source_pbp_sha256"] = current_pbp_hash
    diagnostic["feature_version"] = settings.phase2_feature_version
    diagnostic.to_csv(diagnostic_path, index=False, lineterminator="\n")
    expected.update({"generated_at_utc":datetime.now(timezone.utc).isoformat(),
                     "state_file_sha256":sha256_file(state_path),
                     "source_pbp_path":str(current_pbp_path) if current_pbp_path else "NOT_REQUIRED_WEEK_1"})
    temp = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(manifest_path)
    return {"status":"GENERATED", "state_path":state_path, "manifest_path":manifest_path,
            "rows":len(form)}
