from __future__ import annotations

import pandas as pd


QUALIFYING_VALUE_CLASSES = {"MODEL_VALUE", "STRONG_MODEL_VALUE"}


def select_qualifying_values(values: pd.DataFrame, now=None) -> pd.DataFrame:
    """Return exactly the active, best-price rows eligible for the value ledger."""
    if values.empty:
        return values.copy()
    # Old ledgers remain readable, but old uncalibrated rows cannot enter a new
    # certified ledger merely because their legacy score called them value.
    if not {'actionable_now', 'probability_certified', 'decision_label'}.issubset(values.columns):
        return values.iloc[0:0].copy()
    from nfl2026.markets.certified_value import truth
    from nfl2026.frontend.value_contract import value_payload
    # Recheck expiry when consumed; a true flag stored earlier is not perpetual.
    current = value_payload(values.to_dict('records'),now=now)['selections']
    allowed = pd.Series([truth(r.get('actionable_now')) for r in current],index=values.index)
    return values[
        values.value_class.isin(QUALIFYING_VALUE_CLASSES)
        & values.status.eq("ACTIVE")
        & values.is_best_price.map(truth)
        & values.actionable_now.map(truth)
        & values.probability_certified.map(truth)
        & values.decision_label.eq('AI Back Value')
        & allowed
    ].copy()
