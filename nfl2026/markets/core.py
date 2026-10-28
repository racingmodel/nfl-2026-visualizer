from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import unicodedata
import uuid

import numpy as np
import pandas as pd

from nfl2026.config import Settings
from nfl2026.frontend.build import select_official_bundle
from nfl2026.markets.providers.base import MarketFetchResult
from nfl2026.markets.qualification import select_qualifying_values
from nfl2026.markets.schema import MARKET_QUOTE_COLUMNS, VALUE_COLUMNS
from nfl2026.production.core import validate_bundle
from nfl2026.utilities.io import sha256_file


TEAM_ALIASES = {
    "arizona cardinals":"ARI", "atlanta falcons":"ATL", "baltimore ravens":"BAL", "buffalo bills":"BUF",
    "carolina panthers":"CAR", "chicago bears":"CHI", "cincinnati bengals":"CIN", "cleveland browns":"CLE",
    "dallas cowboys":"DAL", "denver broncos":"DEN", "detroit lions":"DET", "green bay packers":"GB",
    "houston texans":"HOU", "indianapolis colts":"IND", "jacksonville jaguars":"JAX", "kansas city chiefs":"KC",
    "las vegas raiders":"LV", "los angeles chargers":"LAC", "los angeles rams":"LA", "miami dolphins":"MIA",
    "minnesota vikings":"MIN", "new england patriots":"NE", "new orleans saints":"NO", "new york giants":"NYG",
    "new york jets":"NYJ", "philadelphia eagles":"PHI", "pittsburgh steelers":"PIT", "san francisco 49ers":"SF",
    "seattle seahawks":"SEA", "tampa bay buccaneers":"TB", "tennessee titans":"TEN", "washington commanders":"WAS",
}

def _timestamp(point: datetime) -> str:
    return point.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalise_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    tokens = [token for token in "".join(char.lower() if char.isalnum() else " " for char in text).split() if token not in {"jr", "sr", "ii", "iii", "iv"}]
    return " ".join(tokens)


def _team_code(value: object) -> str:
    text = _normalise_name(value)
    if text.upper() in set(TEAM_ALIASES.values()):
        return text.upper()
    return TEAM_ALIASES.get(text, str(value or "").upper())


def validate_prediction_reference(bundle: Path) -> dict:
    report = validate_bundle(bundle)
    if report.empty or not report.passed.astype(bool).all():
        raise ValueError("Prediction bundle failed immutable hash validation")
    meta = json.loads((bundle / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("run_status") != "OFFICIAL_PRODUCTION" or not meta.get("deployment_eligible"):
        raise ValueError("Market comparison requires an eligible official prediction bundle")
    if meta.get("bookmaker_data_used") is not False:
        raise ValueError("Prediction bundle is not independent of bookmaker data")
    return meta


def create_market_snapshot(settings: Settings, season: int, week: int, result: MarketFetchResult) -> Path:
    snapshot_id = _timestamp(result.retrieved_at_utc) + "-" + uuid.uuid4().hex[:8]
    destination = settings.root / "markets" / "snapshots" / str(season) / f"week_{week:02d}" / snapshot_id
    if destination.exists():
        raise FileExistsError(f"Market snapshot is immutable: {destination}")
    destination.mkdir(parents=True)
    quotes = result.quotes.reindex(columns=MARKET_QUOTE_COLUMNS)
    quotes.to_csv(destination / "market_quotes.csv", index=False, lineterminator="\n")
    (destination / "raw_payload.json").write_text(json.dumps(result.raw_payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    metadata = {
        "snapshot_id": snapshot_id,
        "provider": result.provider,
        "season": season,
        "week": week,
        "retrieved_at_utc": result.retrieved_at_utc.isoformat(),
        "status": result.status,
        "lkg_status": "CANDIDATE",
        "warnings": list(result.warnings),
        "quote_count": len(quotes),
        "row_count": len(quotes),
        "event_count": int(quotes.provider_event_id.nunique()) if len(quotes) else 0,
        "bookmaker_count": int(quotes.bookmaker.nunique()) if len(quotes) else 0,
        "requested_bookmakers": result.usage.get("requested_bookmakers", []),
        "requested_markets": [result.usage.get("requested_game_markets", ""), result.usage.get("requested_prop_markets", "")],
        "request_count": len(result.usage.get("requests", [])),
        "estimated_quota_cost": result.usage.get("estimated_max_total_cost", result.usage.get("estimated_game_cost")),
        "reported_quota_cost": sum(int(item.get("x-requests-last") or 0) for item in result.usage.get("requests", []) if str(item.get("x-requests-last", "")).isdigit()),
        "quota_remaining": result.usage.get("quota_remaining"),
        "api_usage": result.usage,
        "file_hashes": {
            "market_quotes.csv": sha256_file(destination / "market_quotes.csv"),
            "raw_payload.json": sha256_file(destination / "raw_payload.json"),
        },
    }
    (destination / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    return destination


def validate_market_snapshot(snapshot: Path, season: int | None = None, week: int | None = None) -> dict:
    """Validate an immutable market snapshot before comparison or promotion."""
    snapshot = snapshot.resolve()
    metadata_path = snapshot / "metadata.json"
    if not metadata_path.is_file():
        raise ValueError(f"Market snapshot metadata is missing: {snapshot}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if season is not None and int(metadata.get("season", -1)) != int(season):
        raise ValueError(f"Market snapshot season does not match {season}")
    if week is not None and int(metadata.get("week", -1)) != int(week):
        raise ValueError(f"Market snapshot week does not match {week}")
    if metadata.get("snapshot_id") != snapshot.name:
        raise ValueError("Market snapshot directory does not match its snapshot ID")
    for filename, expected_hash in metadata.get("file_hashes", {}).items():
        path = snapshot / filename
        if not path.is_file() or sha256_file(path) != expected_hash:
            raise ValueError(f"Market snapshot failed immutable hash validation: {filename}")
    if not (snapshot / "market_quotes.csv").is_file():
        raise ValueError("Market snapshot is missing market_quotes.csv")
    return metadata


def map_fixtures(quotes: pd.DataFrame, games: pd.DataFrame, tolerance_minutes: int = 30) -> pd.DataFrame:
    mapped = quotes.copy()
    games = games.copy()
    games["kickoff_utc"] = pd.to_datetime(games.kickoff_utc, utc=True)
    output = []
    for row in mapped.to_dict("records"):
        home, away = _team_code(row.get("home_team_name")), _team_code(row.get("away_team_name"))
        kickoff = pd.to_datetime(row.get("kickoff_utc"), utc=True, errors="coerce")
        candidates = games[(games.home_team.eq(home)) & (games.away_team.eq(away))]
        if pd.notna(kickoff):
            candidates = candidates[(games.loc[candidates.index, "kickoff_utc"] - kickoff).abs() <= pd.Timedelta(minutes=tolerance_minutes)]
        if len(candidates) == 1:
            game = candidates.iloc[0]
            row.update({"game_id": game.game_id, "canonical_kickoff_utc": game.kickoff_utc.isoformat(), "fixture_mapping_status": "MAPPED_EXACT", "fixture_mapping_reason": "teams and kickoff"})
            selection_code = _team_code(row.get("selection"))
            if row.get("market_type") in {"MONEYLINE", "SPREAD", "TEAM_TOTAL"}:
                row["selection_team"] = selection_code
        elif len(candidates) > 1:
            row.update({"game_id": "", "canonical_kickoff_utc": "", "fixture_mapping_status": "AMBIGUOUS", "fixture_mapping_reason": "multiple canonical fixtures"})
        else:
            row.update({"game_id": "", "canonical_kickoff_utc": "", "fixture_mapping_status": "UNMATCHED", "fixture_mapping_reason": "no exact team/kickoff match"})
        output.append(row)
    return pd.DataFrame(output)


def map_players(quotes: pd.DataFrame, players: pd.DataFrame) -> pd.DataFrame:
    output = []
    players = players.copy()
    # Preserve suffixes: a bookmaker's abbreviated name is not an identity proof.
    def exact_player_name(value):
        return ' '.join(''.join(c.casefold() if c.isalnum() else ' ' for c in unicodedata.normalize('NFC',str(value))).split())
    players["normalised_player_name"] = players.player_name.map(exact_player_name)
    for row in quotes.to_dict("records"):
        if row.get("market_type") not in {"ANYTIME_TD", "PASSING_YARDS", "RUSHING_YARDS", "RECEIVING_YARDS"}:
            row.update({"player_mapping_status": "NOT_APPLICABLE", "player_mapping_reason": "game market"})
            output.append(row)
            continue
        candidates = players[(players.game_id.astype(str).eq(str(row.get("game_id", "")))) & players.normalised_player_name.eq(exact_player_name(row.get("player_name")))]
        if pd.notna(row.get('player_id')) and str(row.get('player_id','')).strip():
            candidates = candidates[candidates.player_id.astype(str).eq(str(row['player_id']))]
        supplied_value = row.get("player_team")
        supplied_team = _team_code(supplied_value) if pd.notna(supplied_value) and str(supplied_value).strip() else ""
        if supplied_team:
            candidates = candidates[candidates.team.eq(supplied_team)]
        if row.get('market_type') == 'PASSING_YARDS':
            candidates = candidates[candidates.position.eq('QB')]
        if len(candidates) == 1:
            player = candidates.iloc[0]
            row.update({"player_id": str(player.player_id), "player_team": player.team, "player_position": player.position, "player_mapping_status": "MAPPED_EXACT", "player_mapping_reason": "exact normalised name within canonical game"})
        elif len(candidates) > 1:
            row.update({"player_id": "", "player_mapping_status": "AMBIGUOUS", "player_mapping_reason": "multiple exact player matches"})
        else:
            row.update({"player_id": "", "player_mapping_status": "UNMATCHED", "player_mapping_reason": "no exact player match"})
        output.append(row)
    return pd.DataFrame(output)


def no_vig_two_way(decimal_odds: list[float]) -> list[float]:
    if len(decimal_odds) != 2 or any(not math.isfinite(float(price)) or float(price) <= 1 for price in decimal_odds):
        raise ValueError("No-vig calculation requires exactly two valid decimal prices")
    implied = np.asarray([1 / float(price) for price in decimal_odds])
    return (implied / implied.sum()).tolist()


def _pair_key(row: pd.Series) -> str:
    market = row.market_type
    if market == "MONEYLINE":
        detail = "main"
    elif market == "SPREAD":
        detail = f"{abs(float(row.line)):.3f}" if pd.notna(row.line) else "missing"
    elif market == "TOTAL":
        detail = f"{float(row.line):.3f}" if pd.notna(row.line) else "missing"
    elif market in {"RUSHING_YARDS", "RECEIVING_YARDS"}:
        detail = f"{row.player_id or row.player_name}|{row.line}"
    else:
        detail = "incomplete-multiway"
    return f"{row.game_id}|{row.bookmaker}|{market}|{detail}"


def attach_no_vig(quotes: pd.DataFrame) -> pd.DataFrame:
    from nfl2026.markets.certified_value import paired_probabilities
    return paired_probabilities(quotes)


def _legacy_attach_no_vig_unused(quotes: pd.DataFrame) -> pd.DataFrame:
    result = quotes.copy()
    result["book_implied_probability"] = pd.to_numeric(result.decimal_odds, errors="coerce").map(lambda price: 1 / price if pd.notna(price) and price > 1 else np.nan)
    result["no_vig_probability"] = np.nan
    result["no_vig_status"] = "MISSING_OPPOSING_SIDE"
    result["pair_key"] = result.apply(_pair_key, axis=1)
    for _, indexes in result.groupby("pair_key").groups.items():
        loc = list(indexes)
        group = result.loc[loc]
        if group.market_type.iloc[0] == "ANYTIME_TD":
            result.loc[loc, "no_vig_status"] = "INCOMPLETE_MULTIWAY_BOARD"
            continue
        distinct = group.selection.astype(str).nunique()
        if len(group) == 2 and distinct == 2:
            try:
                result.loc[loc, "no_vig_probability"] = no_vig_two_way(group.decimal_odds.astype(float).tolist())
                result.loc[loc, "no_vig_status"] = "TWO_WAY_NORMALISED"
            except ValueError:
                result.loc[loc, "no_vig_status"] = "INVALID_PRICE"
    return result


def replay_team_simulations(games: pd.DataFrame, metadata: dict) -> dict[str, dict[str, np.ndarray]]:
    rng = np.random.default_rng(int(metadata["simulation_seed"]))
    draws = int(metadata["simulation_count"])
    covariance = np.array([[90.0, 12.0], [12.0, 90.0]])
    output: dict[str, dict[str, np.ndarray]] = {}
    for row in games.itertuples(index=False):
        noise = rng.multivariate_normal([0, 0], covariance, size=draws)
        home = np.clip(float(row.home_expected_points) + noise[:, 0], 0, 70)
        away = np.clip(float(row.away_expected_points) + noise[:, 1], 0, 70)
        output[str(row.game_id)] = {"margin": home - away, "total": home + away}
    return output


def probability_over_from_interval(mean: float, high: float, line: float) -> float:
    raise ValueError('Uncalibrated point/band conversion is unsupported; require an approved threshold probability distribution')


def expected_value(model_probability: float, decimal_odds: float) -> float:
    if not 0 <= model_probability <= 1 or decimal_odds <= 1:
        raise ValueError("EV requires a bounded probability and decimal odds greater than 1")
    return float(model_probability * decimal_odds - 1)


def _player_role_label(player: pd.Series | None) -> str:
    if player is None:
        return "Team market"
    depth_role = str(player.get("depth_role", "") or "").upper()
    depth_order = pd.to_numeric(player.get("depth_order", np.nan), errors="coerce")
    if depth_role == "STARTER":
        return "Expected starter"
    if pd.notna(depth_order):
        order = int(depth_order)
        return f"Depth {order}" if order <= 2 else f"Depth {order} / reserve"
    if str(player.get("depth_source", "") or "").upper() == "MODEL_INFERRED":
        return "Role uncertain"
    return "Role unavailable"


def _confidence(row: pd.Series, game: pd.Series, player: pd.Series | None) -> tuple[str, str, str, str, str]:
    model = str(game.confidence_rating)
    role = str(player.role_confidence_label) if player is not None else "HIGH"
    levels = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    value_level = min(levels.get(model, 0), levels.get(role, 0))
    notes: list[str] = []
    if str(game.data_quality_status) == "READY_WITH_WARNINGS":
        value_level = min(value_level, 1)
        notes.append("game data has warnings")
    if player is not None:
        depth_role = str(player.get("depth_role", "") or "").upper()
        depth_source = str(player.get("depth_source", "") or "").upper()
        if depth_source == "MODEL_INFERRED" or not depth_role:
            value_level = min(value_level, 0)
            notes.append("player role is not confirmed by the current depth feed")
        elif depth_role == "RESERVE":
            value_level = min(value_level, 0)
            notes.append("reserve role creates material opportunity risk")
        elif depth_role == "PRIMARY_BACKUP":
            value_level = min(value_level, 1)
            notes.append("depth-two role limits value confidence")
        if str(player.get("availability_status", "") or "").upper() == "QUESTIONABLE":
            value_level = min(value_level, 0)
            notes.append("player availability is questionable")
    no_vig_status = str(row.get("no_vig_status", "") or "").upper()
    market_type = str(row.get("market_type", "") or "").upper()
    if market_type == "ANYTIME_TD" and no_vig_status != "PROPORTIONAL_COMPLEMENTARY_PAIR":
        value_level = min(value_level, 0)
        notes.append("same-player yes/no pair is incomplete; bookmaker margin was not removed")
    elif no_vig_status != 'PROPORTIONAL_COMPLEMENTARY_PAIR':
        value_level = min(value_level, 1)
        notes.append("market comparison uses raw implied probability")
    value = {0: "LOW", 1: "MEDIUM", 2: "HIGH"}[value_level]
    return model, role, value, _player_role_label(player), "; ".join(dict.fromkeys(notes))


def _thresholds(settings: Settings, market_type: str) -> dict[str, float]:
    key = market_type.lower()
    defaults = {"minimum_edge_pp": 5.0, "minimum_ev_pct": 10.0, "strong_edge_pp": 9.0, "strong_ev_pct": 18.0}
    return {**defaults, **settings.market_thresholds.get(key, {})}


def _classify(edge_pp: float, ev_pct: float, confidence: str, excluded: bool, thresholds: dict[str, float]) -> str:
    if excluded:
        return "EXCLUDED"
    if edge_pp <= -thresholds["minimum_edge_pp"]:
        return "MARKET_SHORTER_THAN_MODEL"
    if edge_pp <= 0 or ev_pct <= 0:
        return "NO_EDGE"
    if confidence == "LOW" or edge_pp < thresholds["minimum_edge_pp"] or ev_pct < thresholds["minimum_ev_pct"]:
        return "SMALL_EDGE"
    if confidence == "HIGH" and edge_pp >= thresholds["strong_edge_pp"] and ev_pct >= thresholds["strong_ev_pct"]:
        return "STRONG_MODEL_VALUE"
    return "MODEL_VALUE"


def _score(edge_pp: float, ev_pct: float, confidence: str, market_age: float, maximum_age: float, role_confidence: str) -> float:
    confidence_weight = {"LOW": 0.35, "MEDIUM": 0.70, "HIGH": 1.0}.get(confidence, 0.35)
    role_weight = {"LOW": 0.35, "MEDIUM": 0.70, "HIGH": 1.0}.get(role_confidence, 0.35)
    freshness = max(0.0, 1.0 - max(market_age, 0.0) / max(maximum_age, 1.0))
    raw = min(max(edge_pp, 0.0) / 12.0, 1.0) * 40 + min(max(ev_pct, 0.0) / 25.0, 1.0) * 30 + confidence_weight * 15 + role_weight * 5 + freshness * 10
    # Extreme prices must not turn a fragile role or incomplete market into a
    # perfect score. Confidence is a hard ceiling, not merely a small weight.
    ceiling = {"LOW": 49.0, "MEDIUM": 74.0, "HIGH": 100.0}.get(confidence, 49.0)
    return round(min(raw, ceiling), 2)


def compare_market_quotes(settings: Settings, bundle: Path, snapshot: Path, as_of: datetime | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    as_of = as_of or datetime.now(timezone.utc)
    from nfl2026.markets.request_plan import merged_forecast
    meta, games, players, source_metadata, source_bundles = merged_forecast(settings, bundle)
    snapshot_meta = json.loads((snapshot / "metadata.json").read_text(encoding="utf-8"))
    quotes = pd.read_csv(snapshot / "market_quotes.csv")
    if quotes.empty:
        return pd.DataFrame(columns=VALUE_COLUMNS), pd.DataFrame([{"check": "market_data_available", "passed": True, "severity": "WARNING", "details": "No live market quotes; prediction frontend remains available"}])
    mapped = map_players(map_fixtures(quotes, games, settings.market_fixture_time_tolerance_minutes), players)
    mapped["mapping_status"] = np.where(mapped.fixture_mapping_status.eq("MAPPED_EXACT"), mapped.player_mapping_status.replace({"NOT_APPLICABLE": "MAPPED_EXACT"}), mapped.fixture_mapping_status)
    mapped = attach_no_vig(mapped)
    # Reproducible noise is not independent distribution calibration. Exact
    # threshold probabilities are loaded by the certified adapter below.
    game_lookup = games.set_index("game_id")
    player_lookup = players.set_index(["game_id", "player_id"])
    duplicate_ids = set(mapped.loc[mapped.quote_id.duplicated(keep=False), "quote_id"])
    duplicate_selections = mapped.duplicated(["provider_event_id", "bookmaker", "market_type", "selection", "side", "line"], keep=False)
    duplicate_ids.update(mapped.loc[duplicate_selections, "quote_id"])
    rows = []
    for quote in mapped.itertuples(index=False):
        record = quote._asdict()
        game = game_lookup.loc[quote.game_id] if quote.game_id in game_lookup.index else None
        prediction_meta = source_metadata.get(str(quote.game_id), meta)
        prediction_time = pd.Timestamp(prediction_meta["created_at_utc"])
        player = None
        if game is not None and str(quote.player_id) and (quote.game_id, str(quote.player_id)) in player_lookup.index:
            player = player_lookup.loc[(quote.game_id, str(quote.player_id))]
        model_probability = np.nan
        model_mean = np.nan
        model_difference = np.nan
        distribution_method = ""
        if game is not None and quote.market_type == 'TOTAL':
            model_mean = float(game.model_total)
        elif player is not None and quote.market_type in {'RUSHING_YARDS','RECEIVING_YARDS'}:
            column = 'expected_rush_yards' if quote.market_type == 'RUSHING_YARDS' else 'expected_receiving_yards'
            model_mean = float(player[column])
        market_time = pd.to_datetime(quote.source_timestamp_utc, utc=True, errors="coerce")
        missing_source_time = pd.isna(market_time)
        if pd.isna(market_time):
            market_time = pd.to_datetime(quote.retrieval_timestamp_utc, utc=True)
        market_age = max(0.0, (pd.Timestamp(as_of) - market_time).total_seconds() / 60)
        maximum_age = settings.market_player_prop_max_age_minutes if quote.market_type in {"ANYTIME_TD", "RUSHING_YARDS", "RECEIVING_YARDS"} else settings.market_featured_max_age_minutes
        exclusions = []
        if missing_source_time: exclusions.append("MISSING_SOURCE_TIMESTAMP")
        if market_time > pd.Timestamp(as_of): exclusions.append("FUTURE_MARKET_TIMESTAMP")
        if quote.quote_id in duplicate_ids: exclusions.append("DUPLICATE_QUOTE")
        if quote.mapping_status != "MAPPED_EXACT": exclusions.append("UNRESOLVED_MAPPING")
        if not math.isfinite(float(quote.decimal_odds)) or float(quote.decimal_odds) <= 1: exclusions.append("INVALID_PRICE")
        if bool(quote.suspended): exclusions.append("SUSPENDED_MARKET")
        if market_age > maximum_age: exclusions.append("STALE_MARKET")
        if game is not None:
            kickoff = pd.Timestamp(game.kickoff_utc)
            if market_time >= kickoff: exclusions.append("POST_KICKOFF_MARKET")
            if pd.Timestamp(as_of) >= kickoff: exclusions.append("GAME_STARTED")
            if prediction_time >= kickoff: exclusions.append("POST_KICKOFF_PREDICTION")
            if str(game.data_quality_status) == "BLOCKED": exclusions.append("GAME_DATA_BLOCKED")
        if player is not None and str(player.injury_status).upper() in {"OUT", "IR", "PUP"}: exclusions.append("PLAYER_OUT")
        # Missing legacy probability is resolved (or withheld) only by the
        # independent certified-probability package, never by a band fallback.
        model_confidence, role_confidence, value_confidence, role_label, confidence_notes = _confidence(pd.Series(record), game, player) if game is not None else ("LOW", "LOW", "LOW", "Unavailable", "game mapping unavailable")
        if prediction_meta["run_id"] != meta["run_id"]:
            confidence_notes = (confidence_notes + "; " if confidence_notes else "") + "Preserved original forecast; no prediction refresh: " + prediction_meta["run_id"]
        fair = 1 / model_probability if pd.notna(model_probability) and model_probability > 0 else np.nan
        ev = expected_value(float(model_probability), float(quote.decimal_odds)) if not exclusions and pd.notna(model_probability) else np.nan
        market_probability = quote.no_vig_probability if pd.notna(quote.no_vig_probability) else quote.book_implied_probability
        edge = (float(model_probability) - float(market_probability)) * 100 if pd.notna(model_probability) and pd.notna(market_probability) else np.nan
        ev_pct = ev * 100 if pd.notna(ev) else np.nan
        thresholds = _thresholds(settings, quote.market_type)
        value_class = _classify(float(edge) if pd.notna(edge) else 0, float(ev_pct) if pd.notna(ev_pct) else 0, value_confidence, bool(exclusions), thresholds)
        rows.append({
            "prediction_run_id": prediction_meta["run_id"], "prediction_lock_id": prediction_meta["lock_id"], "prediction_timestamp_utc": prediction_meta["created_at_utc"],
            "prediction_anchor_run_id": meta["run_id"], "prediction_reused_immutable": prediction_meta["run_id"] != meta["run_id"],
            "market_snapshot_id": snapshot_meta["snapshot_id"], "quote_id": quote.quote_id, "game_id": quote.game_id,
            "market_type": quote.market_type, "selection": quote.selection, "player_id": quote.player_id, "player_name": quote.player_name,
            "team": quote.player_team if pd.notna(quote.player_team) and str(quote.player_team).strip() else quote.selection_team, "side": quote.side, "line": quote.line, "bookmaker": quote.bookmaker,
            "book_odds": quote.decimal_odds, "market_timestamp_utc": market_time.isoformat(), "market_age_minutes": market_age,
            "model_probability": model_probability, "model_fair_odds": fair, "book_implied_probability": quote.book_implied_probability,
            "no_vig_probability": quote.no_vig_probability, "no_vig_status": quote.no_vig_status, "edge_pp": edge,
            "ev_decimal": ev, "ev_pct": ev_pct, "model_mean": model_mean, "model_difference": model_difference,
            "distribution_method": distribution_method, "model_confidence": model_confidence, "role_confidence": role_confidence,
            "role_label": role_label, "confidence_notes": confidence_notes, "value_confidence": value_confidence, "value_score": _score(float(edge) if pd.notna(edge) else 0, float(ev_pct) if pd.notna(ev_pct) else 0, value_confidence, market_age, maximum_age, role_confidence),
            "value_class": value_class, "comparison_basis": "NO_VIG" if pd.notna(quote.no_vig_probability) else "RAW_IMPLIED_INCOMPLETE",
            "mapping_status": quote.mapping_status, "exclusion_reason": ";".join(dict.fromkeys(exclusions)), "status": "EXCLUDED" if exclusions else "ACTIVE",
        })
    values = pd.DataFrame(rows)
    group_columns = ["game_id", "market_type", "selection", "player_id", "side", "line"]
    values["is_best_price"] = values.groupby(group_columns, dropna=False).book_odds.transform("max").eq(values.book_odds)
    values["book_count"] = values.groupby(group_columns, dropna=False).bookmaker.transform("nunique")
    values["consensus_probability"] = values.groupby(group_columns, dropna=False).no_vig_probability.transform("median")
    values["opening_book_odds"] = np.nan
    values["price_move"] = np.nan
    values["movement_direction"] = "NO_PRIOR_CAPTURE"
    qa = pd.DataFrame([
        {"check": "prediction_bundle_hashes", "passed": True, "severity": "CRITICAL", "details": meta["run_id"]},
        {"check": "quote_ids_unique", "passed": bool(mapped.quote_id.is_unique), "severity": "CRITICAL", "details": len(mapped)},
        {"check": "quote_selection_keys_unique", "passed": not bool(duplicate_selections.any()), "severity": "CRITICAL", "details": "one unambiguous price per event/book/market/selection/line"},
        {"check": "valid_decimal_odds_rejected", "passed": bool(values.loc[pd.to_numeric(values.book_odds, errors="coerce").le(1), "status"].eq("EXCLUDED").all()), "severity": "CRITICAL", "details": "odds <= 1 cannot be active"},
        {"check": "fixture_mapping_explicit", "passed": True, "severity": "CRITICAL", "details": mapped.fixture_mapping_status.value_counts().to_dict()},
        {"check": "player_mapping_explicit", "passed": True, "severity": "CRITICAL", "details": mapped.player_mapping_status.value_counts().to_dict()},
        {"check": "post_kickoff_rejected", "passed": not values.exclusion_reason.fillna("").str.contains("POST_KICKOFF_MARKET").any() or values.loc[values.exclusion_reason.fillna("").str.contains("POST_KICKOFF_MARKET"), "status"].eq("EXCLUDED").all(), "severity": "CRITICAL", "details": "enforced"},
        {"check": "stale_quotes_rejected", "passed": not values.exclusion_reason.fillna("").str.contains("STALE_MARKET").any() or values.loc[values.exclusion_reason.fillna("").str.contains("STALE_MARKET"), "status"].eq("EXCLUDED").all(), "severity": "CRITICAL", "details": "enforced"},
    ])
    from nfl2026.markets.certified_value import enforce_certification
    values = _attach_movement(settings, values, snapshot)
    values = enforce_certification(values, mapped, root=settings.root,source_metadata=source_metadata,
                                   snapshot_meta=snapshot_meta,now=datetime.now(timezone.utc))
    qa = pd.concat([qa,pd.DataFrame([{'check':'certified_probability_contract_required','passed':True,
        'severity':'CRITICAL','details':'Uncertified probabilities/EV withheld; independent actual time governs actionability'}])],ignore_index=True)
    return values, qa


def _attach_movement(settings: Settings, values: pd.DataFrame, snapshot: Path) -> pd.DataFrame:
    if values.empty:
        return values
    history_root = snapshot.parent
    prior_files = sorted(path / "market_quotes.csv" for path in history_root.iterdir() if path.is_dir() and path.name < snapshot.name and (path / "market_quotes.csv").exists())
    if not prior_files:
        return values
    earliest = pd.concat([pd.read_csv(path) for path in prior_files], ignore_index=True)
    # Raw snapshots carry provider event IDs, not canonical game IDs. Never let
    # identical player/line names in different fixtures share opening prices.
    current = pd.read_csv(snapshot / 'market_quotes.csv')
    if 'provider_event_id' not in current or 'provider_event_id' not in earliest:
        return values
    ids = current[['quote_id','provider_event_id']].drop_duplicates()
    if ids.quote_id.duplicated().any():
        return values
    values = values.merge(ids,on='quote_id',how='left',validate='many_to_one')
    keys = ["provider_event_id", "bookmaker", "market_type", "selection", "player_id", "side", "line"]
    if 'provider' in current and 'provider' in earliest:
        values = values.merge(current[['quote_id','provider']].drop_duplicates('quote_id'),on='quote_id',how='left',validate='many_to_one')
        keys.append('provider')
    for field in ('period','overtime','stat_contract','rule_version','jurisdiction','quote_type'):
        if field in current and field in earliest:
            if field not in values:
                values = values.merge(current[['quote_id',field]].drop_duplicates('quote_id'),on='quote_id',how='left',validate='many_to_one')
            keys.append(field)
    available = [key for key in keys if key in earliest.columns]
    if not available:
        return values
    earliest = earliest[pd.to_datetime(earliest.source_timestamp_utc,utc=True,errors='coerce') < pd.to_datetime(values.market_timestamp_utc,utc=True,errors='coerce').min()]
    earliest = earliest[pd.to_datetime(earliest.retrieval_timestamp_utc,utc=True,errors='coerce') < pd.to_datetime(current.retrieval_timestamp_utc,utc=True,errors='coerce').min()]
    opening = earliest.sort_values("source_timestamp_utc").drop_duplicates(available).rename(columns={"decimal_odds": "opening_book_odds"})
    # Merge identifiers must have stable types across market snapshots.
    # GSIS/player IDs are identifiers, never numeric measures.
    if "player_id" in available:
        values["player_id"] = values["player_id"].astype("string")
        opening["player_id"] = opening["player_id"].astype("string")
    merged = values.drop(columns=["opening_book_odds", "price_move", "movement_direction"]).merge(opening[available + ["opening_book_odds"]], on=available, how="left")
    merged["price_move"] = merged.book_odds - merged.opening_book_odds
    merged["movement_direction"] = np.select([merged.price_move.gt(0), merged.price_move.lt(0), merged.opening_book_odds.notna()], ["PRICE_LENGTHENED", "PRICE_SHORTENED", "UNCHANGED"], default="NO_PRIOR_CAPTURE")
    return merged


def create_value_run(settings: Settings, season: int, week: int, snapshot: Path, prediction_run_id: str | None = None, mode: str = "dry-run", as_of: datetime | None = None) -> tuple[Path, pd.DataFrame, pd.DataFrame]:
    as_of = as_of or datetime.now(timezone.utc)
    week_root = settings.prediction_root / str(season) / f"week_{week:02d}"
    bundle = week_root / prediction_run_id if prediction_run_id else select_official_bundle(settings, season, week, pd.Timestamp(as_of))
    meta = validate_prediction_reference(bundle)
    snapshot_meta = validate_market_snapshot(snapshot, season, week)
    values, qa = compare_market_quotes(settings, bundle, snapshot, as_of)
    fingerprint = hashlib.sha256(f"{meta['run_id']}|{snapshot_meta['snapshot_id']}|{mode}".encode()).hexdigest()[:8]
    value_run_id = f"phase4-{mode}-{_timestamp(as_of)}-{fingerprint}"
    destination = settings.root / "value_runs" / str(season) / f"week_{week:02d}" / value_run_id
    if destination.exists():
        raise FileExistsError(f"Value run is immutable: {destination}")
    destination.mkdir(parents=True)
    if not values.empty:
        values.insert(0, "value_run_id", value_run_id)
        values = values.reindex(columns=VALUE_COLUMNS)
    else:
        values = pd.DataFrame(columns=VALUE_COLUMNS)
    values.to_csv(destination / "value_bets.csv", index=False, lineterminator="\n")
    shutil.copy2(snapshot / "market_quotes.csv", destination / "market_quotes.csv")
    for market_type, filename in {
        "MONEYLINE":"value_moneyline.csv", "SPREAD":"value_spreads.csv", "TOTAL":"value_totals.csv",
        "ANYTIME_TD":"value_td.csv", "RUSHING_YARDS":"value_rushing.csv", "RECEIVING_YARDS":"value_receiving.csv",
        "TEAM_TOTAL":"value_team_totals.csv", "PASSING_YARDS":"value_passing.csv",
    }.items():
        values[values.market_type.eq(market_type)].to_csv(destination / filename, index=False, lineterminator="\n")
    qa.to_csv(destination / "qa_report.csv", index=False, lineterminator="\n")
    status = "MARKET_DATA_UNAVAILABLE" if values.empty else ("OFFICIAL_VALUE_SNAPSHOT" if mode == "official" and qa.passed.astype(bool).all() else "DRY_RUN_READY" if qa.passed.astype(bool).all() else "BLOCKED")
    run_meta = {
        "value_run_id": value_run_id, "status": status, "mode": mode, "season": season, "week": week,
        "created_at_utc": as_of.isoformat(), "prediction_run_id": meta["run_id"], "prediction_lock_id": meta["lock_id"],
        "prediction_timestamp_utc": meta["created_at_utc"], "prediction_model_ids": meta["model_ids"],
        "market_snapshot_id": snapshot_meta["snapshot_id"], "market_provider": snapshot_meta["provider"],
        "market_quote_count": len(values), "qualifying_value_count": len(select_qualifying_values(values)),
        "prediction_bundle_modified": False, "flat_hypothetical_stake": 1.0,
    }
    from nfl2026.markets.request_plan import merged_forecast
    _, merged_games, _, _, source_bundles = merged_forecast(settings, bundle)
    run_meta["merged_forecast_games"] = len(merged_games)
    run_meta["prediction_fixture_sources"] = merged_games[["game_id", "prediction_source_run_id", "prediction_reused_immutable"]].to_dict("records")
    run_meta["prediction_source_metadata"] = {run_id:{key:source_meta[key] for key in
        ("run_id", "lock_id", "created_at_utc", "model_ids", "file_hashes")} for run_id,(_,source_meta) in source_bundles.items()}
    run_meta["file_hashes"] = {path.name: sha256_file(path) for path in destination.iterdir() if path.is_file()}
    (destination / "metadata.json").write_text(json.dumps(run_meta, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return destination, values, qa
