"""Source-backed QB expectations and explicit historical certification failures.

No postgame starter field is read. Weekly-only records cannot acquire invented
publication clocks. A reconstructed expectation is distinct from certified
pre-kickoff roster eligibility and a confirmed starting assignment.
"""
from pathlib import Path
import hashlib,json,unicodedata
import numpy as np
import pandas as pd

from nfl2026.features.qb_identity_archive import IDENTITIES,valid_gsis


def utc_evidence(value):
    if value is None or pd.isna(value):return None
    t=pd.Timestamp(value)
    if pd.isna(t) or t.tzinfo is None:return None
    return t.tz_convert('UTC')


def reconstruct_expected_identities(historical,depth_sources,roster_sources,provenance,*,max_age_hours=36):
    required={'game_id','season','week','kickoff_utc','home_team','away_team'}
    if not required.issubset(historical) or historical.game_id.duplicated().any() or not historical.index.is_unique:
        raise ValueError('Historical fixture schema/unique game_id required')
    if any(c in historical for c in IDENTITIES):raise ValueError('Reconstruction requires a separate identity-free archive; never overwrite existing identities')
    result=historical.copy(deep=True)
    for c in IDENTITIES:result[c]=pd.Series(pd.NA,index=result.index,dtype='string')
    timed={}
    for year,frame in depth_sources.items():
        if {'dt','pos_abb','team'}.issubset(frame):
            q=frame[frame.pos_abb.eq('QB')].copy();q['_known']=q.dt.map(utc_evidence)
            timed[year]={team:part for team,part in q.groupby('team')}
    audit=[]
    for index,game in historical.iterrows():
        kick=utc_evidence(game.kickoff_utc)
        if kick is None:raise ValueError('Historical kickoff requires an explicit timezone')
        cutoff=kick-pd.Timedelta(minutes=1);year=int(game.season)
        for side,opposite in [('home','away'),('away','home')]:
            team=str(game[side+'_team']);opponent=str(game[opposite+'_team'])
            record=dict(game_id=str(game.game_id),season=year,week=int(game.week),side=side,team=team,opponent=opponent,
                kickoff_utc=kick.isoformat(),cutoff_utc=cutoff.isoformat(),expectation_reconstructed=False,
                official_certified=False,confirmed_starter=False,player_id=None,player_name=None,depth_known_at_utc=None,
                roster_known_at_utc=None,roster_status=None,expected_role_changed=None,replacement_qb=None,
                source_provenance=provenance.get(year,{}))
            depth=depth_sources.get(year,pd.DataFrame())
            roster=roster_sources.get(year,pd.DataFrame())
            if depth.empty:
                record['reason']='DEPTH_SOURCE_MISSING';audit.append(record);continue
            if 'dt' not in depth:
                record['reason']='WEEKLY_DEPTH_PUBLICATION_TIME_UNPROVEN';audit.append(record);continue
            required_depth={'team','pos_abb','pos_rank','gsis_id','player_name','dt'}
            if not required_depth.issubset(depth):
                record['reason']='TIMESTAMPED_DEPTH_SCHEMA_UNSUPPORTED';audit.append(record);continue
            selected=timed.get(year,{}).get(team,pd.DataFrame(columns=[*depth.columns,'_known']))
            selected=selected[selected._known.notna() & (pd.to_datetime(selected._known,utc=True)<cutoff)]
            if selected.empty:
                record['reason']='NO_PRE_CUTOFF_QB_SNAPSHOT';audit.append(record);continue
            latest=max(selected._known);selected=selected[selected._known.eq(latest)]
            if (cutoff-latest).total_seconds()>max_age_hours*3600:
                record.update(reason='QB_SNAPSHOT_STALE',depth_known_at_utc=latest.isoformat());audit.append(record);continue
            selected=selected[pd.to_numeric(selected.pos_rank,errors='coerce').eq(1)]
            if len(selected)!=1:
                record['reason']='QB1_SNAPSHOT_AMBIGUOUS_OR_ABSENT';audit.append(record);continue
            q=selected.iloc[0]
            if 'game_id' in selected and str(q.game_id)!=str(game.game_id):
                record['reason']='DEPTH_FIXTURE_JOIN_CONFLICT';audit.append(record);continue
            if not valid_gsis(q.gsis_id) or pd.isna(q.player_name) or not str(q.player_name).strip():
                record['reason']='INVALID_SOURCE_QB_IDENTITY';audit.append(record);continue
            record.update(player_id=q.gsis_id,player_name=q.player_name,depth_known_at_utc=latest.isoformat())
            need={'season','week','team','gsis_id','full_name','position','status'}
            if not need.issubset(roster):
                record['reason']='ROSTER_SCHEMA_OR_SOURCE_MISSING';audit.append(record);continue
            candidates=roster[pd.to_numeric(roster.season,errors='coerce').eq(year)&pd.to_numeric(roster.week,errors='coerce').eq(int(game.week))&roster.team.eq(team)&roster.gsis_id.eq(q.gsis_id)]
            if len(candidates)!=1:
                record['reason']='EXACT_ROSTER_JOIN_MISSING_OR_AMBIGUOUS';audit.append(record);continue
            r=candidates.iloc[0];record['roster_status']=str(r.status)
            name_key=lambda v:''.join(c for c in unicodedata.normalize('NFKC',str(v)).casefold() if c.isalnum())
            if name_key(q.player_name)!=name_key(r.full_name):
                record['reason']='EXACT_ROSTER_NAME_CONTRADICTION';audit.append(record);continue
            if r.position!='QB' or str(r.status).upper()!='ACT':
                record['reason']='ROSTER_POSITION_OR_ELIGIBILITY_CONTRADICTION';audit.append(record);continue
            # Only these six expectation metadata fields may be added. None of
            # the learned ratings/replacement flags is rewritten.
            result.loc[index,side+'_expected_qb_id']=q.gsis_id
            result.loc[index,side+'_expected_qb_name']=str(q.player_name)
            # Preserve the original builder's documented source-method label.
            # It is not a calibrated probability or a claim of roster clearance.
            result.loc[index,side+'_qb_source_reliability']='EXACT_POINT_IN_TIME'
            record['expectation_reconstructed']=True
            roster_clock=utc_evidence(r.get('known_at_utc'))
            record['roster_known_at_utc']=roster_clock.isoformat() if roster_clock is not None else None
            if roster_clock is None:
                record['reason']='EXPECTED_ROLE_ONLY_ROSTER_PUBLICATION_TIME_UNPROVEN'
            elif roster_clock>=cutoff:
                record['reason']='EXPECTED_ROLE_ONLY_ROSTER_KNOWN_AFTER_CUTOFF'
            else:
                record.update(official_certified=True,reason='DATED_EXPECTATION_AND_ACTIVE_ROSTER_BEFORE_CUTOFF')
            audit.append(record)
    audit=pd.DataFrame(audit)
    for team,part in audit.sort_values(['kickoff_utc','game_id','side']).groupby('team',sort=False):
        previous=None
        for idx,row in part.iterrows():
            if previous is not None and row.expectation_reconstructed and previous.expectation_reconstructed:
                changed=row.player_id!=previous.player_id
                audit.at[idx,'expected_role_changed']=bool(changed)
                if row.official_certified and previous.official_certified:audit.at[idx,'replacement_qb']=bool(changed)
            previous=row
    pd.testing.assert_frame_equal(result[list(historical.columns)],historical)
    return result,audit


def validate_identity_certificate(historical,archive_path,model_sha256,root):
    """Production cannot accept a six-column backfill without its row evidence."""
    path=Path(archive_path).with_suffix('.qb_identity_certificate.json')
    if not path.is_file():raise ValueError('Historical QB source/chronology certificate missing; metadata-only recovery is not production certification')
    certificate=json.loads(path.read_text(encoding='utf-8'))
    sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
    if certificate.get('status')!='PASS' or certificate.get('archive_sha256')!=sha(archive_path):
        raise ValueError('Historical QB certificate missing PASS or archive hash mismatch')
    if certificate.get('model_artifact_sha256')!=model_sha256:raise ValueError('Historical QB certificate is bound to a different fitted model')
    for gate in ['training_numeric_parity','chronological_replay','future_perturbation','inference_contract_parity']:
        if certificate.get(gate)!='PASS':raise ValueError('Historical QB certificate gate incomplete: '+gate)
    root=Path(root).resolve();audit_path=(root/certificate['audit_path']).resolve()
    if not audit_path.is_relative_to(root) or not audit_path.is_file() or sha(audit_path)!=certificate.get('audit_sha256'):
        raise ValueError('Historical QB row evidence absent/hash-invalid')
    audit=pd.read_csv(audit_path,low_memory=False)
    keys=['game_id','side']
    if len(audit)!=2*len(historical) or audit.duplicated(keys).any():raise ValueError('Historical QB certificate row coverage incomplete')
    if not certificate.get('source_files'):raise ValueError('Historical QB original source hashes missing')
    for name,digest in certificate['source_files'].items():
        source=(root/name).resolve()
        if not source.is_relative_to(root) or not source.is_file() or sha(source)!=digest:raise ValueError('Historical QB source hash mismatch')
    for r in audit.itertuples():
        games=historical[historical.game_id.eq(r.game_id)]
        if len(games)!=1 or r.side not in {'home','away'}:raise ValueError('Historical QB certificate fixture mismatch')
        g=games.iloc[0];other='away' if r.side=='home' else 'home'
        if int(r.season)!=int(g.season) or int(r.week)!=int(g.week) or r.team!=g[r.side+'_team'] or r.opponent!=g[other+'_team'] or r.player_id!=g[r.side+'_expected_qb_id']:
            raise ValueError('Historical QB certificate team/opponent/player join mismatch')
        if r.player_name!=g[r.side+'_expected_qb_name'] or g[r.side+'_qb_source_reliability']!='EXACT_POINT_IN_TIME':
            raise ValueError('Historical QB certificate name/source-method mismatch')
        cutoff=utc_evidence(g.kickoff_utc)-pd.Timedelta(minutes=1)
        times=[utc_evidence(r.depth_known_at_utc),utc_evidence(r.roster_known_at_utc)]
        if any(t is None or t>=cutoff for t in times) or r.roster_status!='ACT':
            raise ValueError('Historical QB certificate lacks pre-cutoff eligibility clocks')
        if cutoff-times[0]>pd.Timedelta(hours=36):raise ValueError('Historical QB certificate stale depth snapshot')
        if not valid_gsis(r.player_id):raise ValueError('Historical QB certificate invalid GSIS identity')
    return certificate
