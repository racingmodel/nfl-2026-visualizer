from __future__ import annotations

from datetime import datetime
import json
from typing import Callable
from urllib.parse import urlencode

import pandas as pd

from nfl2026.live.providers.base import ProviderResult, fetch_bytes, utc_iso


WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
WMO = {0:"Clear",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",45:"Fog",48:"Rime fog",51:"Light drizzle",53:"Drizzle",55:"Heavy drizzle",61:"Light rain",63:"Rain",65:"Heavy rain",71:"Light snow",73:"Snow",75:"Heavy snow",80:"Rain showers",81:"Rain showers",82:"Heavy rain showers",95:"Thunderstorm",96:"Thunderstorm with hail",99:"Thunderstorm with hail"}
VENUE_OVERRIDES = {
    # The schedule feed inherits the Rams' normal dome flag for this neutral-site game.
    "Melbourne Cricket Ground": {"lat":-37.8199,"lon":144.9834,"roof":"OUTDOOR"},
}


def _roof(schedule_roof: object, reference_roof: object) -> str:
    value = str(schedule_roof if pd.notna(schedule_roof) else reference_roof).strip().upper()
    if value in {"DOME","INDOORS","CLOSED"}:
        return "DOME"
    if value in {"OUTDOORS","OPEN"}:
        return "OUTDOOR"
    if "RETRACT" in value:
        return "RETRACTABLE_UNKNOWN"
    return "UNKNOWN"


def _nearest(hourly: dict, kickoff: pd.Timestamp) -> int:
    times = pd.to_datetime(hourly.get("time", []), utc=True)
    if not len(times):
        raise ValueError("Forecast response contains no hourly timestamps")
    return int(abs(times-kickoff).argmin())


def load_weather(
    schedule: pd.DataFrame,
    stadiums: pd.DataFrame,
    retrieved_at: datetime,
    http_get: Callable[[str], bytes] | None = None,
) -> ProviderResult:
    http_get = http_get or (lambda url: fetch_bytes(url, attempts=3, timeout_seconds=20))
    refs = stadiums.drop_duplicates("team").set_index("team")
    rows = []
    failures = []
    for game in schedule.itertuples(index=False):
        override=VENUE_OVERRIDES.get(str(game.stadium))
        ref = pd.Series(override) if override else (refs.loc[game.home_team] if game.home_team in refs.index else pd.Series(dtype=object))
        roof = _roof(ref.get("roof") if override else getattr(game, "roof", None), ref.get("roof"))
        base = {
            "game_id": game.game_id, "venue": game.stadium, "forecast_location": f"{game.stadium} (venue override)" if override else game.stadium,
            "kickoff_utc": pd.Timestamp(game.kickoff_utc).isoformat(), "roof_status": roof,
            "retrieved_at_utc": utc_iso(retrieved_at), "known_at_utc": utc_iso(retrieved_at),
            "source_known_at_basis": "retrieval time; provider response does not expose issuance time",
            "source": "Open-Meteo Best Match forecast", "model_use": "DISPLAY_CONTEXT_ONLY",
        }
        if roof == "DOME":
            rows.append({**base, "forecast_status":"ROOF_SUPPRESSED", "temperature_f":None, "wind_speed_mph":None,
                         "wind_gust_mph":None, "precipitation_probability":None, "precipitation_type":"NONE",
                         "humidity_percent":None, "weather_condition":"Indoor/closed-roof context"})
            continue
        if ref.empty or pd.isna(ref.get("lat")) or pd.isna(ref.get("lon")):
            failures.append(game.game_id)
            rows.append({**base, "forecast_status":"FAILED_NO_COORDINATES"})
            continue
        params = {
            "latitude": float(ref.lat), "longitude": float(ref.lon), "timezone":"UTC", "forecast_days":16,
            "temperature_unit":"fahrenheit", "wind_speed_unit":"mph", "precipitation_unit":"inch",
            "hourly":"temperature_2m,relative_humidity_2m,precipitation_probability,rain,snowfall,weather_code,wind_speed_10m,wind_gusts_10m",
        }
        url = f"{WEATHER_URL}?{urlencode(params)}"
        try:
            payload = json.loads(http_get(url))
            hourly = payload["hourly"]
            kickoff = pd.Timestamp(game.kickoff_utc)
            index = _nearest(hourly, kickoff)
            value = lambda key: hourly.get(key, [None] * (index + 1))[index]
            rain, snow, code = value("rain"), value("snowfall"), value("weather_code")
            precip_type = "SNOW" if (snow or 0) > 0 else ("RAIN" if (rain or 0) > 0 else "NONE")
            rows.append({**base, "forecast_status":"AVAILABLE", "temperature_f":value("temperature_2m"),
                         "wind_speed_mph":value("wind_speed_10m"), "wind_gust_mph":value("wind_gusts_10m"),
                         "precipitation_probability":value("precipitation_probability"), "precipitation_type":precip_type,
                         "humidity_percent":value("relative_humidity_2m"), "weather_condition":WMO.get(code, f"WMO {code}"),
                         "latitude":float(ref.lat), "longitude":float(ref.lon)})
        except Exception as exc:
            failures.append(game.game_id)
            rows.append({**base, "forecast_status":"FAILED_RETRIEVAL", "failure":str(exc)[:240]})
    records = pd.DataFrame(rows)
    coverage = records[["game_id","venue","kickoff_utc","roof_status","forecast_status","known_at_utc","retrieved_at_utc","model_use"]].copy()
    covered = int(coverage.forecast_status.isin(["AVAILABLE","ROOF_SUPPRESSED"]).sum())
    status = "READY" if covered == len(coverage) else "READY_WITH_WARNINGS"
    warnings = [] if not failures else [f"forecast retrieval unavailable for {len(failures)}/{len(coverage)} games; weather is not an active model feature"]
    return ProviderResult(
        name="weather", records=records, coverage=coverage, status=status, source_url=WEATHER_URL,
        retrieved_at_utc=utc_iso(retrieved_at), source_known_at_utc=utc_iso(retrieved_at),
        source_known_at_basis="retrieval time; Open-Meteo default endpoint returns latest forecast but no issuance timestamp",
        reliability="PRELOCK_FORECAST_CONTEXT_ONLY", warnings=warnings,
    )
