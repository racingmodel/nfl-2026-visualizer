"""Execution-time event and quota gates for fresh odds captures."""
from datetime import datetime, timezone
import re

import pandas as pd

from nfl2026.markets.providers.base import MarketFetchResult
from nfl2026.markets.schema import MARKET_QUOTE_COLUMNS
from nfl2026.markets.request_plan import eligible_fixtures


def fetch_scoped(provider, season, week, retrieved_at_utc, target_fixtures=None, *,
                 cached_payload=None, fetch_game_markets=True, fetch_props=True,
                 state_loader=None, clock=None, max_credits=None):
    from nfl2026.markets.core import _team_code
    from nfl2026.markets.providers.the_odds_api import normalise_payload
    from nfl2026.production.partial_gate import fetch_fixture_states
    clock = clock or (lambda: datetime.now(timezone.utc))
    state_loader = state_loader or fetch_fixture_states
    provider.usage_events = []
    audit = []
    usage = {"requests": provider.usage_events, "fixture_checks": audit,
        "requested_bookmakers": list(provider.bookmakers),
        "requested_game_markets": provider.GAME_MARKETS,
        "requested_prop_markets": provider.PROP_MARKETS}
    def unavailable(reason):
        return MarketFetchResult(provider.name, clock(), pd.DataFrame(columns=MARKET_QUOTE_COLUMNS), {},
            "MARKET_DATA_UNAVAILABLE", (reason,), usage)
    if not provider.api_key:
        return unavailable("ODDS_API_KEY is not configured")
    required = {"game_id", "kickoff_utc", "home_team", "away_team"}
    if target_fixtures is None or target_fixtures.empty or not required.issubset(target_fixtures):
        return unavailable("Explicit canonical target fixtures are required; unscoped odds requests are forbidden")
    if target_fixtures.game_id.duplicated().any():
        return unavailable("Duplicate canonical fixtures")
    game_cost = provider.estimate_cost(provider.GAME_MARKETS, len(provider.bookmakers), len(provider.regions.split(",")))
    prop_cost = provider.estimate_cost(provider.PROP_MARKETS, len(provider.bookmakers), len(provider.regions.split(",")))
    budget = max_credits if max_credits is not None else game_cost + len(target_fixtures) * prop_cost
    spent = 0
    usage.update({"maximum_credits":budget, "estimated_credits_attempted":0,
        "estimated_game_cost":game_cost, "estimated_prop_cost_per_event":prop_cost})

    def checked(fixtures, stage):
        states, evidence = state_loader(season, week)
        now = clock()
        timestamp = pd.to_datetime(evidence.get("retrieved_at_utc"), utc=True, errors="coerce")
        age = (pd.Timestamp(now) - timestamp).total_seconds() if pd.notna(timestamp) else float("inf")
        if age < -5 or age > 60 or evidence.get("provisional_offline"):
            raise ValueError("Independent game state is not fresh")
        result = eligible_fixtures(fixtures, states, now, margin_seconds=provider.timeout + 5)
        audit.append({"stage": stage, "checked_at_utc": now.isoformat(), "source": evidence,
            "fixtures": result[["game_id", "eligibility_state", "request_eligible", "exclusion_reasons"]].to_dict("records")})
        return result.loc[result.request_eligible].drop(columns=["eligibility_state", "request_eligible", "exclusion_reasons"])

    def remaining():
        if not provider.usage_events:
            return None
        value = provider.usage_events[-1].get("quota_remaining", provider.usage_events[-1].get("x-requests-remaining"))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def charge(path, params, fixtures, cost):
        nonlocal spent
        # Rechecked again by _get if a retry is ever enabled. No chargeable retry
        # is automatic: a lost response might already have consumed credits.
        def gate():
            current = checked(fixtures, path)
            if set(current.game_id) != set(fixtures.game_id):
                raise ValueError("Event became ineligible before request")
        gate()
        quota = remaining()
        if quota is None or quota < provider.quota_reserve + cost or spent + cost > budget:
            raise ValueError("Quota unknown, reserve reached or maximum credit budget exceeded")
        provider._retry_gate = gate
        spent += cost
        usage["estimated_credits_attempted"] = spent
        try:
            return provider._get(path, params)
        finally:
            provider._retry_gate = None

    try:
        targets = checked(target_fixtures, "before_event_discovery")
        if targets.empty:
            return unavailable("No independently eligible pre-kickoff fixtures")
        # This metadata-only endpoint consumes zero credits. No odds are requested
        # until IDs have matched exact teams AND UTC kickoff against independent state.
        start = pd.Timestamp(clock()) + pd.Timedelta(seconds=provider.timeout + 5)
        finish = pd.to_datetime(targets.kickoff_utc, utc=True).max()
        events = provider._get(f"sports/{provider.sport_key}/events", {"dateFormat": "iso",
            "commenceTimeFrom":start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "commenceTimeTo":finish.strftime("%Y-%m-%dT%H:%M:%SZ")})
        if not isinstance(events, list):
            return unavailable("Malformed event discovery response")
        matches = {}
        for event in events:
            event_id = str(event.get("id", ""))
            kickoff = pd.to_datetime(event.get("commence_time"), utc=True, errors="coerce")
            candidates = targets.loc[targets.home_team.eq(_team_code(event.get("home_team"))) &
                targets.away_team.eq(_team_code(event.get("away_team"))) &
                pd.to_datetime(targets.kickoff_utc, utc=True).eq(kickoff)]
            if len(candidates) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", event_id):
                gid = str(candidates.iloc[0].game_id)
                if event_id in matches and matches[event_id] != gid:
                    raise ValueError("Provider event ID has conflicting mappings")
                matches[event_id] = gid
        if len(set(matches.values())) != len(matches):
            return unavailable("Multiple provider events map to one fixture")
        if not matches:
            return unavailable("No exact provider event/team/kickoff mappings")
        unmapped = sorted(set(targets.game_id.astype(str)) - set(matches.values()))
        targets = checked(targets[targets.game_id.astype(str).isin(matches.values())], "before_featured_request")
        matches = {eid: gid for eid, gid in matches.items() if gid in set(targets.game_id.astype(str))}
        if not matches:
            return unavailable("All mapped events became ineligible")
        base_params = {"oddsFormat": "decimal", "dateFormat": "iso"}
        base_params["bookmakers" if provider.bookmakers else "regions"] = ",".join(provider.bookmakers) if provider.bookmakers else provider.regions
        if not fetch_game_markets:
            return unavailable("Fresh scoped captures require fresh game markets; cached component reuse is forbidden")
        featured = charge(f"sports/{provider.sport_key}/odds",
            {**base_params, "markets": provider.GAME_MARKETS, "eventIds": ",".join(sorted(matches))}, targets, game_cost)
        if not isinstance(featured, list):
            return unavailable("Malformed game-market response")
        def valid_event(event):
            eid = str(event.get("id", ""))
            if eid not in matches:
                return False
            fixture = targets.loc[targets.game_id.astype(str).eq(matches[eid])].iloc[0]
            return (_team_code(event.get("home_team")) == fixture.home_team and
                _team_code(event.get("away_team")) == fixture.away_team and
                pd.to_datetime(event.get("commence_time"), utc=True, errors="coerce") == pd.Timestamp(fixture.kickoff_utc))
        unique = {}
        for event in featured:
            if not valid_event(event):
                raise ValueError("Provider returned an unrequested/conflicting event")
            eid = str(event["id"])
            if eid in unique and unique[eid] != event:
                raise ValueError("Conflicting duplicate provider event")
            unique[eid] = event
        featured = list(unique.values())
        props, failed, skipped, deferred = {}, [], [], []
        for event in featured if fetch_props else []:
            eid = str(event["id"])
            fixture = targets[targets.game_id.astype(str).eq(matches[eid])]
            if provider.low_credit_mode and pd.Timestamp(event["commence_time"]) > pd.Timestamp(clock()) + pd.Timedelta(hours=provider.prop_window_hours):
                deferred.append(eid)
                continue
            try:
                payload = charge(f"sports/{provider.sport_key}/events/{eid}/odds",
                    {**base_params, "markets": provider.PROP_MARKETS}, fixture, prop_cost)
                if not isinstance(payload, dict) or not valid_event(payload) or str(payload.get("id")) != eid:
                    raise ValueError("Conflicting prop event")
                if payload.get("bookmakers"):
                    props[eid] = payload
                else:
                    failed.append(eid)
            except ValueError:
                skipped.append(eid)
            except Exception:
                failed.append(eid)
        current = checked(targets, "after_capture")
        allowed = set(current.game_id.astype(str))
        featured = [event for event in featured if matches[str(event["id"])] in allowed]
        props = {eid: event for eid, event in props.items() if matches[eid] in allowed}
        raw = {"featured": featured, "props": props, "season": season, "week": week,
            "target_featured_event_count": len(featured), "provider_featured_event_count": len(events),
            "request_fixture_mapping": matches, "fixture_checks": audit,
            "unmapped_canonical_fixture_ids": unmapped,
            "component_status": {"game_markets": "READY" if featured else "UNAVAILABLE",
                "player_props": "PARTIAL" if failed or skipped else ("READY" if props else "NOT_REQUESTED"),
                "prop_failed_events": failed, "prop_skipped_events": skipped, "prop_deferred_events": deferred},
            "cached_components_reused": False}
        quotes = normalise_payload(raw, clock())
        usage.update({"estimated_game_cost": game_cost, "estimated_prop_cost_per_event": prop_cost,
            "estimated_max_total_cost": game_cost + len(featured) * prop_cost if fetch_props else game_cost,
            "estimated_credits_attempted": spent, "maximum_credits": budget,
            "quota_remaining": remaining(), "expected_event_count": len(current)})
        status = "READY" if len(quotes) and quotes.market_type.isin(["MONEYLINE", "SPREAD", "TOTAL"]).any() else "MARKET_DATA_UNAVAILABLE"
        warnings = []
        if unmapped:
            warnings.append("No exact provider event mapping for: " + ", ".join(unmapped))
        if skipped or failed:
            warnings.append(f"Player props: {len(skipped)} skipped; {len(failed)} failed; no cached props reused")
        if len(featured) < len(current):
            warnings.append(f"Provider returned {len(featured)}/{len(current)} eligible fixtures")
        return MarketFetchResult(provider.name, clock(), quotes, raw, status, tuple(warnings), usage)
    except Exception as exc:
        detail = str(exc).replace(str(provider.api_key), "[REDACTED]")
        return unavailable(f"Scoped provider retrieval failed safely ({type(exc).__name__}: {detail})")
