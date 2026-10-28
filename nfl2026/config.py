from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class Settings:
    root: Path
    current_season: int
    training_seasons: tuple[int, ...]
    model_version: str
    feature_version: str
    legacy_root: Path
    artifact_root: Path
    report_root: Path
    prediction_root: Path
    team_rolling_window: int
    player_rolling_window: int
    player_history_window: int
    prior_pseudo_games: float
    near_constant_threshold: float
    mostly_missing_threshold: float
    random_seed: int
    simulation_draws: int
    minimum_training_games: int
    score_min: float
    score_max: float
    td_reconciliation_tolerance: float
    forbidden_feature_tokens: tuple[str, ...]
    phase2_model_version: str
    phase2_player_model_version: str
    phase2_feature_version: str
    phase2_team_history_window: int
    phase2_player_history_window: int
    phase2_prior_grid: tuple[float, ...]
    market_provider: str
    market_sport_key: str
    market_regions: str
    market_bookmakers: tuple[str, ...]
    market_game_refresh_minutes: int
    market_prop_refresh_minutes: int
    market_prop_window_hours: int
    market_quota_reserve: int
    market_low_credit_mode: bool
    market_featured_max_age_minutes: int
    market_player_prop_max_age_minutes: int
    market_fixture_time_tolerance_minutes: int
    market_thresholds: dict[str, dict[str, float]]
    operation_stages: tuple[str, ...]
    drift_minimum_completed_weeks: int
    drift_multiplier: float


def load_settings(path: str | Path = "config/nfl2026.toml") -> Settings:
    config_path = Path(path).resolve()
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    root = config_path.parent.parent
    paths = raw["paths"]
    markets = raw.get("markets", {})
    return Settings(
        root=root,
        current_season=int(raw["project"]["current_season"]),
        training_seasons=tuple(int(v) for v in raw["project"]["training_seasons"]),
        model_version=str(raw["project"]["model_version"]),
        feature_version=str(raw["project"]["feature_version"]),
        legacy_root=root / paths["legacy_root"],
        artifact_root=root / paths["artifact_root"],
        report_root=root / paths["report_root"],
        prediction_root=root / paths["prediction_root"],
        team_rolling_window=int(raw["features"]["team_rolling_window"]),
        player_rolling_window=int(raw["features"]["player_rolling_window"]),
        player_history_window=int(raw["features"]["player_history_window"]),
        prior_pseudo_games=float(raw["features"]["prior_pseudo_games"]),
        near_constant_threshold=float(raw["features"]["near_constant_threshold"]),
        mostly_missing_threshold=float(raw["features"]["mostly_missing_threshold"]),
        random_seed=int(raw["simulation"]["random_seed"]),
        simulation_draws=int(raw["simulation"]["draws"]),
        minimum_training_games=int(raw["validation"]["minimum_training_games"]),
        score_min=float(raw["validation"]["score_min"]),
        score_max=float(raw["validation"]["score_max"]),
        td_reconciliation_tolerance=float(raw["validation"]["td_reconciliation_tolerance"]),
        forbidden_feature_tokens=tuple(raw["contracts"]["forbidden_feature_tokens"]),
        phase2_model_version=str(raw.get("phase2", {}).get("model_version", "phase2-v1")),
        phase2_player_model_version=str(
            raw.get("phase2", {}).get(
                "player_model_version",
                raw.get("phase2", {}).get("model_version", "phase2-v1"),
            )
        ),
        phase2_feature_version=str(raw.get("phase2", {}).get("feature_version", "advanced-pit-v1")),
        phase2_team_history_window=int(raw.get("phase2", {}).get("team_history_window", 8)),
        phase2_player_history_window=int(raw.get("phase2", {}).get("player_history_window", 8)),
        phase2_prior_grid=tuple(float(v) for v in raw.get("phase2", {}).get("prior_grid", [0, 1, 2, 4, 8, 12, 16, 24, 32, 48, 64])),
        market_provider=str(markets.get("provider", "the_odds_api")),
        market_sport_key=str(markets.get("sport_key", "americanfootball_nfl")),
        market_regions=str(markets.get("regions", "uk,us")),
        market_bookmakers=tuple(str(value) for value in markets.get("bookmakers", ["williamhill","skybet","betfair_ex_uk","betfair_sb_uk","fanduel","draftkings"])),
        market_game_refresh_minutes=int(markets.get("game_refresh_minutes", 240)),
        market_prop_refresh_minutes=int(markets.get("prop_refresh_minutes", 240)),
        market_prop_window_hours=int(markets.get("prop_window_hours", 48)),
        market_quota_reserve=int(markets.get("quota_reserve", 50)),
        market_low_credit_mode=bool(markets.get("low_credit_mode", True)),
        market_featured_max_age_minutes=int(markets.get("featured_max_age_minutes", 120)),
        market_player_prop_max_age_minutes=int(markets.get("player_prop_max_age_minutes", 60)),
        market_fixture_time_tolerance_minutes=int(markets.get("fixture_time_tolerance_minutes", 30)),
        market_thresholds={str(k): {str(name): float(value) for name, value in values.items()} for k, values in markets.get("thresholds", {}).items()},
        operation_stages=tuple(str(value).upper() for value in raw.get("operations", {}).get("stages", ["EARLY_WEEK", "MID_WEEK", "FINAL", "GAME_DAY"])),
        drift_minimum_completed_weeks=int(raw.get("operations", {}).get("drift_minimum_completed_weeks", 4)),
        drift_multiplier=float(raw.get("operations", {}).get("drift_multiplier", 1.35)),
    )


def ensure_directories(settings: Settings) -> None:
    for path in (
        settings.artifact_root / "canonical",
        settings.artifact_root / "features",
        settings.artifact_root / "backtests",
        settings.artifact_root / "registry",
        settings.artifact_root / "models",
        settings.artifact_root / "reports",
        settings.artifact_root / "phase2",
        settings.report_root,
        settings.prediction_root,
        settings.root / "markets" / "snapshots",
        settings.root / "value_runs",
        settings.root / "deploy" / "artifacts",
        settings.root / "logs",
    ):
        path.mkdir(parents=True, exist_ok=True)
