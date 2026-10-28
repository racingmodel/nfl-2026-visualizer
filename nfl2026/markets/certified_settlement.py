"""Pure contract-specific settlement for v2 research; no ledger mutation."""
from nfl2026.markets.certified_value import MARKETS, clean

def settle_contract(quote, actual):
    for k in ['game_id','period','overtime','stat_contract']:
        if not clean(quote.get(k)) or quote.get(k) != actual.get(k):
            raise ValueError('Actual/quote settlement contract mismatch: '+k)
    market = quote.get('market_type')
    if market not in MARKETS or actual.get('status') != 'FINAL': raise ValueError('Final matching actual required')
    if quote.get('quote_type','BACK') != 'BACK': raise ValueError('Exchange settlement requires net-market commission accounting')
    odds = float(quote['decimal_odds'])
    if odds <= 1: raise ValueError('Invalid decimal odds')
    team = quote.get('selection_team'); side = quote.get('side')
    if market == 'MONEYLINE':
        difference = actual['scores'][team]-actual['scores'][next(t for t in actual['scores'] if t!=team)]
        if difference == 0 and 'tie_void' not in quote['stat_contract']: raise ValueError('Tie grading not supported by this two-way contract')
    elif market == 'SPREAD':
        difference = actual['scores'][team]-actual['scores'][next(t for t in actual['scores'] if t!=team)]+float(quote['line'])
    elif market == 'TOTAL': difference = sum(actual['scores'].values())-float(quote['line'])
    elif market == 'TEAM_TOTAL': difference = actual['scores'][team]-float(quote['line'])
    else:
        name = {'ANYTIME_TD':'anytime_offensive_td','PASSING_YARDS':'passing_yards','RUSHING_YARDS':'rushing_yards','RECEIVING_YARDS':'receiving_yards'}[market]
        player = actual['players'][quote['player_id']]
        if not player.get('participation_verified'): raise ValueError('Missing participation/void evidence')
        value = player[name]
        if market == 'ANYTIME_TD':
            win = bool(value) if side == 'YES' else not bool(value)
            return ('WIN',odds) if win else ('LOSS',0.)
        difference = float(value)-float(quote['line'])
    if side == 'UNDER': difference = -difference
    result = 'WIN' if difference>0 else 'PUSH' if difference==0 else 'LOSS'
    return result, odds if result=='WIN' else 1. if result=='PUSH' else 0.
