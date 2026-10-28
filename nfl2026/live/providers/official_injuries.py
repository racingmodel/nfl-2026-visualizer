from __future__ import annotations

from datetime import datetime
from io import BytesIO
import re

from lxml import html
import pandas as pd

from nfl2026.live.providers.base import utc_iso
from nfl2026.live.providers.injuries import normalise_game_status, normalise_practice


OFFICIAL_INJURY_URL = "https://www.nfl.com/injuries/league/{season}/reg{week}"
TEAM_TITLES = {
    "Cardinals":"ARI","Falcons":"ATL","Ravens":"BAL","Bills":"BUF","Panthers":"CAR","Bears":"CHI",
    "Bengals":"CIN","Browns":"CLE","Cowboys":"DAL","Broncos":"DEN","Lions":"DET","Packers":"GB",
    "Texans":"HOU","Colts":"IND","Jaguars":"JAX","Chiefs":"KC","Raiders":"LV","Chargers":"LAC",
    "Rams":"LA","Dolphins":"MIA","Vikings":"MIN","Patriots":"NE","Saints":"NO","Giants":"NYG",
    "Jets":"NYJ","Eagles":"PHI","Steelers":"PIT","49ers":"SF","Seahawks":"SEA","Buccaneers":"TB",
    "Titans":"TEN","Commanders":"WAS",
}


def _key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def inspect_official_page_identity(payload: bytes, season: int, week: int) -> dict:
    """Recognise an old league landing page without accepting its injury rows."""
    try:
        tree=html.fromstring(payload)
    except (TypeError,ValueError):
        return {'state':'ERROR','reason':'Malformed official injury page'}
    title=' '.join(tree.xpath('//title//text()'))
    headings=' '.join(' '.join(n.itertext()) for n in tree.xpath('//h1|//h2'))
    text=title+' '+headings
    seasons={int(v) for v in re.findall(r'\b(20\d{2})\b',text)}
    weeks={int(v) for v in re.findall(r'\bweek\s*(\d{1,2})\b',text,re.I)}
    if 'injuryreport' not in _key(text) or seasons!={season} or len(weeks)!=1:
        return {'state':'ERROR','reason':'Official season/week identity absent, conflicting or wrong season'}
    observed_week=next(iter(weeks))
    selected=' '.join(tree.xpath('//option[@selected]//text()'))
    selected_weeks={int(v) for v in re.findall(r'\bweek\s*(\d{1,2})\b',selected,re.I)}
    if selected_weeks and selected_weeks!={observed_week}:
        return {'state':'ERROR','reason':'Official selected week contradicts page identity'}
    body=' '.join(tree.xpath('//body//text()')).lower()
    if any(s in body for s in ['captcha','checking your browser','access denied','request blocked','challenge-platform']):
        return {'state':'ERROR','reason':'Official challenge/error page'}
    state='CURRENT' if observed_week==week else 'PREVIOUS_WEEK_QUARANTINED' if observed_week==week-1 else 'ERROR'
    return {'state':state,'requested_season':season,'requested_week':week,'observed_season':season,'observed_week':observed_week,
            'reason':'No wrong-week player/coverage row may be ingested' if state!='CURRENT' else 'Exact page identity'}


def load_official_injury_tables(payload: bytes, roster: pd.DataFrame, season: int, week: int, retrieved_at: datetime) -> pd.DataFrame:
    try:
        tree = html.fromstring(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("official NFL injury page HTML could not be parsed") from exc

    title = " ".join(tree.xpath("//title//text()"))
    headings = [" ".join(node.itertext()).strip() for node in tree.xpath("//h1 | //h2")]
    page_identity = _key(" ".join([title, *headings]))
    if inspect_official_page_identity(payload,season,week)['state']!='CURRENT':
        raise ValueError("official NFL injury page identity/season/week could not be verified")
    body_text = " ".join(tree.xpath("//body//text()" )).lower()
    if any(marker in body_text for marker in (
        "checking your browser", "access denied", "request blocked", "verify you are human",
        "just a moment", "captcha", "challenge-platform",
    )):
        raise ValueError("NFL injury request returned a challenge or error page")

    roster_week = roster[(roster.season.eq(season)) & (roster.week.eq(week))].copy()
    if not {"team", "full_name", "gsis_id"}.issubset(roster_week.columns):
        raise ValueError("official injury parsing requires team/player identity fields in the weekly roster")
    roster_week["name_key"] = roster_week.full_name.map(_key)
    id_map = {(row.team, row.name_key): row.gsis_id for row in roster_week.itertuples(index=False)}

    rows = []
    coverage_rows = []
    seen_teams: set[str] = set()
    team_sections = tree.xpath("//div[contains(@class, 'nfl-t-stats__title')]")
    if not team_sections:
        raise ValueError("official NFL injury page contains no team report sections")

    player_columns = {"player", "position", "injuries", "practicestatus", "gamestatus"}
    for header in team_sections:
        title_text = " ".join(header.itertext()).strip()
        team = TEAM_TITLES.get(title_text)
        if team is None:
            raise ValueError(f"official NFL injury page contains an unrecognized team section: {title_text!r}")
        if team in seen_teams:
            raise ValueError(f"official NFL injury page contains duplicate team section: {team}")
        seen_teams.add(team)

        section = header.getnext()
        if section is None:
            raise ValueError(f"official NFL injury page has no report section for {team}")
        section_text = " ".join(section.itertext()).strip()
        normalized_section = _key(section_text)
        tables = section.xpath(".//table")
        if tables:
            if len(tables) != 1:
                raise ValueError(f"official NFL injury section has an ambiguous table structure for {team}")
            try:
                parsed_tables = pd.read_html(BytesIO(html.tostring(tables[0])))
            except (ValueError, ImportError) as exc:
                raise ValueError(f"official NFL injury table could not be parsed for {team}") from exc
            if len(parsed_tables) != 1:
                raise ValueError(f"official NFL injury section has an unexpected table count for {team}")
            table = parsed_tables[0]
            columns = {_key(column): column for column in table.columns}
            if not player_columns.issubset(columns):
                raise ValueError(f"official NFL injury table has unexpected columns for {team}: {sorted(columns)}")
            if table.empty:
                raise ValueError(f"official NFL injury table is empty without an explicit no-injuries marker for {team}")
            player_count = 0
            unresolved = 0
            for _, record in table.iterrows():
                player = record[columns["player"]]
                position = record[columns["position"]]
                injury = record[columns["injuries"]]
                practice = record[columns["practicestatus"]]
                game = record[columns["gamestatus"]]
                game_status = normalise_game_status(game)
                practice_status = normalise_practice(practice)
                player_id = id_map.get((team, _key(player)))
                unresolved += int(pd.isna(player_id))
                player_count += 1
                rows.append({
                    "season": season, "week": week, "team": team, "player_id": player_id,
                    "player_name": player, "position": position, "game_status": game_status,
                    "practice_status_normalized": practice_status,
                    "normalized_status": game_status if game_status != "UNKNOWN" else practice_status,
                    "injury": "" if pd.isna(injury) else injury,
                    "original_report_status": "" if pd.isna(game) else game,
                    "original_practice_status": "" if pd.isna(practice) else practice,
                    "known_at_utc": utc_iso(retrieved_at), "retrieved_at_utc": utc_iso(retrieved_at),
                    "source": "official NFL injury report",
                    "source_known_at_basis": "retrieval time; page exposes no row publication timestamp",
                    "record_type": "PLAYER_REPORT", "coverage_status": "REPORT_PUBLISHED",
                    "source_state": "VALID", "availability_status": game_status,
                })
            report_empty = False
        elif "noresults" in _key(section.get("class", "")) and "noinjuriesreported" in normalized_section:
            # Positive official evidence of a published, intentionally empty
            # team report. No synthetic player rows are created.
            player_count = 0
            unresolved = 0
            report_empty = True
        else:
            raise ValueError(f"official NFL injury section is malformed or has no recognized empty marker for {team}")

        coverage_rows.append({
            "season": season, "week": week, "team": team, "records": player_count,
            "coverage_status": "REPORT_PUBLISHED", "source_state": "VALID",
            "report_empty": report_empty, "latest_report_known_at_utc": utc_iso(retrieved_at),
            "known_at_utc": utc_iso(retrieved_at), "retrieved_at_utc": utc_iso(retrieved_at),
            "unresolved_player_ids": unresolved, "record_type": "TEAM_COVERAGE",
            "source": "official NFL injury report",
            "source_known_at_basis": "retrieval time; page exposes no row publication timestamp",
        })

    if not coverage_rows:
        raise ValueError("official NFL injury page contains no recognized team reports")
    return pd.DataFrame([*rows, *coverage_rows])
