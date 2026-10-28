"""Fail-closed value contracts. This module never fetches odds or fits a model.

The consumer must pin a certified probability table in the immutable forecast,
and a current, independently sourced eligibility receipt in the odds snapshot.
Legacy score noise/yardage bands are not probability certificates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math

import pandas as pd

MARKETS = {'MONEYLINE', 'SPREAD', 'TOTAL', 'TEAM_TOTAL', 'ANYTIME_TD',
           'PASSING_YARDS', 'RUSHING_YARDS', 'RECEIVING_YARDS'}
PLAYER_MARKETS = {'ANYTIME_TD', 'PASSING_YARDS', 'RUSHING_YARDS', 'RECEIVING_YARDS'}
WITHHELD = 'Withheld — source or calibration incomplete'
CONTRACT_FIELDS = ('game_id', 'market_type', 'player_id', 'selection_team', 'side',
                   'line', 'period', 'overtime', 'stat_contract', 'rule_version', 'jurisdiction')
EXTRA_COLUMNS = ['decision_label', 'actionable_now', 'probability_certified', 'p_win', 'p_loss', 'p_push',
    'conditional_model_probability', 'probability_lower', 'probability_upper', 'conservative_ev_pct',
    'certificate_sha256', 'calibration_sample_size', 'model_published_at_utc', 'quote_retrieved_at_utc',
    'evaluated_at_utc', 'valid_until_utc', 'quote_type', 'liability_per_unit', 'commission_rate',
    'safety_reasons', 'contract_key', 'market_contract', 'edge_basis']
EXTRA_COLUMNS += ['exchange_break_even_lay_odds', 'ev_basis']

def stamp(value):
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError('Explicit UTC/offset timestamp required')
    return result.tz_convert('UTC')

def clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ''
    return str(value).strip()

def truth(value):
    return value is True or clean(value).lower() == 'true'

def contract_key(row):
    def norm(key):
        value = clean(row.get(key))
        if key == 'line' and value:
            return format(float(value), '.8g')
        return value
    return '|'.join(norm(key) for key in CONTRACT_FIELDS)

def back_metrics(win, loss, push, odds):
    if any(not math.isfinite(float(p)) or p < 0 or p > 1 for p in (win, loss, push)) or abs(win+loss+push-1) > 1e-8:
        raise ValueError('Win/loss/push probabilities must sum to one')
    if not math.isfinite(odds) or odds <= 1:
        raise ValueError('Invalid decimal odds')
    return {'ev_decimal': win*(odds-1)-loss, 'model_fair_odds': (1-push)/win if win else None,
            'conditional_model_probability': win/(win+loss) if win+loss else None}

def lay_metrics(win, loss, push, odds, commission):
    back_metrics(win, loss, push, odds)
    if not 0 <= commission < 1:
        raise ValueError('Invalid exchange commission')
    liability = odds-1
    profit = loss*(1-commission)-win*liability
    return {'ev_decimal': profit/liability, 'expected_profit_per_backer_stake': profit,
            'liability_per_unit': liability}

def paired_probabilities(quotes):
    """Proportional margin removal only for proven complementary contracts.

    Anytime scorers are overlapping events. Only a same-player YES/NO pair
    can be normalized. Missing grading/period evidence is never filled in.
    """
    out = quotes.copy()
    prices = pd.to_numeric(out.decimal_odds, errors='coerce')
    out['book_implied_probability'] = prices.where(prices.gt(1) & prices.lt(float('inf'))).rdiv(1)
    out['no_vig_probability'] = float('nan')
    out['no_vig_status'] = 'UNVERIFIED_OR_INCOMPLETE_COMPLEMENT'
    groups = {}
    for index, row in out.iterrows():
        r = row.to_dict(); market = r.get('market_type')
        needed = ['game_id','bookmaker','period','overtime','stat_contract','rule_version','jurisdiction']
        if market not in MARKETS or any(not clean(r.get(k)) for k in needed):
            continue
        if clean(r.get('quote_type', 'BACK')).upper() != 'BACK':
            continue
        base = tuple(clean(r.get(k)) for k in needed) + (market,)
        if market == 'SPREAD':
            try: base += (abs(float(r['line'])),)
            except (ValueError, TypeError): continue
        elif market in {'TOTAL','TEAM_TOTAL','PASSING_YARDS','RUSHING_YARDS','RECEIVING_YARDS'}:
            try: line = float(r.get('line')); assert math.isfinite(line)
            except (ValueError,TypeError,AssertionError): continue
            base += (clean(r.get('player_id')), clean(r.get('selection_team')) if market == 'TEAM_TOTAL' else '',format(line,'.8g'))
        elif market == 'ANYTIME_TD':
            if not clean(r.get('player_id')): continue
            base += (clean(r['player_id']),)
        groups.setdefault(base, []).append(index)
    for indices in groups.values():
        if len(indices) != 2: continue
        a, b = [out.loc[i].to_dict() for i in indices]
        market = a['market_type']
        if market in {'MONEYLINE','SPREAD'}:
            complement = bool(clean(a.get('selection_team')) and clean(b.get('selection_team')) and a['selection_team'] != b['selection_team'])
            # Only an evidenced two-way rule is supported; three-way draw needs a different contract.
            complement &= a.get('stat_contract') == b.get('stat_contract') and '2way' in clean(a.get('stat_contract')).lower()
            if market == 'SPREAD': complement &= abs(float(a['line'])+float(b['line'])) < 1e-8
        else:
            complement = {a.get('side'), b.get('side')} == ({'YES','NO'} if market == 'ANYTIME_TD' else {'OVER','UNDER'})
        try:
            times = [stamp(r['source_timestamp_utc']) for r in (a,b)]
            prices = [float(r['decimal_odds']) for r in (a,b)]
            complement &= abs((times[0]-times[1]).total_seconds()) <= 60
            complement &= all(math.isfinite(p) and p > 1 for p in prices)
        except (KeyError, ValueError, TypeError): continue
        if complement:
            raw = [1/p for p in prices]
            out.loc[indices, 'no_vig_probability'] = [p/sum(raw) for p in raw]
            out.loc[indices, 'no_vig_status'] = 'PROPORTIONAL_COMPLEMENTARY_PAIR'
    return out

def pinned_json(root, reference):
    if isinstance(reference, str):
        reference = json.loads(reference)
    if not isinstance(reference, dict): raise ValueError('Missing immutable evidence reference')
    path = (Path(root)/reference['path']).resolve()
    if not path.is_relative_to(Path(root).resolve()): raise ValueError('Evidence outside project root')
    if hashlib.sha256(path.read_bytes()).hexdigest() != reference.get('sha256'):
        raise ValueError('Evidence hash mismatch')
    return json.loads(path.read_text(encoding='utf-8'))

def load_probability_package(root, meta):
    """No self-selected certificate: the original official forecast must pin it.

    Probability records cover exact thresholds and settlement contracts, not a
    point estimate. Approval and holdout artifacts are independently hash-bound.
    """
    if meta.get('deployment_eligible') is not True or meta.get('qa_status',meta.get('validation_result')) != 'PASS' or meta.get('bookmaker_data_used') is not False:
        raise ValueError('Official forecast not certified')
    reference = meta.get('approved_value_certificate')
    package = pinned_json(root, reference)
    if package.get('prediction_run_id') != meta.get('run_id') or package.get('status') != 'CERTIFIED':
        raise ValueError('Probability package run/status mismatch')
    if stamp(package['published_at_utc']) != stamp(meta['created_at_utc']):
        raise ValueError('Probability publication is not the immutable forecast publication')
    approval = pinned_json(root, package.get('approval'))
    calibration = pinned_json(root, package.get('calibration'))
    model = package.get('model_artifact', {})
    model_path = (Path(root)/model.get('path','')).resolve()
    if not model_path.is_relative_to(Path(root).resolve()) or not model_path.is_file() or hashlib.sha256(model_path.read_bytes()).hexdigest() != model.get('sha256'):
        raise ValueError('Model artifact identity mismatch')
    if model.get('sha256') not in meta.get('model_artifact_hashes', {}).values():
        raise ValueError('Probability model is not the approved forecast model')
    feature = package.get('feature_artifact',{})
    feature_path = (Path(root)/feature.get('path','')).resolve()
    if (not feature_path.is_relative_to(Path(root).resolve()) or not feature_path.is_file()
        or hashlib.sha256(feature_path.read_bytes()).hexdigest() != feature.get('sha256')
        or feature.get('sha256') not in meta.get('file_hashes',{}).values()):
        raise ValueError('Immutable forecast feature identity differs')
    if approval.get('decision') != 'APPROVED' or approval.get('model_sha256') != model.get('sha256') or not approval.get('approval_id'):
        raise ValueError('Separate model/market approval missing')
    if approval.get('calibration_sha256') != package['calibration']['sha256'] or calibration.get('model_sha256') != model.get('sha256'):
        raise ValueError('Calibration/approval model binding differs')
    if not stamp(calibration['holdout_end_utc']) <= stamp(approval['approved_at_utc']) <= stamp(package['published_at_utc']):
        raise ValueError('Approval was not established at forecast publication')
    if not (stamp(calibration['training_cutoff_utc']) < stamp(calibration['holdout_start_utc']) <= stamp(calibration['holdout_end_utc']) < stamp(package['published_at_utc'])):
        raise ValueError('Independent chronological calibration required')
    if not truth(calibration.get('independent_holdout')) or not truth(calibration.get('all_promotion_gates_passed')):
        raise ValueError('Calibration/promotion gates not passed')
    records = package.get('probabilities', [])
    keys = [contract_key(row) for row in records]
    if len(set(keys)) != len(keys): raise ValueError('Ambiguous probability records')
    for r in records:
        if r.get('market_type') not in approval.get('approved_markets', []): raise ValueError('Market not approved')
        stats = calibration.get('markets', {}).get(r.get('market_type'), {})
        scores = [float(stats.get(k,float('nan'))) for k in ('ece','brier','baseline_brier')]
        if int(stats.get('n', 0)) < 200 or not all(math.isfinite(x) and 0 <= x <= 1 for x in scores) or scores[0] > .05 or scores[1] > scores[2]:
            raise ValueError('Independent market calibration/sample guard failed')
        if r.get('stat_contract') != stats.get('stat_contract'): raise ValueError('Calibration label definition differs')
        line = clean(r.get('line'))
        covered = stats.get('validated_lines',[])
        if not any((not line and value is None) or (line and value is not None and abs(float(line)-float(value))<1e-8) for value in covered):
            raise ValueError('Threshold was not independently calibrated')
        if stats.get('probability_contract') != 'WIN_LOSS_PUSH' or not truth(stats.get('pushes_evaluated')):
            raise ValueError('Push/void outcome distribution not validated')
        back_metrics(float(r['p_win']),float(r['p_loss']),float(r['p_push']),2.)
        if not 0 <= float(r['win_lower']) <= float(r['p_win']) <= float(r['win_upper']) <= 1-float(r['p_push']):
            raise ValueError('Invalid calibrated uncertainty bounds')
    return package, calibration, reference['sha256']

def evaluate_quote(quote, probability, *, package, calibration, certificate_sha, rule, context, now):
    """Evaluate a single exact contract. Inputs come from the pinned loaders."""
    from nfl2026.certification.contracts import require_grading_equivalence
    reasons = []
    result = dict(decision_label=WITHHELD, actionable_now=False, probability_certified=False,
                  safety_reasons=[], contract_key=contract_key(quote), evaluated_at_utc=stamp(now).isoformat(),
                  market_contract={k:quote.get(k) for k in CONTRACT_FIELDS})
    try:
        current = stamp(now); kickoff = stamp(context['kickoff_utc']); cutoff = kickoff-pd.Timedelta(minutes=1)
        source = stamp(quote['source_timestamp_utc']); retrieval = stamp(quote['retrieval_timestamp_utc'])
        published = stamp(package['published_at_utc']); observed = stamp(context['observed_at_utc'])
        if current >= cutoff or context.get('fixture_state') in {'LIVE','FINAL'}:
            result.update(decision_label='Historical/Expired', safety_reasons=['FIXTURE_CLOSED']); return result
        if context.get('fixture_state') != 'PRE_KICKOFF' or current-observed > pd.Timedelta(minutes=15) or observed > current:
            reasons.append('MISSING_FRESH_INDEPENDENT_PRE_KICKOFF_STATE')
        if clean(context.get('game_id')) != clean(quote.get('game_id')) or stamp(quote['kickoff_utc']) != kickoff:
            reasons.append('FIXTURE_IDENTITY_CONFLICT')
        if not published <= source <= retrieval <= current: reasons.append('INCOMPATIBLE_MODEL_QUOTE_CLOCKS')
        if stamp(package['source_cutoff_utc']) > published or published >= cutoff: reasons.append('POST_CUTOFF_OR_FUTURE_MODEL')
        max_age = 15 if quote.get('market_type') in PLAYER_MARKETS else 30
        age = (current-source).total_seconds()/60
        if age > max_age:
            result.update(decision_label='Historical/Expired', safety_reasons=['STALE_QUOTE']); return result
        if contract_key(quote) != contract_key(probability): reasons.append('INCOMPATIBLE_MODEL_MARKET_CONTRACT')
        require_grading_equivalence(probability['stat_contract'], quote, rule)
        if any(clean(rule.get(k)) != clean(quote.get(k)) for k in ('period','overtime')): reasons.append('PERIOD_OR_OVERTIME_MISMATCH')
        if not truth(context.get('source_gates_passed')): reasons.append('INCOMPLETE_INJURY_ROSTER_QB_SOURCES')
        if clean(quote.get('market_type')) not in MARKETS: reasons.append('UNSUPPORTED_MARKET')
        if quote.get('market_type') in PLAYER_MARKETS:
            eligibility = context.get('players', {}).get(clean(quote.get('player_id')), {})
            if not truth(eligibility.get('eligible')) or eligibility.get('gameday_status') != 'ACTIVE_CONFIRMED':
                reasons.append('PLAYER_NOT_GAME_DAY_CERTIFIED')
            if eligibility.get('medical_status') in {'OUT','IR','INACTIVE','PUP','DOUBTFUL','QUESTIONABLE','UNKNOWN',None}:
                reasons.append('PLAYER_ADVERSE_OR_UNKNOWN')
            if not truth(eligibility.get('original_role_eligible')): reasons.append('ORIGINAL_ROLE_NOT_ELIGIBLE')
        if not truth(quote.get('executable')) or truth(quote.get('suspended')): reasons.append('NOT_EXECUTABLE')
        if clean(quote.get('mapping_status','MAPPED_EXACT')) != 'MAPPED_EXACT': reasons.append('UNRESOLVED_MAPPING')
        win, loss, push = [float(probability[k]) for k in ('p_win','p_loss','p_push')]
        odds = float(quote['decimal_odds']); lower, upper = [float(probability[k]) for k in ('win_lower','win_upper')]
        metrics = back_metrics(win,loss,push,odds)
        conservative = back_metrics(lower,1-push-lower,push,odds)['ev_decimal']
        quote_type = clean(quote.get('quote_type','BACK')).upper()
        label = 'AI Back Value'
        liability = None; commission = None
        if quote_type == 'LAY':
            commission = float(quote.get('commission_rate', -1)); size = float(quote.get('available_size', 0))
            stake = float(quote.get('requested_backer_stake',1))
            if quote.get('venue') != 'EXCHANGE' or not truth(quote.get('offer_verified')) or not math.isfinite(size) or not math.isfinite(stake) or stake <= 0 or size < stake:
                reasons.append('NO_EXECUTABLE_EXCHANGE_LIQUIDITY')
            metrics.update(lay_metrics(win,loss,push,odds,commission))
            conservative = lay_metrics(upper,1-push-upper,push,odds,commission)['ev_decimal']
            liability = odds-1; label = 'AI Lay Value'
        elif quote_type != 'BACK': reasons.append('UNSUPPORTED_QUOTE_TYPE')
        market_probability = quote.get('no_vig_probability')
        if market_probability is None or pd.isna(market_probability):
            # A one-sided back price is a break-even threshold, not a fair market probability.
            market_probability = 1/odds; edge_basis = 'RAW_BREAK_EVEN_NOT_MARKET_FAIR'
        else: edge_basis = 'PROPORTIONAL_COMPLEMENTARY_PAIR'
        conditional = metrics['conditional_model_probability']
        edge = (conditional-float(market_probability))*100
        if quote_type == 'LAY': edge = -edge
        movement = quote.get('opening_book_odds')
        if movement is not None and pd.notna(movement) and abs(odds/float(movement)-1) > .20:
            reasons.append('MATERIAL_PRICE_MOVEMENT_REVIEW_REQUIRED')
        stats = calibration['markets'][quote['market_type']]
        result.update(probability_certified=True, p_win=win,p_loss=loss,p_push=push,model_probability=win,
            probability_lower=lower,probability_upper=upper, model_fair_odds=back_metrics(win,loss,push,odds)['model_fair_odds'],
            conditional_model_probability=conditional, ev_decimal=metrics['ev_decimal'],ev_pct=metrics['ev_decimal']*100,
            conservative_ev_pct=100*conservative,edge_pp=edge,edge_basis=edge_basis,quote_type=quote_type,
            liability_per_unit=liability,commission_rate=commission,certificate_sha256=certificate_sha,
            exchange_break_even_lay_odds=1+loss*(1-commission)/win if quote_type=='LAY' and win>0 else None,
            ev_basis='RETURN_ON_LIABILITY' if quote_type=='LAY' else 'RETURN_ON_BACK_STAKE',
            calibration_sample_size=stats['n'],model_published_at_utc=published.isoformat(),
            quote_retrieved_at_utc=retrieval.isoformat(), valid_until_utc=min(cutoff,source+pd.Timedelta(minutes=max_age),observed+pd.Timedelta(minutes=15)).isoformat())
        if not reasons:
            qualifies = edge >= 5 and metrics['ev_decimal'] >= .10 and conservative > 0
            result.update(decision_label=label if qualifies else 'No Value', actionable_now=qualifies)
    except (KeyError, ValueError, TypeError, OSError) as exc:
        reasons.append(str(exc))
    result['safety_reasons'] = reasons
    return result

def choose_recommendations(rows):
    """One best executable offer; opposing directions are withheld, not arbitraged."""
    out = [dict(r) for r in rows]
    groups = {}
    for r in out:
        if truth(r.get('actionable_now')):
            # Collapse opposite sides, alternate lines and back/lay into a risk family.
            family = (r.get('game_id'),r.get('market_type'),clean(r.get('player_id')),
                      r.get('market_contract',{}).get('selection_team') if r.get('market_type') == 'TEAM_TOTAL' else '',
                      r.get('market_contract',{}).get('period'),r.get('market_contract',{}).get('stat_contract'))
            groups.setdefault(family, []).append(r)
    for group in groups.values():
        directions = {(r.get('side'), r.get('team') if r.get('market_type') in {'MONEYLINE','SPREAD'} else '',r.get('quote_type','BACK')) for r in group}
        if len(directions) > 1:
            for r in group:
                r.update(actionable_now=False,decision_label=WITHHELD,safety_reasons=r.get('safety_reasons',[])+['CONFLICTING_VALUE_SELECTIONS'])
        else:
            best = sorted(group,key=lambda r:(-float(r.get('conservative_ev_pct',-1e9)),-float(r.get('ev_pct',-1e9)),clean(r.get('bookmaker')),clean(r.get('quote_id'))))[0]
            for r in group:
                if r is not best: r.update(actionable_now=False,decision_label='No Value',safety_reasons=r.get('safety_reasons',[])+['DUPLICATE_ALTERNATIVE_OFFER'])
    return out

def enforce_certification(values, mapped, *, root, source_metadata, snapshot_meta, now):
    """Adapter for the existing comparison runner. Unsupported legacy math is erased."""
    if values.empty: return values
    quote_lookup = {clean(r['quote_id']):r for r in mapped.to_dict('records')}
    packages = {}; rows = []
    try:
        contexts = pinned_json(root, snapshot_meta.get('current_value_context'))
        # Each source is a separately archived raw capture, not an unbound READY flag.
        for reference in contexts.get('source_evidence', []):
            path = (Path(root)/reference['path']).resolve()
            if not path.is_relative_to(Path(root).resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != reference['sha256']:
                raise ValueError('Current source evidence hash mismatch')
        if not contexts.get('source_evidence'): raise ValueError('Missing independent raw source bindings')
    except (KeyError, ValueError, TypeError, OSError): contexts = {}
    for r in values.to_dict('records'):
        quote = {**quote_lookup.get(clean(r['quote_id']), {}),'opening_book_odds':r.get('opening_book_odds')}
        gid = clean(r['game_id']); meta = source_metadata.get(gid,{})
        for key in ('model_probability','model_fair_odds','edge_pp','ev_decimal','ev_pct'):
            r[key] = None
        result = dict(decision_label=WITHHELD,actionable_now=False,probability_certified=False,
                      safety_reasons=['CERTIFIED_PROBABILITY_OR_CURRENT_CONTEXT_UNAVAILABLE'])
        try:
            if meta.get('run_id') not in packages: packages[meta['run_id']] = load_probability_package(root,meta)
            package, calibration, digest = packages[meta['run_id']]
            matches = [p for p in package['probabilities'] if contract_key(p) == contract_key(quote)]
            if len(matches) != 1: raise ValueError('No exact calibrated threshold probability')
            context = contexts['fixtures'][gid]
            rule = pinned_json(root, quote.get('grading_rule_evidence'))
            result = evaluate_quote(quote,matches[0],package=package,calibration=calibration,certificate_sha=digest,rule=rule,context=context,now=now)
        except (KeyError, ValueError, TypeError, OSError) as exc: result['safety_reasons'] = [str(exc)]
        # Historical classification does not require a certificate, and never activates.
        try:
            if stamp(now) >= stamp(quote['kickoff_utc'])-pd.Timedelta(minutes=1) or (stamp(now)-stamp(quote['source_timestamp_utc'])).total_seconds() > (900 if quote.get('market_type') in PLAYER_MARKETS else 1800):
                result.update(decision_label='Historical/Expired',actionable_now=False)
        except (KeyError, ValueError, TypeError): pass
        r.update(result)
        if clean(r.get('exclusion_reason')):
            r.update(actionable_now=False,decision_label='Historical/Expired' if r.get('decision_label') == 'Historical/Expired' else WITHHELD)
            r['safety_reasons'] += [r['exclusion_reason']]
        r['distribution_method'] = 'CERTIFIED_EXACT_THRESHOLD' if r['probability_certified'] else 'WITHHELD_UNCERTIFIED_DISTRIBUTION'
        r['status'] = 'ACTIVE' if r['probability_certified'] and not r['safety_reasons'] and r['decision_label'] != 'Historical/Expired' else 'EXCLUDED'
        r['value_class'] = 'MODEL_VALUE' if r['actionable_now'] else 'NO_EDGE' if r['decision_label'] == 'No Value' else 'EXCLUDED'
        rows.append(r)
    rows = choose_recommendations(rows)
    for r in rows:
        r['value_class'] = 'MODEL_VALUE' if r['actionable_now'] else 'NO_EDGE' if r['decision_label'] == 'No Value' else 'EXCLUDED'
        if r['decision_label'] == WITHHELD: r['status'] = 'EXCLUDED'
        r['is_best_price'] = truth(r['actionable_now'])
        r['safety_reasons'] = json.dumps(r.get('safety_reasons',[]),ensure_ascii=True)
        r['market_contract'] = json.dumps(r.get('market_contract',{}),ensure_ascii=True,sort_keys=True)
    return pd.DataFrame(rows)
