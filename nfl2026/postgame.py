from __future__ import annotations
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import uuid
import numpy as np
import pandas as pd
from nfl2026.live.providers.base import fetch_bytes
from nfl2026.utilities.io import sha256_file

SCHEDULE_URL="https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
PLAYER_URL="https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.csv"

SKILL_POSITIONS={"QB","RB","FB","WR","TE"}
FINAL_STATES={"FINAL","COMPLETED","COMPLETE","CLOSED","FINISHED","POST","STATUS_FINAL","GAME_FINAL"}
ACTUAL_COLUMNS=("season","week","team","opponent_team","player_id","display_name","position","carries","rushing_yards",
                "rushing_tds","targets","receptions","receiving_yards","air_yards","receiving_tds",
                "anytime_offensive_tds","anytime_offensive_td")


class WeekPreparationError(RuntimeError):
    """A prediction week cannot be prepared safely from current point-in-time data."""


def _utc_iso()->str:
    return datetime.now(timezone.utc).isoformat()


def _frame_digest(frame:pd.DataFrame)->str:
    value=frame.copy()
    keys=[c for c in ("season","week","game_id","team","player_id","gsis_id") if c in value]
    if keys and len(value):
        value=value.sort_values(keys,kind="mergesort",na_position="last")
    body=value.to_csv(index=False,na_rep="",float_format="%.12g",lineterminator="\n")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _atomic_json(path,value)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value,indent=2,sort_keys=True,default=str)+"\n",encoding="utf-8")
    temp.replace(path)


def _atomic_csv(frame:pd.DataFrame,path)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    frame.to_csv(temp,index=False,lineterminator="\n")
    temp.replace(path)


def _week_rows(frame:pd.DataFrame,season:int,week:int)->pd.DataFrame:
    if not {"season","week"}.issubset(frame.columns):
        return frame.iloc[0:0].copy()
    return frame[pd.to_numeric(frame.season,errors="coerce").eq(season)
                 &pd.to_numeric(frame.week,errors="coerce").eq(week)].copy()


def _regular_games(frame:pd.DataFrame,season:int,week:int)->pd.DataFrame:
    required={"season","week","game_type","game_id","away_team","home_team","away_score","home_score"}
    missing=required-set(frame.columns)
    if missing:
        raise WeekPreparationError(f"Schedule missing fields: {', '.join(sorted(missing))}")
    games=frame[pd.to_numeric(frame.season,errors="coerce").eq(season)
                &pd.to_numeric(frame.week,errors="coerce").eq(week)
                &frame.game_type.astype(str).str.upper().eq("REG")].copy()
    if games.empty:
        raise WeekPreparationError(f"Schedule has no REG fixtures for {season} Week {week}")
    if games.game_id.isna().any() or games.game_id.astype(str).duplicated().any():
        raise WeekPreparationError(f"Schedule has missing or duplicate game IDs for {season} Week {week}")
    return games


def _completion_mask(games:pd.DataFrame)->pd.Series:
    away=pd.to_numeric(games.away_score,errors="coerce")
    home=pd.to_numeric(games.home_score,errors="coerce")
    scored=away.notna()&home.notna()&away.ge(0)&home.ge(0)
    status_col=next((c for c in ("game_status","result_status","status","game_state") if c in games),None)
    status_final=pd.Series(False,index=games.index)
    if status_col:
        status_final=games[status_col].fillna("").astype(str).str.strip().str.upper().isin(FINAL_STATES)
    derived=pd.Series(True,index=games.index); evidence=False
    if "result" in games:
        result=pd.to_numeric(games.result,errors="coerce")
        derived &= result.notna()&((result-(home-away)).abs()<1e-6); evidence=True
    if "total" in games:
        total=pd.to_numeric(games.total,errors="coerce")
        derived &= total.notna()&((total-(home+away)).abs()<1e-6); evidence=True
    # nflverse's schedule has no explicit game status: final scores plus its
    # derived result/total values are the completion evidence.
    explicit=status_col and games[status_col].notna().any()
    state=status_final if explicit else (derived if evidence else status_final)
    return scored&state&(derived if evidence else True)


def _require_complete(games:pd.DataFrame,season:int,week:int,source:str)->pd.DataFrame:
    done=_completion_mask(games)
    if not done.all():
        raise WeekPreparationError(f"Previous week incomplete: {int(done.sum())}/{len(games)} games final for {season} W{week:02d} ({source})")
    return games


def _expected_games(settings,season:int,week:int)->pd.DataFrame:
    path=settings.root/"data"/"phase2_external"/"games.csv"
    if not path.exists():
        raise WeekPreparationError("Staged schedule missing; run scripts/refresh_live_data.py first")
    return _require_complete(_regular_games(pd.read_csv(path,low_memory=False),season,week),season,week,"staged canonical schedule")


def refresh_completed_week(settings, season:int, week:int):
    """Validate and idempotently ingest independent postgame actuals."""
    result=ensure_completed_week_actuals(settings,season,week)
    _merge_completed_scores(settings,season,week,result["archive"])
    return result["archive"]

def _generate_next_week_player_features(settings,season:int,completed_week:int,next_week:int)->int:
    """Roll prior actual player/PBP state into this week's current-roster universe."""
    from nfl2026.features.player_rollforward import (aggregate_player_red_zone,
        filter_unusable_player_observations, roll_player_usage_features)
    from nfl2026.features.team_advanced import _pbp_path

    path=settings.artifact_root/"phase2"/"player_pregame_universe_features.csv"
    all_features=pd.read_csv(path,low_memory=False)
    all_features=all_features[~((all_features.season.eq(season))&all_features.week.eq(next_week))]
    roster_source=pd.read_csv(settings.root/"data"/"phase2_external"/f"weekly_roster_{season}.csv",low_memory=False)
    roster=roster_source[
    (roster_source.week.eq(next_week))
        & roster_source.game_type.eq("REG")
        & roster_source.position.isin(["QB","RB","FB","WR","TE"])
    ].copy()
    schedule=pd.read_csv(settings.root/"data"/"phase2_external"/"games.csv",low_memory=False)
    schedule=schedule[(schedule.season.eq(season))&schedule.week.eq(next_week)&schedule.game_type.eq("REG")]
    if roster.empty or schedule.empty: raise ValueError("Refresh the next-week roster and schedule first")
    actual=pd.read_csv(settings.artifact_root/"canonical"/"player_weekly.csv",low_memory=False)
    actual=actual[(pd.to_numeric(actual.season,errors="coerce").eq(season))
                  &pd.to_numeric(actual.week,errors="coerce").lt(next_week)].copy()
    fixtures={}
    game_rows=[]
    for game in schedule.itertuples(index=False):
        fixtures[game.home_team]=(game.game_id,game.away_team,game.gameday,game.gametime)
        fixtures[game.away_team]=(game.game_id,game.home_team,game.gameday,game.gametime)
    # Existing candidate roster is authoritative only for target-week identity;
    # it must not contribute any target-week outcome to its own features.
    rows=[]
    for player in roster.itertuples(index=False):
        pid=str(player.gsis_id)
        if player.team not in fixtures or pid=="nan": continue
        game_id,opponent,day,time=fixtures[player.team]
        kickoff=pd.Timestamp(f"{day} {time if pd.notna(time) else '13:00'}",tz="America/New_York").tz_convert("UTC").isoformat()
        row={column:pd.NA for column in all_features.columns}
        row.update({"game_id":game_id,"season":season,"week":next_week,"kickoff_utc":kickoff,"prediction_cutoff_utc":datetime.now(timezone.utc).isoformat(),
          "team":player.team,"opponent":opponent,"player_id":pid,"player_name":player.full_name,"position":player.position,"status":player.status,
          "status_eligible":str(player.status).upper() in {"ACT","ACTIVE"},"rookie_year":player.rookie_year,"years_exp":player.years_exp,
          "effective_utc":pd.NA,"universe_rule":"CURRENT_WEEKLY_ROSTER","universe_reliability":"WEEKLY_ASSUMED_PREGAME",
          "feature_version":str(getattr(settings,"phase2_feature_version","advanced-pit-v2")),
          "evaluation_population":"true_pregame_roster_universe"})
        for position in ("QB","RB","FB","WR","TE"): row[f"position_{position}"]=float(player.position==position)
        rows.append(row)
        game_rows.append({"game_id":game_id,"season":season,"week":next_week,"team":player.team,
            "opponent":opponent,"player_id":pid,"player_name":player.full_name,"position":player.position,
            "kickoff_utc":kickoff,"status":player.status,"status_eligible":str(player.status).upper() in {"ACT","ACTIVE"}})
    output=pd.DataFrame(rows)

    # Historical model observations supply the trained 2022-25 rolling state.
    # Current season actuals are appended only for weeks strictly before target.
    conditional_path=settings.artifact_root/"phase2"/"player_advanced_conditional_matrix.csv"
    if not conditional_path.exists():
        raise WeekPreparationError(f"Historical player feature observations missing: {conditional_path}")
    conditional=pd.read_csv(conditional_path,low_memory=False)
    conditional_history=conditional[pd.to_numeric(conditional.season,errors="coerce").lt(season)].copy()
    default_columns=["prior_carries","prior_targets","prior_rushing_yards","prior_receiving_yards","prior_anytime_offensive_td"]
    defaults=conditional_history.groupby("position")[default_columns].median(numeric_only=True)
    history_fields=["game_id","player_id","team","position","kickoff_utc","carries","targets","rushing_yards",
        "receiving_yards","rushing_tds","receptions","air_yards","receiving_tds","anytime_offensive_tds",
        "anytime_offensive_td","red_zone_carries","inside_10_carries","inside_5_carries",
        "red_zone_targets","inside_10_targets","inside_5_targets","offense_pct"]

    # Rebuild older history from the same canonical weekly actual and PBP
    # sources used to form Phase 2. The conditional matrix is retained only as
    # the persisted historical snap-share join and no-history prior source.
    weekly_path=settings.artifact_root/"canonical"/"player_weekly.csv"
    pbp_weekly_path=settings.artifact_root/"canonical"/"player_pbp_weekly.csv"
    games_path=settings.artifact_root/"canonical"/"canonical_games.csv"
    historical=conditional.iloc[0:0].copy()
    if weekly_path.exists() and pbp_weekly_path.exists() and games_path.exists():
        weekly_all=pd.read_csv(weekly_path,low_memory=False)
        weekly_all=weekly_all[pd.to_numeric(weekly_all.season,errors="coerce").lt(season)].copy()
        canonical_games=pd.read_csv(games_path,low_memory=False)
        canonical_games=canonical_games[canonical_games.game_type.astype(str).str.upper().eq("REG")]
        canonical_games=canonical_games[pd.to_numeric(canonical_games.season,errors="coerce").lt(season)]
        hist_sides=pd.concat([
            canonical_games[["game_id","season","week","kickoff_utc","home_team"]].rename(columns={"home_team":"team"}),
            canonical_games[["game_id","season","week","kickoff_utc","away_team"]].rename(columns={"away_team":"team"})
        ],ignore_index=True)
        historical=weekly_all.merge(hist_sides,on=["season","week","team"],how="inner",validate="many_to_one")
        historical["anytime_offensive_td"]=pd.to_numeric(historical.get("anytime_offensive_td"),errors="coerce")
        pbp_weekly=pd.read_csv(pbp_weekly_path,low_memory=False)
        red_cols=["red_zone_carries","inside_10_carries","inside_5_carries","red_zone_targets","inside_10_targets","inside_5_targets"]
        historical=historical.merge(pbp_weekly[["game_id","player_id","team",*red_cols]].drop_duplicates(["game_id","player_id","team"]),
            on=["game_id","player_id","team"],how="left",validate="one_to_one")
        snap_hist=conditional[["game_id","player_id","offense_pct"]].drop_duplicates(["game_id","player_id"])
        historical=historical.merge(snap_hist,on=["game_id","player_id"],how="left",validate="one_to_one")
        historical=historical.rename(columns={"rushing_yards":"rushing_yards","receiving_yards":"receiving_yards"})
        for col in ("carries","targets","rushing_yards","receiving_yards","anytime_offensive_td",*red_cols):
            if col not in historical: historical[col]=0
        historical=historical[history_fields].copy()
    else:
        # Small isolated unit fixtures without canonical historical archives
        # use the persisted conditional history; production has canonical files.
        historical=conditional_history.rename(columns={"target_carries":"carries","target_targets":"targets",
            "target_rushing_yards":"rushing_yards","target_receiving_yards":"receiving_yards",
            "target_anytime_offensive_td":"anytime_offensive_td"})
        historical=historical[[c for c in history_fields if c in historical]].copy()

    schedule_all=pd.read_csv(settings.root/"data"/"phase2_external"/"games.csv",low_memory=False)
    schedule_all=schedule_all[(pd.to_numeric(schedule_all.season,errors="coerce").eq(season))
        &pd.to_numeric(schedule_all.week,errors="coerce").lt(next_week)&schedule_all.game_type.astype(str).str.upper().eq("REG")].copy()
    schedule_all["kickoff_utc"]=pd.to_datetime(schedule_all.gameday.astype(str)+" "+schedule_all.gametime.fillna("13:00").astype(str),
        errors="coerce").dt.tz_localize("America/New_York",ambiguous="NaT",nonexistent="shift_forward").dt.tz_convert("UTC")
    if schedule_all.kickoff_utc.isna().any():
        raise WeekPreparationError("Prior-game schedule has invalid kickoff timestamps")
    pbp_path=_pbp_path(settings,season)
    if not pbp_path.exists():
        raise WeekPreparationError(f"Prior-season player PBP unavailable for red-zone feature refresh: {pbp_path}")
    try:
        raw_pbp=pd.read_parquet(pbp_path)
    except Exception as exc:
        raise WeekPreparationError(f"Could not read prior-game PBP for player features: {exc}") from exc
    raw_pbp=raw_pbp[(pd.to_numeric(raw_pbp.season,errors="coerce").eq(season))
                    &pd.to_numeric(raw_pbp.week,errors="coerce").lt(next_week)].copy()
    redzone=aggregate_player_red_zone(raw_pbp)
    current=actual.rename(columns={"display_name":"player_name","opponent_team":"opponent"}).copy()
    side_games=pd.concat([
        schedule_all[["game_id","season","week","home_team","kickoff_utc"]].rename(columns={"home_team":"team"}),
        schedule_all[["game_id","season","week","away_team","kickoff_utc"]].rename(columns={"away_team":"team"})
    ],ignore_index=True)
    current=current.merge(side_games,on=["season","week","team"],how="left",validate="many_to_one")
    current=current.merge(redzone,on=["game_id","player_id","team"],how="left",validate="one_to_one")
    for col in ("red_zone_carries","inside_10_carries","inside_5_carries","red_zone_targets","inside_10_targets","inside_5_targets"):
        current[col]=pd.to_numeric(current[col],errors="coerce").fillna(0)
    current["offense_pct"]=np.nan
    snap_path=settings.root/"data"/"phase2_external"/f"snap_counts_{season}.csv"
    snap_available=snap_path.exists()
    if snap_available:
        snap=pd.read_csv(snap_path,low_memory=False)
        if "player_id" not in snap and {"pfr_player_id"}.issubset(snap):
            if "pfr_id" not in roster:
                snap_available=False
            else:
                id_map=roster_source[["gsis_id","pfr_id"]].dropna().drop_duplicates("pfr_id").rename(columns={"gsis_id":"player_id"})
                snap=snap.merge(id_map,left_on="pfr_player_id",right_on="pfr_id",how="left")
        if snap_available and {"game_id","player_id","offense_pct"}.issubset(snap):
            snap_rows=snap[["game_id","player_id","offense_pct"]].dropna(subset=["game_id","player_id"])
            snap_rows=snap_rows.drop_duplicates(["game_id","player_id"])
            current=current.drop(columns="offense_pct").merge(snap_rows,
                on=["game_id","player_id"],how="left",validate="one_to_one")
        else:
            snap_available=False
    observations=pd.concat([historical,current[[c for c in history_fields if c in current]]],ignore_index=True,sort=False)
    try:
        observations,observation_diagnostic=filter_unusable_player_observations(observations)
    except ValueError as exc:
        raise WeekPreparationError(f"Unsafe player-history observations: {exc}") from exc
    # Strict pre-kickoff filtering and model-parity windows are centralized.
    output=roll_player_usage_features(observations,output,snap_source_available=snap_available)
    for column in ("prior_carries","prior_targets","prior_rushing_yards","prior_receiving_yards","prior_anytime_offensive_td"):
        output[column]=output[column].fillna(output.position.map(defaults[column])).fillna(0)
    output["rookie_or_no_history"]=pd.to_numeric(output.player_games_before,errors="coerce").fillna(0).eq(0).astype(float)
    for position in ("QB","RB","FB","WR","TE"):
        output[f"position_{position}"]=output.position.eq(position).astype(float)
    _atomic_csv(pd.concat([all_features,output[all_features.columns]],ignore_index=True),path)
    audit=settings.artifact_root/"phase2"/f"week_{next_week:02d}_lagged_feature_update.json"
    _atomic_json(audit,{"season":season,"completed_week":completed_week,"next_week":next_week,"rows":len(output),
      "method":"strict pre-kickoff last-eight player actual-game means, last-four snap means, last-six target-team game means",
      **observation_diagnostic,
      "snap_source_state":"CURRENT" if snap_available else "NOT_YET_AVAILABLE; feature left missing for trained pipeline imputer",
      "refreshed_fields":list(__import__("nfl2026.features.player_rollforward",fromlist=["PLAYER_USAGE_FIELDS"]).PLAYER_USAGE_FIELDS),
      "pbp_source":str(pbp_path),"pbp_sha256":sha256_file(pbp_path)})
    return len(output)



def _convert_stats(stats:pd.DataFrame,columns:list[str])->pd.DataFrame:
    required={"season","week","season_type","team","opponent_team","player_id","player_display_name","position",
              "carries","rushing_yards","rushing_tds","targets","receptions","receiving_yards","receiving_tds"}
    missing=required-set(stats.columns)
    if missing:
        raise WeekPreparationError(f"Player stats missing fields: {', '.join(sorted(missing))}")
    out=pd.DataFrame({"season":stats.season,"week":stats.week,"team":stats.team,"opponent_team":stats.opponent_team,
        "player_id":stats.player_id,"display_name":stats.player_display_name,"position":stats.position,
        "carries":stats.carries,"rushing_yards":stats.rushing_yards,"rushing_tds":stats.rushing_tds,
        "targets":stats.targets,"receptions":stats.receptions,"receiving_yards":stats.receiving_yards,
        "air_yards":stats.get("receiving_air_yards",0),"receiving_tds":stats.receiving_tds})
    out["anytime_offensive_tds"]=pd.to_numeric(out.rushing_tds,errors="coerce").fillna(0)+pd.to_numeric(out.receiving_tds,errors="coerce").fillna(0)
    out["anytime_offensive_td"]=out.anytime_offensive_tds.gt(0).astype(int)
    for col in columns:
        if col not in out: out[col]=pd.NA
    return out[columns]


def _validate_source(schedule,stats_all,season:int,week:int,expected:pd.DataFrame,columns:list[str]):
    games=_require_complete(_regular_games(schedule,season,week),season,week,"nflverse download")
    if set(games.game_id.astype(str))!=set(expected.game_id.astype(str)):
        raise WeekPreparationError(f"Downloaded Week {week} game IDs differ from staged schedule")
    left=expected.assign(game_id=expected.game_id.astype(str)).set_index("game_id")
    right=games.assign(game_id=games.game_id.astype(str)).set_index("game_id")
    for gid in left.index:
        for col in ("away_score","home_score"):
            a=float(pd.to_numeric(pd.Series([left.loc[gid,col]]),errors="coerce").iloc[0])
            b=float(pd.to_numeric(pd.Series([right.loc[gid,col]]),errors="coerce").iloc[0])
            if not math.isclose(a,b,abs_tol=1e-6):
                raise WeekPreparationError(f"nflverse score mismatch for {gid} ({col})")
    required_stats={"season","week","season_type","team","player_id"}
    if not required_stats.issubset(stats_all.columns):
        raise WeekPreparationError(f"Player stats missing fields: {sorted(required_stats-set(stats_all.columns))}")
    stats=stats_all[pd.to_numeric(stats_all.season,errors="coerce").eq(season)
                    &pd.to_numeric(stats_all.week,errors="coerce").eq(week)
                    &stats_all.season_type.astype(str).str.upper().eq("REG")].copy()
    if stats.empty or stats.team.isna().any():
        raise WeekPreparationError(f"Missing or invalid {season} Week {week} player stats")
    teams=set(expected.home_team.astype(str))|set(expected.away_team.astype(str))
    if set(stats.team.astype(str))!=teams:
        raise WeekPreparationError("Player-stat team coverage does not match completed schedule")
    count=stats.player_id.dropna().astype(str).nunique()
    per_team=stats.assign(_pid=stats.player_id.astype("string")).groupby("team")._pid.nunique()
    if count<max(8,len(games)*8) or (per_team.reindex(sorted(teams),fill_value=0)<4).any():
        raise WeekPreparationError(f"Player stats implausibly sparse: {count} identified players across {len(games)} games")
    return games,_convert_stats(stats,columns)


def _archive_root(settings,season:int,week:int):
    return settings.root/"data"/"postgame_downloads"/str(season)/f"week_{week:02d}"


def _canonical_week(settings,season:int,week:int):
    path=settings.artifact_root/"canonical"/"player_weekly.csv"
    if not path.exists():
        return path,pd.DataFrame(columns=list(ACTUAL_COLUMNS))
    frame=pd.read_csv(path,low_memory=False)
    return path,frame



def _archive_candidate(settings,archive,season:int,week:int,expected:pd.DataFrame):
    manifest_path=archive/"manifest.json"
    games_path=archive/"games.csv"
    stats_path=archive/f"stats_player_week_{season}.csv"
    if not (manifest_path.exists() and games_path.exists() and stats_path.exists()):
        return None
    try:
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
        if int(manifest.get("season",-1))!=season or int(manifest.get("week",-1))!=week:
            return None
        if manifest.get("schedule_sha256")!=sha256_file(games_path) or manifest.get("player_stats_sha256")!=sha256_file(stats_path):
            return None
        canonical_path=settings.artifact_root/"canonical"/"player_weekly.csv"
        columns=list(pd.read_csv(canonical_path,nrows=0).columns) if canonical_path.exists() else list(ACTUAL_COLUMNS)
        games,converted=_validate_source(pd.read_csv(games_path,low_memory=False),
            pd.read_csv(stats_path,low_memory=False),season,week,expected,columns)
        if int(manifest.get("games",len(games)))!=len(games) or int(manifest.get("player_rows",len(converted)))!=len(converted):
            return None
        return manifest_path,manifest,converted
    except (OSError,ValueError,KeyError,TypeError,WeekPreparationError,pd.errors.ParserError):
        return None


def _actual_state_path(archive):
    return archive/"ingestion_state.json"


def _state_is_current(settings,archive,manifest_path,manifest,converted,season:int,week:int)->bool:
    state_path=_actual_state_path(archive)
    if not state_path.exists():
        return False
    try:
        state=json.loads(state_path.read_text(encoding="utf-8"))
        canonical_path,canonical=_canonical_week(settings,season,week)
        block=_week_rows(canonical,season,week)
        return (state.get("status")=="CURRENT" and int(state.get("season",-1))==season
            and int(state.get("week",-1))==week
            and state.get("manifest_path")==str(manifest_path.relative_to(settings.root))
            and state.get("manifest_sha256")==sha256_file(manifest_path)
            and state.get("canonical_week_sha256")==_frame_digest(block)
            and state.get("canonical_week_sha256")==_frame_digest(converted)
            and int(state.get("row_count",-1))==len(block))
    except (OSError,ValueError,TypeError):
        return False


def _write_actual_state(settings,archive,manifest_path,converted,season:int,week:int)->dict:
    _,canonical=_canonical_week(settings,season,week)
    block=_week_rows(canonical,season,week)
    state={"schema_version":1,"status":"CURRENT","season":season,"week":week,
        "manifest_path":str(manifest_path.relative_to(settings.root)),
        "manifest_sha256":sha256_file(manifest_path),"canonical_week_sha256":_frame_digest(block),
        "row_count":len(block),"source_player_rows":len(converted),"generated_at_utc":_utc_iso()}
    _atomic_json(_actual_state_path(archive),state)
    return state


def _write_canonical_week(settings,season:int,week:int,converted:pd.DataFrame)->None:
    path=settings.artifact_root/"canonical"/"player_weekly.csv"
    if path.exists():
        canonical=pd.read_csv(path,low_memory=False)
        for column in canonical.columns:
            if column not in converted: converted[column]=pd.NA
        for column in converted.columns:
            if column not in canonical: canonical[column]=pd.NA
        converted=converted[canonical.columns]
    else:
        canonical=pd.DataFrame(columns=converted.columns)
    keep=~(pd.to_numeric(canonical.season,errors="coerce").eq(season)
           &pd.to_numeric(canonical.week,errors="coerce").eq(week))
    _atomic_csv(pd.concat([canonical[keep],converted],ignore_index=True),path)


def _archives(settings,season:int,week:int):
    root=_archive_root(settings,season,week)
    return sorted((p for p in root.iterdir() if p.is_dir()),key=lambda p:p.name,reverse=True) if root.exists() else []


def inspect_completed_week_actuals(settings,season:int,week:int,expected_games:pd.DataFrame|None=None)->dict:
    """Read-only verification of archived actual source, state, and canonical rows."""
    expected=expected_games if expected_games is not None else _expected_games(settings,season,week)
    for archive in _archives(settings,season,week):
        candidate=_archive_candidate(settings,archive,season,week,expected)
        if candidate is None: continue
        manifest_path,manifest,converted=candidate
        _,canonical=_canonical_week(settings,season,week)
        block=_week_rows(canonical,season,week)
        matches=len(block)==len(converted) and _frame_digest(block)==_frame_digest(converted)
        status=("ALREADY_CURRENT" if _state_is_current(settings,archive,manifest_path,manifest,converted,season,week)
                else "VALID_LEGACY_NEEDS_STATE") if matches else "VALID_ARCHIVE_NEEDS_INGESTION"
        return {"status":status,"archive":archive,"manifest_path":manifest_path,
                "manifest":manifest,"converted":converted,"row_count":len(converted)}
    return {"status":"MISSING_OR_INVALID","archive":None,"manifest_path":None,
            "manifest":None,"converted":None,"row_count":0}



def ensure_completed_week_actuals(settings,season:int,week:int,force:bool=False,
                                  expected_games:pd.DataFrame|None=None)->dict:
    """Reuse a validated local archive; download only if missing, invalid, or forced."""
    schedule_bytes=None
    if expected_games is not None:
        expected=expected_games
    else:
        try:
            expected=_expected_games(settings,season,week)
        except WeekPreparationError:
            expected=_archived_expected_games(settings,season,week)
            if expected is None:
                schedule_bytes=fetch_bytes(SCHEDULE_URL)
                source_schedule=pd.read_csv(BytesIO(schedule_bytes),low_memory=False)
                expected=_require_complete(_regular_games(source_schedule,season,week),season,week,"nflverse postgame schedule")
    existing=inspect_completed_week_actuals(settings,season,week,expected)
    if existing["status"]=="ALREADY_CURRENT" and not force:
        return existing
    if existing["status"] in {"VALID_LEGACY_NEEDS_STATE","VALID_ARCHIVE_NEEDS_INGESTION"} and not force:
        archive=existing["archive"]; manifest_path=existing["manifest_path"]; converted=existing["converted"]
        if existing["status"]=="VALID_ARCHIVE_NEEDS_INGESTION":
            _write_canonical_week(settings,season,week,converted)
            status="REPAIRED_FROM_LOCAL_ARCHIVE"
        else:
            status="ADOPTED_VALID_LEGACY_ARCHIVE"
        _write_actual_state(settings,archive,manifest_path,converted,season,week)
        return {**existing,"status":status}
    if schedule_bytes is None:
        schedule_bytes=fetch_bytes(SCHEDULE_URL)
    player_url=PLAYER_URL.format(season=season)
    player_bytes=fetch_bytes(player_url)
    archive=_archive_root(settings,season,week)/datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive.mkdir(parents=True,exist_ok=False)
    schedule_path=archive/"games.csv"; stats_path=archive/f"stats_player_week_{season}.csv"
    schedule_path.write_bytes(schedule_bytes); stats_path.write_bytes(player_bytes)
    schedule=pd.read_csv(schedule_path,low_memory=False); stats=pd.read_csv(stats_path,low_memory=False)
    canonical_path=settings.artifact_root/"canonical"/"player_weekly.csv"
    columns=list(pd.read_csv(canonical_path,nrows=0).columns) if canonical_path.exists() else list(ACTUAL_COLUMNS)
    games,converted=_validate_source(schedule,stats,season,week,expected,columns)
    manifest={"schema_version":1,"season":season,"week":week,"retrieved_at_utc":_utc_iso(),
        "games":len(games),"game_ids":sorted(games.game_id.astype(str).tolist()),
        "player_rows":len(converted),"identified_player_rows":int(converted.player_id.notna().sum()),
        "team_coverage":sorted(set(games.home_team.astype(str))|set(games.away_team.astype(str))),
        "schedule_url":SCHEDULE_URL,"player_stats_url":player_url,
        "schedule_sha256":sha256_file(schedule_path),"player_stats_sha256":sha256_file(stats_path),
        "purpose":"postgame actuals only; excluded from locked predictions"}
    manifest_path=archive/"manifest.json"
    _atomic_json(manifest_path,manifest)
    _write_canonical_week(settings,season,week,converted)
    _write_actual_state(settings,archive,manifest_path,converted,season,week)
    return {"status":"DOWNLOADED_AND_INGESTED","archive":archive,"manifest_path":manifest_path,
            "manifest":manifest,"converted":converted,"row_count":len(converted)}



def _target_inputs(settings,season:int,week:int,now:datetime|None=None):
    now=now or datetime.now(timezone.utc)
    root=settings.root/"data"/"phase2_external"
    schedule_path=root/"games.csv"
    roster_path=root/f"weekly_roster_{season}.csv"
    provider_path=root/f"live_provider_manifest_{season}_w{week:02d}.csv"
    for label,path in (("schedule",schedule_path),("current roster",roster_path),("provider manifest",provider_path)):
        if not path.exists():
            raise WeekPreparationError(f"Current {label} missing; run scripts/refresh_live_data.py --season {season} --week {week}")
    schedule=_regular_games(pd.read_csv(schedule_path,low_memory=False),season,week)
    if schedule[["away_score","home_score"]].notna().any().any():
        raise WeekPreparationError(f"Target Week {week} already has results; refusing a pregame roll-forward")
    roster_all=pd.read_csv(roster_path,low_memory=False)
    required={"week","game_type","team","position","status","gsis_id"}
    if not required.issubset(roster_all.columns):
        raise WeekPreparationError(f"Current roster missing fields: {sorted(required-set(roster_all.columns))}")
    roster=roster_all[pd.to_numeric(roster_all.week,errors="coerce").eq(week)
        &roster_all.game_type.astype(str).str.upper().eq("REG")
        &roster_all.position.astype(str).str.upper().isin(SKILL_POSITIONS)].copy()
    teams=set(schedule.home_team.astype(str))|set(schedule.away_team.astype(str))
    eligible=roster[roster.status.astype(str).str.upper().isin({"ACT","ACTIVE"})].copy()
    covered=set(eligible.team.dropna().astype(str))
    if not teams.issubset(covered):
        raise WeekPreparationError(f"Week {week} skill roster coverage {len(teams&covered)}/{len(teams)}; missing={sorted(teams-covered)}")
    if eligible.empty or eligible.gsis_id.isna().any() or eligible.gsis_id.astype(str).duplicated().any():
        raise WeekPreparationError(f"Week {week} skill roster has missing or duplicate GSIS player IDs")
    provider=pd.read_csv(provider_path,low_memory=False)
    if not {"source","data_status","source_known_at_utc"}.issubset(provider.columns):
        raise WeekPreparationError("Live provider manifest lacks status/provenance fields")
    provider=provider.drop_duplicates("source",keep="last").set_index("source")
    for source,max_age in (("schedule",168),("roster",72)):
        if source not in provider.index:
            raise WeekPreparationError(f"Live provider manifest has no {source} record")
        row=provider.loc[source]
        if str(row.data_status).upper()=="BLOCKED":
            raise WeekPreparationError(f"Current {source} source is BLOCKED; refresh Week {week} live inputs")
        known=pd.to_datetime(row.source_known_at_utc,utc=True,errors="coerce")
        age=(pd.Timestamp(now)-known).total_seconds()/3600 if pd.notna(known) else float("inf")
        if age < -1 or age > max_age:
            raise WeekPreparationError(f"Current {source} source age {age:.1f}h exceeds {max_age}h; refresh Week {week} live inputs")
    return schedule,roster


def _roll_manifest_path(settings,season:int,week:int):
    return settings.artifact_root/"phase2"/"rollforward_manifests"/str(season)/f"week_{week:02d}.json"


def _official_week_frozen(settings,season:int,week:int)->bool:
    root=settings.prediction_root/str(season)/f"week_{week:02d}"
    for path in root.glob("*/metadata.json") if root.exists() else []:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("run_status")=="OFFICIAL_PRODUCTION":
                return True
        except (OSError,ValueError):
            continue
    freezes=settings.artifact_root/"official_freezes"/str(season)/f"week_{week:02d}"
    return freezes.exists() and any(path.is_dir() for path in freezes.iterdir())


def _features(settings,season:int,week:int):
    path=settings.artifact_root/"phase2"/"player_pregame_universe_features.csv"
    if not path.exists(): raise WeekPreparationError(f"Player feature file missing: {path}")
    frame=pd.read_csv(path,low_memory=False)
    return path,frame,_week_rows(frame,season,week)



def _rollforward_inputs(settings,season:int,completed_week:int,target_week:int,actual:dict,
                        schedule:pd.DataFrame,roster:pd.DataFrame)->dict:
    from nfl2026.features.team_advanced import _pbp_path
    _,canonical=_canonical_week(settings,season,completed_week)
    actual_block=_week_rows(canonical,season,completed_week)
    if actual_block.empty: raise WeekPreparationError(f"Canonical Week {completed_week} actuals are empty")
    _,features,target_block=_features(settings,season,target_week)
    prior=_week_rows(features,season,completed_week)
    base=features[~(pd.to_numeric(features.season,errors="coerce").eq(season)
                    &pd.to_numeric(features.week,errors="coerce").eq(target_week))]
    manifest_path=actual["manifest_path"]
    state_path=_actual_state_path(actual["archive"])
    historical_path=settings.artifact_root/"phase2"/"player_advanced_conditional_matrix.csv"
    weekly_path=settings.artifact_root/"canonical"/"player_weekly.csv"
    canonical_pbp_path=settings.artifact_root/"canonical"/"player_pbp_weekly.csv"
    pbp_path=_pbp_path(settings,season)
    snap_path=settings.root/"data"/"phase2_external"/f"snap_counts_{season}.csv"
    rollforward_code_path=Path(__file__).resolve().parent/"features"/"player_rollforward.py"
    coverage={"player_actuals":"CURRENT" if weekly_path.exists() else "MISSING",
        "pbp":"CURRENT" if pbp_path.exists() else "MISSING",
        "snap_counts":"CURRENT" if snap_path.exists() else "NOT_YET_AVAILABLE",
        "historical_player_matrix":"CURRENT" if historical_path.exists() else "MISSING",
        "canonical_player_pbp":"CURRENT" if canonical_pbp_path.exists() else "MISSING",
        "target_roster":"CURRENT" if not roster.empty else "MISSING"}
    return {"season":season,"source_completed_week":completed_week,"target_week":target_week,
        "source_actual_manifest_path":str(manifest_path.relative_to(settings.root)),
        "source_actual_manifest_sha256":sha256_file(manifest_path),
        "source_actual_state_sha256":sha256_file(state_path),
        "source_actuals_week_sha256":_frame_digest(actual_block),
        "player_actuals_sha256":sha256_file(weekly_path) if weekly_path.exists() else "MISSING",
        "historical_player_matrix_sha256":sha256_file(historical_path) if historical_path.exists() else "MISSING",
        "canonical_player_pbp_sha256":sha256_file(canonical_pbp_path) if canonical_pbp_path.exists() else "MISSING",
        "pbp_sha256":sha256_file(pbp_path) if pbp_path.exists() else "MISSING",
        "snap_participation_sha256":sha256_file(snap_path) if snap_path.exists() else "NOT_YET_AVAILABLE",
        "source_coverage":coverage,
        "source_prior_features_sha256":_frame_digest(prior),
        "source_feature_table_without_target_sha256":_frame_digest(base),
        "target_roster_sha256":_frame_digest(roster),
        "target_schedule_sha256":_frame_digest(schedule),
        "player_rollforward_code_sha256":hashlib.sha256(
            (Path(__file__).read_bytes()+rollforward_code_path.read_bytes())
        ).hexdigest(),
        "feature_version":str(getattr(settings,"phase2_feature_version","unknown")),
        "feature_schema_sha256":hashlib.sha256("|".join(sorted(features.columns.astype(str))).encode()).hexdigest(),
        "feature_rows_sha256":_frame_digest(target_block),"row_count":len(target_block),
        "eligible_row_count":int(target_block.get("status_eligible",pd.Series(dtype=bool)).astype(str).str.lower().isin(["true","1"]).sum())}


def _rollforward_is_current(path,expected:dict)->bool:
    if not path.exists(): return False
    try:
        saved=json.loads(path.read_text(encoding="utf-8"))
        return all(saved.get(key)==value for key,value in expected.items())
    except (OSError,ValueError,TypeError):
        return False


def _ensure_rollforward(settings,season:int,completed_week:int,target_week:int,actual:dict,
                        schedule:pd.DataFrame,roster:pd.DataFrame,check_only:bool=False)->dict:
    manifest_path=_roll_manifest_path(settings,season,target_week)
    expected=_rollforward_inputs(settings,season,completed_week,target_week,actual,schedule,roster)
    if _rollforward_is_current(manifest_path,expected):
        saved=json.loads(manifest_path.read_text(encoding="utf-8"))
        return {"status":"ALREADY_CURRENT","rows":expected["row_count"],"eligible_rows":expected["eligible_row_count"],
                "manifest_path":manifest_path,
                "player_observation_diagnostics":saved.get("player_observation_diagnostics",{})}
    if check_only:
        return {"status":"NEEDS_ROLL_FORWARD","rows":expected["row_count"],"eligible_rows":expected["eligible_row_count"],
                "manifest_path":manifest_path}
    if _official_week_frozen(settings,season,target_week):
        raise WeekPreparationError(f"Week {target_week} has an official prediction/freeze; refusing to change its feature rows")
    generated=_generate_next_week_player_features(settings,season,completed_week,target_week)
    _,features,block=_features(settings,season,target_week)
    teams=set(schedule.home_team.astype(str))|set(schedule.away_team.astype(str))
    if block.empty or not teams.issubset(set(block.team.dropna().astype(str))):
        raise WeekPreparationError(f"Week {target_week} player features do not cover all scheduled teams")
    expected.update({"feature_rows_sha256":_frame_digest(block),"row_count":len(block),
        "eligible_row_count":int(block.get("status_eligible",pd.Series(dtype=bool)).astype(str).str.lower().isin(["true","1"]).sum())})
    if generated!=len(block): raise WeekPreparationError(f"Roll-forward row count mismatch: generated {generated}, stored {len(block)}")
    observation_audit=settings.artifact_root/"phase2"/f"week_{target_week:02d}_lagged_feature_update.json"
    try:
        player_observation_diagnostics=json.loads(observation_audit.read_text(encoding="utf-8"))
    except (OSError,ValueError) as exc:
        raise WeekPreparationError(f"Player observation diagnostics missing after roll-forward: {exc}") from exc
    expected.update({"schema_version":2,"generated_at_utc":_utc_iso(),
        "player_observation_diagnostics":{key:value for key,value in player_observation_diagnostics.items()
            if key.startswith("player_observations_") or key.startswith("unidentified_") or key.startswith("unkeyed_")},
        "refreshed_fields":["prior_carries","prior_targets","prior_rushing_yards","prior_receiving_yards",
            "prior_anytime_offensive_td","recent_red_zone_carries","recent_inside_10_carries","recent_inside_5_carries",
            "recent_red_zone_targets","recent_inside_10_targets","recent_inside_5_targets","recent_snap_pct",
            "recent_team_carries","recent_team_targets","recent_yards_per_carry","recent_yards_per_target","player_games_before"],
        "fallback_fields":["recent_snap_pct"] if expected["source_coverage"]["snap_counts"]!="CURRENT" else [],
        "method":"strict pre-kickoff training-parity last-eight player means, last-four snap means, last-six current-team game means"})
    _atomic_json(manifest_path,expected)
    return {"status":"GENERATED","rows":len(block),"eligible_rows":expected["eligible_row_count"],
            "manifest_path":manifest_path,"player_observation_diagnostics":expected["player_observation_diagnostics"]}


def prepare_weekly_prediction(settings,season:int,target_week:int,check_only:bool=False,
                              force_actual_refresh:bool=False)->dict:
    """Idempotent previous-week actuals and current-week feature preflight."""
    if target_week==1:
        return {"ready":True,"previous_week":"NOT_REQUIRED","actuals":"NOT_REQUIRED",
                "player_roll_forward":"NOT_REQUIRED","feature_rows":0,"eligible_rows":0}
    if target_week<1: raise WeekPreparationError("NFL week must be a positive integer")
    completed_week=target_week-1
    expected=_expected_games(settings,season,completed_week)
    schedule,roster=_target_inputs(settings,season,target_week)
    inspected=inspect_completed_week_actuals(settings,season,completed_week,expected)
    if check_only:
        if inspected["status"]!="ALREADY_CURRENT":
            return {"ready":False,"previous_week":f"COMPLETE {season} W{completed_week:02d}",
                "actuals":inspected["status"],"player_roll_forward":"NOT_CHECKED",
                "feature_rows":0,"eligible_rows":0}
        actual=inspected
        roll=_ensure_rollforward(settings,season,completed_week,target_week,actual,schedule,roster,True)
        team={"status":"NOT_REQUIRED_BEFORE_WEEK_4"}
        if target_week>=4:
            from nfl2026.features.team_rollforward import ensure_team_state, TeamRollforwardError
            try: team=ensure_team_state(settings,season,target_week,check_only=True)
            except TeamRollforwardError as exc: raise WeekPreparationError(str(exc)) from exc
        return {"ready":roll["status"]=="ALREADY_CURRENT" and team["status"] in {"ALREADY_CURRENT","NOT_REQUIRED_BEFORE_WEEK_4"},"previous_week":f"COMPLETE {season} W{completed_week:02d}",
            "actuals":inspected["status"],"player_roll_forward":roll["status"],
            "team_roll_forward":team["status"],"feature_rows":roll["rows"],"eligible_rows":roll["eligible_rows"],"manifest_path":roll["manifest_path"]}
    actual=ensure_completed_week_actuals(settings,season,completed_week,force_actual_refresh,expected)
    if target_week>=4:
        from nfl2026.features.team_rollforward import ensure_team_state, TeamRollforwardError
        try: team=ensure_team_state(settings,season,target_week)
        except TeamRollforwardError as exc: raise WeekPreparationError(str(exc)) from exc
    else:
        team={"status":"NOT_REQUIRED_BEFORE_WEEK_4"}
    roll=_ensure_rollforward(settings,season,completed_week,target_week,actual,schedule,roster)
    return {"ready":True,"previous_week":f"COMPLETE {season} W{completed_week:02d}",
        "actuals":actual["status"],"player_roll_forward":roll["status"],"team_roll_forward":team["status"],
        "feature_rows":roll["rows"],"eligible_rows":roll["eligible_rows"],
        "actual_manifest":actual["manifest_path"],"rollforward_manifest":roll["manifest_path"],
        "team_rollforward_manifest":team.get("manifest_path")}



def prepare_next_week_player_features(settings,season:int,completed_week:int,next_week:int)->int:
    """Compatibility entry point routed through the safe weekly preparation gate."""
    if next_week!=completed_week+1:
        raise WeekPreparationError("Feature roll-forward must target the immediately following week")
    result=prepare_weekly_prediction(settings,season,next_week)
    return int(result["feature_rows"])



def _archived_expected_games(settings,season:int,week:int):
    for archive in _archives(settings,season,week):
        manifest_path=archive/"manifest.json"; schedule_path=archive/"games.csv"
        if not manifest_path.exists() or not schedule_path.exists(): continue
        try:
            manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
            if (int(manifest.get("season",-1))!=season or int(manifest.get("week",-1))!=week
                or manifest.get("schedule_sha256")!=sha256_file(schedule_path)):
                continue
            return _require_complete(_regular_games(pd.read_csv(schedule_path,low_memory=False),season,week),
                                     season,week,"validated local postgame schedule")
        except (OSError,ValueError,TypeError,WeekPreparationError,pd.errors.ParserError):
            continue
    return None


def _merge_completed_scores(settings,season:int,week:int,archive)->None:
    """Refresh only result fields for settlement while retaining staged pregame fields."""
    source=pd.read_csv(archive/"games.csv",low_memory=False)
    source=_require_complete(_regular_games(source,season,week),season,week,"postgame archive")
    path=settings.root/"data"/"phase2_external"/"games.csv"
    if not path.exists(): return
    current=pd.read_csv(path,low_memory=False)
    if "game_id" not in current or "game_id" not in source: return
    source=source.assign(_game_id=source.game_id.astype(str)).set_index("_game_id")
    ids=current.game_id.astype(str); changed=False
    for col in ("away_score","home_score","result","total","overtime"):
        if col not in source or col not in current: continue
        mask=ids.isin(source.index)
        if mask.any():
            values=ids[mask].map(source[col])
            existing=pd.to_numeric(current.loc[mask,col],errors="coerce").reset_index(drop=True)
            incoming=pd.to_numeric(values,errors="coerce").reset_index(drop=True)
            if not (existing.eq(incoming)|(existing.isna()&incoming.isna())).all():
                current.loc[mask,col]=values
                changed=True
    if changed: _atomic_csv(current,path)
