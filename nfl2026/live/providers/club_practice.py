"""Dated, fixture-identified club practice tables. Never final/active clearance."""
import re
import pandas as pd

from nfl2026.live.availability import validate_events, final_report_due
from nfl2026.live.providers.injuries import first_practice_report_due, normalise_game_status, normalise_practice
from nfl2026.live.providers.transactions import TEAM_NAMES
from nfl2026.live.providers.qb_evidence import TEAM_CITIES


def fixture_tag_matches(article, game):
    cities={**TEAM_CITIES,'LA':'Los Angeles','LAC':'Los Angeles','NYG':'New York','NYJ':'New York'}
    names={code:name for name,code in TEAM_NAMES.items()}
    expected=(f'{cities[game.away_team]} {names[game.away_team]} at '
              f'{cities[game.home_team]} {names[game.home_team]} ({int(game.season)}-REG-{int(game.week)})')
    tags=[str(k).strip() for k in article.get('keywords',[]) if re.search(r'\(20\d{2}-REG-\d+\)',str(k))]
    return bool(tags) and all(k.casefold()==expected.casefold() for k in tags)


def parse_practice_tables(tree, source, roster, game, article, published, modified, observed, asof):
    """Accept only exact fixture tags and explicit current practice columns.

    A missing game-status column/cell stays UNKNOWN. Full participation,
    report completeness and absence of a player never establish game-day activity.
    """
    if not fixture_tag_matches(article,game):raise ValueError('Practice report exact fixture tag missing/mismatched')
    teams=set(source['teams']);allowed={game.home_team,game.away_team}
    if not teams or not teams.issubset(allowed):raise ValueError('Practice report fixture teams mismatch')
    names={code:name for name,code in TEAM_NAMES.items()}
    cities={**TEAM_CITIES,'LA':'Los Angeles','LAC':'Los Angeles','NYG':'New York','NYJ':'New York'}
    aliases={t:{names[t].casefold(),(cities[t]+' '+names[t]).casefold()} for t in teams}
    key=lambda v:re.sub(r'[^a-z0-9]','',str(v).casefold())
    first=pd.Timestamp(first_practice_report_due(pd.Timestamp(game.kickoff_utc))).tz_convert('America/New_York').date()
    last=final_report_due(game.kickoff_utc).tz_convert('America/New_York').date()
    version=modified.tz_convert('America/New_York').date()
    dates={pd.Timestamp(d).strftime('%A').casefold():pd.Timestamp(d).date()
           for d in pd.date_range(first,last)}
    valid_practice={'DNP','LP','FP','DID NOT PARTICIPATE','LIMITED PARTICIPATION','FULL PARTICIPATION',
                    'DID NOT PARTICIPATE IN PRACTICE','LIMITED PARTICIPATION IN PRACTICE','FULL PARTICIPATION IN PRACTICE'}
    events=[];coverage={};seen=set()
    for table in tree.xpath('//table'):
        headers=table.xpath('./thead/tr/th|./tr/th')
        labels=[key(' '.join(h.itertext())) for h in headers]
        if not ('player' in labels or 'name' in labels):continue
        if len(labels)!=len(set(labels)):raise ValueError('Practice table columns duplicated')
        headings=table.xpath('preceding::*[self::h1 or self::h2 or self::h3 or self::h4]')
        heading=' '.join(' '.join(headings[-1].itertext()).split()).casefold() if headings else ''
        team=next((t for t in teams if heading in aliases[t]),None)
        if team is None:raise ValueError('Practice table team identity missing')
        if team in seen:raise ValueError('Practice table team duplicated')
        seen.add(team)
        rows=[[' '.join(' '.join(c.itertext()).split()) for c in row.xpath('./td')]
              for row in table.xpath('./tbody/tr|./tr[td]')]
        if not rows or any(len(r)!=len(labels) for r in rows):raise ValueError('Practice table rows empty/ambiguous')
        candidates=[]
        for day,date in dates.items():
            index=next((i for i,l in enumerate(labels) if l in {day,day[:3]}),None)
            if index is not None and date<=version and any(r[index].upper() in valid_practice for r in rows):
                candidates.append((date,index))
        if not candidates:raise ValueError('Practice table has no dated published practice observations')
        day,index=max(candidates)
        player_index=labels.index('player') if 'player' in labels else labels.index('name')
        position_index=labels.index('position') if 'position' in labels else None
        status_index=labels.index('gamestatus') if 'gamestatus' in labels else None
        errors=[]
        for cells in rows:
            name=cells[player_index];position=cells[position_index].upper() if position_index is not None else ''
            if position_index is None:
                match=re.fullmatch(r'(.+?),\s*([A-Z/]+)',name)
                if not match:raise ValueError('Practice table explicit player/position missing')
                name,position=match.groups()
            matched=roster[roster.team.eq(team)&roster.full_name.map(key).eq(key(name))]
            if len(matched)!=1 or pd.isna(matched.iloc[0].gsis_id):
                errors.append({'name':name,'position':position,'reason':'Exact team/player identity unresolved','critical':True});continue
            p=matched.iloc[0]
            depth=str(p.get('depth_chart_position',''))
            # Preserve the source taxonomy; an OL composite must include the
            # roster's own position. EDGE/DL or DE/LB conflicts are not overridden.
            compatible=position==str(p.position) or position==depth or (
                str(p.position)=='OL' and depth in position.split('/'))
            if not compatible:
                from nfl2026.live.providers.official_roster_identity import corroborated_edge_role
                compatible=corroborated_edge_role(p,position,asof)
            if not compatible:
                errors.append({'name':name,'position':position,'roster_position':str(p.position),'depth_position':depth,'reason':'Position conflict','critical':True});continue
            original=cells[status_index] if status_index is not None else ''
            if original.upper() not in {'','-','–','—','(-)','UNKNOWN','QUESTIONABLE','DOUBTFUL','OUT','IR','PUP','INACTIVE','INACT'}:
                raise ValueError('Practice table unsupported game designation')
            practice=cells[index].upper()
            if practice not in valid_practice|{'','-','–','—','(-)'}:raise ValueError('Practice table unsupported participation')
            events.append({'game_id':game.game_id,'team':team,'player_id':str(p.gsis_id),'player_name':p.full_name,'position':p.position,
                'status':normalise_game_status(original),'original_game_designation':original,'source_position':position,'roster_position':p.position,'depth_position':depth,
                'official_roster_identity_proof':p.get('official_roster_identity_proof',''),'practice_status_normalized':normalise_practice(practice),
                'practice_date':day.isoformat(),'practice_estimated':'estimat' in str(article.get('articleBody','')).casefold(),
                'report_stage':'PRACTICE','published_at_utc':published.isoformat(),'modified_at_utc':modified.isoformat(),
                'observed_at_utc':observed.isoformat(),'known_at_utc':observed.isoformat(),'source_url':source['url'],
                'canonical_url':article['url'],'source_sha256':source['sha256'],'explicit_clearance':False})
        coverage[game.game_id,team]={'complete_practice':True,'complete_final':False,'allow_absent_clearance':False,
            'critical_identity_errors':bool(errors),'identity_warnings':errors,'source_url':source['url'],'source_sha256':source['sha256'],
            'published_at_utc':published.isoformat(),'modified_at_utc':modified.isoformat(),'observed_at_utc':observed.isoformat(),
            'practice_date':day.isoformat(),'format':'DATED_PRACTICE_TABLE_NO_ACTIVITY_CLEARANCE'}
    if seen!=teams:raise ValueError('Required practice team tables incomplete')
    frame=pd.DataFrame(events)
    if frame.empty or frame.duplicated(['game_id','team','player_id']).any():raise ValueError('Practice identities missing/duplicated')
    return validate_events(frame,pd.DataFrame([game]),asof),coverage
