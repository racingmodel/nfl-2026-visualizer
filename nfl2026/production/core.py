from __future__ import annotations



from dataclasses import replace

from datetime import datetime, timezone

import hashlib

import json

from pathlib import Path

import shutil

import subprocess

from typing import Any



import joblib

import numpy as np

import pandas as pd



from nfl2026.config import Settings

from nfl2026.live.providers.depth import infer_missing_roles
from nfl2026.production.replacement import compare_with_existing_run

from nfl2026.models.player_advanced import reconcile_td_probabilities

from nfl2026.models.team_advanced import simulate_games

from nfl2026.registry.core import append_run, load_registered_model

from nfl2026.utilities.io import sha256_file, write_csv_json





SOURCE_SPECS = {

    "schedule": {"file":"games.csv", "url":"https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv", "critical":True, "max_age_hours":168, "reliability":"EXACT_POINT_IN_TIME"},

    "roster": {"file":"weekly_roster_{season}.csv", "url":"https://github.com/nflverse/nflverse-data/releases/download/weekly_rosters/roster_weekly_{season}.csv", "critical":True, "max_age_hours":72, "reliability":"WEEKLY_ASSUMED_PREGAME"},

    "injuries": {"file":"injuries_{season}.csv", "url":"https://github.com/nflverse/nflverse-data/releases/download/injuries/injuries_{season}.csv", "critical":True, "max_age_hours":36, "reliability":"STRUCTURED_CURRENT_REPORTS"},

    "snap_history": {"file":"snap_counts_2025.csv", "url":"https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_2025.csv", "critical":False, "max_age_hours":8760, "reliability":"EXACT_POSTGAME_LAGGED"},

    "participation_history": {"file":"participation_2025.csv", "url":"https://github.com/nflverse/nflverse-data/releases/download/pbp_participation/pbp_participation_2025.csv", "critical":False, "max_age_hours":8760, "reliability":"EXACT_POSTGAME_LAGGED"},

    "depth": {"file":"depth_charts_{season}.csv", "url":"https://github.com/nflverse/nflverse-data/releases/download/depth_charts/depth_charts_{season}.csv", "critical":False, "max_age_hours":36, "reliability":"EXACT_CURRENT_DEPTH_WITH_MODEL_FALLBACK"},

    "quarterbacks": {"file":"qb_status_{season}_w{week:02d}.csv", "url":"nflverse schedule + depth_charts + weekly_rosters", "critical":True, "max_age_hours":36, "reliability":"CORROBORATED_EXPECTED_STARTER"},

    "transactions": {"file":"transactions_{season}.csv", "url":"https://github.com/nflverse/nflverse-data/releases/download/trades/trades.csv", "critical":False, "max_age_hours":8760, "reliability":"SUPPLEMENTAL_TRADES_ONLY"},

    "weather": {"file":"weather_{season}_w{week:02d}.csv", "url":"https://api.open-meteo.com/v1/forecast", "critical":False, "max_age_hours":6, "reliability":"PRELOCK_FORECAST_CONTEXT_ONLY"},

}





def utc_now() -> datetime:

    return datetime.now(timezone.utc)





def _timestamp(value: datetime) -> str:

    return value.strftime("%Y%m%dT%H%M%SZ")





def _schema_hash(frame: pd.DataFrame) -> str:

    text="|".join(f"{c}:{frame[c].dtype}" for c in frame.columns)

    return hashlib.sha256(text.encode()).hexdigest()


def _latest_injury_download_attempt(settings: Settings, season: int) -> tuple[datetime, bool] | None:
    root=settings.root/"data"/"live_downloads"/str(season)
    attempts=[]
    for retrieval in root.glob("*/retrieval.json") if root.exists() else []:
        try:
            payload=json.loads(retrieval.read_text(encoding="utf-8"))
            retrieved=pd.to_datetime(payload["retrieved_at_utc"],utc=True).to_pydatetime()
            injury_failed=any(str(item.get("file","")) in {f"injuries_{season}.csv","injuries_timestamp.json"} for item in payload.get("failures",[]))
            attempts.append((retrieved,injury_failed))
        except Exception:
            continue
    return max(attempts,key=lambda item:item[0]) if attempts else None





def detect_current_week(settings: Settings, season: int, as_of: datetime | None = None) -> int:

    """Return the earliest regular-season week with an unplayed future fixture."""

    as_of = as_of or utc_now()

    schedule = pd.read_csv(settings.root/"data"/"phase2_external"/"games.csv", low_memory=False)

    candidates = schedule[(schedule.season.eq(season)) & schedule.game_type.eq("REG")].copy()

    local = pd.to_datetime(candidates.gameday.astype(str)+" "+candidates.gametime.fillna("13:00"), errors="coerce")

    candidates["kickoff_utc"] = local.dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward").dt.tz_convert("UTC")

    future = candidates[(candidates.kickoff_utc > pd.Timestamp(as_of)) & candidates.home_score.isna() & candidates.away_score.isna()]

    if future.empty:

        raise ValueError(f"No upcoming unplayed regular-season fixtures for {season}")

    return int(future.sort_values("kickoff_utc").week.iloc[0])





def build_qb_status(schedule: pd.DataFrame, roster: pd.DataFrame, week: int, cutoff_utc: str) -> pd.DataFrame:

    active_qbs=set(roster[(roster.week.eq(week))&roster.status.eq("ACT")&roster.position.eq("QB")].gsis_id.dropna().astype(str))

    rows=[]

    for game in schedule.itertuples(index=False):

        for side in ("away","home"):

            player_id=str(getattr(game,f"{side}_qb_id")); player_name=getattr(game,f"{side}_qb_name")

            valid=bool(player_id and player_id!="nan" and pd.notna(player_name) and player_id in active_qbs)

            rows.append({"game_id":game.game_id,"team":getattr(game,f"{side}_team"),"projected_qb_id":player_id,"projected_qb":player_name,"source":"locked nflverse schedule + weekly roster; starter inferred, not confirmed","status":"EXPECTED" if valid else "UNCERTAIN","source_state":"VALID" if valid else "SOURCE_MISSING","injury_source_state":"SOURCE_MISSING","confidence":"MEDIUM" if valid else "LOW","starter_confirmed":False,"starter_resolution":"SCHEDULE_ROSTER_INFERRED","injury_status":"UNKNOWN","backup_hierarchy":"[]","known_at_utc":cutoff_utc,"retrieved_at_utc":cutoff_utc})

    return pd.DataFrame(rows)





def filter_current_candidates(candidate: pd.DataFrame, roster: pd.DataFrame, injuries: pd.DataFrame, week: int, transactions: pd.DataFrame | None = None, *, cutoff=None) -> pd.DataFrame:
    if 'transaction_reconciliation_error' in roster:
        errors=roster[roster.transaction_reconciliation_error.fillna('').ne('') & roster.gsis_id.astype(str).isin(candidate.player_id.astype(str))]
        if len(errors):raise ValueError('Roster transaction reconciliation unresolved: '+str(errors[['gsis_id','team','transaction_reconciliation_error']].to_dict('records')))
    active_roster=roster[(roster.week.eq(week))&roster.status.astype(str).str.upper().eq("ACT")].copy()
    # A contradictory active label must never override explicit reserve state.
    if "current_transaction_state" in active_roster:
        active_roster=active_roster[~active_roster.current_transaction_state.fillna("").astype(str).str.upper().isin(["IR","RESERVE","PUP","INACTIVE","RELEASED","WAIVED","PRACTICE_SQUAD"])]
    active=set(active_roster.gsis_id.dropna().astype(str))
    filtered=candidate[candidate.player_id.astype(str).isin(active)].copy()
    if 'team' in active_roster:
        assignments=active_roster.dropna(subset=['gsis_id']).groupby('gsis_id').team.nunique()
        if assignments.gt(1).any():raise ValueError('Current roster has ambiguous active team assignments')
    if transactions is not None and len(transactions) and 'transaction_state' in transactions:
        from nfl2026.live.providers.transaction_state import resolve_player_state, INACTIVE
        conflicts=[]
        for row in filtered.itertuples():
            identity=active_roster[active_roster.gsis_id.astype(str).eq(str(row.player_id))]
            pfr=str(identity.iloc[0].get('pfr_id','')) if len(identity) else ''
            roster_known=identity.iloc[0].get('roster_source_known_at_utc',None) if len(identity) else None
            resolved=resolve_player_state(transactions,str(row.player_id),str(row.player_name),str(row.team),cutoff=cutoff,pfr_id=pfr,roster_known_at=roster_known)
            if resolved and (resolved['state'] in INACTIVE or resolved['team']!=str(row.team)):
                conflicts.append({'player_id':row.player_id,'team':row.team,'state':resolved['state'],'latest_team':resolved['team']})
        if conflicts:
            raise ValueError(f'Current active roster conflicts with latest locked inactive transaction; source reconciliation required: {conflicts}')
    if "team" in filtered and "team" in active_roster:
        current_teams=active_roster.dropna(subset=["gsis_id"]).drop_duplicates("gsis_id",keep="last").set_index("gsis_id").team.astype(str).to_dict()
        expected_team=filtered.player_id.astype(str).map(current_teams)
        mismatch=expected_team.notna()&filtered.team.astype(str).ne(expected_team)
        if mismatch.any():
            examples=filtered.loc[mismatch,["player_id","player_name","team"]].assign(current_roster_team=expected_team[mismatch]).head(8).to_dict("records")
            raise ValueError(f"Active candidate team conflicts with current roster transaction state: {examples}")

    idcol="gsis_id" if "gsis_id" in injuries else "player_id" if "player_id" in injuries else None

    statuscol="game_status" if "game_status" in injuries else "normalized_status" if "normalized_status" in injuries else "report_status" if "report_status" in injuries else "practice_status" if "practice_status" in injuries else None

    if "live_injury_status" in filtered:
        effective_status=filtered.live_injury_status.astype(str).str.upper()
        filtered=filtered[~effective_status.isin(["OUT","IR","PUP","INACTIVE"])]
    elif idcol and statuscol:

        unavailable=set(injuries[injuries[statuscol].astype(str).str.upper().isin(["OUT","IR","PUP","INACTIVE"])][idcol].dropna().astype(str))

        filtered=filtered[~filtered.player_id.astype(str).isin(unavailable)]

    return filtered.reset_index(drop=True)





# Explicit health resolver is followed by the unavailable-player projection filter.
def resolve_player_availability(player_id: object, team: object, injury_rows: pd.DataFrame, team_source_states: dict[str, str]) -> tuple[str, str]:
    """Return reported player status and source state; absence from a report is never HEALTHY."""
    pid=str(player_id)
    rows=injury_rows[injury_rows.get("record_type",pd.Series(index=injury_rows.index,dtype=object)).eq("PLAYER_REPORT")] if len(injury_rows) else pd.DataFrame()
    report=rows[rows.player_id.astype(str).eq(pid)].tail(1) if len(rows) and "player_id" in rows else pd.DataFrame()
    if report.empty:
        return "UNKNOWN", team_source_states.get(str(team),"SOURCE_MISSING")
    row=report.iloc[-1]
    raw=str(row.get("game_status","UNKNOWN")).upper()
    known={"HEALTHY","ACTIVE","QUESTIONABLE","DOUBTFUL","OUT","IR","PUP","INACTIVE"}
    availability=raw if raw in known else "UNKNOWN"
    source_state=str(row.get("source_state",team_source_states.get(str(team),"SOURCE_MISSING")))
    if source_state in {"", "NAN", "NONE"}: source_state=team_source_states.get(str(team),"SOURCE_MISSING")
    return availability,source_state



    """Remove confirmed unavailable players without inventing replacement volume."""
def redistribute_unavailable_opportunity(candidate: pd.DataFrame, expected: dict[str,np.ndarray], unavailable_ids: set[str]) -> tuple[pd.DataFrame,dict[str,np.ndarray]]:

    keep=~candidate.player_id.astype(str).isin(unavailable_ids)

    kept=candidate[keep].reset_index(drop=True)

    adjusted={name:np.asarray(values)[keep.to_numpy()].copy() for name,values in expected.items()}

    original_teams=candidate.team.reset_index(drop=True)

    kept_teams=kept.team.reset_index(drop=True)

    for name in ("carries","targets","rushing_yards","receiving_yards"):

        values=np.asarray(expected[name])

        for team in kept_teams.unique():

            original_total=float(values[original_teams.eq(team)].sum())

            mask=kept_teams.eq(team).to_numpy()

            remaining=float(adjusted[name][mask].sum())

            if original_total>remaining>0:

                # A finite cap prevents a single late scratch from creating implausible volume.

                pass  # Replacement opportunity requires a learned model; do not hand-scale it here.

    return kept,adjusted





def ingest_snapshots(settings: Settings, season: int, week: int, retrieved_at: datetime | None=None) -> tuple[Path,pd.DataFrame]:

    retrieved_at=retrieved_at or utc_now(); stamp=_timestamp(retrieved_at)

    destination=settings.root/"data"/"snapshots"/str(season)/f"week_{week:02d}"/stamp

    if destination.exists(): raise FileExistsError(f"Snapshot is immutable: {destination}")

    destination.mkdir(parents=True)

    source_root=settings.root/"data"/"phase2_external"; rows=[]

    scheduled_teams:set[str]=set()

    schedule_source=source_root/"games.csv"

    if schedule_source.exists():

        all_games=pd.read_csv(schedule_source,low_memory=False)

        target=all_games[(all_games.season.eq(season))&(all_games.week.eq(week))&all_games.game_type.eq("REG")]

        scheduled_teams=set(target.home_team)|set(target.away_team)

    provider_path=source_root/f"live_provider_manifest_{season}_w{week:02d}.csv"

    provider_manifest=pd.read_csv(provider_path,low_memory=False).set_index("source") if provider_path.exists() else pd.DataFrame()

    for name,spec in SOURCE_SPECS.items():

        filename=spec["file"].format(season=season,week=week) if spec["file"] else None

        source=source_root/filename if filename else None

        exists=bool(source and source.exists())

        target_path=destination/filename if exists else None

        frame=pd.read_csv(source,low_memory=False) if exists else pd.DataFrame()

        if exists: shutil.copy2(source,target_path)

        warnings=[]; status="READY"; source_modified=""; age_hours=""

        provider=provider_manifest.loc[name] if len(provider_manifest) and name in provider_manifest.index else None

        source_url=str(provider.source_url) if provider is not None else spec["url"]

        known_at=pd.to_datetime(provider.source_known_at_utc,utc=True).to_pydatetime() if provider is not None else retrieved_at

        reliability=str(provider.reliability) if provider is not None else spec["reliability"]
        source_state=str(getattr(provider,"source_state","VALID")) if provider is not None else "VALID"

        if provider is not None:
            status=str(provider.data_status)
            if pd.notna(provider.quality_warnings) and str(provider.quality_warnings): warnings.append(str(provider.quality_warnings))
        if spec["critical"] and source_state in {"STALE","SOURCE_MISSING","ERROR"}:
            status="BLOCKED"
            warnings.append(f"critical source state is {source_state}")

        if not exists:

            status="BLOCKED" if spec["critical"] else "READY_WITH_WARNINGS"; source_state="SOURCE_MISSING"; warnings.append("source unavailable")

        else:

            source_modified=datetime.fromtimestamp(source.stat().st_mtime,tz=timezone.utc)

            if provider is None: known_at=min(source_modified,retrieved_at)

            age_hours=max(0.0,(retrieved_at-known_at).total_seconds()/3600)

            if age_hours>spec["max_age_hours"]:

                status="BLOCKED" if spec["critical"] else "READY_WITH_WARNINGS"
                source_state="STALE"

                warnings.append(f"staged source age {age_hours:.1f}h exceeds {spec['max_age_hours']}h")

        if exists and name=="schedule":

            target=frame[(frame.season.eq(season))&(frame.week.eq(week))&frame.game_type.eq("REG")]

            if target.empty: status="BLOCKED"; warnings.append("requested week has no fixtures")

            if not target.game_id.is_unique: status="BLOCKED"; warnings.append("duplicate game IDs")

            if target[["home_score","away_score"]].notna().any().any(): status="BLOCKED"; warnings.append("target-week result present")

        elif exists and name=="roster":

            target=frame[(frame.season.eq(season))&(frame.week.eq(week))&frame.game_type.eq("REG")]

            coverage=len(set(target.team)&scheduled_teams)

            if coverage<len(scheduled_teams): status="BLOCKED"; warnings.append(f"team coverage {coverage}/{len(scheduled_teams)}")

        elif exists and name=="injuries":

            coverage_file=source_root/f"injuries_coverage_{season}_w{week:02d}.csv"

            if coverage_file.exists():

                assessed=pd.read_csv(coverage_file)

                if set(assessed.team)!=scheduled_teams: status="BLOCKED"; warnings.append("not every scheduled team has an injury coverage assessment")

                states=assessed.source_state.astype(str) if "source_state" in assessed else assessed.coverage_status.astype(str)
                source_state="ERROR" if states.eq("ERROR").any() else "STALE" if states.eq("STALE").any() else "SOURCE_MISSING" if states.eq("SOURCE_MISSING").any() else "NOT_YET_PUBLISHED" if states.eq("NOT_YET_PUBLISHED").all() else "VALID"
                missing=int(states.eq("SOURCE_MISSING").sum())
                not_yet=int(states.eq("NOT_YET_PUBLISHED").sum())
                stale=int(states.eq("STALE").sum())
                errors=int(states.eq("ERROR").sum())
                if missing: status="BLOCKED"; warnings.append(f"{missing} team reports missing after normal deadline")
                if stale: status="BLOCKED"; warnings.append(f"{stale} team injury reports stale")
                if errors: status="BLOCKED"; warnings.append(f"{errors} team injury sources errored")
                if not missing and not stale and not errors and not_yet: status="READY_WITH_WARNINGS"; warnings.append(f"{not_yet} team reports verified not yet due")

            else:

                status="BLOCKED"; source_state="SOURCE_MISSING"; warnings.append("team injury coverage assessment unavailable; player report absence is not healthy")

        elif exists and name=="quarterbacks":

            if "source_state" not in frame or not {"injury_source_state","injury_status","starter_confirmed","confidence"}.issubset(frame.columns):
                status="BLOCKED"; source_state="SOURCE_MISSING"; warnings.append("QB source-state/provenance columns unavailable")
            elif frame.source_state.astype(str).ne("VALID").any():
                status="BLOCKED"; source_state=str(frame.loc[frame.source_state.astype(str).ne("VALID"),"source_state"].iloc[0]); warnings.append("QB source state is not VALID")

        date_columns=[c for c in frame if any(k in c.lower() for k in ("date","time","dt"))]

        dates=pd.concat([pd.to_datetime(frame[c],errors="coerce",utc=True) for c in date_columns],ignore_index=True) if date_columns else pd.Series(dtype="datetime64[ns, UTC]")

        if exists and name=="injuries":
            attempt=_latest_injury_download_attempt(settings,season)
            staged_at=pd.to_datetime(provider.retrieved_at_utc,utc=True).to_pydatetime() if provider is not None and hasattr(provider,"retrieved_at_utc") else source_modified
            if attempt and attempt[0]>staged_at:
                source_state="ERROR" if attempt[1] else "STALE"
                status="BLOCKED"
                warnings.append("newer injury refresh failed" if attempt[1] else "newer injury download has not been staged")

        rows.append({"source":name,"source_url":source_url,"source_state":source_state,"retrieved_at_utc":retrieved_at.isoformat(),"source_modified_at_utc":source_modified.isoformat() if source_modified else "","source_age_hours":round(age_hours,2) if age_hours != "" else "","max_age_hours":spec["max_age_hours"],"known_at_utc":known_at.isoformat(),"valid_from":known_at.isoformat(),"valid_to":"","season":season,"week":week,"snapshot_path":str(target_path.relative_to(settings.root)) if target_path else "","sha256":sha256_file(target_path) if target_path else "","row_count":len(frame),"schema_version":_schema_hash(frame) if len(frame) else "","minimum_date":dates.min() if dates.notna().any() else "","maximum_date":dates.max() if dates.notna().any() else "","freshness_status":status,"data_status":status,"critical":spec["critical"],"reliability":reliability,"quality_warnings":"; ".join(dict.fromkeys(warnings))})

    manifest=pd.DataFrame(rows)

    write_csv_json(manifest,destination/"snapshot_manifest.csv",destination/"snapshot_manifest.json")

    write_csv_json(manifest,settings.report_root/"phase3_source_status.csv")

    history_path=settings.artifact_root/"data_manifest.csv"

    if history_path.exists():

        history=pd.read_csv(history_path,low_memory=False)

        additions=[]

        for row in manifest[manifest.snapshot_path.ne("")].itertuples(index=False):

            additions.append({"source":"phase3_weekly_snapshot","season":season,"data_type":row.source,"file_path":row.snapshot_path,"row_count":row.row_count,"column_count":"","schema_fingerprint":row.schema_version,"minimum_date":row.minimum_date,"maximum_date":row.maximum_date,"minimum_week":week,"maximum_week":week,"duplicate_status":"immutable_snapshot","canonical_path":row.snapshot_path,"last_modified_utc":row.retrieved_at_utc,"sha256":row.sha256})

        if additions:

            pd.concat([history,pd.DataFrame(additions)],ignore_index=True).to_csv(history_path,index=False,lineterminator="\n")

    return destination,manifest





def latest_snapshot_before(settings: Settings,season:int,week:int,cutoff:datetime) -> tuple[Path,pd.DataFrame]:

    root=settings.root/"data"/"snapshots"/str(season)/f"week_{week:02d}"

    choices=[]

    for manifest_path in root.glob("*/snapshot_manifest.csv") if root.exists() else []:

        frame=pd.read_csv(manifest_path)

        retrieved=pd.to_datetime(frame.retrieved_at_utc.iloc[0],utc=True).to_pydatetime()

        if retrieved<=cutoff: choices.append((retrieved,manifest_path.parent,frame))

    if not choices: raise FileNotFoundError("No snapshot exists at or before cutoff")

    _,path,frame=max(choices,key=lambda x:x[0]); return path,frame





def create_week_lock(settings:Settings,season:int,week:int,cutoff:datetime,run_type:str,
                     replacement_player_state:bool=False,
                     replacement_previous_run:Path|None=None) -> tuple[Path,pd.DataFrame,dict[str,Any]]:

    if cutoff.tzinfo is None: raise ValueError("Cutoff must include UTC timezone")

    snapshot,manifest=latest_snapshot_before(settings,season,week,cutoff.astimezone(timezone.utc))

    if (pd.to_datetime(manifest.known_at_utc,utc=True,format="mixed")>cutoff).any(): raise ValueError("Post-cutoff snapshot selected")

    from nfl2026.production.feature_archive import prepared_feature_sources
    feature_sources,feature_archive=prepared_feature_sources(settings,season,week,cutoff,require_model_contracts=run_type=="production")

    replacement_state=None; replacement_comparison=None; player_feature_source_sha=""
    player_rollforward_manifest_sha=""
    if replacement_player_state:
        if week<2:
            raise ValueError("Run-scoped replacement player state requires a completed prior week")
        from nfl2026.production.replacement import build_run_scoped_player_state
        feature_path=settings.artifact_root/"phase2"/"player_pregame_universe_features.csv"
        rollforward_manifest=settings.artifact_root/"phase2"/"rollforward_manifests"/str(season)/f"week_{week:02d}.json"
        required={"games.csv","weekly_roster_{0}.csv".format(season),"injuries_{0}.csv".format(season),"depth_charts_{0}.csv".format(season)}
        if not feature_path.exists() or not rollforward_manifest.exists():
            raise FileNotFoundError("Validated global player feature block or roll-forward manifest is missing")
        snapshot_files={path.name:path for path in snapshot.iterdir() if path.is_file()}
        if not required.issubset(snapshot_files):
            raise FileNotFoundError(f"Replacement lock snapshot lacks required live player state: {sorted(required-set(snapshot_files))}")
        roll_meta=json.loads(rollforward_manifest.read_text(encoding="utf-8"))
        if int(roll_meta.get("season",-1))!=season or int(roll_meta.get("target_week",-1))!=week or int(roll_meta.get("source_completed_week",-1))!=week-1:
            raise ValueError("Global player feature roll-forward is not validated through exactly the prior week")
        features=pd.read_csv(feature_path,low_memory=False)
        target_features=features[pd.to_numeric(features.season,errors="coerce").eq(season)&pd.to_numeric(features.week,errors="coerce").eq(week)].copy()
        if target_features.empty or int(roll_meta.get("row_count",-1))!=len(target_features):
            raise ValueError("Global Week player feature block does not match its validated roll-forward manifest")
        feature_cutoffs=pd.to_datetime(target_features.prediction_cutoff_utc,utc=True,errors="coerce")
        if feature_cutoffs.isna().any() or feature_cutoffs.gt(pd.Timestamp(cutoff)).any():
            raise ValueError("Global player feature block has invalid or post-lock prediction cutoffs")
        replacement_state,replacement_comparison=build_run_scoped_player_state(
            features,
            pd.read_csv(snapshot_files[f"weekly_roster_{season}.csv"],low_memory=False),
            pd.read_csv(snapshot_files[f"injuries_{season}.csv"],low_memory=False),
            pd.read_csv(snapshot_files[f"depth_charts_{season}.csv"],low_memory=False),
            pd.read_csv(snapshot_files["games.csv"],low_memory=False),
            season=season,week=week,feature_version=settings.phase2_feature_version,
        )
        registry=settings.artifact_root/"registry"/"model_registry.csv"
        model_versions={}
        for model_name, version in (("team_score_model",settings.phase2_model_version),
                                    ("player_opportunity_model",settings.phase2_player_model_version),
                                    ("player_td_model",settings.phase2_model_version)):
            model_path,model_registry_row=load_registered_model(registry,model_name,version,settings.phase2_feature_version)
            model_artifact=joblib.load(model_path)
            required_columns=set(model_artifact.get("features",[]))
            if model_name=="player_opportunity_model":
                required_columns.update(*(set(model.get("features",[])) for model in model_artifact.get("models",{}).values() if isinstance(model,dict)))
            if model_name=="team_score_model" and any(token in str(column).lower() for column in required_columns for token in ("moneyline","spread_line","total_line","bookmaker","odds")):
                raise ValueError("Registered team model unexpectedly consumes bookmaker/schedule-market columns")
            if model_name!="team_score_model":
                missing_columns=sorted(required_columns-set(replacement_state.columns))
                if missing_columns:
                    raise ValueError(f"Run-scoped player state violates {model_name} input contract: {missing_columns}")
            model_key={"team_score_model":"team","player_td_model":"td","player_opportunity_model":"player_opportunity"}[model_name]
            model_versions[model_key]={"version":version,"artifact_sha256":str(model_registry_row["artifact_sha256"])}
        replacement_comparison["model_versions"]=model_versions
        if replacement_previous_run is not None:
            prior_meta=json.loads((replacement_previous_run/"metadata.json").read_text(encoding="utf-8"))
            if prior_meta.get("run_status")!="OFFICIAL_PRODUCTION" or int(prior_meta.get("season",-1))!=season or int(prior_meta.get("week",-1))!=week:
                raise ValueError("Replacement comparison source is not an official run for this season/week")
            for filename,expected_sha in prior_meta.get("file_hashes",{}).items():
                path=replacement_previous_run/filename
                if not path.exists() or sha256_file(path)!=expected_sha:
                    raise ValueError(f"Existing official run is not hash-valid: {filename}")
            old_lock_id=str(prior_meta.get("lock_id", ""))
            old_lock=settings.root/"locks"/str(season)/f"week_{week:02d}"/old_lock_id
            if not old_lock.exists() or verify_lock(old_lock):
                raise ValueError("Existing official run lock is missing or hash-invalid")
            old_player_file=replacement_previous_run/"players.csv"
            old_roster_file=next((old_lock/"inputs").glob(f"weekly_roster_{season}.csv"),None)
            if not old_player_file.exists() or old_roster_file is None:
                raise ValueError("Existing official run lacks player/roster artifacts for comparison")
            replacement_comparison=compare_with_existing_run(
                pd.read_csv(old_player_file,low_memory=False),
                pd.read_csv(old_roster_file,low_memory=False),replacement_state,replacement_comparison)
            replacement_comparison["old_official_run_id"]=prior_meta["run_id"]
            replacement_comparison["old_lock_id"]=old_lock_id
        player_feature_source_sha=sha256_file(feature_path)
        player_rollforward_manifest_sha=sha256_file(rollforward_manifest)

    team_state_source=None; team_state_sha=""
    if week>=4:
        from nfl2026.features.team_rollforward import ensure_team_state, state_paths
        if ensure_team_state(settings,season,week,check_only=True)["status"]!="ALREADY_CURRENT":
            raise ValueError("Team state changed after preparation; rerun weekly preparation")
        state_path,state_manifest,_=state_paths(settings,season,week)
        if not state_path.exists() or not state_manifest.exists():
            raise ValueError("Week 4+ PIT team state missing; run weekly preparation")
        state_meta=json.loads(state_manifest.read_text(encoding="utf-8"))
        team_state_sha=sha256_file(state_path)
        if state_meta.get("state_file_sha256")!=team_state_sha or state_meta.get("season")!=season or state_meta.get("target_week")!=week:
            raise ValueError("Team roll-forward state provenance mismatch")
        generated_at=pd.to_datetime(state_meta.get("generated_at_utc"),utc=True,errors="coerce")
        if pd.isna(generated_at) or generated_at>pd.Timestamp(cutoff):
            raise ValueError("Team state was generated after the lock cutoff")
        team_state_source=state_path
    feature_digest="|".join(feature_archive.get("source_sha256",{}).values())
    digest=hashlib.sha256((f"{season}|{week}|{cutoff.isoformat()}|"+"|".join(manifest.sha256.fillna(""))+"|"+team_state_sha+"|"+player_feature_source_sha+"|"+player_rollforward_manifest_sha+"|"+feature_digest).encode()).hexdigest()[:16]

    lock_id=f"lock-{season}-w{week:02d}-{digest}"

    destination=settings.root/"locks"/str(season)/f"week_{week:02d}"/lock_id

    if destination.exists(): raise FileExistsError(f"Lock is immutable: {destination}")

    inputs=destination/"inputs"; inputs.mkdir(parents=True)
    for name,source in feature_sources.items():
        shutil.copy2(source,inputs/name)
    if team_state_source is not None:
        shutil.copy2(team_state_source,inputs/f"team_feature_state_{season}_w{week:02d}.csv")
        shutil.copy2(state_manifest,inputs/f"team_feature_state_{season}_w{week:02d}.json")

    if replacement_state is not None:
        player_state_path=inputs/"player_pregame_universe_features_run_scoped.csv"
        replacement_state.to_csv(player_state_path,index=False,lineterminator="\n")
        (inputs/"player_replacement_comparison.json").write_text(json.dumps(replacement_comparison,indent=2,sort_keys=True),encoding="utf-8")

    selected=[]

    for row in manifest.itertuples(index=False):

        copied=""

        if isinstance(row.snapshot_path,str) and row.snapshot_path:

            source=settings.root/row.snapshot_path; target=inputs/source.name; shutil.copy2(source,target); copied=str(target.relative_to(settings.root))

        selected.append({**row._asdict(),"locked_path":copied,"selected_for_lock":True})

    lock_manifest=pd.DataFrame(selected)

    feature_rows=[]
    for name,source in feature_sources.items():
        row={column:"" for column in lock_manifest.columns}
        row.update({"source":"exact_"+name.removesuffix(".csv"),"source_url":str(source.relative_to(settings.root)),
            "source_state":"VALID","retrieved_at_utc":cutoff.isoformat(),"known_at_utc":cutoff.isoformat(),
            "season":season,"week":week,"snapshot_path":"","locked_path":str((inputs/name).relative_to(settings.root)),
            "sha256":feature_archive["source_sha256"][name],"row_count":feature_archive["source_row_counts"][name],
            "data_status":"READY","freshness_status":"READY","critical":True,"selected_for_lock":True,
            "reliability":"EXACT_PREPARED_FEATURE_SOURCE_BYTES","quality_warnings":"Numerical features unchanged; archived for future replay"})
        feature_rows.append(row)
    if feature_rows:
        lock_manifest=pd.concat([lock_manifest,pd.DataFrame(feature_rows)],ignore_index=True)

    if replacement_state is not None:
        player_state_path=inputs/"player_pregame_universe_features_run_scoped.csv"
        comparison_path=inputs/"player_replacement_comparison.json"
        player_state_row={column:"" for column in lock_manifest.columns}
        player_state_row.update({
            "source":"player_feature_state_run_scoped","source_url":"validated global Week feature block + locked current eligibility overlay",
            "source_state":"VALID","retrieved_at_utc":cutoff.isoformat(),"source_known_at_utc":cutoff.isoformat(),
            "known_at_utc":cutoff.isoformat(),"valid_from":cutoff.isoformat(),"season":season,"week":week,
            "snapshot_path":"","locked_path":str(player_state_path.relative_to(settings.root)),"row_count":len(replacement_state),
            "sha256":sha256_file(player_state_path),"schema_version":_schema_hash(replacement_state),
            "data_status":"READY","freshness_status":"READY","critical":True,
            "reliability":"LOCKED_PIT_NUMERICAL_FEATURES_WITH_CURRENT_ELIGIBILITY_OVERLAY",
            "quality_warnings":"Numerical history copied unchanged; active eligibility and depth annotations refreshed",
            "selected_for_lock":True,
        })
        comparison_row={column:"" for column in lock_manifest.columns}
        comparison_row.update({"source":"replacement_preflight_comparison","source_url":"frozen official run vs run-scoped player input state",
            "source_state":"VALID","retrieved_at_utc":cutoff.isoformat(),"source_known_at_utc":cutoff.isoformat(),
            "known_at_utc":cutoff.isoformat(),"valid_from":cutoff.isoformat(),"season":season,"week":week,
            "snapshot_path":"","locked_path":str(comparison_path.relative_to(settings.root)),"row_count":1,
            "sha256":sha256_file(comparison_path),"schema_version":"json-v1","data_status":"READY",
            "freshness_status":"READY","critical":True,"reliability":"IMMUTABLE_OLD_RUN_COMPARISON",
            "quality_warnings":"Audit only; original official run and lock are read-only","selected_for_lock":True})
        lock_manifest=pd.concat([lock_manifest,pd.DataFrame([player_state_row,comparison_row])],ignore_index=True)

    write_csv_json(lock_manifest,destination/"lock_manifest.csv",destination/"lock_manifest.json")

    schedule_file=inputs/"games.csv"

    schedule=pd.read_csv(schedule_file,low_memory=False)

    target=schedule[(schedule.season.eq(season))&(schedule.week.eq(week))&schedule.game_type.eq("REG")].copy()

    local=pd.to_datetime(target.gameday.astype(str)+" "+target.gametime.fillna("13:00"),errors="coerce")

    target["kickoff_utc"]=local.dt.tz_localize("America/New_York",ambiguous="NaT",nonexistent="shift_forward").dt.tz_convert("UTC")

    game_errors=[]

    if target.empty or not target.game_id.is_unique: game_errors.append("target fixtures missing or duplicated")

    if target[["home_team","away_team"]].isna().any().any(): game_errors.append("fixture teams missing")

    if target.kickoff_utc.isna().any(): game_errors.append("fixture kickoff missing")

    if target.kickoff_utc.le(pd.Timestamp(cutoff)).any(): game_errors.append("one or more target games already started")

    for file in inputs.glob("*.csv"):

        frame=pd.read_csv(file,low_memory=False)

        for column in ("known_at_utc","source_known_at_utc"):

            if column in frame and pd.to_datetime(frame[column],utc=True,errors="coerce").gt(pd.Timestamp(cutoff)).any():

                game_errors.append(f"post-cutoff row in {file.name}:{column}")

    production_blockers=lock_manifest[(lock_manifest.critical.astype(bool))&lock_manifest.data_status.eq("BLOCKED")].source.tolist()
    if run_type=="production" and feature_archive["status"]!="READY":
        production_blockers.append("exact_model_feature_archive")

    if game_errors: production_blockers.append("lock_time_fixture_or_cutoff_validation")

    lock_status="BLOCKED" if production_blockers else ("READY_WITH_WARNINGS" if lock_manifest.data_status.ne("READY").any() else "READY")

    metadata={"lock_id":lock_id,"created_at_utc":utc_now().isoformat(),"cutoff_utc":cutoff.isoformat(),"season":season,"week":week,"run_type":run_type,"snapshot_id":snapshot.name,"lock_status":lock_status,"production_blockers":production_blockers,"fixture_cutoff_validation":"PASS" if not game_errors else "FAIL", "fixture_cutoff_reasons":game_errors,"feature_version":settings.phase2_feature_version,"model_version":settings.phase2_model_version,"manifest_sha256":sha256_file(destination/"lock_manifest.csv"),"architecture":"single explicit weekly cutoff; every displayed prediction must predate its game kickoff"}
    metadata["exact_feature_archive"]=feature_archive
    if team_state_source is not None:
        metadata["team_feature_state_sha256"]=team_state_sha
        metadata["team_feature_manifest_sha256"]=sha256_file(inputs/f"team_feature_state_{season}_w{week:02d}.json")
    if replacement_state is not None:
        metadata.update({
            "run_scoped_replacement":True,
            "run_scoped_player_state_path":str((inputs/"player_pregame_universe_features_run_scoped.csv").relative_to(settings.root)),
            "run_scoped_player_state_sha256":sha256_file(inputs/"player_pregame_universe_features_run_scoped.csv"),
            "global_player_feature_sha256":player_feature_source_sha,
            "player_rollforward_manifest_sha256":player_rollforward_manifest_sha,
            "replacement_player_comparison":replacement_comparison,
            "production_model_ids":model_versions,
            "transaction_source_state":str(lock_manifest.loc[lock_manifest.source.eq("transactions"),"data_status"].iloc[0]) if lock_manifest.source.eq("transactions").any() else "SOURCE_MISSING",
        })
    metadata["critical_source_states"]=[{k:getattr(row,k) for k in ("source","source_state","data_status","retrieved_at_utc","known_at_utc","sha256","quality_warnings")} for row in lock_manifest[lock_manifest.source.isin(["injuries","quarterbacks"])].itertuples(index=False)]

    (destination/"data_lock.json").write_text(json.dumps(metadata,indent=2,sort_keys=True),encoding="utf-8")

    report=lock_manifest[["source","data_status","critical","reliability","row_count","sha256","quality_warnings"]].copy(); report.insert(0,"lock_id",lock_id)

    write_csv_json(report,settings.report_root/"phase3_data_lock_report.csv")

    return destination,lock_manifest,metadata





def verify_lock(lock:Path) -> list[str]:

    manifest=pd.read_csv(lock/"lock_manifest.csv"); errors=[]

    for row in manifest.itertuples(index=False):

        if isinstance(row.locked_path,str) and row.locked_path:

            path=lock.parents[3]/row.locked_path if False else Path(lock.anchor)/Path(row.locked_path)

            # Stored paths are workspace-relative; infer workspace from locks/<season>/week/run.

            workspace=lock.parents[3]; path=workspace/row.locked_path

            if not path.exists() or sha256_file(path)!=row.sha256: errors.append(f"hash mismatch: {row.source}")

    meta=json.loads((lock/"data_lock.json").read_text(encoding="utf-8"))
    archive=meta.get("exact_feature_archive",{})
    if archive.get("status")=="READY":
        for name,digest in archive.get("source_sha256",{}).items():
            path=lock/"inputs"/name
            if not path.is_file() or sha256_file(path)!=digest:
                errors.append(f"exact feature archive hash mismatch: {name}")
    if int(meta.get("week",0))>=4:
        if "team_feature_state_sha256" not in meta or "team_feature_manifest_sha256" not in meta:
            errors.append("Week 4+ team feature state is not locked")
            return errors
        season=int(meta["season"]); week=int(meta["week"])
        state=lock/"inputs"/f"team_feature_state_{season}_w{week:02d}.csv"
        state_manifest=lock/"inputs"/f"team_feature_state_{season}_w{week:02d}.json"
        if not state.exists() or sha256_file(state)!=meta["team_feature_state_sha256"]:
            errors.append("team feature state hash mismatch")
        if not state_manifest.exists() or sha256_file(state_manifest)!=meta["team_feature_manifest_sha256"]:
            errors.append("team feature manifest hash mismatch")
    return errors





def _git_version(root:Path)->str:

    try:return subprocess.check_output(["git","rev-parse","HEAD"],cwd=root,text=True).strip()+("+dirty" if subprocess.check_output(["git","status","--porcelain"],cwd=root,text=True).strip() else "")

    except Exception:return "unavailable"





def _build_preview(games:pd.DataFrame,players:pd.DataFrame,metadata:dict)->dict:

    fixtures=[]

    game_records=json.loads(games.sort_values("kickoff_utc").to_json(orient="records",date_format="iso"))

    for game in game_records:

        gp=players[players.game_id.eq(game["game_id"])]

        display=gp[(gp.expected_carries.ge(3))|gp.expected_targets.ge(2.5)|gp.reconciled_td_probability.ge(.08)].copy()

        game["players"]=json.loads(display.sort_values("reconciled_td_probability",ascending=False).to_json(orient="records",date_format="iso"))

        fixtures.append(game)

    return {"metadata":metadata,"games":fixtures}





def generate_bundle(settings:Settings,lock:Path,mode:str="dry-run",seed:int|None=None,weekly_stage:str="EARLY_WEEK",run_reason:str|None=None,*,_partial_attempt:int=0)->tuple[Path,pd.DataFrame]:

    base_lock=lock
    initial_meta=json.loads((lock/"data_lock.json").read_text())
    runtime_checks=[]
    if mode=="production" and initial_meta.get("prediction_scope")=="UNPLAYED_ONLY":
        from nfl2026.production.partial_gate import prepare_partial_inference_lock
        if _partial_attempt>=3:
            raise RuntimeError("Partial fixture states kept changing; originals retained; refresh and retry")
        lock=prepare_partial_inference_lock(settings,base_lock)

    lock_meta=json.loads((lock/"data_lock.json").read_text()); cutoff=pd.Timestamp(lock_meta["cutoff_utc"])

    if mode=="production" and lock_meta["lock_status"]=="BLOCKED": raise RuntimeError(f"Production blocked by: {lock_meta['production_blockers']}")

    if verify_lock(lock): raise ValueError("Lock hash verification failed")

    schedule_path=next((lock/"inputs").glob("games.csv")); schedule=pd.read_csv(schedule_path,low_memory=False)

    schedule=schedule[(schedule.season.eq(lock_meta["season"]))&(schedule.week.eq(lock_meta["week"]))&schedule.game_type.eq("REG")].copy()

    from nfl2026.live.pipeline import normalize_schedule_kickoffs
    schedule=normalize_schedule_kickoffs(schedule,required_game_ids=set(schedule.game_id.astype(str)))

    if (schedule.kickoff_utc<=cutoff).any(): raise ValueError("Cutoff is not before every target kickoff")

    if schedule[["home_score","away_score"]].notna().any().any(): raise ValueError("Target-week result leakage")

    registry=settings.artifact_root/"registry"/"model_registry.csv"

    team_path,team_reg=load_registered_model(registry,"team_score_model",settings.phase2_model_version,settings.phase2_feature_version)

    player_path,player_reg=load_registered_model(

        registry,

        "player_opportunity_model",

        settings.phase2_player_model_version,

        settings.phase2_feature_version

    )

    td_path,td_reg=load_registered_model(registry,"player_td_model",settings.phase2_model_version,settings.phase2_feature_version)
    archived_contracts=lock_meta.get("exact_feature_archive",{}).get("model_contracts",{})
    for name,row in (("team_score_model",team_reg),("player_opportunity_model",player_reg),("player_td_model",td_reg)):
        expected_contract=archived_contracts.get(name)
        if expected_contract and (expected_contract["version"]!=row["model_version"] or expected_contract["artifact_sha256"]!=row["artifact_sha256"]):
            raise ValueError(f"Registered model changed after input lock: {name}")

    from scripts.build_phase2 import _current_team_matrix

    partial_run = lock_meta.get("prediction_scope") == "UNPLAYED_ONLY"
    run_scoped_replacement = bool(lock_meta.get("run_scoped_replacement"))
    historical_path = lock/"inputs"/"historical_team_features.csv"
    if not historical_path.exists():
        if partial_run or lock_meta.get("exact_feature_archive",{}).get("status")=="READY":
            raise ValueError("Locked historical team feature archive is missing")
        historical_path=settings.artifact_root/"phase2"/"team_advanced_matrix.csv"
    historical=pd.read_csv(historical_path,low_memory=False); historical["kickoff_utc"]=pd.to_datetime(historical.kickoff_utc,utc=True)

    tmodel=joblib.load(team_path); features=tmodel["features"]
    state_path=lock/"inputs"/f"team_feature_state_{lock_meta['season']}_w{lock_meta['week']:02d}.csv"
    team_state=pd.read_csv(state_path,low_memory=False) if state_path.exists() else None
    if partial_run:
        context_path=lock/"inputs"/"team_context_schedule.csv"
        context=pd.read_csv(context_path,low_memory=False)
        target_week=int(lock_meta["week"]); target_season=int(lock_meta["season"])
        for column in ("away_score","home_score","result","total","overtime"):
            if column in context and context.loc[context.season.eq(target_season)&context.week.ge(target_week),column].notna().any():
                raise ValueError(f"Partial PIT context contains target/future outcome data: {column}")
        candidate_lock=lock/"inputs"/"player_pregame_universe_features.csv"
        if not candidate_lock.exists() or sha256_file(candidate_lock)!=lock_meta.get("pregame_player_features_sha256"):
            raise ValueError("Partial PIT player-feature lock is missing or hash-invalid")
        if not context_path.exists() or sha256_file(context_path)!=lock_meta.get("team_context_schedule_sha256"):
            raise ValueError("Partial PIT team schedule context is missing or hash-invalid")
        if sha256_file(historical_path)!=lock_meta.get("historical_team_features_sha256"):
            raise ValueError("Partial historical team feature lock is missing or hash-invalid")
        eligibility_path=lock/"inputs"/"game_eligibility.csv"
        if not eligibility_path.exists() or sha256_file(eligibility_path)!=lock_meta.get("game_eligibility_sha256"):
            raise ValueError("Partial game eligibility artifact is missing or hash-invalid")
        eligibility=pd.read_csv(eligibility_path)
        from nfl2026.live.partial_week import (
            assert_partial_team_matrix_coverage, build_partial_current_team_matrix,
        )
        eligible_ids=set(map(str,lock_meta["eligible_game_ids"]))
        if team_state is not None:
            team_state=team_state[team_state.game_id.astype(str).isin(eligible_ids)].copy()
        current=build_partial_current_team_matrix(
            context,historical,features,team_state,int(lock_meta["week"]),
            lock_meta.get("eligible_game_ids"),eligibility,_current_team_matrix,
        )
        current=current[current.game_id.astype(str).isin(eligible_ids)].reset_index(drop=True)
        assert_partial_team_matrix_coverage(current,lock_meta.get("eligible_game_ids"))
    else:
        current=_current_team_matrix(schedule,historical,features,team_state=team_state)

    if partial_run and mode=="production":
        from nfl2026.production.partial_gate import recheck_inference_window
        safe,checked=recheck_inference_window(lock)
        if not safe:
            return generate_bundle(settings,base_lock,mode,seed,weekly_stage,run_reason,_partial_attempt=_partial_attempt+1)
        runtime_checks.append(checked)

    home=np.clip(tmodel["home"].predict(current[features]),0,60); away=np.clip(tmodel["away"].predict(current[features]),0,60)

    original_seed=settings.random_seed

    point=pd.DataFrame({"game_id":current.game_id,"home_expected_points":home,"away_expected_points":away})

    simulation_settings=replace(settings,random_seed=seed if seed is not None else original_seed)

    simulated=simulate_games(simulation_settings,point)

    games=current.merge(simulated,on="game_id").merge(schedule[["game_id","stadium","roof","surface"]],on="game_id",how="left")

    injury_files=list((lock/"inputs").glob(f"injuries_{lock_meta['season']}.csv"))

    injuries=pd.read_csv(injury_files[0],low_memory=False) if injury_files else pd.DataFrame()

    depth_files=list((lock/"inputs").glob(f"depth_charts_{lock_meta['season']}.csv"))

    depth=pd.read_csv(depth_files[0],low_memory=False) if depth_files else pd.DataFrame()

    weather_files=list((lock/"inputs").glob(f"weather_{lock_meta['season']}_w{lock_meta['week']:02d}.csv"))

    weather=pd.read_csv(weather_files[0],low_memory=False) if weather_files else pd.DataFrame()

    qb_files=list((lock/"inputs").glob(f"qb_status_{lock_meta['season']}_w{lock_meta['week']:02d}.csv"))

    qbs=pd.read_csv(qb_files[0],low_memory=False) if qb_files else pd.DataFrame()

    injury_coverage=injuries[injuries.get("record_type",pd.Series(index=injuries.index,dtype=object)).eq("TEAM_COVERAGE")].copy() if len(injuries) and "record_type" in injuries else pd.DataFrame(columns=["team","coverage_status","source_state"])

    if "source_state" not in injury_coverage:
        legacy=injury_coverage.coverage_status.astype(str)
        injury_coverage["source_state"]=legacy.map({"REPORT_PUBLISHED":"VALID","NOT_YET_PUBLISHED":"NOT_YET_PUBLISHED","SOURCE_MISSING":"SOURCE_MISSING"}).fillna("SOURCE_MISSING")
    injury_lookup=injury_coverage.drop_duplicates("team",keep="last").set_index("team").source_state.to_dict()

    qb_lookup=qbs.drop_duplicates(["game_id","team"],keep="last").set_index(["game_id","team"]).to_dict("index") if len(qbs) and {"game_id","team"}.issubset(qbs.columns) else {}
    weather_lookup=weather.set_index("game_id").to_dict("index") if len(weather) else {}

    game_status=[]; game_warnings=[]; weather_summaries=[]; source_gate_failures=[]

    for row in games.itertuples(index=False):

        reasons=[]; blockers=[]; source_issues=[]

        for team in (row.away_team,row.home_team):

            value=injury_lookup.get(team,"SOURCE_MISSING")
            if value!="VALID":
                source_issues.append(f"{team} injury state {value}")
                if value=="NOT_YET_PUBLISHED": reasons.append(f"{team} injury report not yet published")
                else: blockers.append(f"{team} injury source {value}")
            qb=qb_lookup.get((row.game_id,team),{})
            qb_state=str(qb.get("source_state","SOURCE_MISSING"))
            qb_status=str(qb.get("status","UNCERTAIN"))
            qb_health=str(qb.get("injury_status","UNKNOWN")).upper()
            if qb_state!="VALID" or qb_status not in ("EXPECTED","VALIDATED","CONFIRMED"):
                source_issues.append(f"{team} QB source/state {qb_state}/{qb_status}")
            if qb_health in ("QUESTIONABLE","DOUBTFUL","OUT","IR","PUP","INACTIVE"):
                source_issues.append(f"{team} expected QB availability {qb_health}")

        wx=weather_lookup.get(row.game_id,{})

        wx_status=wx.get("forecast_status","FAILED_RETRIEVAL")

        if wx_status=="AVAILABLE":

            weather_summaries.append(f"{wx.get('weather_condition','Forecast')} · {float(wx.get('temperature_f')):.0f}°F · wind {float(wx.get('wind_speed_mph')):.0f} mph")

            if wx.get("roof_status")=="RETRACTABLE_UNKNOWN": reasons.append("retractable roof status unknown; forecast is context only")

        elif wx_status=="ROOF_SUPPRESSED": weather_summaries.append("Indoor / closed-roof context")

        else:

            weather_summaries.append("Forecast unavailable")

            reasons.append("weather unavailable; not used by registered model")

        if source_issues:
            reasons.extend(issue for issue in source_issues if issue not in reasons and issue not in blockers)
            source_gate_failures.extend(f"{row.game_id}: {issue}" for issue in source_issues)
        game_status.append("DEGRADED" if source_issues else ("READY_WITH_WARNINGS" if reasons else "READY"))
        game_warnings.append("; ".join(blockers+reasons))

    final_near_kickoff=weekly_stage.upper() in {"FINAL","FINAL_WEEK"} and (pd.to_datetime(games.kickoff_utc,utc=True)-cutoff).dt.total_seconds().le(48*3600).any()
    if final_near_kickoff and source_gate_failures and mode=="production":
        raise RuntimeError("Final injury/QB source-state gate failed: " + "; ".join(source_gate_failures))

    games["display_home_score"]=games.home_expected_points.round().astype(int); games["display_away_score"]=games.away_expected_points.round().astype(int)

    games["predicted_winner"]=np.where(games.home_win_probability>=.5,games.home_team,games.away_team)

    games["model_margin"]=games.margin_mean; games["model_total"]=games.total_mean

    games["confidence_rating"]=["LOW" if value in ("BLOCKED","DEGRADED") else ("MEDIUM" if value=="READY_WITH_WARNINGS" else "HIGH") for value in game_status]

    games["data_quality_status"]=game_status; games["data_quality_warnings"]=game_warnings; games["weather_summary"]=weather_summaries

    games["weather_model_use"]="DISPLAY_CONTEXT_ONLY"

    games["main_factors"]=[json.dumps(["rolling team efficiency and opponent matchup","registered QB context","rest and venue context"]) for _ in range(len(games))]

    keep_games=["game_id","season","week","kickoff_utc","away_team","home_team","stadium","roof","surface","away_expected_points","home_expected_points","display_away_score","display_home_score","predicted_winner","home_win_probability","away_win_probability","model_margin","model_total",*[f"home_score_p{q}" for q in (10,25,50,75,90)],*[f"away_score_p{q}" for q in (10,25,50,75,90)],*[f"margin_p{q}" for q in (10,25,50,75,90)],*[f"total_p{q}" for q in (10,25,50,75,90)],"confidence_rating","data_quality_status","data_quality_warnings","weather_summary","weather_model_use","main_factors"]

    games=games[keep_games]

    roster=pd.read_csv(next((lock/"inputs").glob(f"weekly_roster_{lock_meta['season']}.csv")),low_memory=False)

    if qbs.empty:

        qbs=build_qb_status(schedule,roster,lock_meta["week"],lock_meta["cutoff_utc"])

        qbs=qbs.rename(columns={"projected_qb_id":"qb_player_id","projected_qb":"qb_name"})

    if lock_meta.get("run_scoped_replacement"):
        candidate_path=lock/"inputs"/"player_pregame_universe_features_run_scoped.csv"
        if not candidate_path.exists() or sha256_file(candidate_path)!=lock_meta.get("run_scoped_player_state_sha256"):
            raise ValueError("Replacement-run player state is missing or hash-invalid")
    else:
        candidate_path=lock/"inputs"/"player_pregame_universe_features.csv"
        if not candidate_path.exists():
            if partial_run or lock_meta.get("exact_feature_archive",{}).get("status")=="READY":
                raise ValueError("Locked player feature archive is missing")
            candidate_path=settings.artifact_root/"phase2"/"player_pregame_universe_features.csv"
    candidate=pd.read_csv(candidate_path,low_memory=False)

    # Eligibility is re-evaluated against the locked current roster/inactive
    # sources below. Retain previously ineligible target-week rows here so a
    # later authoritative activation can restore them without rebuilding a
    # model-specific candidate list.
    candidate=candidate[(candidate.season.eq(lock_meta["season"]))&candidate.week.eq(lock_meta["week"])].reset_index(drop=True)
    if lock_meta.get("eligible_game_ids"):
        candidate=candidate[candidate.game_id.astype(str).isin(set(map(str,lock_meta["eligible_game_ids"])))].reset_index(drop=True)

    transaction_path=lock/"inputs"/f"transactions_{lock_meta['season']}.csv"
    locked_transactions=pd.read_csv(transaction_path,low_memory=False) if transaction_path.exists() else None
    candidate=filter_current_candidates(candidate,roster,injuries,lock_meta["week"],transactions=locked_transactions,cutoff=lock_meta['cutoff_utc'])

    candidate = candidate[

        candidate.position.isin(["QB","RB","FB","WR","TE"])

    ].reset_index(drop=True)

    pmodel=joblib.load(player_path); expected={}

    for target,model in pmodel["models"].items(): expected[target]=np.clip(candidate[f"prior_{target}"].fillna(0).to_numpy() if isinstance(model,dict) else model.predict(candidate[pmodel["features"]]),0,None)

    idcol="player_id" if "player_id" in injuries else "gsis_id" if "gsis_id" in injuries else None

    statuscol="game_status" if "game_status" in injuries else "report_status" if "report_status" in injuries else None

    unavailable=(set() if run_scoped_replacement else
        set(injuries[injuries[statuscol].astype(str).str.upper().isin(["OUT","IR","PUP","INACTIVE"])][idcol].dropna().astype(str)) if idcol and statuscol else set())

    candidate,expected=redistribute_unavailable_opportunity(candidate,expected,unavailable)

    tdmodel=joblib.load(td_path); raw=np.clip(tdmodel["model"].predict_proba(candidate[tdmodel["features"]])[:,1],.001,.999); reconciled=np.zeros(len(candidate))

    score_lookup={(r.game_id,r.home_team):r.home_expected_points for r in games.itertuples()}; score_lookup.update({(r.game_id,r.away_team):r.away_expected_points for r in games.itertuples()})

    for _,idx in candidate.groupby(["game_id","team"]).groups.items():

        loc=np.asarray(list(idx)); target=float(np.clip(score_lookup[(candidate.loc[loc[0],"game_id"],candidate.loc[loc[0],"team"])]/7*.72,.35,4.25)); reconciled[loc]=reconcile_td_probabilities(raw[loc],target)

    def band(mean,kind):

        sd=np.sqrt(np.maximum(mean,0)+1) if kind=="count" else 8+np.sqrt(np.maximum(mean,0))*3

        return np.maximum(0,mean-1.282*sd),np.maximum(0,mean+1.282*sd)

    c_lo,c_hi=band(expected["carries"],"count");t_lo,t_hi=band(expected["targets"],"count");r_lo,r_hi=band(expected["rushing_yards"],"yards");y_lo,y_hi=band(expected["receiving_yards"],"yards")

    rush_sd=8+np.sqrt(np.maximum(expected["rushing_yards"],0))*3

    receiving_sd=8+np.sqrt(np.maximum(expected["receiving_yards"],0))*3

    history=np.minimum(candidate.player_games_before.fillna(0)/8,1)

    player_injuries=injuries[injuries.get("record_type",pd.Series(index=injuries.index,dtype=object)).eq("PLAYER_REPORT")].drop_duplicates("player_id",keep="last").set_index("player_id") if len(injuries) and "record_type" in injuries else pd.DataFrame()

    if run_scoped_replacement:
        injury_status=candidate.live_injury_status.fillna("UNKNOWN").astype(str).str.upper().tolist()
        injury_source_states=[injury_lookup.get(str(team),"SOURCE_MISSING") for team in candidate.team]
    else:
        availability=[resolve_player_availability(pid,team,injuries,injury_lookup) for pid,team in zip(candidate.player_id,candidate.team)]
        injury_status=[status for status,_ in availability]
        injury_source_states=[state for _,state in availability]

    practice_status=[str(player_injuries.loc[str(pid),"practice_status_normalized"]) if len(player_injuries) and str(pid) in player_injuries.index else "" for pid in candidate.player_id]

    players=pd.DataFrame({"game_id":candidate.game_id,"player_id":candidate.player_id,"player_name":candidate.player_name,"team":candidate.team,"opponent":candidate.opponent,"position":candidate.position,"roster_status":candidate.status,"availability_status":["QUESTIONABLE" if x in ("QUESTIONABLE","DOUBTFUL") else "ACTIVE_ROSTER" for x in injury_status],"injury_status":injury_status,"practice_status":practice_status,"expected_carries":expected["carries"],"carries_low":c_lo,"carries_high":c_hi,"expected_targets":expected["targets"],"targets_low":t_lo,"targets_high":t_hi,"expected_rush_yards":expected["rushing_yards"],"rush_yards_low":r_lo,"rush_yards_high":r_hi,"rush_distribution_mean":expected["rushing_yards"],"rush_distribution_sd":rush_sd,"expected_receiving_yards":expected["receiving_yards"],"receiving_yards_low":y_lo,"receiving_yards_high":y_hi,"receiving_distribution_mean":expected["receiving_yards"],"receiving_distribution_sd":receiving_sd,"yard_distribution_family":"NORMAL_TRUNCATED_AT_ZERO","yard_distribution_interval":"CENTRAL_80_PERCENT","raw_td_probability":raw,"reconciled_td_probability":reconciled,"model_fair_odds":1/np.clip(reconciled,.001,1),"data_confidence":.75,"explanation":[json.dumps(["lagged individual usage","current-team opportunity context","team scoring environment"]) for _ in range(len(candidate))]})
    # The threshold CDF is Normal; flooring displayed lower bounds at zero
    # does not create a mathematically truncated Normal distribution.
    players["yard_distribution_family"]="NORMAL"
    players["yard_distribution_interval"]="CENTRAL_80_PERCENT_ZERO_FLOORED_DISPLAY"
    players["availability_status"]=[x if x in ("HEALTHY","ACTIVE","QUESTIONABLE","DOUBTFUL","OUT","IR","PUP","INACTIVE") else "UNKNOWN" for x in injury_status]

    if len(depth):

        from nfl2026.live.providers.depth import merge_offensive_depth
        players=merge_offensive_depth(players,depth,roster)

        players["injury_source_state"]=injury_source_states
    else:

        players["depth_order"]=np.nan; players["depth_role"]=""; players["depth_source"]=""
        players["injury_source_state"]=injury_source_states

    players=infer_missing_roles(players)

    base=np.where(players.depth_source.eq("NFLVERSE_CURRENT"),np.where(players.depth_order.eq(1),.85,np.where(players.depth_order.eq(2),.7,.5)),.4)

    penalty=np.where(players.injury_status.isin(["QUESTIONABLE","DOUBTFUL"]),.2,np.where(players.practice_status.eq("DNP"),.15,0))

    players["role_confidence"]=np.clip(base+.1*history.to_numpy()-penalty,.1,.95)

    players["role_confidence_label"]=pd.cut(players.role_confidence,bins=[0,.49,.74,1],labels=["LOW","MEDIUM","HIGH"],include_lowest=True).astype(str)

    players["role_status"]=np.where((players.depth_role.eq("STARTER"))&players.depth_source.eq("NFLVERSE_CURRENT")&players.position.isin(["QB","RB","FB","WR","TE"]),"EXPECTED_STARTER",np.where(players.depth_source.eq("MODEL_INFERRED"),"ROLE_UNCERTAIN",""))

    players["warnings"]=["; ".join(x for x in ["Questionable availability" if inj in ("QUESTIONABLE","DOUBTFUL") else "","Role inferred from modelled opportunity" if src=="MODEL_INFERRED" else ""] if x) for inj,src in zip(players.injury_status,players.depth_source)]

    if partial_run and mode=="production":
        safe,checked=recheck_inference_window(lock)
        if not safe:
            # Discard this attempt before writing any prediction; recertify the remaining fixtures.
            return generate_bundle(settings,base_lock,mode,seed,weekly_stage,run_reason,_partial_attempt=_partial_attempt+1)
        runtime_checks.append(checked)

    run_id=f"phase3_5-{mode}-{_timestamp(cutoff.to_pydatetime())}-{hashlib.sha256((lock_meta['lock_id']+str(seed or original_seed)).encode()).hexdigest()[:8]}"

    destination=settings.prediction_root/str(lock_meta["season"])/f"week_{lock_meta['week']:02d}"/run_id

    if destination.exists(): raise FileExistsError(f"Prediction bundle is immutable: {destination}")

    destination.mkdir(parents=True)

    games.to_csv(destination/"games.csv",index=False,lineterminator="\n"); players.to_csv(destination/"players.csv",index=False,lineterminator="\n")

    partial_lineage = None
    if partial_run:
        eligibility=pd.read_csv(lock/"inputs"/"game_eligibility.csv")
        from nfl2026.operations import build_partial_game_lineage
        partial_lineage=build_partial_game_lineage(settings,int(lock_meta["season"]),int(lock_meta["week"]),
            cutoff.to_pydatetime(),run_id,str(lock_meta["lock_id"]),str(lock_meta.get("game_refresh_manifest","")),eligibility,games,
            generated_at=runtime_checks[-1]["checked_at_utc"] if runtime_checks else None)
        partial_lineage.to_csv(destination/"game_prediction_manifest.csv",index=False,lineterminator="\n")

    if partial_run and mode=="production":
        shutil.copy2(lock/"inputs"/"fixture_gate_audit.json",destination/"fixture_gate_audit.json")
        (destination/"fixture_runtime_checks.json").write_text(json.dumps(runtime_checks,indent=2,default=str),encoding="utf-8")

    qbs.to_csv(destination/"qb_status.csv",index=False,lineterminator="\n")

    weather.to_csv(destination/"weather.csv",index=False,lineterminator="\n")

    shutil.copy2(lock/"lock_manifest.csv",destination/"source_provenance.csv")

    players.sort_values(["game_id","team","reconciled_td_probability"],ascending=[True,True,False]).groupby(["game_id","team"]).head(6).to_csv(destination/"td_scorers.csv",index=False,lineterminator="\n")

    players[(players.expected_carries>=1)|(players.expected_rush_yards>=5)].sort_values(["game_id","expected_rush_yards"],ascending=[True,False]).to_csv(destination/"rushing.csv",index=False,lineterminator="\n")

    players[(players.expected_targets>=1)|(players.expected_receiving_yards>=8)].sort_values(["game_id","expected_receiving_yards"],ascending=[True,False]).to_csv(destination/"receiving.csv",index=False,lineterminator="\n")

    shutil.copy2(lock/"data_lock.json",destination/"data_lock.json")

    model_versions={"team":{"name":"team_score_model","version":settings.phase2_model_version,"hash":team_reg["artifact_sha256"]},"player":{"name":"player_opportunity_model","version":settings.phase2_player_model_version,"hash":player_reg["artifact_sha256"]},"td":{"name":"player_td_model","version":settings.phase2_model_version,"hash":td_reg["artifact_sha256"]}}

    (destination/"model_versions.json").write_text(json.dumps(model_versions,indent=2,sort_keys=True),encoding="utf-8")

    validation_gate_passed=not (final_near_kickoff and bool(source_gate_failures))
    qb_good=qbs.status.astype(str).isin(["VALIDATED","EXPECTED","CONFIRMED"])

    validations=[("fixture_count",len(games)==len(schedule),f"{len(games)}/{len(schedule)}"),("game_id_unique",games.game_id.is_unique,len(games)),("player_key_unique",not players.duplicated(["game_id","player_id"]).any(),len(players)),("qb_coverage",len(qbs)==2*len(games) and qb_good.all(),f"{qb_good.sum()}/{2*len(games)}"),("pregame_cutoff",(pd.to_datetime(games.kickoff_utc,utc=True)>cutoff).all(),cutoff.isoformat()),("probability_bounds",games.home_win_probability.between(0,1).all() and players.reconciled_td_probability.between(0,1).all(),"bounded"),("source_known_at_cutoff",not any(pd.to_datetime(pd.read_csv(p).get("known_at_utc",pd.Series(dtype=object)),utc=True,errors="coerce").gt(cutoff).any() for p in (lock/"inputs").glob("*.csv")),cutoff.isoformat()),("no_bookmaker_fields",not any(any(token in c.lower() for token in ("moneyline","bookmaker","spread_line")) for c in list(games)+list(players)),"model_fair_odds is reciprocal model probability only")]
    validations.append(("injury_qb_source_gate",validation_gate_passed,"; ".join(source_gate_failures) if not validation_gate_passed else "critical sources valid or not a FINAL near-kickoff run"))

    validation=pd.DataFrame(validations,columns=["check","passed","details"]); validation.to_csv(destination/"validation_report.csv",index=False,lineterminator="\n")

    status="DRY_RUN" if mode=="dry-run" else "OFFICIAL_PRODUCTION"

    deployment=bool(mode=="production" and validation.passed.all() and lock_meta["lock_status"]!="BLOCKED")

    metadata={"run_id":run_id,"lock_id":lock_meta["lock_id"],"model_ids":model_versions,"feature_version":settings.phase2_feature_version,"code_version":_git_version(settings.root),"created_at_utc":cutoff.isoformat(),"cutoff_utc":cutoff.isoformat(),"season":lock_meta["season"],"week":lock_meta["week"],"run_status":status,"weekly_stage":weekly_stage,"official_selection_policy":"latest valid production prediction created before each game kickoff","simulation_seed":seed or original_seed,"simulation_count":settings.simulation_draws,"data_quality_result":lock_meta["lock_status"],"validation_result":"PASS" if validation.passed.all() else "FAIL","deployment_eligible":deployment,"bookmaker_data_used":False}
    metadata["prediction_scope"]=lock_meta.get("prediction_scope","FULL_WEEK")
    if run_reason:
        metadata["run_reason"]=run_reason
    if partial_run:
        metadata["game_prediction_manifest_sha256"]=sha256_file(destination/"game_prediction_manifest.csv")
        if runtime_checks:
            metadata["created_at_utc"]=runtime_checks[-1]["checked_at_utc"]
            metadata["inference_started_at_utc"]=runtime_checks[0]["checked_at_utc"]
            metadata["inference_completed_at_utc"]=runtime_checks[-1]["checked_at_utc"]

    preview=_build_preview(games,players,metadata); (destination/"game_preview.json").write_text(json.dumps(preview,indent=2,default=str),encoding="utf-8")

    validation.to_json(destination/"validation_report.json",orient="records",indent=2)

    file_hashes={p.name:sha256_file(p) for p in destination.iterdir() if p.is_file()}; metadata["file_hashes"]=file_hashes

    (destination/"metadata.json").write_text(json.dumps(metadata,indent=2,sort_keys=True),encoding="utf-8")

    report=validation.copy(); report.insert(0,"run_id",run_id); write_csv_json(report,settings.report_root/"phase3_current_week_validation.csv")

    append_run(settings.artifact_root/"registry"/"run_registry.csv",{"run_id":run_id,"timestamp_utc":cutoff.isoformat(),"season":lock_meta["season"],"week":lock_meta["week"],"data_cutoff_utc":cutoff.isoformat(),"data_manifest_sha256":lock_meta["manifest_sha256"],"feature_version":settings.phase2_feature_version,"team_model_version":settings.phase2_model_version,"player_model_versions":settings.phase2_player_model_version,"code_version":metadata["code_version"],"prediction_output_locations":[str(destination)]})

    return destination,validation





def validate_bundle(bundle:Path)->pd.DataFrame:

    rows=[]; meta=json.loads((bundle/"metadata.json").read_text())

    for name,expected in meta["file_hashes"].items():

        path=bundle/name; rows.append({"check":f"hash_{name}","passed":path.exists() and sha256_file(path)==expected,"details":name})

    games=pd.read_csv(bundle/"games.csv"); players=pd.read_csv(bundle/"players.csv")

    rows += [{"check":"games_unique","passed":games.game_id.is_unique,"details":len(games)},{"check":"players_unique","passed":not players.duplicated(["game_id","player_id"]).any(),"details":len(players)},{"check":"dry_not_deployable","passed":meta["run_status"]!="DRY_RUN" or not meta["deployment_eligible"],"details":meta["run_status"]}]

    from nfl2026.production.partial_gate import validate_partial_certificate
    errors=validate_partial_certificate(bundle,meta)
    if meta.get("prediction_scope")=="UNPLAYED_ONLY":
        rows.append({"check":"partial_fixture_certification","passed":not errors,"details":"; ".join(errors) or "certified fixture subset"})
    return pd.DataFrame(rows)
