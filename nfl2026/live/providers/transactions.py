from __future__ import annotations

from datetime import datetime
from io import BytesIO
import re
from urllib.parse import urlparse

import pandas as pd
from lxml import html

from nfl2026.live.providers.base import ProviderResult, utc_iso


TRADES_URL = "https://github.com/nflverse/nflverse-data/releases/download/trades/trades.csv"
NFL_TRANSACTION_URL = "https://www.nfl.com/transactions/league/{category}/{season}/{month}"
TRANSACTION_CATEGORIES = ("reserve-list", "signings", "waivers", "terminations", "other")
TEAM_NAMES = {
    "Cardinals":"ARI","Falcons":"ATL","Ravens":"BAL","Bills":"BUF","Panthers":"CAR","Bears":"CHI",
    "Bengals":"CIN","Browns":"CLE","Cowboys":"DAL","Broncos":"DEN","Lions":"DET","Packers":"GB",
    "Texans":"HOU","Colts":"IND","Jaguars":"JAX","Chiefs":"KC","Raiders":"LV","Chargers":"LAC",
    "Rams":"LA","Dolphins":"MIA","Vikings":"MIN","Patriots":"NE","Saints":"NO","Giants":"NYG",
    "Jets":"NYJ","Eagles":"PHI","Steelers":"PIT","49ers":"SF","Seahawks":"SEA","Buccaneers":"TB",
    "Titans":"TEN","Commanders":"WAS",
}


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _team_code(value: object) -> str:
    text = str(value or "").strip()
    if not text or text in {"--", "nan", "None"}:
        return ""
    key = _key(text)
    return next((code for name, code in TEAM_NAMES.items() if _key(name) in key), "")


def _eligibility_state(description: object) -> str:
    from nfl2026.live.providers.transaction_state import classify
    return classify(description)


def inspect_official_transaction_page(
    payload: bytes,
    url: str,
    category: str,
    season: int,
    month: int,
):
    """Validate NFL page identity and return (tree, state).

    State is VALID for a recognized transaction table and VALID_EMPTY only for
    an identity-verified NFL page with an explicit no-results marker.
    Unknown/no-table and challenge pages fail closed.
    """
    try:
        tree = html.fromstring(payload)
    except (ValueError, TypeError) as exc:
        raise ValueError("official transaction page HTML could not be parsed") from exc

    expected_path = f"/transactions/league/{category}/{season}/{month}"
    canonical = tree.xpath("string(//link[translate(@rel,'CANONICAL','canonical')='canonical']/@href)").strip()
    canonical_url = urlparse(canonical)
    requested_url = urlparse(url)
    canonical_host = (canonical_url.hostname or "").lower()
    requested_path = requested_url.path.rstrip("/")
    canonical_path = canonical_url.path.rstrip("/")
    if (
        canonical_host not in {"nfl.com", "www.nfl.com"}
        or canonical_path != expected_path
        or requested_path != expected_path
    ):
        raise ValueError("official transaction page identity could not be verified")

    title = " ".join(tree.xpath("//title//text()" )).strip()
    title_key = _key(title)
    if "nflcom" not in title_key or str(season) not in title_key or _key(category) not in title_key:
        raise ValueError("official transaction page heading could not be verified")

    page_text = " ".join(tree.xpath("//body//text()" )).lower()
    if any(marker in page_text for marker in (
        "checking your browser", "access denied", "request blocked", "verify you are human",
        "just a moment", "captcha", "challenge-platform",
    )):
        raise ValueError("NFL transaction request returned a challenge or error page")

    tables = tree.xpath("//table")
    if tables:
        recognized = False
        for table in pd.read_html(BytesIO(payload)):
            cols = {_key(column) for column in table.columns}
            if {"from", "to", "date", "name", "transaction"}.issubset(cols):
                recognized = True
                break
        if not recognized:
            raise ValueError("official transaction page table has an unexpected structure")
        return tree, "VALID"

    normalized_text = re.sub(r"\s+", " ", page_text)
    if "no transactions available" in normalized_text or re.search(r"\bno results\.?\b", normalized_text):
        return tree, "VALID_EMPTY"
    raise ValueError("official transaction page contains no table or recognized empty-results marker")


def parse_official_transaction_pages(pages: dict[str, bytes], season: int, retrieved_at: datetime) -> pd.DataFrame:
    """Parse NFL.com's current-month transaction tables; timestamps are retrieval-time provenance."""
    records = []
    for filename, payload in sorted(pages.items()):
        match = re.match(r"^nfl_transactions_(.+)_(\d{2})_page\d+\.html$", filename)
        if not match:
            raise ValueError(f"unexpected official transaction page filename: {filename}")
        category, month_text = match.groups()
        month = int(month_text)
        if category not in TRANSACTION_CATEGORIES:
            raise ValueError(f"unexpected official transaction category: {category}")
        url = NFL_TRANSACTION_URL.format(category=category, season=season, month=month)
        tree, page_state = inspect_official_transaction_page(payload, url, category, season, month)
        if page_state == "VALID_EMPTY":
            continue
        tables = pd.read_html(BytesIO(payload))
        matched = False
        for element, table in zip(tree.xpath("//table"), tables):
            cols = {_key(column): column for column in table.columns}
            if not {"from", "to", "date", "name", "transaction"}.issubset(cols):
                continue
            matched = True
            body_rows = element.xpath(".//tbody/tr")
            for html_row, (_, row) in zip(body_rows, table.iterrows()):
                cells = html_row.xpath("./td")
                if len(cells) < 6:
                    raise ValueError(f"official transaction page contains a malformed transaction row: {filename}")
                date_text = str(row[cols["date"]]).strip()
                try:
                    event_at = pd.Timestamp(f"{season}-{month:02d}-{int(date_text.split('/')[-1]):02d}", tz="UTC")
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"official transaction page contains an invalid transaction date: {filename}") from exc
                player_name = str(row[cols["name"]]).strip()
                detail = str(row[cols["transaction"]]).strip()
                records.append({
                    "event_id": f"nfl:{category}:{event_at.date()}:{_key(player_name)}:{len(records)}",
                    "season": season, "event_at_utc": event_at.isoformat(), "event_type": "OFFICIAL_TRANSACTION",
                    "transaction_state": _eligibility_state(detail), "from_team": _team_code(" ".join(cells[0].itertext())),
                    "to_team": _team_code(" ".join(cells[1].itertext())), "player_id": "", "pfr_id": "",
                    "player_name": player_name, "position": str(row[cols.get("position", "")]).strip() if "position" in cols else "",
                    "transaction_detail": detail, "transaction_category": category,
                    "known_at_utc": utc_iso(retrieved_at), "retrieved_at_utc": utc_iso(retrieved_at),
                    "source": "NFL.com official transactions", "source_state": "VALID",
                    "effective_precision":"DATE", "effective_date":event_at.date().isoformat(),
                    "published_at_utc":"", "source_url":url, "source_file":filename,
                    "source_sha256":__import__('hashlib').sha256(payload).hexdigest(),
                })
        if not matched:
            raise ValueError(f"official transaction page has no recognized transaction table: {filename}")
    return pd.DataFrame(records)


def load_transactions(raw: pd.DataFrame, season: int, cutoff: datetime, source_known_at: datetime, retrieved_at: datetime, official_pages: dict[str, bytes] | None = None, complete_categories: set[str] | None = None, club_records: pd.DataFrame | None = None) -> ProviderResult:
    target = raw[raw.season.eq(season)].copy()
    target["event_at_utc"] = pd.to_datetime(target.trade_date, utc=True, errors="coerce")
    target = target[target.event_at_utc.le(pd.Timestamp(cutoff))].copy()
    records = pd.DataFrame({
        "event_id": target.trade_id,
        "season": target.season,
        "event_at_utc": target.event_at_utc.map(lambda value: value.isoformat()),
        "event_type": "TRADED",
        "from_team": target.gave,
        "to_team": target.received,
        "player_id": "",
        "pfr_id": target.pfr_id,
        "player_name": target.pfr_name,
        "known_at_utc": utc_iso(source_known_at),
        "retrieved_at_utc": utc_iso(retrieved_at),
        "source": "nflverse trades release",
    }).reset_index(drop=True)
    records["transaction_state"] = "TEAM_CHANGE"
    records["transaction_detail"] = "Trade"
    records["transaction_category"] = "trades"
    records["position"] = ""
    records["source_state"] = "VALID"
    official = parse_official_transaction_pages(official_pages or {}, season, retrieved_at)
    if len(official):
        official["event_at_utc"] = pd.to_datetime(official.event_at_utc, utc=True)
        official = official[official.event_at_utc.le(pd.Timestamp(cutoff))].copy()
        official["event_at_utc"] = official.event_at_utc.map(lambda value: value.isoformat())
        records = pd.concat([records, official], ignore_index=True)
        records = records.drop_duplicates(["event_at_utc","player_name","from_team","to_team","transaction_detail"],keep="last").reset_index(drop=True)
    if club_records is not None and len(club_records):
        records=pd.concat([records,club_records],ignore_index=True)
    has_club=club_records is not None and len(club_records)>0
    records["player_name"] = records.player_name.fillna("").astype(str)
    records["player_id"] = records.player_id.fillna("").astype(str)
    unresolved = int(records[records.player_name.notna()].player_id.eq("").sum())
    coverage = pd.DataFrame([{
        "season": season, "records_pre_cutoff": len(records), "player_rows_without_gsis_id": unresolved,
        "latest_event_at_utc": records.event_at_utc.max() if len(records) else "",
        "coverage_status": "OFFICIAL_CURRENT_MONTH" if official_pages and (set(complete_categories or TRANSACTION_CATEGORIES) >= set(TRANSACTION_CATEGORIES)) else "PARTIAL_OFFICIAL_CURRENT_MONTH" if official_pages else "SUPPLEMENTAL_ONLY",
        "official_current_month_rows": len(official),
        "unparsed_or_unmapped_records": 0,
        "eligibility_authority": "NFL.com current-month transaction categories plus NFLverse trades; roster fallback is overlaid by later transaction state",
        "official_club_supplement_teams":','.join(sorted(set(club_records.to_team.dropna().astype(str))|set(club_records.from_team.dropna().astype(str)))) if has_club else '',
        "official_club_rows":len(club_records) if has_club else 0,
    }])
    complete_categories = set(complete_categories or TRANSACTION_CATEGORIES)
    complete = bool(official_pages) and complete_categories >= set(TRANSACTION_CATEGORIES) and all(any(name.startswith(f"nfl_transactions_{category}_") for name in official_pages) for category in TRANSACTION_CATEGORIES)
    incomplete = sorted(set(TRANSACTION_CATEGORIES)-complete_categories)
    warnings = [] if complete else [f"official NFL current-month transaction pages incomplete for {', '.join(incomplete) or 'one or more categories'}; matched records are still overlaid, NFLverse trades alone are supplemental"]
    return ProviderResult(
        name="transactions", records=records, coverage=coverage, status="READY" if complete else "READY_WITH_WARNINGS",
        source_url=f"{TRADES_URL}; NFL.com monthly transaction pages; official club histories/articles" if has_club else f"{TRADES_URL}; NFL.com monthly reserve/signing/waiver/termination/other pages" if complete else TRADES_URL, retrieved_at_utc=utc_iso(retrieved_at),
        source_known_at_utc=utc_iso(retrieved_at) if complete or has_club else utc_iso(source_known_at),
        source_known_at_basis="per-event publication and observation retained separately; history dates have DATE precision" if has_club else "NFL.com page retrieval time; event date is shown by source" if complete else "nflverse trades release timestamp",
        reliability="PARTIAL_LEAGUE_WITH_OFFICIAL_CLUB_SUPPLEMENT" if has_club else "OFFICIAL_CURRENT_TRANSACTION_STATE" if complete else "SUPPLEMENTAL_TRADES_ONLY",
        warnings=warnings,
    )


def overlay_transactions_on_roster(roster: pd.DataFrame, transactions: pd.DataFrame, schedule: pd.DataFrame, *, cutoff=None, roster_known_at=None) -> tuple[pd.DataFrame, dict[str, int]]:
    """Apply the same certified state machine consumed by production eligibility.

    Exact roster snapshots use their publication time; a future fixture is never
    mistaken for a roster publication timestamp. Earlier contradictory inactive
    evidence remains a blocker unless a later qualifying transaction resolves it.
    """
    from nfl2026.live.providers.transaction_state import apply_states
    return apply_states(roster, transactions, cutoff=cutoff, roster_known_at=roster_known_at)
