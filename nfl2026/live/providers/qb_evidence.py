"""Dated club evidence, scoped to identities and fixtures; never a QB override table."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from lxml import html
import pandas as pd

from nfl2026.live.providers.transactions import TEAM_NAMES

CLUB_HOSTS = dict(zip(
    ('ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV LAC LA MIA MIN NE NO NYG NYJ PHI PIT SEA SF TB TEN WAS').split(),
    ('azcardinals.com atlantafalcons.com baltimoreravens.com buffalobills.com panthers.com chicagobears.com bengals.com clevelandbrowns.com dallascowboys.com denverbroncos.com detroitlions.com packers.com houstontexans.com colts.com jaguars.com chiefs.com raiders.com chargers.com therams.com miamidolphins.com vikings.com patriots.com neworleanssaints.com giants.com newyorkjets.com philadelphiaeagles.com steelers.com seahawks.com 49ers.com buccaneers.com tennesseetitans.com commanders.com').split()))

# Only unambiguous franchise cities. New York/Los Angeles cannot identify a club.
TEAM_CITIES = dict(zip(
    ('ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX KC LV MIA MIN NE NO PHI PIT SEA SF TB TEN WAS').split(),
    ('Arizona|Atlanta|Baltimore|Buffalo|Carolina|Chicago|Cincinnati|Cleveland|Dallas|Denver|Detroit|Green Bay|Houston|Indianapolis|Jacksonville|Kansas City|Las Vegas|Miami|Minnesota|New England|New Orleans|Philadelphia|Pittsburgh|Seattle|San Francisco|Tampa Bay|Tennessee|Washington').split('|')))


def validate_source(source):
    parsed = urlparse(source['url'])
    if parsed.scheme != 'https' or parsed.hostname not in {CLUB_HOSTS.get(source['team']), 'www.'+CLUB_HOSTS.get(source['team'], '')}:
        raise ValueError('QB evidence must come from the matching official club host')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('QB source URL must be a public canonical article')


def capture_sources(settings, season, destination, retrieved_at, fetch, *, week=None):
    catalog = settings.root/'config/qb_evidence_sources.json'
    if not catalog.exists():
        return
    sources = json.loads(catalog.read_text(encoding='utf-8'))['sources']
    receipt = []
    for source in sources:
        if int(source['season']) != season:
            continue
        if week is not None and source.get('week') is not None and int(source['week'])!=week:
            continue
        validate_source(source)
        body = fetch(source['url'])
        observed = datetime.now(timezone.utc).isoformat()
        filename = 'qb_evidence_'+hashlib.sha256(source['url'].encode()).hexdigest()[:16]+'.html'
        (destination/filename).write_bytes(body)
        receipt.append({**source, 'filename':filename, 'sha256':hashlib.sha256(body).hexdigest(),
                        'retrieved_at_utc':observed})
    (destination/'qb_evidence_sources.json').write_text(json.dumps({'sources':receipt}, indent=2), encoding='utf-8')


def _timestamp(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError('QB evidence requires an explicit publication timezone')
    return stamp.tz_convert('UTC')


def parse_article(body, source, roster, schedule, retrieved_at):
    """Only explicit, supported prose forms become events; unrecognized prose adds nothing.

    IR remains a persistent event requiring fresh inactive roster corroboration.
    Starter/bench/suspension statements require an identified fixture. A prior
    week's starter announcement never confirms a subsequent week's starter.
    """
    validate_source(source)
    digest = hashlib.sha256(body).hexdigest()
    if source.get('sha256', digest) != digest:
        raise ValueError('QB evidence raw source hash differs')
    nodes = html.fromstring(body).xpath('//script[@type="application/ld+json"]/text()')
    articles = []
    for text in nodes:
        item = json.loads(text)
        items = item if isinstance(item, list) else item.get('@graph', [item])
        articles.extend(a for a in items if a.get('@type') == 'NewsArticle')
    articles = [a for a in articles if a.get('url') == source['url']]
    if len(articles) != 1:
        raise ValueError('QB evidence article identity is missing or ambiguous')
    article = articles[0]
    published = _timestamp(article['datePublished'])
    modified = _timestamp(article.get('dateModified', article['datePublished']))
    asof = _timestamp(retrieved_at)
    retrieved = _timestamp(source.get('retrieved_at_utc', retrieved_at))
    if published > modified or modified > retrieved or published.year != int(source['season']):
        raise ValueError('QB evidence publication/modification is future or outside season')
    if retrieved > asof:
        raise ValueError('QB evidence receipt is from the future')
    if source.get('kind')=='CONTEXT_ONLY':
        return []
    text = re.sub(r'\s+', ' ', article['articleBody'].replace('\\n', ' ')).strip()
    team = source['team']
    candidates = roster[(roster.team.eq(team)) & roster.position.eq('QB')].drop_duplicates('gsis_id')
    def identity(name):
        key = lambda s: re.sub(r'[^a-z0-9]', '', str(s).lower())
        match = candidates[candidates.full_name.map(key).eq(key(name))]
        if len(match) != 1 or pd.isna(match.iloc[0].gsis_id):
            raise ValueError('Official QB evidence has an unresolved/ambiguous player identity')
        return str(match.iloc[0].gsis_id)
    def fixture(sentence):
        games = schedule[(schedule.season.eq(int(source['season']))) & schedule.game_type.eq('REG') &
                         (schedule.home_team.eq(team)|schedule.away_team.eq(team))].copy()
        week = re.search(r'\bWeek\s+(\d+)\b', sentence, flags=re.I)
        if week:
            games = games[games.week.eq(int(week[1]))]
        else:
            day = re.search(r'\bthis\s+(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b', sentence, flags=re.I)
            if not day:
                raise ValueError('QB starter/suspension/bench evidence lacks fixture applicability')
            weekdays = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
            local = published.tz_convert('America/New_York')
            if local.date() != modified.tz_convert('America/New_York').date():
                raise ValueError('Relative starter date is ambiguous after an article update on a different date')
            date = (local+pd.Timedelta(days=(weekdays.index(day[1].lower())-local.weekday())%7)).date()
            games = games[pd.to_datetime(games.gameday).dt.date.eq(date)]
        opponents = {code for name, code in TEAM_NAMES.items() if re.search(r'\b'+re.escape(name)+r'\b', sentence, re.I)}-{team}
        opponents.update(code for code, city in TEAM_CITIES.items() if code != team and re.search(r'\b'+re.escape(city)+r'\b', sentence, re.I))
        if len(opponents) != 1:
            raise ValueError('QB evidence must name one opposing team')
        opponent = next(iter(opponents))
        games = games[(games.home_team.eq(opponent)|games.away_team.eq(opponent))]
        if len(games) != 1:
            raise ValueError('QB evidence fixture identity is ambiguous')
        return str(games.iloc[0].game_id)
    events = []
    def event(name, state, sentence, game_id=''):
        events.append({'team':team, 'season':int(source['season']), 'player_id':identity(name),
                       'player_name':name.strip(), 'state':state, 'game_id':game_id,
                       'published_at_utc':published.isoformat(), 'known_at_utc':modified.isoformat(),
                       'retrieved_at_utc':retrieved.isoformat(), 'source_url':source['url'],
                       'source_sha256':digest, 'source_file':source.get('filename',''), 'evidence_text':sentence})
    # Require an affirmative placement, not a rumour, negation or quotation.
    # Keep initials inside an exact player name (e.g. A.J.) in one sentence.
    segmented = re.sub(r'\b(?:[A-Z]\.){2,}', lambda m: m[0].replace('.', '<INITIAL>'), text)
    for part in re.split(r'(?<=[.!?])\s+', segmented):
        sentence = part.replace('<INITIAL>', '.')
        if re.search(r'\b(not|might|may|could|if|would|should|considering|plans? to|intend to|will have|expected to|planning to|reportedly)\b', sentence, re.I):
            continue
        placed = re.search(r'\b(?:have |has )?placed (?:quarterback|QB) ([A-Za-z .\'-]+?) on (?:the )?Injured Reserve\b', sentence, re.I)
        if placed:
            event(placed[1], 'IR', sentence)
        activated = re.search(r'\b(?:have |has )?activated (?:quarterback|QB) ([A-Za-z .\'-]+?) from (?:the )?Injured Reserve\b', sentence, re.I)
        if activated:
            event(activated[1], 'ACTIVE', sentence)
        starter = re.search(r'\b(?:quarterback|QB) ([A-Za-z .\'-]+?) (?:will start|is set to start)\b', sentence, re.I)
        if not starter:
            starter = re.search(r'^(?:Quarterback |QB )?([A-Za-z .\'-]+?) will be (?:the )?(?:[A-Za-z]+[\x27\u2019]s? )?starting quarterback\b', sentence, re.I)
        if starter:
            event(starter[1], 'STARTER', sentence, fixture(sentence))
        suspended = re.search(r'\b(?:quarterback|QB) ([A-Za-z .\'-]+?) (?:is|has been) suspended\b', sentence, re.I)
        if suspended:
            event(suspended[1], 'SUSPENDED', sentence, fixture(sentence))
        benched = re.search(r'\b(?:quarterback|QB) ([A-Za-z .\'-]+?) (?:is|has been) benched\b', sentence, re.I)
        if benched:
            event(benched[1], 'BENCHED', sentence, fixture(sentence))
    return events


def load_evidence(raw_dir, roster, schedule, retrieved_at):
    path = Path(raw_dir)/'qb_evidence_sources.json'
    if not path.exists():
        return []
    records = []
    for source in json.loads(path.read_text(encoding='utf-8'))['sources']:
        if source['team'] not in set(roster.team):
            continue
        filename = source['filename']
        if Path(filename).name != filename:
            raise ValueError('QB source archive path must be a filename')
        records.extend(parse_article((Path(raw_dir)/filename).read_bytes(), source, roster, schedule, retrieved_at))
    return records


def reconcile_quarterbacks(schedule, roster, depth, injuries, week, retrieved_at, evidence=()):
    """Return explicit source reconciliation, preserving health and confirmation semantics."""
    out = schedule.copy()
    reports = injuries[injuries.record_type.eq('PLAYER_REPORT')] if len(injuries) else pd.DataFrame()
    now = _timestamp(retrieved_at)
    for side in ('home','away'):
        out[f'{side}_qb_original_id'] = out[f'{side}_qb_id']
        out[f'{side}_qb_original_name'] = out[f'{side}_qb_name']
        out[f'{side}_qb_resolution'] = 'SCHEDULE_PROJECTION'
        out[f'{side}_qb_confirmed'] = False
        out[f'{side}_qb_evidence'] = '[]'
        out[f'{side}_qb_evidence_error'] = ''
        out[f'{side}_qb_depth_superseded'] = False
        for idx, game in out.iterrows():
            team, pid = str(game[f'{side}_team']), str(game[f'{side}_qb_id'])
            kickoff = _timestamp(game.kickoff_utc)
            relevant = [e for e in evidence if e['team']==team and int(e['season'])==int(game.get('season', kickoff.year)) and
                        e['game_id'] in ('', str(game.game_id)) and _timestamp(e['known_at_utc']) < kickoff and
                        _timestamp(e['known_at_utc']) <= now and _timestamp(e['retrieved_at_utc']) <= now and
                        _timestamp(e['retrieved_at_utc']) < kickoff]
            # A later dated activation may supersede IR, but never an undated
            # roster label. Conflicting same-time IR/ACTIVE remains inactive.
            persistent = {}
            for e in sorted(relevant, key=lambda e: (_timestamp(e['known_at_utc']), e['state']=='IR')):
                if e['state'] in {'IR','ACTIVE'}:
                    persistent[e['player_id']] = e
            effective = [e for e in relevant if e['state'] not in {'IR','ACTIVE'}] + list(persistent.values())
            starters = {e['player_id'] for e in relevant if e['state']=='STARTER'}
            old_evidence = [e for e in effective if e['player_id']==pid]
            original = roster[(roster.week.eq(week)) & roster.team.eq(team) & roster.position.eq('QB') & roster.gsis_id.astype(str).eq(pid)]
            health = reports[reports.team.eq(team)&reports.player_id.astype(str).eq(pid)] if len(reports) else pd.DataFrame()
            unavailable = len(health) and health.game_status.astype(str).str.upper().isin({'OUT','IR','PUP','INACTIVE','SUSPENDED'}).any()
            if len(original) and 'current_transaction_state' in original:
                unavailable = unavailable or original.current_transaction_state.astype(str).str.upper().isin({'IR','RELEASED','PRACTICE_SQUAD','SUSPENDED'}).any()
            persistent_ir = any(e['state']=='IR' for e in old_evidence)
            inactive = len(original)==1 and str(original.iloc[0].status).upper() in {'RES','IR','PUP','INACTIVE','SUSPENDED'}
            if persistent_ir and not inactive:
                out.loc[idx,f'{side}_qb_evidence_error'] = 'IR announcement conflicts with current roster; activation evidence required'
                continue
            suspended = any(e['state']=='SUSPENDED' for e in old_evidence)
            if suspended and not inactive:
                # Changing the team projection alone must not leave a suspended
                # original QB eligible for downstream player predictions.
                out.loc[idx,f'{side}_qb_evidence_error'] = 'Suspension conflicts with current active roster; eligibility reconciliation required'
                continue
            unavailable = unavailable or (persistent_ir and inactive) or suspended
            benched = any(e['state']=='BENCHED' for e in old_evidence)
            if not (unavailable or benched or starters):
                continue
            out.loc[idx,f'{side}_qb_evidence'] = json.dumps(relevant, separators=(',',':'))
            if len(starters)>1:
                out.loc[idx,f'{side}_qb_evidence_error'] = 'Conflicting fixture-specific official starter evidence'
                continue
            current = depth[depth.team.eq(team)&depth.position.eq('QB')].copy()
            current['depth_order'] = pd.to_numeric(current.depth_order, errors='coerce')
            # A new persistent event requires fresh exact QB1 corroboration.
            # A fixture-specific official starter announcement outranks a
            # stale ordinal on an otherwise fresh identity-verified chart.
            # Inferred replacements still require unique QB1 corroboration.
            if (persistent_ir or benched) and not starters:
                current = current[current.depth_order.eq(1)]
            current = current.sort_values('depth_order')
            active = roster[roster.week.eq(week)&roster.team.eq(team)&roster.position.eq('QB')&roster.status.eq('ACT')]
            candidates = current[current.player_id.astype(str).isin(active.gsis_id.astype(str))].copy()
            excluded = set(reports.loc[reports.team.eq(team)&reports.game_status.astype(str).str.upper().isin(
                {'OUT','IR','PUP','INACTIVE','QUESTIONABLE','DOUBTFUL','SUSPENDED'}),'player_id'].astype(str)) if len(reports) else set()
            excluded.update(e['player_id'] for e in effective if e['state'] in {'IR','SUSPENDED','BENCHED'})
            candidates = candidates[~candidates.player_id.astype(str).isin(excluded)]
            if starters:
                candidates = candidates[candidates.player_id.astype(str).isin(starters)]
            if candidates.empty or candidates.depth_order.eq(candidates.depth_order.min()).sum()!=1:
                out.loc[idx,f'{side}_qb_evidence_error'] = 'No unique eligible corroborated replacement QB'
                continue
            replacement = candidates.iloc[0]
            known = _timestamp(replacement.known_at_utc)
            if not 0 <= (now-known).total_seconds()/3600 <= 36 or known >= kickoff:
                out.loc[idx,f'{side}_qb_evidence_error'] = 'Replacement QB depth evidence stale/future/post-kickoff'
                continue
            if str(replacement.get('depth_source','NFLVERSE_CURRENT')) != 'NFLVERSE_CURRENT':
                out.loc[idx,f'{side}_qb_evidence_error'] = 'Replacement requires exact current depth evidence'
                continue
            out.loc[idx,f'{side}_qb_id'] = replacement.player_id
            out.loc[idx,f'{side}_qb_name'] = replacement.player_name
            out.loc[idx,f'{side}_qb_confirmed'] = bool(starters)
            out.loc[idx,f'{side}_qb_depth_superseded'] = bool(starters and replacement.depth_order != 1)
            out.loc[idx,f'{side}_qb_resolution'] = ('OFFICIAL_FIXTURE_STARTER' if starters else
                'CURRENT_QB1_AFTER_DATED_IR' if persistent_ir else
                'CURRENT_QB1_AFTER_DATED_BENCH' if benched else
                'CURRENT_QB1_AFTER_DATED_SUSPENSION' if any(e['state']=='SUSPENDED' for e in old_evidence) else
                'CURRENT_DEPTH_AFTER_OFFICIAL_OUT')
    return out
