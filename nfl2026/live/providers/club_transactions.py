"""Structured club history supplements incomplete league pagination, with raw receipts."""
from datetime import datetime, timezone
import hashlib,json,re
from pathlib import Path

from lxml import html
import pandas as pd

from nfl2026.live.providers.qb_evidence import validate_source
from nfl2026.live.providers.transaction_state import classify,key

POSITIONS=r'QB|RB|FB|WR|TE|OLB|ILB|LB|DB|CB|S|LS|K|P|DL|DT|DE|NT|OT|OG|T|G|C|OL'

def capture_sources(settings,season,destination,retrieved_at,fetch):
    catalog=settings.root/'config/transaction_evidence_sources.json'
    if not catalog.exists():return
    receipts=[]
    for source in json.loads(catalog.read_text(encoding='utf-8'))['sources']:
        if int(source['season'])!=season:continue
        routes=([('history',source['history_url'])] if source.get('history_url') else [])+[('article',u) for u in source['articles']]
        if not routes:raise ValueError('Club transaction source has no configured history or article route')
        for kind,url in routes:
            validate_source({'team':source['team'],'url':url})
            body=fetch(url)
            name='club_transactions_'+hashlib.sha256(url.encode()).hexdigest()[:16]+'.html'
            (destination/name).write_bytes(body)
            receipts.append({**source,'kind':kind,'url':url,'filename':name,
                'sha256':hashlib.sha256(body).hexdigest(),'retrieved_at_utc':datetime.now(timezone.utc).isoformat()})
    (destination/'club_transaction_sources.json').write_text(json.dumps({'sources':receipts},indent=2,ensure_ascii=True),encoding='utf-8')

def _base(source,date,detail,name,position,state,group,index,published=''):
    team=source['team']
    return {'event_id':'club:'+hashlib.sha256((source['url']+date+detail+name).encode()).hexdigest()[:24],
        'season':int(source['season']),'event_at_utc':date,'effective_date':date,'effective_precision':'DATE',
        'event_type':'OFFICIAL_TRANSACTION','transaction_state':state,
        'from_team':team if state not in {'ACTIVE'} else '',
        'to_team':team if state in {'ACTIVE','PRACTICE_SQUAD','IR','RETURN_DESIGNATED','ELEVATED'} else '',
        'player_id':'','pfr_id':'','player_name':name.strip(' .,;'),'position':position,
        'transaction_detail':detail,'transaction_category':'club_history',
        'known_at_utc':published or source['retrieved_at_utc'],'retrieved_at_utc':source['retrieved_at_utc'],
        'published_at_utc':published,'source':'official club '+team,'source_state':'VALID',
        'source_url':source['url'],'source_sha256':source['sha256'],'source_file':source['filename'],
        'sequence_group':group,'sequence_index':index,'valid_from_date':date,'valid_to_date':''}

def parse_history(body,source,cutoff):
    validate_source({'team':source['team'],'url':source['url']})
    if hashlib.sha256(body).hexdigest()!=source['sha256']:raise ValueError('Club transaction history hash differs')
    tree=html.fromstring(body.decode('utf-8'))
    canonical=tree.xpath('//link[@rel="canonical"]/@href')
    years=tree.xpath('//select/option[@selected]/text()')
    if canonical!=[source['url']] or str(source['season']) not in {y.strip() for y in years}:
        raise ValueError('Club transaction history canonical identity/season not verified')
    records=[];rows=tree.xpath('//tr[td[contains(@class,"transactions-report__date")]]')
    if not rows:raise ValueError('Club transaction history table missing')
    for cell_index,row in enumerate(rows):
        date_text=row.xpath('./td')[0].text_content().strip()
        month,day=map(int,date_text.split('/'))
        if month!=pd.Timestamp(cutoff).month:continue
        date=f"{int(source['season'])}-{month:02d}-{day:02d}"
        if pd.Timestamp(date).date()>pd.Timestamp(cutoff).date():continue
        detail=' '.join(row.xpath('./td')[1].itertext())
        detail=re.sub(r'\s+',' ',detail).strip()
        # Split on a new explicit action, keeping Jr. and multi-part names intact.
        actions=r'Sign(?:ed)?|Re-sign(?:ed)?|Promot(?:e|ed)|Plac(?:e|ed)|Activat(?:e|ed)|Releas(?:e|ed)|Waiv(?:e|ed)|Elevat(?:e|ed)|Designat(?:e|ed)|Reinstat(?:e|ed)'
        parts=re.split(r'(?<=[.,;])\s+(?=(?:'+actions+r')\b)',detail,flags=re.I)
        for sequence,part in enumerate(parts):
            # Classify each action, not the whole multi-action table cell.
            verbs={'sign':'signed','re-sign':'re-signed','release':'released','waive':'waived','promote':'promoted','activate':'activated','elevate':'elevated','place':'placed','revert':'reverted'}
            normalized=re.sub(r'\b(re-sign|sign|release|waive|promote|activate|elevate|place|revert)\b',lambda m:verbs[m[0].lower()],part,flags=re.I)
            state=classify(normalized)
            if re.search(r'\bopen\b.*\bpractice window\b',part,re.I):state='RETURN_DESIGNATED'
            if re.search(r'\bawarded\b.*\boff waivers\b',part,re.I):state='ACTIVE'
            trade=re.search(r'\btrade(?:d)?\s+('+POSITIONS+r')\s+(.+?)(?:\s+and\s+\d{4}.*?)?\s+to\s+(.+?)\s+for\b',part,re.I)
            if trade:
                from nfl2026.live.providers.transactions import TEAM_NAMES
                destinations={code for name,code in TEAM_NAMES.items() if re.search(r'\b'+re.escape(name)+r'\b',trade[3],re.I)}
                if len(destinations)!=1:raise ValueError('Club trade destination ambiguous')
                record=_base(source,date,part,trade[2],trade[1].upper(),'TEAM_CHANGE',source['sha256']+':'+str(cell_index),sequence)
                record['to_team']=next(iter(destinations));records.append(record);continue
            subject=re.compile(r'\b('+POSITIONS+r')\s+(.+?)(?=\s*,\s*(?:and\s+)?(?:'+POSITIONS+r')\s+|\s+and\s+(?:'+POSITIONS+r')\s+|\s+(?:to|from|on|as|for|off waivers|revert(?:ed|s)?|returned)\b|\s*\(|[.]?$)',re.I)
            subjects=list(subject.finditer(part))
            if not subjects:raise ValueError('Unparsed official club transaction statement: '+part)
            for match in subjects:
                records.append(_base(source,date,part,match[2],match[1].upper(),state,
                    source['sha256']+':'+str(cell_index),sequence))
    return records

def parse_article(body,source,cutoff):
    validate_source({'team':source['team'],'url':source['url']})
    if hashlib.sha256(body).hexdigest()!=source['sha256']:raise ValueError('Club article hash differs')
    tree=html.fromstring(body.decode('utf-8'))
    articles=[json.loads(t) for t in tree.xpath('//script[@type="application/ld+json"]/text()')]
    articles=[a for a in articles if a.get('@type')=='NewsArticle' and a.get('url')==source['url']]
    if len(articles)!=1:raise ValueError('Club transaction article identity unverified')
    article=articles[0];published=pd.Timestamp(article['datePublished']);modified=pd.Timestamp(article.get('dateModified',article['datePublished']))
    observed=pd.Timestamp(source['retrieved_at_utc']);asof=pd.Timestamp(cutoff)
    season=int(source['season'])
    season_calendar=published.year==season or (published.year==season+1 and published.month<=2)
    if (published.tzinfo is None or modified.tzinfo is None or observed.tzinfo is None or asof.tzinfo is None
        or not published<=modified<=observed<=asof or not season_calendar):
        raise ValueError('Club transaction article publication time invalid/future')
    text=re.sub(r'\s+',' ',article['articleBody'])
    # Only explicit permanent active-roster additions become article events.
    patterns=[r'promoted (?:wide receiver|running back|quarterback|tight end|WR|RB|QB|TE) (.+?) from the practice squad to the active roster',
              r'(?:signing|signed) (?:wide receiver|running back|quarterback|tight end|WR|RB|QB|TE) (.+?) (?:back )?to the 53-man roster']
    records=[]
    for pattern in patterns:
        for match in re.finditer(pattern,text,re.I):
            date=published.tz_convert(source['timezone']).date().isoformat()
            records.append(_base(source,date,match[0],match[1],'','ACTIVE','',0,modified.isoformat()))
    # A definite own-club IR headline can bind an immediately corroborating
    # body sentence whose subject is the same unique position. Neither the
    # headline alone nor future/conditional prose establishes a transaction.
    from nfl2026.live.providers.transactions import TEAM_NAMES
    nickname=next(name for name,team in TEAM_NAMES.items() if team==source['team'])
    role=r'(right tackle|left tackle|quarterback|running back|wide receiver|tight end)'
    headline=re.fullmatch(re.escape(nickname)+r' place(?:s|d)? '+role+r' (.+?) on injured reserve',str(article.get('headline','')),re.I)
    if headline:
        position,name=headline.groups();opening=text[:650]
        stated=re.search(r'\b'+re.escape(nickname)+r' placed (?:the veteran |the )?'+re.escape(position)+r' on injured reserve\b',opening,re.I)
        first_sentence=re.split(r'[.!?](?:\s|$)',opening,maxsplit=1)[0]
        if stated and re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',first_sentence) and not re.search(r'\b(?:will|would|could|may|might|plan(?:s|ned)? to|expected to)\b',stated[0],re.I):
            positions={'right tackle':'OT','left tackle':'OT','quarterback':'QB','running back':'RB','wide receiver':'WR','tight end':'TE'}
            date=published.tz_convert(source['timezone']).date().isoformat()
            records.append(_base(source,date,headline[0]+'; '+stated[0],name,positions[position.lower()],'IR','',0,modified.isoformat()))
    if not records:raise ValueError('Configured club article has no supported definite roster action')
    return records

def load_sources(raw_dir,season,cutoff):
    path=Path(raw_dir)/'club_transaction_sources.json'
    if not path.exists():return pd.DataFrame()
    records=[]
    for source in json.loads(path.read_text(encoding='utf-8'))['sources']:
        if int(source['season'])!=season:continue
        if Path(source['filename']).name!=source['filename']:raise ValueError('Invalid club transaction archive path')
        if pd.Timestamp(source['retrieved_at_utc'])>pd.Timestamp(cutoff):raise ValueError('Club source observed after cutoff')
        body=(Path(raw_dir)/source['filename']).read_bytes()
        records.extend((parse_history if source['kind']=='history' else parse_article)(body,source,cutoff))
    return pd.DataFrame(records)
