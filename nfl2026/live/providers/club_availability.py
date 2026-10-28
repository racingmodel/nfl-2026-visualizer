"""Official club final reports and inactives, resolved by fixture and exact identity.

Raw articles and their observed times are retained. Publication never substitutes
for the time the pipeline first observed a version of an article.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from lxml import html
import pandas as pd

from nfl2026.live.availability import timestamp, validate_events, final_report_due
from nfl2026.live.providers.qb_evidence import CLUB_HOSTS, TEAM_CITIES, validate_source
from nfl2026.live.providers.transactions import TEAM_NAMES


def name_key(value):
    value=re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", str(value).lower())
    return re.sub(r'[^a-z0-9]', '', value)


def capture_sources(settings, season, destination, retrieved_at, fetch, *, week=None):
    catalog=settings.root/'config/availability_sources.json'
    if not catalog.exists():return
    receipts=[]
    for source in json.loads(catalog.read_text(encoding='utf-8'))['sources']:
        if int(source['season'])!=season:continue
        if week is not None and int(source['week'])!=week:continue
        validate_source(source)
        body=fetch(source['url'])
        observed=datetime.now(timezone.utc).isoformat()
        filename='availability_'+hashlib.sha256(source['url'].encode()).hexdigest()[:16]+'.html'
        (destination/filename).write_bytes(body)
        receipts.append({**source,'filename':filename,'sha256':hashlib.sha256(body).hexdigest(),'retrieved_at_utc':observed})
    (destination/'availability_sources.json').write_text(json.dumps({'sources':receipts},indent=2),encoding='utf-8')


def _paired_final_tables(tree,source,roster,game,article,published,modified,observed,asof):
    """Read complete paired, dated final-report tables without inferring health."""
    columns=lambda node:re.sub(r'[^a-z]', '', ' '.join(node.itertext()).lower())
    tables=[]
    for table in tree.xpath('//table'):
        headers=table.xpath('./thead/tr/th|./tr/th')
        labels=[columns(h) for h in headers]
        if 'gamestatus' in labels and ('player' in labels or 'name' in labels):tables.append((table,labels))
    if not tables:return None
    due=final_report_due(game.kickoff_utc).tz_convert('America/New_York')
    if modified.tz_convert('America/New_York').date()<due.date():raise ValueError('Final table version predates required final-report day')
    day=due.strftime('%A').lower();nicknames={code:name for name,code in TEAM_NAMES.items()}
    expected=set(source['teams']);seen=set();events=[];errors=[]
    cities={**TEAM_CITIES,'LA':'Los Angeles','LAC':'Los Angeles','NYG':'New York','NYJ':'New York'}
    aliases={t:{nicknames[t].upper(),(cities[t]+' '+nicknames[t]).upper()} for t in expected}
    for table,labels in tables:
        if len(labels)!=len(set(labels)):raise ValueError('Final injury table column identity duplicated')
        # A page may contain legends, but never infer a table's team from order.
        headings=table.xpath('preceding::*[self::h1 or self::h2 or self::h3 or self::h4]')
        team=next((code for h in reversed(headings) for code in expected
                   if ' '.join(h.itertext()).strip().upper() in aliases[code]),None)
        if team is None or team in seen:raise ValueError('Final injury table team identity missing or duplicated')
        seen.add(team)
        day_index=next((i for i,c in enumerate(labels) if c in {day,day[:3]}),None)
        if day_index is None:raise ValueError('Final injury table lacks required final practice day')
        player_index=labels.index('player') if 'player' in labels else labels.index('name')
        status_index=labels.index('gamestatus');position_index=labels.index('position') if 'position' in labels else None
        rows=table.xpath('./tbody/tr|./tr[td]');has_practice=False
        if not rows:raise ValueError('Final injury table is empty without explicit coverage proof')
        for row in rows:
            cells=[' '.join(' '.join(c.itertext()).split()) for c in row.xpath('./td')]
            if len(cells)!=len(labels):raise ValueError('Final injury table row shape ambiguous')
            practice=cells[day_index].upper()
            has_practice|=practice in {'FP','LP','DNP','FULL PARTICIPATION','LIMITED PARTICIPATION','DID NOT PARTICIPATE','FULL PARTICIPATION IN PRACTICE','LIMITED PARTICIPATION IN PRACTICE','DID NOT PARTICIPATE IN PRACTICE'}
            raw=cells[status_index].upper().strip()
            if raw not in {'OUT','DOUBTFUL','QUESTIONABLE','-','–','—'}:raise ValueError('Final injury table game designation missing or unsupported')
            status=raw if raw in {'OUT','DOUBTFUL','QUESTIONABLE'} else 'UNKNOWN'
            name=cells[player_index];position=cells[position_index].upper() if position_index is not None else ''
            if position_index is None:
                match=re.match(r'^(QB|RB|FB|WR|TE|OLB|ILB|EDGE|LB|DB|CB|S|DT|DE|DL|OT|OG|OL|G/C|G|T|C|K|P|LS)\s+(.+)$',name,re.I)
                if not match:raise ValueError('Final injury table player position missing')
                position,name=match.groups();position=position.upper()
            matches=roster[roster.team.eq(team)&roster.full_name.map(name_key).eq(name_key(name.strip(' *')))]
            if len(matches)!=1:
                errors.append({'team':team,'name':name,'position':position,'critical':position in {'QB','RB','FB','WR','TE'},'reason':'Exact roster identity not unique/found'})
                continue
            player=matches.iloc[0]
            if position in {'QB','RB','FB','WR','TE'} and str(player.position)!=position:raise ValueError('Offensive availability position mismatch')
            events.append({'game_id':game.game_id,'team':team,'player_id':str(player.gsis_id),'player_name':player.full_name,'position':player.position,'status':status,
                'published_at_utc':published.isoformat(),'modified_at_utc':modified.isoformat(),'observed_at_utc':observed.isoformat(),'known_at_utc':observed.isoformat(),
                'source_url':source['url'],'canonical_url':article['url'],'source_sha256':source['sha256'],'explicit_clearance':False,'original_game_designation':raw})
        if not has_practice:raise ValueError('Final injury table final-day observations not established')
    if seen!=expected:raise ValueError('Complete paired final injury tables not established')
    events=pd.DataFrame(events)
    if events.empty or events.duplicated(['game_id','team','player_id']).any():raise ValueError('Final injury table player identity missing or duplicated')
    events=validate_events(events,pd.DataFrame([game]),asof)
    coverage={(game.game_id,t):{'complete_final':True,'allow_absent_clearance':False,'critical_identity_errors':any(e['critical'] and e['team']==t for e in errors),
        'identity_warnings':[e for e in errors if e['team']==t],'source_url':source['url'],'source_sha256':source['sha256'],
        'published_at_utc':published.isoformat(),'modified_at_utc':modified.isoformat(),'observed_at_utc':observed.isoformat(),'format':'PAIRED_FINAL_TABLES_UNKNOWN_PRESERVED'} for t in expected}
    return events,coverage


def parse_report(body,source,roster,schedule,asof):
    validate_source(source)
    digest=hashlib.sha256(body).hexdigest()
    if digest!=source['sha256']:raise ValueError('Availability raw source hash mismatch')
    games=schedule[schedule.game_id.eq(source['game_id'])]
    if len(games)!=1:raise ValueError('Availability fixture missing or ambiguous')
    game=games.iloc[0];observed=timestamp(source['retrieved_at_utc'])
    if timestamp(asof)>=timestamp(game.kickoff_utc)-pd.Timedelta(minutes=1):raise ValueError('Availability fixture pregame cutoff passed')
    if observed>timestamp(asof):raise ValueError('Availability observation after cutoff')
    if timestamp(asof)-observed>pd.Timedelta(hours=36):raise ValueError('Availability observation is stale')
    fixture_teams={game.home_team,game.away_team}
    if not set(source['teams']).issubset(fixture_teams) or (source.get('kind','FINAL_REPORT')=='FINAL_REPORT' and fixture_teams!=set(source['teams'])):raise ValueError('Availability fixture teams mismatch')
    tree=html.fromstring(body);articles=[]
    for raw in tree.xpath('//script[@type="application/ld+json"]/text()'):
        item=json.loads(raw)
        articles.extend(a for a in (item if isinstance(item,list) else item.get('@graph',[item])) if a.get('@type')=='NewsArticle')
    articles=[a for a in articles if urlparse(a.get('url','')).hostname in {CLUB_HOSTS[source['team']],'www.'+CLUB_HOSTS[source['team']]}]
    if len(articles)!=1:raise ValueError('Official availability article identity missing or ambiguous')
    article=articles[0];published=timestamp(article['datePublished']);modified=timestamp(article.get('dateModified',article['datePublished']))
    if article.get('url')!=source['url']:raise ValueError('Availability canonical article URL mismatch')
    if not published<=modified<=observed:raise ValueError('Availability publication/version timestamp order')
    if int(source['season'])!=int(game.season) or int(source['week'])!=int(game.week):raise ValueError('Availability season/week mismatch')
    identity=str(article.get('headline',''))+' '+str(article.get('url',''))
    declared_weeks={int(w) for w in re.findall(r'week[ -]?(\d+)\b',identity,re.I)}
    if declared_weeks and declared_weeks!={int(game.week)}:raise ValueError('Article explicitly belongs to a different week')
    # Club headlines often name an injured player rather than the fixture.
    # A bounded opening paragraph may establish the same explicit week and
    # opponents; unrelated later history cannot supply fixture applicability.
    identity+=' '+str(article.get('articleBody',''))[:1200]
    week_verified=bool(re.search(r'week[ -]?'+str(int(game.week))+r'\b',identity,re.I))
    game_day=timestamp(game.kickoff_utc).tz_convert('America/New_York').date()
    inactive_day_verified=source.get('kind')=='INACTIVES' and published.tz_convert('America/New_York').date()==game_day and 'inactive' in identity.lower()
    from nfl2026.live.providers.club_practice import fixture_tag_matches,parse_practice_tables
    tagged_practice=source.get('kind')=='PRACTICE_REPORT' and fixture_tag_matches(article,game)
    if not (week_verified or inactive_day_verified or tagged_practice):raise ValueError('Article week/fixture day not verified')
    if published.year!=int(game.season):raise ValueError('Availability article season mismatch')
    # A report from the previous meeting or a previous week cannot clear a player.
    if not timestamp(game.kickoff_utc)-pd.Timedelta(days=7)<=published<timestamp(game.kickoff_utc):raise ValueError('Availability article not fixture-current')
    nicknames={code:name for name,code in TEAM_NAMES.items()}
    if not all(nicknames[t].lower() in identity.lower() for t in fixture_teams):raise ValueError('Article opponents not verified')
    if source.get('kind')=='PRACTICE_REPORT':
        return parse_practice_tables(tree,source,roster,game,article,published,modified,observed,asof)
    if source.get('kind','FINAL_REPORT')=='FINAL_REPORT':
        tables=_paired_final_tables(tree,source,roster,game,article,published,modified,observed,asof)
        if tables is not None:return tables
    roster=roster.copy();roster['identity_key']=roster.full_name.map(name_key)
    mapping={};errors=[];events=[];seen_teams=set();headers={};current_team=None;status=None;date_seen=None;final=False
    for team in source['teams']:
        # Full franchise names only, not a nickname substring inside prose.
        headers[nicknames[team].upper()]=team
    containers=tree.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," nfl-c-body-part--text ")]')
    if not containers:raise ValueError('Availability structured article body absent')
    kind=source.get('kind','FINAL_REPORT')
    for container in containers:
        nodes=[]
        for child in container:
            nodes.extend(list(child) if child.tag in {'ul','ol'} else [child])
        for node in nodes:
            text=' '.join(' '.join(node.itertext()).split());upper=text.upper()
            date_match=re.match(r'^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY), ',upper)
            if date_match:
                date=pd.to_datetime(text).date()
                if date_seen is not None and date!=date_seen:break
                date_seen=date
                if date!=(modified.tz_convert('America/New_York').date()):raise ValueError('Final report date/version mismatch')
                continue
            if date_seen is not None and re.match(r'^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY), ',upper):break
            header_team=next((code for name,code in headers.items() if node.tag in {'h2','h3','h4'} and (upper==name or upper.endswith(' '+name))),None)
            if header_team:current_team=header_team;seen_teams.add(current_team);status=None;continue
            if kind=='INACTIVES' and len(source['teams'])==1:current_team=source['teams'][0];seen_teams.add(current_team);status='INACTIVE'
            if not current_team:continue
            # The final block is bounded by its dated heading. Older practice blocks are ignored.
            if kind=='FINAL_REPORT' and date_seen is None:continue
            status_match=re.match(r'^(OUT|DOUBTFUL|QUESTIONABLE|NO GAME STATUS GIVEN)\b[:\s]*',upper)
            if status_match:
                label=status_match.group(1);status='NO_GAME_DESIGNATION' if label=='NO GAME STATUS GIVEN' else label
                text=text[status_match.end():];final=True
            if status is None:continue
            # Each named line includes an explicit football position. No fuzzy identity matching.
            pattern=r'\b(QB|RB|FB|WR|TE|OL|G|T|C|OT|OG|LB|DB|CB|S|DT|DE|DL|K|P|LS)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .\'’\-]+?)(?=\s+-\s|\s*\(|,|$)'
            for match in re.finditer(pattern,text):
                position,name=match.groups();matches=roster[roster.team.eq(current_team)&roster.identity_key.eq(name_key(name))]
                if len(matches)!=1:
                    errors.append({'team':current_team,'name':name.strip(),'position':position,'critical':position in {'QB','RB','FB','WR','TE'},'reason':'Exact roster identity not unique/found'})
                    continue
                player=matches.iloc[0]
                offense={'QB','RB','FB','WR','TE'}
                if position in offense and str(player.position) != position:raise ValueError('Offensive availability position mismatch')
                events.append({'game_id':game.game_id,'team':current_team,'player_id':str(player.gsis_id),'player_name':player.full_name,
                    'position':player.position,'status':status,'published_at_utc':published.isoformat(),'modified_at_utc':modified.isoformat(),
                    'observed_at_utc':observed.isoformat(),'known_at_utc':observed.isoformat(),'source_url':source['url'],
                    'canonical_url':article['url'],'source_sha256':digest,'explicit_clearance':status=='NO_GAME_DESIGNATION'})
        else:continue
        break
    if kind=='FINAL_REPORT' and (not final or seen_teams!=set(source['teams'])):raise ValueError('Complete final designation report not established')
    if kind=='INACTIVES' and not events:raise ValueError('No identifiable official inactive list')
    columns=['game_id','team','player_id','player_name','position','status','published_at_utc','modified_at_utc','observed_at_utc','known_at_utc','source_url','canonical_url','source_sha256','explicit_clearance']
    events=pd.DataFrame(events,columns=columns).drop_duplicates(['game_id','team','player_id','status'])
    events=validate_events(events,schedule,asof)
    coverage={(game.game_id,t):{'complete_final':kind=='FINAL_REPORT','critical_identity_errors':any(e['critical'] and e['team']==t for e in errors),
        'identity_warnings':[e for e in errors if e['team']==t],'source_url':source['url'],'source_sha256':digest,
        'published_at_utc':published.isoformat(),'modified_at_utc':modified.isoformat(),'observed_at_utc':observed.isoformat()} for t in seen_teams}
    return events,coverage


def load_sources(raw_dir,roster,schedule,asof):
    path=Path(raw_dir)/'availability_sources.json'
    if not path.exists():return pd.DataFrame(),{}
    frames=[];coverage={}
    for source in json.loads(path.read_text(encoding='utf-8'))['sources']:
        if source['game_id'] not in set(schedule.game_id):continue
        events,states=parse_report((Path(raw_dir)/source['filename']).read_bytes(),source,roster,schedule,asof)
        frames.append(events)
        for key,state in states.items():
            previous=coverage.get(key,{})
            if key not in coverage or state['complete_final'] or (
                not previous.get('complete_final') and state.get('complete_practice') and
                timestamp(state['observed_at_utc'])>=timestamp(previous.get('observed_at_utc',state['observed_at_utc']))):coverage[key]=state
            coverage[key]['critical_identity_errors']=bool(previous.get('critical_identity_errors') or state.get('critical_identity_errors'))
            coverage[key]['identity_warnings']=previous.get('identity_warnings',[])+state.get('identity_warnings',[])
    return (pd.concat(frames,ignore_index=True) if frames else pd.DataFrame()),coverage
