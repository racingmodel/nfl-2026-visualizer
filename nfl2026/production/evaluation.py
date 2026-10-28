from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.models.probability import expected_calibration_error, log_loss_binary
from nfl2026.utilities.io import sha256_file


def select_evaluation_runs(settings: Settings, season: int, week: int, selected_at: datetime | None=None) -> pd.DataFrame:
    """Choose the last valid production prediction before kickoff for each game."""
    selected_at=selected_at or datetime.now(timezone.utc)
    candidates=[]
    root=settings.prediction_root/str(season)/f"week_{week:02d}"
    for metadata_path in root.glob("*/metadata.json") if root.exists() else []:
        try:
            meta=json.loads(metadata_path.read_text(encoding="utf-8"))
            if meta.get("run_status")!="OFFICIAL_PRODUCTION" or meta.get("validation_result")!="PASS" or not meta.get("deployment_eligible"): continue
            if meta.get("prediction_scope")=="UNPLAYED_ONLY":
                from nfl2026.production.partial_gate import validate_partial_certificate
                if validate_partial_certificate(metadata_path.parent,meta): continue
            games=pd.read_csv(metadata_path.parent/"games.csv")
            for game in games.itertuples(index=False):
                cutoff=pd.Timestamp(meta["cutoff_utc"]); kickoff=pd.Timestamp(game.kickoff_utc)
                if cutoff<kickoff and cutoff<=pd.Timestamp(selected_at):
                    candidates.append({"game_id":game.game_id,"kickoff_utc":kickoff.isoformat(),"selected_run_id":meta["run_id"],"run_cutoff_utc":cutoff.isoformat(),"bundle_path":str(metadata_path.parent)})
        except Exception:
            continue
    frame=pd.DataFrame(candidates)
    if frame.empty: return pd.DataFrame(columns=["season","week","game_id","kickoff_utc","selected_run_id","run_cutoff_utc","selection_policy","selected_at_utc"])
    frame["run_cutoff_utc"]=pd.to_datetime(frame.run_cutoff_utc,utc=True)
    selected=frame.sort_values(["game_id","run_cutoff_utc","selected_run_id"]).groupby("game_id",as_index=False).tail(1).copy()
    selected.insert(0,"season",season); selected.insert(1,"week",week)
    selected["run_cutoff_utc"]=selected.run_cutoff_utc.map(lambda value:value.isoformat())
    selected["selection_policy"]="last valid production prediction before kickoff; never accuracy-selected"
    selected["selected_at_utc"]=selected_at.isoformat()
    return selected.drop(columns="bundle_path").sort_values("game_id").reset_index(drop=True)


def _write_immutable(frame: pd.DataFrame, path: Path) -> None:
    if path.exists():
        existing = pd.read_csv(path, low_memory=False)
        if not existing.equals(frame):
            raise FileExistsError(f"Actual artifact is immutable and differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, lineterminator="\n")


def ingest_completed_actuals(settings: Settings, season: int, week: int) -> Path:
    """Freeze independent completed-game and player actuals without touching predictions."""
    schedule = pd.read_csv(settings.root/"data"/"phase2_external"/"games.csv", low_memory=False)
    games = schedule[(schedule.season.eq(season)) & schedule.week.eq(week) & schedule.game_type.eq("REG") & schedule.home_score.notna() & schedule.away_score.notna()].copy()
    games = games[["game_id","season","week","gameday","away_team","home_team","away_score","home_score"]]
    games["actual_margin"] = games.home_score-games.away_score
    games["actual_total"] = games.home_score+games.away_score
    games["winner"] = np.where(games.actual_margin>0,games.home_team,np.where(games.actual_margin<0,games.away_team,"TIE"))
    games["actual_source"] = "independent locked nflverse schedule results"
    if games.empty:
        raise ValueError("No completed games are available; no actual artifact was written")
    identity=games.to_csv(index=False,lineterminator="\n").encode()
    import hashlib
    actual_id=f"actual-{hashlib.sha256(identity).hexdigest()[:12]}"
    destination = settings.artifact_root/"actuals"/str(season)/f"week_{week:02d}"/actual_id
    _write_immutable(games.reset_index(drop=True), destination/"team_actuals.csv")

    weekly_path=settings.artifact_root/"canonical"/"player_weekly.csv"
    weekly=pd.read_csv(weekly_path,low_memory=False) if weekly_path.exists() else pd.DataFrame()
    players=weekly[(weekly.season.eq(season))&weekly.week.eq(week)].copy() if not weekly.empty else pd.DataFrame()
    if not players.empty:
        players=players[["season","week","team","opponent_team","player_id","display_name","position","carries","rushing_yards","targets","receiving_yards","anytime_offensive_td"]]
        players=players.rename(columns={"display_name":"player_name","anytime_offensive_td":"actual_td"})
        team_to_game = {}
        for game in games.itertuples(index=False):
            team_to_game[str(game.home_team)] = str(game.game_id)
            team_to_game[str(game.away_team)] = str(game.game_id)
        players["game_id"] = players.team.astype(str).map(team_to_game)
        players["actual_source"]="independent canonical weekly stats"
    _write_immutable(players.reset_index(drop=True),destination/"player_actuals.csv")
    selection=select_evaluation_runs(settings,season,week)
    if not selection.empty:
        _write_immutable(selection,destination/"evaluation_run_selection.csv")
    manifest={"season":season,"week":week,"completed_games":len(games),"players":len(players),"team_actuals_sha256":sha256_file(destination/"team_actuals.csv"),"player_actuals_sha256":sha256_file(destination/"player_actuals.csv"),"evaluation_selection_policy":"last valid production prediction before kickoff; never accuracy-selected","selected_games":len(selection)}
    (destination/"actuals_manifest.json").write_text(json.dumps(manifest,indent=2,sort_keys=True),encoding="utf-8")
    return destination


def evaluate_live_bundle(settings: Settings, bundle: Path) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame]:
    meta=json.loads((bundle/"metadata.json").read_text(encoding="utf-8")); season=int(meta["season"]); week=int(meta["week"])
    actual_root=settings.artifact_root/"actuals"/str(season)/f"week_{week:02d}"
    choices=sorted((p for p in actual_root.glob("actual-*") if p.is_dir()),key=lambda p:p.stat().st_mtime)
    if not choices: raise FileNotFoundError(f"No immutable actuals snapshot for {season} Week {week}")
    actual_dir=choices[-1]
    games=pd.read_csv(bundle/"games.csv"); actual_games=pd.read_csv(actual_dir/"team_actuals.csv")
    joined=games.merge(actual_games,on="game_id",validate="one_to_one") if len(actual_games) else pd.DataFrame()
    team_rows=[]
    if len(joined):
        score_errors=pd.concat([(joined.home_expected_points-joined.home_score).abs(),(joined.away_expected_points-joined.away_score).abs()])
        actual_margin=joined.home_score-joined.away_score; actual_total=joined.home_score+joined.away_score
        y=(actual_margin>0).astype(int)
        team_rows=[{"season":season,"week":week,"model_version":meta["model_ids"]["team"]["version"],"games":len(joined),"score_mae":score_errors.mean(),"margin_mae":((joined.model_margin-actual_margin).abs()).mean(),"total_mae":((joined.model_total-actual_total).abs()).mean(),"winner_accuracy":((joined.home_win_probability>=.5)==(y==1)).mean(),"win_probability_brier":np.mean((joined.home_win_probability-y)**2),"interval_80_coverage":np.mean((joined.home_score.between(joined.home_score_p10,joined.home_score_p90))&(joined.away_score.between(joined.away_score_p10,joined.away_score_p90)))}]
    team_report=pd.DataFrame(team_rows,columns=["season","week","model_version","games","score_mae","margin_mae","total_mae","winner_accuracy","win_probability_brier","interval_80_coverage"])

    predicted=pd.read_csv(bundle/"players.csv"); actual_players=pd.read_csv(actual_dir/"player_actuals.csv")
    if "game_id" in predicted and "game_id" in actual_games:
        predicted=predicted[predicted.game_id.astype(str).isin(set(actual_games.game_id.astype(str)))].copy()
    # Fixture identity is mandatory. An absent weekly-stat row is unresolved,
    # including for inactive players; it is never an implicit zero outcome.
    from nfl2026.weekly.metrics import join_player_labels, probability_metrics
    if len(actual_players):
        if 'anytime_offensive_td' not in actual_players and 'actual_td' in actual_players:
            actual_players=actual_players.rename(columns={'actual_td':'anytime_offensive_td'})
        pj=join_player_labels(predicted,actual_players)
        for name in ('carries','rushing_yards','targets','receiving_yards','anytime_offensive_td'):
            column='actual__'+name
            pj[name]=pd.to_numeric(pj[column],errors='coerce') if column in pj else np.nan
        pj['actual_td']=pj.anytime_offensive_td
    else:pj=pd.DataFrame()
    player_rows=[]; td_rows=[]
    if len(pj):
        carry_valid=pj.carries.notna()&pj.expected_carries.notna();target_valid=pj.targets.notna()&pj.expected_targets.notna()
        matched=int(pj.label_match.eq('both').sum())
        player_rows=[{"season":season,"week":week,"model_version":meta["model_ids"]["player"]["version"],"players":len(pj),
            "matched_players":matched,"unknown_players":len(pj)-matched,
            "carry_rows":int(carry_valid.sum()),"target_rows":int(target_valid.sum()),
            "rush_rows":int((pj.expected_rush_yards.notna()&pj.rushing_yards.notna()).sum()),
            "receiving_rows":int((pj.expected_receiving_yards.notna()&pj.receiving_yards.notna()).sum()),
            "carry_mae":(pj.expected_carries-pj.carries).abs().mean(),"target_mae":(pj.expected_targets-pj.targets).abs().mean(),
            "rush_yards_mae":(pj.expected_rush_yards-pj.rushing_yards).abs().mean(),"receiving_yards_mae":(pj.expected_receiving_yards-pj.receiving_yards).abs().mean(),
            "carry_interval_80_coverage":pj.loc[carry_valid,'carries'].between(pj.loc[carry_valid,'carries_low'],pj.loc[carry_valid,'carries_high']).mean(),
            "target_interval_80_coverage":pj.loc[target_valid,'targets'].between(pj.loc[target_valid,'targets_low'],pj.loc[target_valid,'targets_high']).mean()}]
        valid=pj.actual_td.notna()&pj.reconciled_td_probability.notna()
        if valid.any():
            metrics=probability_metrics(pj.loc[valid,'actual_td'],pj.loc[valid,'reconciled_td_probability'])
            td_rows=[{"season":season,"week":week,"model_version":meta["model_ids"]["td"]["version"],"players":int(valid.sum()),
                "frozen_players":len(pj),"unknown_players":int((~valid).sum()),"td_brier":metrics['brier'],"td_log_loss":metrics['log_loss'],"td_ece":metrics['ece']}]
    player_report=pd.DataFrame(player_rows,columns=["season","week","model_version","players","matched_players","unknown_players","carry_rows","target_rows","rush_rows","receiving_rows","carry_mae","target_mae","rush_yards_mae","receiving_yards_mae","carry_interval_80_coverage","target_interval_80_coverage"])
    td_report=pd.DataFrame(td_rows,columns=["season","week","model_version","players","frozen_players","unknown_players","td_brier","td_log_loss","td_ece"])
    for frame,name in ((team_report,"live_2026_team_performance.csv"),(player_report,"live_2026_player_performance.csv"),(td_report,"live_2026_td_calibration.csv")):
        path=settings.report_root/name
        prior=pd.read_csv(path) if path.exists() else pd.DataFrame(columns=frame.columns)
        if len(prior):
            combined=pd.concat([prior[~((prior.season.eq(season))&(prior.week.eq(week)))],frame],ignore_index=True)
        else:
            combined=frame.copy()
        combined.to_csv(path,index=False,lineterminator="\n")
    return team_report,player_report,td_report


def evaluate_official_week(settings: Settings, season: int, week: int) -> tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame]:
    """Evaluate the per-game official selection, including weeks with multiple valid pre-kickoff runs."""
    selection = select_evaluation_runs(settings, season, week)
    if selection.empty:
        raise FileNotFoundError(f"No official pre-kickoff prediction selection for {season} Week {week}")
    games_parts = []
    player_parts = []
    versions = {"team": set(), "player": set(), "td": set()}
    for row in selection.itertuples(index=False):
        bundle = settings.prediction_root / str(season) / f"week_{week:02d}" / str(row.selected_run_id)
        games = pd.read_csv(bundle / "games.csv")
        players = pd.read_csv(bundle / "players.csv")
        games_parts.append(games[games.game_id.astype(str).eq(str(row.game_id))])
        player_parts.append(players[players.game_id.astype(str).eq(str(row.game_id))])
        meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
        for key in versions:
            versions[key].add(str(meta["model_ids"][key]["version"]))
    selected_games = pd.concat(games_parts, ignore_index=True)
    selected_players = pd.concat(player_parts, ignore_index=True)
    with tempfile.TemporaryDirectory() as folder:
        bundle = Path(folder) / "official-per-game"
        bundle.mkdir()
        selected_games.to_csv(bundle / "games.csv", index=False)
        selected_players.to_csv(bundle / "players.csv", index=False)
        metadata = {"season":season,"week":week,"model_ids":{key:{"version":"+".join(sorted(value))} for key,value in versions.items()}}
        (bundle / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return evaluate_live_bundle(settings, bundle)
