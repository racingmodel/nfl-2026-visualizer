"""Explicit research output contracts and configurable fantasy scoring."""
from dataclasses import dataclass, asdict
import numpy as np

from nfl2026.phase7.common import json_write


@dataclass(frozen=True)
class FantasyScoring:
    reception: float = 0.0
    passing_yard: float = .04
    rushing_yard: float = .1
    receiving_yard: float = .1
    passing_td: float = 4.0
    rushing_td: float = 6.0
    receiving_td: float = 6.0
    interception: float = -2.0
    lost_fumble: float = -2.0
    two_point_conversion: float = 2.0
    special_teams_td: float = 6.0


PRESETS = {"standard": FantasyScoring(), "half_ppr": FantasyScoring(reception=.5),
           "ppr": FantasyScoring(reception=1.0)}


def fantasy_points(stats: dict, scoring: FantasyScoring, *, allow_partial=False):
    """Missing categories are never silently zero. Use draw-level stats for ranges."""
    fields = {"receptions": "reception", "passing_yards": "passing_yard", "rushing_yards": "rushing_yard",
        "receiving_yards": "receiving_yard", "passing_tds": "passing_td", "rushing_tds": "rushing_td",
        "receiving_tds": "receiving_td", "interceptions": "interception", "lost_fumbles": "lost_fumble",
        "passing_2pt_conversions":"two_point_conversion", "rushing_2pt_conversions":"two_point_conversion",
        "receiving_2pt_conversions":"two_point_conversion", "special_teams_tds":"special_teams_td"}
    missing = sorted(set(fields) - set(stats))
    if missing and not allow_partial:
        raise ValueError(f"Fantasy inputs unavailable: {missing}")
    values = [np.asarray(stats[field], dtype=float) * getattr(scoring, weight)
              for field, weight in fields.items() if field in stats]
    if not values or not all(np.isfinite(value).all() for value in values):
        raise ValueError("Fantasy inputs empty/nonfinite")
    shapes = [v.shape for v in values]
    if len(set(shapes)) != 1:
        raise ValueError("Fantasy draw shapes differ")
    return {"points": sum(values), "state": "PARTIAL" if missing else "COMPLETE",
            "missing_categories": missing, "scoring": asdict(scoring)}


def summarize_draws(samples, thresholds):
    samples = np.asarray(samples, float)
    if samples.ndim != 1 or not len(samples) or not np.isfinite(samples).all():
        raise ValueError("Actual validated simulation draws are required")
    return {"mean": float(samples.mean()), "quantiles": {f"p{q}": float(np.quantile(samples, q/100))
        for q in (10, 25, 50, 75, 90)}, "thresholds": [{"line": float(line),
        "over_probability": float(np.mean(samples > line)), "under_probability": float(np.mean(samples < line)),
        "push_probability": float(np.mean(samples == line))} for line in thresholds]}


def write_output_contracts(settings):
    targets = [
        ("QB passing yards", "passing_yards", "QB with certified expected-starter evidence", "BLOCKED", "historical starter provenance and passing-yard target parity required"),
        ("QB passing TD counts", "passing_tds", "QB with certified expected-starter evidence", "BLOCKED", "count model and starter provenance required"),
        ("Receptions", "receptions", "WR/TE/RB/FB nominal weekly roster", "EXPERIMENTAL", "V4 targets × fitted binomial catch rate; publication proof missing"),
        ("Targets", "targets", "WR/TE/RB/FB", "PRODUCTION_EXISTING", "incumbent model unchanged"),
        ("Rushing attempts", "carries", "QB/RB/FB", "PRODUCTION_EXISTING", "incumbent model unchanged"),
        ("Red-zone opportunities", "red_zone_carries/targets", "offensive weekly roster", "EXPERIMENTAL", "V4 fitted targets; eligibility uncertified"),
        ("Goal-line opportunities", "inside_5_carries/targets", "offensive weekly roster", "EXPERIMENTAL", "V4 fitted targets; eligibility uncertified"),
        ("Yard/reception thresholds", "yards/receptions", "same parent-model population", "EXPERIMENTAL", "requires calibrated draw-level distribution; not point-forecast conversion"),
        ("Player 2+ TD", "offensive_td_count", "offensive players", "BLOCKED", "anytime probability is insufficient; TD count calibration required"),
        ("Team total/margin/game total thresholds", "team_scores", "scheduled teams", "NOT_STARTED", "existing correlated draws can support future certified threshold output"),
        ("Fantasy points/floors/ceilings", "configured fantasy scoring", "players with full category coverage", "PARTIAL_IMPLEMENTATION", "scoring utility tested; no complete calibrated fantasy forecasts"),
    ]
    json_write(settings.report_root / "phase7_prediction_contracts.json", [{"market": m, "training_target": t,
        "eligible_population": p, "status": s, "notes": n,
        "model_version": "production incumbent" if s == "PRODUCTION_EXISTING" else "phase7 research only",
        "required_inputs": t, "confidence": "NOT_CERTIFIED" if s != "PRODUCTION_EXISTING" else "incumbent unchanged",
        "evaluation_metric": "MAE/RMSE; probability outputs require Brier/CRPS/coverage",
        "frontend_contract": "status must accompany every optional output; unavailable does not mean zero"}
        for m,t,p,s,n in targets])
