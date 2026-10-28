"""Opt-in value feed v2. Does not publish or modify the existing public feed."""
from datetime import datetime,timezone
import json
import re
import math
from nfl2026.markets.certified_value import WITHHELD, stamp, truth

def value_payload(rows, now=None):
    now = stamp(now or datetime.now(timezone.utc)); output = []
    for original in rows:
        r = dict(original)
        if not r.get('decision_label'): r['decision_label'] = WITHHELD
        malformed = False
        if isinstance(r.get('market_contract'),str):
            try: r['market_contract'] = json.loads(r['market_contract'])
            except (ValueError,TypeError): r['market_contract'] = {};malformed = True
        reasons = r.get('safety_reasons',[])
        if isinstance(reasons,str):
            try: reasons = json.loads(reasons)
            except (ValueError,TypeError): reasons = ['MALFORMED_SAFETY_RECEIPT']
        permitted = False
        try:
            permitted = (not malformed and truth(r.get('probability_certified')) and truth(r.get('actionable_now')) and not reasons
                and bool(re.fullmatch('[0-9a-f]{64}',r.get('certificate_sha256','')))
                and stamp(r['evaluated_at_utc']) <= now < stamp(r['valid_until_utc'])
                and r.get('decision_label') in {'AI Back Value','AI Lay Value'})
        except (KeyError,ValueError,TypeError): pass
        if not permitted:
            r['actionable_now'] = False
            if r.get('decision_label') in {'AI Back Value','AI Lay Value'}:
                try: expired = bool(r.get('valid_until_utc')) and stamp(r['valid_until_utc'])<=now
                except (ValueError,TypeError): expired = False
                r['decision_label'] = 'Historical/Expired' if expired else WITHHELD
        output.append(r)
    def finite_json(value):
        if isinstance(value,dict): return {k:finite_json(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)): return [finite_json(v) for v in value]
        if isinstance(value,float) and not math.isfinite(value): return None
        if hasattr(value,'item'): return finite_json(value.item())
        return value
    output = finite_json(output)
    return dict(schema='certified-value-feed-v2',generated_at_utc=now.isoformat(),publication=False,
                bookmaker_implied_scores_are_model_predictions=False,selections=output,
                actionable_count=sum(truth(r.get('actionable_now')) for r in output))
