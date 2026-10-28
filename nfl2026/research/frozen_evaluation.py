"""Read-only, point-in-time scoring helpers. Never infer an absent actual as zero."""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_freeze(folder,kickoff,cutoff_minutes=1):
    folder=Path(folder).resolve();manifest=json.loads((folder/'freeze_manifest.json').read_text())
    files=manifest.get('files',manifest.get('file_sha256',{}))
    if not files:raise ValueError('Frozen output hashes required')
    for name,digest in files.items():
        path=(folder/name).resolve()
        if not path.is_relative_to(folder) or sha(path)!=digest:raise ValueError('Frozen artifact differs: '+name)
    created=pd.Timestamp(manifest['created_at_utc'])
    frozen=pd.Timestamp(manifest.get('freeze_at_utc',manifest.get('sealed_at_utc',created)))
    boundary=pd.Timestamp(kickoff)-pd.Timedelta(minutes=cutoff_minutes)
    if created.tzinfo is None or frozen.tzinfo is None or not created<=frozen<boundary:raise ValueError('Invalid pregame creation/freeze timestamps')
    return manifest


def score_matched(predictions,actuals,keys,prediction_col,actual_col):
    """The caller selects the frozen population, never selects from observed outcomes."""
    if predictions.empty:raise ValueError('Empty forecast cohort is not an evaluated population')
    if predictions.duplicated(keys).any() or actuals.duplicated(keys).any():raise ValueError('Duplicate forecast/actual identity')
    joined=predictions.merge(actuals,on=keys,how='left',validate='one_to_one',indicator=True,suffixes=('','_actual'))
    if not joined._merge.eq('both').all():raise ValueError('Missing actuals; no implicit zero or silent population reduction')
    values=joined[[prediction_col,actual_col]].to_numpy(dtype=float)
    if not np.isfinite(values).all():raise ValueError('Finite predictions and explicit actual labels required')
    joined['signed_error']=joined[prediction_col]-joined[actual_col]
    return joined,{'rows':len(joined),'mae':float(joined.signed_error.abs().mean()),'bias':float(joined.signed_error.mean()),
        'rmse':float(np.sqrt(np.mean(joined.signed_error**2)))}


def require_pregame_rows(frame,creation_column,kickoff_column='kickoff_utc'):
    for value in pd.concat([frame[creation_column],frame[kickoff_column]]):
        if pd.isna(value) or pd.Timestamp(value).tzinfo is None:raise ValueError('Explicit forecast/kickoff timezone required')
    creation=pd.to_datetime(frame[creation_column],utc=True,errors='coerce')
    kickoff=pd.to_datetime(frame[kickoff_column],utc=True,errors='coerce')
    if creation.isna().any() or kickoff.isna().any() or (creation>=kickoff-pd.Timedelta(minutes=1)).any():raise ValueError('Post-cutoff or missing forecast timestamp')


def immutable_json(path,value):
    path=Path(path)
    if path.exists():raise FileExistsError('Immutable evaluation already exists: '+str(path))
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,default=str,allow_nan=False),encoding='utf-8')
