"""Stricter evidence coverage and cached, identical-population V4 diagnostics."""
from __future__ import annotations

import json
import numpy as np
import pandas as pd

from nfl2026.phase7.common import json_write
from nfl2026.phase7.player_v4 import KEYS, population_mask
from nfl2026.five_week_review import block_bootstrap_difference, finite


def audit_coverage(settings):
    directory = settings.artifact_root / "research/phase7"
    ledger = pd.read_csv(directory / "historical_universe_evidence.csv", low_memory=False)
    captured = pd.read_csv(directory / "frozen_roster_evidence.csv", low_memory=False)
    ledger["certification"] = "PLAUSIBLE_UNVERIFIED"
    captured["certification"] = np.where(captured.roster_observation_certified.astype(str).str.lower().eq("true"),
        "PARTIAL_ROSTER_CAPTURE", "INSUFFICIENT_TIMING_PROVENANCE")
    records = pd.concat([ledger, captured], ignore_index=True)
    # A contemporaneous roster observation is not a complete injury/reserve/
    # activation certificate. Final participation is NEVER consulted here.
    coverage = records.groupby(["season", "week", "position", "certification"]).size().rename("observations").reset_index()
    coverage.to_csv(settings.report_root / "phase7_continuation_provenance_coverage.csv", index=False)
    inventory = pd.read_csv(settings.report_root / "phase7_source_inventory.csv")
    inventory.to_csv(settings.report_root / "phase7_continuation_evidence_inventory.csv", index=False)
    result = {"historical_roster_observations": len(ledger), "frozen_roster_observations": len(captured),
        "fully_certified": 0, "partially_certified": int(captured.roster_observation_certified.astype(str).str.lower().eq("true").sum()),
        "certified_2023_2025_comparison_rows": 0, "reduction_from_nominal_candidate_rows": 22530,
        "certified_historical_comparison": "BLOCKED_NO_COMPLETE_PREGAME_ELIGIBILITY_EVIDENCE",
        "no_final_participation_used_as_proof": True,
        "remaining": ["historical captured injury/transaction/activation revision chain absent",
            "weekly roster membership and historical depth dates cannot establish complete pregame eligibility",
            "2026 captured official sources retain transaction pagination/unmatched-identity warnings"]}
    json_write(settings.report_root / "phase7_continuation_provenance.json", result)
    return result


def refine_cached_research(settings):
    """No fitting or filtering on favourable realised volume/role outcomes."""
    directory = settings.artifact_root / "research"
    candidates = pd.read_csv(directory / "phase7/player_v4_oos.csv")
    candidates = candidates[candidates.model.eq("phase7-player-v4-opportunity-research-v1-extra_trees")]
    reference = pd.read_csv(directory / "five_week_review/player_production_semantics_replay.csv")
    targets = ["carries", "targets", "rushing_yards", "receiving_yards"]
    data = candidates.merge(reference[KEYS + [f"pred_{t}" for t in targets]], on=KEYS, validate="one_to_one", suffixes=("", "_champion"))
    rows = []
    for season, frame in data.groupby("season"):
        for target in targets:
            part = frame[population_mask(frame, target)]
            opportunity = "carries" if target in {"carries", "rushing_yards"} else "targets"
            # Fixed pregame champion forecasts define volume, not observed targets.
            volume = part[f"pred_{opportunity}_champion"]
            low, high = (5, 15) if opportunity == "carries" else (3, 8)
            groups = [("ALL", part), ("WEEKS_1_4", part[part.week.le(4)]), ("WEEKS_5_PLUS", part[part.week.gt(4)]),
                ("LOW_PREGAME_OPPORTUNITY", part[volume.lt(low)]), ("MID_PREGAME_OPPORTUNITY", part[volume.ge(low) & volume.lt(high)]),
                ("HIGH_PREGAME_OPPORTUNITY", part[volume.ge(high)])]
            groups += [(position, part[part.position.eq(position)]) for position in sorted(part.position.unique())]
            for segment, cohort in groups:
                if cohort.empty:
                    continue
                for variant in ([f"pred_{target}", f"direct_{target}"] if target.endswith("yards") else [f"pred_{target}"]):
                    actual = cohort[f"actual_{target}"]
                    current = cohort[f"pred_{target}_champion"]
                    candidate = cohort[variant]
                    delta, lo, hi = block_bootstrap_difference(cohort, ((candidate-actual).abs()-(current-actual).abs()).to_numpy(), 400)
                    rows.append({"season": int(season), "target": target, "segment": segment, "variant": variant,
                        "rows": len(cohort), "incumbent_mae": float((current-actual).abs().mean()),
                        "candidate_mae": float((candidate-actual).abs().mean()), "delta": delta, "delta_ci_low": lo, "delta_ci_high": hi,
                        "bias": float((candidate-actual).mean()), "historical_eligibility_certified": False})
    pd.DataFrame(rows).to_csv(settings.report_root / "phase7_v4_segment_comparison.csv", index=False)
    from nfl2026.phase7.lab import GATES
    json_write(settings.report_root / "phase7_v4_certification_policy.json", {
        "decision": "DO_NOT_PROMOTE", "required_gates": list(GATES), "prospective_results_observed": False,
        "primary_candidate": "existing extra_trees train-through-2024, two-stage yards; direct-yardage secondary",
        "comparisons": "all folds/positions/volume bins retained, incumbent forecast bins fixed before prospective results",
        "role_changes": "BLOCKED_HISTORICAL_PREGAME_ROLE_PROVENANCE", "QB_starter_backup": "current frozen annotation only, not historically certified features",
        "active_probability": "not fitted without independently evidenced eligibility targets",
        "mean_research_refit": False, "multiweek_confirmation": "required; Week6 alone cannot promote",
        "material_regression": "paired block-bootstrap confidence intervals per season/position; a CI wholly above zero is a review failure",
        "multiple_comparison_qualification": "2025 previously explored; segment CI is descriptive, not a fresh confirmatory test"})
    return {"segments": len(rows), "role_certification": "BLOCKED", "models_refit": False}
