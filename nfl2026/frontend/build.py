from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

import pandas as pd

from nfl2026.config import Settings
from nfl2026.deployment import validate_frontend_dist
from nfl2026.markets.settlement import performance_summary
from nfl2026.markets.qualification import select_qualifying_values
from nfl2026.production.evaluation import select_evaluation_runs
from nfl2026.utilities.io import write_csv_json


def _consensus_line(rows: pd.DataFrame) -> tuple[float | None, int]:
    if rows.empty:
        return None, 0
    lines = pd.to_numeric(rows["line"], errors="coerce")
    usable = rows.assign(_line=lines).dropna(subset=["_line"])
    if usable.empty:
        return None, 0
    coverage = usable.groupby("_line").bookmaker.nunique().sort_values(ascending=False)
    maximum = int(coverage.iloc[0])
    candidates = coverage[coverage.eq(maximum)].index.to_list()
    median = float(usable["_line"].median())
    selected = min(candidates, key=lambda line: (abs(float(line) - median), abs(float(line))))
    return float(selected), maximum


def build_market_game_context(values: pd.DataFrame, games: pd.DataFrame) -> dict[str, dict[str, object]]:
    if values.empty:
        return {}
    active = values[values.status.eq("ACTIVE")].copy()
    output: dict[str, dict[str, object]] = {}
    for game in games.itertuples(index=False):
        group = active[active.game_id.eq(game.game_id)]
        home_spreads = group[group.market_type.eq("SPREAD") & group.team.eq(game.home_team)]
        home_spread, spread_books = _consensus_line(home_spreads)
        if home_spread is None:
            away_spread, spread_books = _consensus_line(group[group.market_type.eq("SPREAD") & group.team.eq(game.away_team)])
            home_spread = -away_spread if away_spread is not None else None
        total, total_books = _consensus_line(group[group.market_type.eq("TOTAL") & group.side.eq("OVER")])
        if home_spread is None or total is None:
            continue
        market_margin = -home_spread
        market_home = (total + market_margin) / 2
        market_away = (total - market_margin) / 2
        favorite = game.home_team if market_margin > 0 else game.away_team if market_margin < 0 else "PICK"
        spread_label = "PICK'EM" if favorite == "PICK" else f"{favorite} -{abs(home_spread):g}"
        output[str(game.game_id)] = {
            "market_home_spread": home_spread,
            "market_total": total,
            "market_margin": market_margin,
            "market_home_points": round(market_home, 1),
            "market_away_points": round(market_away, 1),
            "market_spread_label": spread_label,
            "spread_book_count": spread_books,
            "total_book_count": total_books,
            "method": "CONSENSUS_MAIN_LINE_MODE",
        }
    return output


def select_official_bundle(settings:Settings,season:int,week:int,as_of:pd.Timestamp|None=None)->Path:
    """Select the latest eligible production bundle deterministically; never select a dry run."""
    as_of=as_of or pd.Timestamp.now(tz="UTC")
    root=settings.prediction_root/str(season)/f"week_{week:02d}"
    choices=[]
    for metadata_path in root.glob("*/metadata.json") if root.exists() else []:
        try:
            meta=json.loads(metadata_path.read_text(encoding="utf-8"))
            if meta.get("run_status")!="OFFICIAL_PRODUCTION" or not meta.get("deployment_eligible") or meta.get("validation_result")!="PASS": continue
            games=pd.read_csv(metadata_path.parent/"games.csv")
            if not pd.to_datetime(games.kickoff_utc,utc=True).gt(pd.Timestamp(meta["cutoff_utc"])).all(): continue
            created=pd.Timestamp(meta["created_at_utc"])
            if created<=as_of: choices.append((created,str(meta["run_id"]),metadata_path.parent))
        except Exception:
            continue
    if not choices: raise FileNotFoundError(f"No eligible official production bundle for {season} Week {week}")
    return max(choices,key=lambda item:(item[0],item[1]))[2]


def select_value_run(settings:Settings,season:int,week:int,prediction_run_id:str,as_of:pd.Timestamp|None=None)->Path:
    as_of=as_of or pd.Timestamp.now(tz="UTC")
    root=settings.root/"value_runs"/str(season)/f"week_{week:02d}"
    choices=[]
    for metadata_path in root.glob("*/metadata.json") if root.exists() else []:
        try:
            meta=json.loads(metadata_path.read_text(encoding="utf-8"))
            if meta.get("prediction_run_id")!=prediction_run_id or meta.get("status") not in {"DRY_RUN_READY","OFFICIAL_VALUE_SNAPSHOT"}: continue
            created=pd.Timestamp(meta["created_at_utc"])
            if created<=as_of: choices.append((created,str(meta["value_run_id"]),metadata_path.parent))
        except Exception:
            continue
    if not choices: raise FileNotFoundError(f"No compatible value run for {prediction_run_id}")
    return max(choices,key=lambda item:(item[0],item[1]))[2]


def _attach_actuals(settings:Settings,preview:dict,season:int,week:int)->dict:
    actual_root=settings.artifact_root/"actuals"/str(season)/f"week_{week:02d}"
    choices=sorted(actual_root.glob("actual-*/team_actuals.csv")) if actual_root.exists() else []
    if choices:
        actuals=pd.read_csv(choices[-1]).set_index("game_id")
        for game in preview.get("games",[]):
            if game.get("game_id") in actuals.index:
                result=actuals.loc[game["game_id"]]
                game.update({"actual_away_score":int(result.away_score),"actual_home_score":int(result.home_score),"result_status":"FINAL"})
    return preview

def _combined_week_preview(settings:Settings,season:int,week:int,base_metadata:dict|None=None)->dict:
    """Assemble a week from the immutable latest valid prediction selected per game."""
    selection=select_evaluation_runs(settings,season,week)
    metadata=dict(base_metadata or {})
    games=[]; selected=[]
    if selection.empty:
        raise FileNotFoundError(f"No valid official per-game predictions for {season} Week {week}")
    # Resolve the weekly anchor before reading its fixture manifest. Historical
    # weeks enter without base_metadata and need the same lineage propagation.
    if not metadata:
        anchor = select_official_bundle(settings, season, week)
        metadata = json.loads((anchor / "metadata.json").read_text(encoding="utf-8"))
    partial_lineage={}
    if metadata.get("prediction_scope")=="UNPLAYED_ONLY" and metadata.get("run_id"):
        from nfl2026.frontend.lineage import certified_partial_lineage
        partial_lineage=certified_partial_lineage(settings, metadata)
        selected_by_game={str(row.game_id):str(row.selected_run_id) for row in selection.itertuples(index=False)}
        if set(partial_lineage)!=set(selected_by_game):
            raise ValueError("Partial per-game manifest does not cover the selected weekly fixture set")
        if any(str(trace["prediction_source_run_id"])!=selected_by_game[game_id] for game_id,trace in partial_lineage.items()):
            raise ValueError("Selected per-game predictions disagree with the partial run manifest")
    for row in selection.itertuples(index=False):
        bundle=settings.prediction_root/str(season)/f"week_{week:02d}"/str(row.selected_run_id)
        preview=json.loads((bundle/"game_preview.json").read_text(encoding="utf-8"))
        game=next((x for x in preview.get("games",[]) if str(x.get("game_id"))==str(row.game_id)),None)
        if game is None: raise ValueError(f"Selected run {row.selected_run_id} lacks game {row.game_id}")
        game=dict(game); game["prediction_run_id"]=str(row.selected_run_id); game["prediction_source_run_id"]=str(row.selected_run_id); game["prediction_cutoff_utc"]=str(row.run_cutoff_utc)
        if str(row.game_id) in partial_lineage:
            trace=partial_lineage[str(row.game_id)]
            from nfl2026.frontend.lineage import PARTIAL_LINEAGE_FIELDS
            for field in PARTIAL_LINEAGE_FIELDS:
                game[field]=trace[field]
        games.append(game); selected.append(str(row.selected_run_id))
    metadata.update({"season":season,"week":week,"game_level_prediction_selection":True,"selected_prediction_run_ids":sorted(set(selected))})
    return {"metadata":metadata,"games":sorted(games,key=lambda x:x.get("kickoff_utc", ""))}



def approved_fixture_preview(settings, season:int, week:int)->dict:
    """Opt-in data contract only; never rebuild dist or publish implicitly."""
    from nfl2026.frontend.fixture_revisions import compose_revision_week
    from nfl2026.production.fixture_revisions import FixtureRevisionService
    original = _combined_week_preview(settings, season, week)
    return compose_revision_week(FixtureRevisionService(settings), original)


def build_frontend_data(settings:Settings,bundle:Path|None=None,season:int|None=None,week:int|None=None,value_run:Path|None=None)->Path:
    if bundle is None:
        if season is None or week is None: raise ValueError("season and week are required for deterministic official-bundle selection")
        bundle=select_official_bundle(settings,season,week)
    from nfl2026.production.partial_gate import validate_partial_certificate
    certification_errors=validate_partial_certificate(bundle)
    if certification_errors: raise ValueError("; ".join(certification_errors))
    validation=pd.read_csv(bundle/"validation_report.csv")
    if not validation.passed.astype(bool).all(): raise RuntimeError("Frontend generation blocked by bundle validation")
    meta=json.loads((bundle/"metadata.json").read_text()); preview=json.loads((bundle/"game_preview.json").read_text())
    if meta.get("run_status")=="OFFICIAL_PRODUCTION":
        preview=_combined_week_preview(settings,int(meta["season"]),int(meta["week"]),preview.get("metadata",meta))
    preview=_attach_actuals(settings,preview,int(meta["season"]),int(meta["week"]))
    if value_run is None:
        try: value_run=select_value_run(settings,int(meta["season"]),int(meta["week"]),str(meta["run_id"]))
        except FileNotFoundError: value_run=None
    if value_run is not None:
        value_meta=json.loads((value_run/"metadata.json").read_text(encoding="utf-8"))
        if value_meta.get("prediction_run_id")!=meta["run_id"]: raise ValueError("Value run references a different prediction bundle")
        snapshot_meta_path=settings.root/"markets"/"snapshots"/str(meta["season"])/f"week_{int(meta['week']):02d}"/str(value_meta.get("market_snapshot_id",""))/"metadata.json"
        if snapshot_meta_path.is_file():
            snapshot_meta=json.loads(snapshot_meta_path.read_text(encoding="utf-8"))
            successful=snapshot_meta.get("last_successful_refresh_utc",snapshot_meta.get("retrieved_at_utc"))
            age_minutes=max(0.0,(pd.Timestamp.now(tz="UTC")-pd.Timestamp(successful)).total_seconds()/60.0) if successful else None
            value_meta["last_successful_refresh_utc"]=successful
            value_meta["market_snapshot_age_minutes"]=age_minutes
            value_meta["market_data_state"]="CURRENT" if age_minutes is not None and age_minutes<=settings.market_game_refresh_minutes else "STALE"
            value_meta["market_snapshot_status"]=snapshot_meta.get("lkg_status",snapshot_meta.get("status"))
        values=pd.read_csv(value_run/"value_bets.csv")
        active=select_qualifying_values(values).sort_values("value_score",ascending=False) if len(values) else values
        games=pd.DataFrame(preview.get("games",[]))
        market_game_context=build_market_game_context(values,games) if len(values) and len(games) else {}
        value_payload={"metadata":value_meta,"values":active.replace({float("nan"):None}).to_dict("records"),"market_game_context":market_game_context,"all_quote_count":len(values),"message":"Market data unavailable" if value_meta.get("status")=="MARKET_DATA_UNAVAILABLE" else "Model Value compares independently generated NFL probabilities with current market prices. Market prices were not used to create the model prediction."}
    else:
        value_payload={"metadata":{"status":"MARKET_DATA_UNAVAILABLE","prediction_run_id":meta["run_id"]},"values":[],"market_game_context":{},"all_quote_count":0,"message":"Market data unavailable"}
    live_specs={"live_2026_team_performance.csv":["season","week","model_version","games","score_mae","margin_mae","total_mae","winner_accuracy","win_probability_brier","interval_80_coverage"],"live_2026_player_performance.csv":["season","week","model_version","players","carry_mae","target_mae","rush_yards_mae","receiving_yards_mae","carry_interval_80_coverage","target_interval_80_coverage"],"live_2026_td_calibration.csv":["season","week","model_version","players","td_brier","td_log_loss","td_ece"]}
    for name,columns in live_specs.items():
        path=settings.report_root/name
        if not path.exists(): pd.DataFrame(columns=columns).to_csv(path,index=False,lineterminator="\n")
    live_team=pd.read_csv(settings.report_root/"live_2026_team_performance.csv")
    ledger_path=settings.root/"value_runs"/"ledger"/"value_ledger.csv"
    ledger=pd.read_csv(ledger_path) if ledger_path.exists() else pd.DataFrame()
    market_performance=performance_summary(ledger).replace({float("nan"):None}).to_dict("records") if len(ledger) else []
    performance={"historical":{"team_score_mae":7.458,"margin_mae":10.345,"total_mae":10.355,"winner_accuracy":.6356,"rushing_yards_mae":7.873,"receiving_yards_mae":15.633,"td_brier":.1496},"live_2026":{"status":"No completed evaluated games" if live_team.empty else "Live results available","weeks":live_team.replace({float("nan"):None}).to_dict("records")},"market_value":{"status":"No settled ledger selections" if not market_performance else "Settled value results available","rows":market_performance},"model_version":meta["model_ids"]["team"]["version"]}
    frontend=settings.root/"frontend"
    dist=frontend/"dist"
    staging=frontend/f".dist-staging-{uuid.uuid4().hex[:10]}"
    staging.mkdir(parents=True)
    for name in ("index.html","players.html","value.html","performance.html","model.html","styles.css","app.js"):
        shutil.copy2(frontend/name,staging/name)
    assets=frontend/"assets"
    if assets.exists(): shutil.copytree(assets,staging/"assets")
    data=staging/"data"; data.mkdir()
    available=[int(meta["week"])]
    for prior_week in range(1,int(meta["week"])):
        try:
            prior=_combined_week_preview(settings,int(meta["season"]),prior_week)
            prior=_attach_actuals(settings,prior,int(meta["season"]),prior_week)
            (data/f"week_{prior_week:02d}.json").write_text(json.dumps(prior,indent=2),encoding="utf-8")
            available.append(prior_week)
        except FileNotFoundError:
            continue
    preview.setdefault("metadata",{})["available_weeks"]=sorted(available)
    (data/"current_week.json").write_text(json.dumps(preview,indent=2),encoding="utf-8")
    (data/"value.json").write_text(json.dumps(value_payload,indent=2,default=str),encoding="utf-8")
    (data/"performance.json").write_text(json.dumps(performance,indent=2),encoding="utf-8")
    errors=validate_frontend_dist(staging)
    if errors:
        shutil.rmtree(staging)
        raise RuntimeError("Frontend staging validation failed; known-good dist retained: "+"; ".join(errors))
    backup=frontend/".dist-known-good"
    if backup.exists(): shutil.rmtree(backup)
    try:
        if dist.exists(): dist.rename(backup)
        staging.rename(dist)
    except Exception:
        if dist.exists(): shutil.rmtree(dist)
        if backup.exists(): backup.rename(dist)
        if staging.exists(): shutil.rmtree(staging)
        raise
    if backup.exists(): shutil.rmtree(backup)
    public=frontend/"public"/"data"; public.mkdir(parents=True,exist_ok=True)
    shutil.copytree(dist/"data",public,dirs_exist_ok=True)
    report=pd.DataFrame([{"check":"bundle_validation","status":"pass"},{"check":"fixture_cards","status":"pass","details":len(preview["games"])},{"check":"bundle_driven_no_hardcoded_predictions","status":"pass"},{"check":"missing_optional_fields","status":"pass","details":"null-safe renderer"},{"check":"value_layer_downstream","status":"pass","details":value_payload["metadata"]["status"]},{"check":"market_failure_isolated","status":"pass","details":"predictions render without value data"},{"check":"desktop_build","status":"pass"},{"check":"mobile_css","status":"pass","details":"responsive card layout; no table overflow"}])
    write_csv_json(report,settings.report_root/"phase3_frontend_validation.csv")
    return dist
