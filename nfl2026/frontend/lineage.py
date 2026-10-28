"""Materialize certified display provenance without changing frozen forecasts."""
from copy import deepcopy
import json
from pathlib import Path

import pandas as pd

PARTIAL_LINEAGE_FIELDS = (
    "game_state_at_run", "prediction_reused_immutable",
    "feature_lock_reference", "pregame_refresh_reference",
)


def certified_partial_lineage(settings, metadata):
    if metadata.get("prediction_scope") != "UNPLAYED_ONLY":
        return {}
    from nfl2026.markets.core import validate_prediction_reference
    run_id = str(metadata["run_id"])
    if Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("Invalid partial anchor identity")
    anchor = settings.prediction_root / str(metadata["season"]) / f"week_{int(metadata['week']):02d}" / run_id
    source_meta = validate_prediction_reference(anchor)
    if (source_meta.get("run_id"), source_meta.get("season"), source_meta.get("week"), source_meta.get("prediction_scope")) != (
        run_id, metadata["season"], metadata["week"], "UNPLAYED_ONLY"
    ):
        raise ValueError("Partial header differs from certified anchor identity")
    frame = pd.read_csv(anchor / "game_prediction_manifest.csv", dtype=str, keep_default_na=False)
    required = {"game_id", "prediction_source_run_id", *PARTIAL_LINEAGE_FIELDS}
    if not required.issubset(frame) or frame.empty or frame.game_id.duplicated().any():
        raise ValueError("Partial manifest has missing fields or duplicate fixture IDs")
    if frame[list(required)].eq("").any().any():
        raise ValueError("Partial manifest has empty lineage fields")
    records = frame.set_index("game_id").to_dict("index")
    sources = {}
    for trace in records.values():
        flag = trace["prediction_reused_immutable"]
        if flag not in {"True", "False"}:
            raise ValueError("Partial manifest has invalid preservation flag")
        trace["prediction_reused_immutable"] = flag == "True"
        source_id = trace["prediction_source_run_id"]
        if Path(source_id).name != source_id or source_id in {".", ".."}:
            raise ValueError("Invalid fixture source identity")
        if source_id not in sources:
            source = anchor.parent / source_id
            source_metadata = validate_prediction_reference(source)
            if "data_lock.json" not in source_metadata.get("file_hashes", {}):
                raise ValueError("Fixture source lacks immutable lock provenance")
            source_lock = json.loads((source / "data_lock.json").read_text())
            reference = str(source_lock.get("game_refresh_manifest", source_lock.get("snapshot_id", source_metadata.get("data_lock_snapshot_id", ""))))
            sources[source_id] = (source_metadata, reference)
        source_metadata, reference = sources[source_id]
        if (source_metadata["run_id"] != source_id or source_metadata["season"] != metadata["season"]
            or source_metadata["week"] != metadata["week"]
            or trace["feature_lock_reference"] != source_metadata["lock_id"]
            or trace["pregame_refresh_reference"] != reference
            or trace["prediction_reused_immutable"] != (source_id != run_id)):
            raise ValueError("Certified fixture lineage differs from its immutable source lock")
    return records


def materialize_partial_lineage(settings, payload):
    """Fill absent display fields from a verified manifest; reject contradictions.

    The caller must still run the strict original-forecast verifier on the result.
    Existing fields, including every player/score/probability, remain untouched.
    """
    lineage = certified_partial_lineage(settings, payload["metadata"])
    if not lineage:
        return payload
    result = deepcopy(payload)
    ids = [str(game["game_id"]) for game in result["games"]]
    if len(ids) != len(set(ids)) or set(ids) != set(lineage):
        raise ValueError("Frontend fixture population differs from certified partial lineage")
    for game in result["games"]:
        trace = lineage[str(game["game_id"])]
        source = game.get("prediction_source_run_id") or game.get("prediction_run_id")
        if source != trace["prediction_source_run_id"]:
            raise ValueError("Frontend forecast source differs from certified partial lineage")
        for field in PARTIAL_LINEAGE_FIELDS:
            expected = trace[field]
            if field in game and (game[field] != expected or type(game[field]) is not type(expected)):
                raise ValueError("Frontend partial lineage differs: " + field)
            game[field] = expected
    return result
