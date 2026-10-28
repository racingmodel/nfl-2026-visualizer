"""Fail-closed research source contract, separate from production ingestion."""
from datetime import datetime,timezone
import json
import pandas as pd
from nfl2026.phase7.common import json_write
from nfl2026.utilities.io import sha256_file


def validate_external(row,cutoff,*,purpose='research_feature'):
    required=['player_id','team','source_kind','known_at_utc','redistribution_permitted']
    if any(k not in row or row[k] is None for k in required):raise ValueError('Missing source identity, rights or observation time')
    if not row['player_id'] or not row['team']:raise ValueError('Unmatched player or team')
    known=pd.Timestamp(row['known_at_utc']);stop=pd.Timestamp(cutoff)
    if known.tzinfo is None or stop.tzinfo is None or known>=stop:raise ValueError('Not demonstrably known before cutoff')
    if row['source_kind'] not in ['OBSERVATION','RANKING','STAT_PROJECTION']:raise ValueError('Unknown source definition')
    if purpose=='yardage_projection' and row['source_kind']!='STAT_PROJECTION':raise ValueError('Rankings/opportunity are not yardage forecasts')
    if purpose=='public_display' and row['redistribution_permitted'] is not True:raise ValueError('Public redistribution rights absent')
    if row['source_kind']=='OBSERVATION':
        ended=pd.Timestamp(row.get('observed_game_end_utc'))
        if pd.isna(ended) or ended.tzinfo is None or ended>=stop:raise ValueError('Same/future game observation leakage')
    return True


def inventory(root,out):
    folder=out/'external_sources';records=[]
    contracts={
        'ffopportunity':('ep_weekly_2026.parquet','OBSERVATION','CC-BY-SA-4.0 models/data; GPLv3 package','GSIS player_id','automated after completed PBP; not a weekly pregame projection'),
        'ftn_charting':('ftn_charting_2026.parquet','OBSERVATION','CC-BY-SA-4.0; attribute FTN Data via nflverse','game_id/play_id, requires existing PBP player join','typically within 48 hours; scheduled 4/day'),
        'snap_counts':('snap_counts_2026.parquet','OBSERVATION','upstream PFR rights require review before public redistribution','PFR player ID requires crosswalk','scheduled 4/day; completed games'),
        'rankings':('fp_latest_weekly.csv','RANKING','FantasyPros terms restrict use/redistribution; no public import authorized','FantasyPros ID requires dated crosswalk','weekly scrape; date only, no precise original publication time')}
    for name,(filename,kind,licence,ids,update) in contracts.items():
        path=folder/filename;d=pd.read_parquet(path) if path.suffix=='.parquet' else pd.read_csv(path)
        week=int(d.week.max()) if 'week' in d else None
        records.append(dict(source=name,rows=len(d),max_week=week,kind=kind,license=licence,ids=ids,update=update,
            sha256=sha256_file(path),retrieved_at_utc=datetime.now(timezone.utc).isoformat(),cost='free public retrieval',
            public_ingestion=False,training_ingestion=False,historical_asof_archives='current release does not prove past retrieval/publication; dated local archives required',
            current_2026_coverage='Weeks 1-'+str(week) if week else '2026-10-22 scrape, 658 ranking rows; r2p_pts also present but not validated as a stat-yardage forecast',
            potential='independent past-only opportunity/efficiency benchmark' if kind=='OBSERVATION' else 'independent ordinal ranking comparison; never convert rank to yards'))
    metadata=json.loads((folder/'nextgen_refreshed.json').read_text());assets=[a for a in metadata.get('assets',[]) if '2026' in a['name']]
    ngs=[]
    for path in sorted(folder.glob('ngs_*.csv.gz')):
        frame=pd.read_csv(path);current=frame[frame.season.eq(2026)&frame.week.gt(0)]
        ngs.append(dict(file=path.name,rows_2026=len(current),max_week=int(current.week.max()),sha256=sha256_file(path)))
    records.append(dict(source='NGS',kind='OBSERVATION',verified_aggregate_files=ngs,assets_2026=[{'name':a['name'],'updated_at':a['updated_at'],'url':a['browser_download_url']} for a in assets],
        current_2026_coverage='Verified 2026 Weeks 1-6 in aggregate passing, receiving and rushing files; season-specific assets absent',
        ids='GSIS IDs where supplied; identity and minimum sample selection must be checked',update='nightly, published after games',
        license='NFL/NGS upstream data rights unresolved; code licence does not confer data rights',public_ingestion=False,training_ingestion=False,
        historical_asof_archives='season files available; not an as-of historical publication archive',potential='CPOE, time-to-throw, separation and rushing context; observational, not pregame stat projections'))
    json_write(out/'external_source_inventory.json',{'sources':records,'no_paid_requests':True,'no_restricted_data_republished':True})
