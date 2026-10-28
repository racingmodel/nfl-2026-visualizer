"""Exact current official roster corroboration; never game-day health clearance."""
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urljoin,urlparse
import hashlib,json,unicodedata,re
from lxml import html
import pandas as pd
from nfl2026.live.providers.qb_evidence import validate_source

def name(value):return ' '.join(unicodedata.normalize('NFC',str(value)).casefold().split())

def capture_sources(settings,season,destination,retrieved_at,fetch,*,week=None):
    catalog=settings.root/'config/official_roster_sources.json'
    if not catalog.exists():return
    receipts=[]
    for source in json.loads(catalog.read_text(encoding='utf-8'))['sources']:
        if int(source['season'])!=season or week is not None and int(source['week'])!=week:continue
        validate_source(source);body=fetch(source['url']);observed=datetime.now(timezone.utc).isoformat();filename='official_roster_'+source['team']+'.html'
        (destination/filename).write_bytes(body);receipts.append(dict(**source,filename=filename,sha256=hashlib.sha256(body).hexdigest(),retrieved_at_utc=observed))
    (destination/'official_roster_sources.json').write_text(json.dumps(dict(sources=receipts),indent=2,ensure_ascii=True),encoding='utf-8')

def parse_roster(body,source,roster,asof):
    validate_source(source)
    if hashlib.sha256(body).hexdigest()!=source['sha256']:raise ValueError('Official roster raw hash differs')
    observed=pd.Timestamp(source['retrieved_at_utc']);now=pd.Timestamp(asof)
    if observed.tzinfo is None or now.tzinfo is None or not observed<=now or now-observed>pd.Timedelta(hours=36):raise ValueError('Official roster clock is future/stale/naive')
    tree=html.fromstring(body.decode('utf-8'))
    canonical=tree.xpath('//link[@rel="canonical"]/@href')
    if canonical!=[source['canonical_url']] or urlparse(source['canonical_url']).hostname!=urlparse(source['url']).hostname:raise ValueError('Official roster canonical identity differs')
    expected_title={'GB':'Green Bay Packers','CAR':'Carolina Panthers','ARI':'Arizona Cardinals','DAL':'Dallas Cowboys','CIN':'Cincinnati Bengals'}
    heading=' '.join(tree.xpath('//h1//text()')).casefold();title=' '.join(tree.xpath('//title/text()')).casefold()
    if source['team'] not in expected_title:raise ValueError('Official roster team identity differs')
    franchise=expected_title[source['team']].casefold();nickname=franchise.split()[-1]
    valid_team=franchise in heading or (franchise in title and nickname in heading)
    foreign_team=any(value.casefold() in heading for code,value in expected_title.items() if code!=source['team'])
    declared_years={int(y) for y in re.findall(r'\b20\d{2}\b',heading)}
    if not valid_team or foreign_team or declared_years and declared_years!={int(source['season'])}:raise ValueError('Official roster team/season identity differs')
    current=roster[roster.team.eq(source['team'])&roster.season.eq(int(source['season']))&roster.week.eq(int(source['week']))];records=[];seen=set()
    states={'Active':'ACT','Reserve/Injured':'IR','Reserve/Injured; Designated for Return':'IR','Practice Squad':'DEV','Reserve/Physically Unable to Perform':'PUP','Cut':'RELEASED'}
    for table in tree.xpath('//table'):
        caption=' '.join(table.xpath('./caption//text()')).strip()
        if caption not in states:continue
        for row in table.xpath('./tbody/tr|./tr[td]'):
            cells=row.xpath('./td');nodes=row.xpath('.//span[@data-name]/a')
            if len(cells)<3 or len(nodes)!=1:continue
            full=' '.join(nodes[0].itertext()).strip();matches=current[current.full_name.map(name).eq(name(full))];alias_proof='EXACT_FULL_NAME'
            if len(matches)==0:
                # A display suffix is accepted only with the SAME club's explicit
                # canonical profile slug, structured data-name and exact jersey.
                suffix=re.fullmatch(r'(.+?)\s+(Jr\.?|Sr\.?|II|III|IV)',full,re.I)
                profile=urljoin(source['url'],nodes[0].get('href',''))
                if suffix:
                    candidates=current[current.full_name.map(name).eq(name(suffix[1]))]
                    if len(candidates)==1:
                        candidate=candidates.iloc[0];slug=re.sub(r'[^a-z0-9]+','-',name(candidate.full_name)).strip('-')
                        data_name=name(nodes[0].getparent().get('data-name','')).rstrip('.')
                        expected=name(str(candidate.get('last_name',''))+','+str(candidate.get('first_name',''))+' '+suffix[2]).rstrip('.')
                        if urlparse(profile).path.rstrip('/').endswith('/'+slug) and data_name==expected and expected!=',':
                            matches=candidates;alias_proof='OFFICIAL_DISPLAY_SUFFIX_EXACT_PROFILE_DATA_NAME_AND_JERSEY'
            if len(matches)!=1:continue
            p=matches.iloc[0]
            try:jersey=int(' '.join(cells[1].itertext()).strip());weekly=int(float(p.jersey_number))
            except (ValueError,TypeError):continue
            if jersey!=weekly:continue
            profile=urljoin(source['url'],nodes[0].get('href',''));position=' '.join(cells[2].itertext()).strip().upper()
            if alias_proof!='EXACT_FULL_NAME' and position not in {str(p.position),str(p.get('depth_chart_position',''))}:continue
            if urlparse(profile).hostname!=urlparse(source['url']).hostname or '/team/players-roster/' not in urlparse(profile).path:raise ValueError('Official player profile host/path differs')
            if p.gsis_id in seen:raise ValueError('Duplicate official roster identity/status')
            seen.add(p.gsis_id);records.append(dict(player_id=p.gsis_id,team=source['team'],player_name=p.full_name,official_display_name=full,identity_method=alias_proof,jersey=jersey,official_position=position,official_roster_status=states[caption],
                section=caption,profile_url=profile,data_name=nodes[0].getparent().get('data-name'),source_url=source['url'],source_sha256=source['sha256'],observed_at_utc=observed.isoformat(),published_at_utc='UNKNOWN'))
    if not records:raise ValueError('No exact official roster identities')
    return records

def merge_sources(raw_dir,roster,asof):
    path=Path(raw_dir)/'official_roster_sources.json';result=roster.copy()
    if not path.exists():return result
    for source in json.loads(path.read_text(encoding='utf-8'))['sources']:
        scope=result.team.eq(source['team'])&result.season.eq(int(source['season']))&result.week.eq(int(source['week']))
        if not scope.any():continue # Another fixture/week cannot supply this roster's identity proof.
        if Path(source['filename']).name!=source['filename']:raise ValueError('Official roster path unsafe')
        for proof in parse_roster((Path(raw_dir)/source['filename']).read_bytes(),source,result,asof):
            mask=result.team.eq(proof['team'])&result.gsis_id.eq(proof['player_id'])
            result.loc[mask,'official_roster_identity_proof']=json.dumps(proof,ensure_ascii=True)
    return result

def proof_for(player,asof):
    raw=player.get('official_roster_identity_proof')
    if not isinstance(raw,str) or not raw:return None
    p=json.loads(raw);observed=pd.Timestamp(p['observed_at_utc']);now=pd.Timestamp(asof)
    if not (p['player_id']==player.gsis_id and p['team']==player.team and name(p['player_name'])==name(player.full_name) and p['jersey']==int(float(player.jersey_number))):raise ValueError('Official roster proof/player binding differs')
    if observed.tzinfo is None or not observed<=now or now-observed>pd.Timedelta(hours=36):raise ValueError('Official roster proof clock differs')
    return p

def corroborated_edge_role(player,source_position,asof):
    p=proof_for(player,asof)
    # No generic EDGE->LB/DL conversion. Require the same explicitly identified
    # official EDGE, structured LB and specific OLB depth role; retain all labels.
    return bool(p and source_position=='EDGE' and p['official_position']=='EDGE' and str(player.position)=='LB' and str(player.depth_chart_position)=='OLB')

def reconcile_explicit_negative_roster(roster,asof):
    result=roster.copy()
    for i,player in result.iterrows():
        error=str(player.get('transaction_reconciliation_error',''))
        state=player.get('current_transaction_state')
        if error not in {'Active roster conflicts with latest inactive transaction','Unordered conflicting same-day official transactions'}:continue
        proof=proof_for(player,asof)
        if not proof:continue
        negative_agrees=error=='Active roster conflicts with latest inactive transaction' and ((state=='IR' and proof['official_roster_status']=='IR') or (state=='RELEASED' and proof['official_roster_status']=='RELEASED'))
        squad_corroborated=False
        if state=='CONFLICT' and proof['official_roster_status']=='DEV':
            evidence=json.loads(player.get('current_transaction_evidence') or '[]')
            valid=True;now=pd.Timestamp(asof)
            for event in evidence:
                when=pd.to_datetime(event.get('event_at_utc'),utc=True,errors='coerce');known=pd.to_datetime(event.get('known_at_utc'),utc=True,errors='coerce');observed=pd.to_datetime(event.get('retrieved_at_utc'),utc=True,errors='coerce')
                if any(pd.isna(t) for t in [when,known,observed]) or not when<=known<=observed<=now or event.get('source_state','VALID')!='VALID':valid=False;break
                if name(event.get('player_name',''))!=name(player.full_name) or event.get('player_id') and str(event['player_id'])!=str(player.gsis_id):valid=False;break
                if (event.get('to_team') or event.get('from_team'))!=player.team:valid=False;break
            dates=[pd.Timestamp(e['event_at_utc']).date() for e in evidence] if valid else []
            last=max(dates) if dates else None
            latest=[e for e in evidence if pd.Timestamp(e['event_at_utc']).date()==last] if valid else []
            squad_corroborated=bool(valid and latest and {e.get('transaction_state') for e in latest}.issubset({'RELEASED','PRACTICE_SQUAD'}) and any(e.get('transaction_state')=='PRACTICE_SQUAD' and str(e.get('source',''))=='official club '+player.team for e in latest))
        if negative_agrees or squad_corroborated:
            result.loc[i,'transaction_discrepancy_history']=error
            result.loc[i,'prior_transaction_state_history']=state
            result.loc[i,'transaction_reconciliation_error']=''
            result.loc[i,'current_official_status_observed_at_utc']=proof['observed_at_utc']
            if squad_corroborated:
                result.loc[i,'current_transaction_state']='PRACTICE_SQUAD'
                result.loc[i,'transaction_resolution']='CURRENT_OFFICIAL_PS_AND_DATED_CLUB_PS;HISTORICAL_RELEASE_PS_ORDER_REMAINS_UNPROVEN;UNAVAILABLE'
            else:result.loc[i,'transaction_resolution']='DATED_NEGATIVE_TRANSACTION_AND_CURRENT_OFFICIAL_NEGATIVE_ROSTER;WEEKLY_ACT_RETAINED_AS_SOURCE_HISTORY;UNAVAILABLE'
    return result
