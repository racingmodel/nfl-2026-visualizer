"""Strict supported own-heading/immediate-list format; prose is quarantined."""
import re,json,unicodedata
from urllib.parse import urlparse
from .core import utc,sha

def extract(body,source,roster,game,now):
    from lxml import html
    import hashlib
    from nfl2026.live.providers.qb_evidence import CLUB_HOSTS
    from nfl2026.live.providers.official_injuries import TEAM_TITLES
    nickname={v:k for k,v in TEAM_TITLES.items()};team=source['team'];own=nickname[team];other=nickname[game['home_team'] if game['away_team']==team else game['away_team']]
    if team not in {game['home_team'],game['away_team']} or int(source['season'])!=int(game['season']) or int(source['week'])!=int(game['week']):raise ValueError('Wrong inactive fixture/team/season/week')
    if source['sha256']!=hashlib.sha256(body).hexdigest() or urlparse(source['url']).hostname not in {CLUB_HOSTS[team],'www.'+CLUB_HOSTS[team]}:raise ValueError('Own source identity/hash differs')
    tree=html.fromstring(body);articles=[]
    for raw in tree.xpath('//script[@type="application/ld+json"]/text()'):
        d=json.loads(raw);articles.extend(a for a in (d if isinstance(d,list) else d.get('@graph',[d])) if a.get('@type')=='NewsArticle')
    article=[a for a in articles if a.get('url')==source['url']]
    if len(article)!=1:raise ValueError('Unique official article metadata required')
    a=article[0];published=utc(a['datePublished']);modified=utc(a.get('dateModified') or published);observed=utc(source['retrieved_at_utc']);cutoff=utc(game['kickoff_utc'])
    if not published<=modified<=observed<=utc(now) or not 0<(cutoff-published).total_seconds()<=86400 or utc(now).timestamp()>=cutoff.timestamp()-60:raise ValueError('Inactive article date/version outside exact game day')
    title=a.get('headline','')+' '+source['url'];weeks=set(int(w) for w in re.findall(r'week[- ]?(\d+)\b',title,re.I))
    if weeks!={int(game['week'])} or not all(n.lower() in title.lower() for n in [own,other]):raise ValueError('Inactive fixture/week/opponents not explicit')
    # Deliberately narrow supported forms; richer headings require review.
    allowed={own.casefold(),own.casefold()+' inactives'}
    headings=[h for h in tree.xpath('//h2|//h3') if ' '.join(' '.join(h.itertext()).split()).casefold() in allowed]
    lists=[]
    for h in headings:
        node=h.getnext()
        if node is not None and node.tag in {'ul','ol'}:lists.append(node)
    if len(lists)!=1:raise ValueError('Supported exact own heading and immediate inactive list absent/ambiguous')
    rows=[]
    for li in lists[0].xpath('./li'):
        text=' '.join(' '.join(li.itertext()).split());emergency='emergency' in text.lower();text=re.sub(r'\s*\([^)]*emergency[^)]*\)','',text,flags=re.I)
        m=re.fullmatch(r'(QB|RB|FB|WR|TE|OT|T|G|C|OL|DE|DT|DL|LB|ILB|OLB|EDGE|CB|DB|S|FS|SS|K|P|LS)\s+(.+?)(?:\s+#(\d{1,2}))?',text)
        if not m:raise ValueError('Unsupported inactive row; no prose/fuzzy extraction')
        pos,name,jersey=m.groups();name=unicodedata.normalize('NFC',name);match=roster[roster.team.eq(team)&roster.full_name.map(lambda x:unicodedata.normalize('NFC',x)).eq(name)]
        if len(match)!=1 or str(match.iloc[0].position)!=pos or (jersey and int(float(match.iloc[0].jersey_number))!=int(jersey)):raise ValueError('Exact inactive roster name/position/jersey unresolved')
        r=match.iloc[0];rows.append(dict(type='AVAILABILITY',game_id=game['game_id'],team=team,player_id=str(r.gsis_id),name=name,status='INACTIVE',role='EMERGENCY_THIRD_QB' if emergency else 'INACTIVE',source_url=source['url'],source_sha256=source['sha256'],published_at_utc=published.isoformat(),modified_at_utc=modified.isoformat(),observed_at_utc=observed.isoformat(),validation='EXACT_SOURCE_IDENTITY_VALIDATED',strict_own_list_certificate=True))
    if not rows or len({r['player_id'] for r in rows})!=len(rows):raise ValueError('Empty/duplicate inactive list')
    rows.append(dict(type='OWN_LIST',game_id=game['game_id'],team=team,source_url=source['url'],source_sha256=source['sha256'],published_at_utc=published.isoformat(),modified_at_utc=modified.isoformat(),observed_at_utc=observed.isoformat(),validation='EXACT_SOURCE_IDENTITY_VALIDATED',strict_own_list_certificate=True,negative_list_only=True))
    return rows
