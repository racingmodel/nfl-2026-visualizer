from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from nfl2026.config import load_settings
from nfl2026.utilities.io import write_csv_json
from nfl2026.validation.checks import run_quality_checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the NFL 2026 modelling foundation")
    parser.add_argument("--config", default="config/nfl2026.toml")
    args = parser.parse_args()
    settings = load_settings(args.config)
    a = settings.artifact_root
    r = settings.report_root
    quality = run_quality_checks(
        settings,
        pd.read_csv(a / "canonical" / "canonical_games.csv"),
        pd.read_csv(a / "canonical" / "player_weekly.csv"),
        pd.read_csv(a / "canonical" / "player_team_membership.csv"),
        pd.read_csv(a / "features" / "team_training_matrix.csv"),
        pd.read_csv(a / "features" / "player_training_matrix.csv"),
        pd.read_csv(a / "reports" / "td_reconciliation_report.csv"),
        pd.read_csv(a / "reports" / "feature_quality_report.csv"),
        pd.read_csv(a / "backtests" / "team_baseline_predictions.csv"),
        pd.read_csv(a / "backtests" / "player_baseline_predictions.csv"),
    )
    write_csv_json(quality, a / "reports" / "data_quality_report.csv", a / "reports" / "data_quality_report.json")
    write_csv_json(quality, r / "data_quality_report.csv", r / "data_quality_report.json")
    failures = quality[(quality["severity"].eq("critical")) & (quality["status"].eq("fail"))]
    print(quality.to_string(index=False))
    return 1 if len(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
