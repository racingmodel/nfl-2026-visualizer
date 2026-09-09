const $ = (q, root = document) => root.querySelector(q);
const esc = value => String(value ?? '—').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const num = (value, digits = 1) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '—';
const pct = value => Number.isFinite(Number(value)) ? `${Math.round(Number(value) * 100)}%` : '—';
const TEAM_NAMES = {
  ARI:'Cardinals',ATL:'Falcons',BAL:'Ravens',BUF:'Bills',CAR:'Panthers',CHI:'Bears',CIN:'Bengals',CLE:'Browns',
  DAL:'Cowboys',DEN:'Broncos',DET:'Lions',GB:'Packers',HOU:'Texans',IND:'Colts',JAX:'Jaguars',KC:'Chiefs',
  LAC:'Chargers',LA:'Rams',LV:'Raiders',MIA:'Dolphins',MIN:'Vikings',NE:'Patriots',NO:'Saints',NYG:'Giants',
  NYJ:'Jets',PHI:'Eagles',PIT:'Steelers',SEA:'Seahawks',SF:'49ers',TB:'Buccaneers',TEN:'Titans',WAS:'Commanders'
};

async function loadJSON(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}
async function loadOptionalJSON(path, fallback) { try { return await loadJSON(path); } catch { return fallback; } }
function notice(text) { const element = $('#notice'); if (element) { element.className = 'notice'; element.textContent = text; } }
function when(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Unavailable' : date.toLocaleString([], {weekday:'short', month:'short', day:'numeric', hour:'numeric', minute:'2-digit'});
}

function playerRows(players, type) {
  let list = [...(players || [])];
  if (type === 'td') list = list.filter(p => Number(p.reconciled_td_probability) >= .08).sort((a,b) => b.reconciled_td_probability - a.reconciled_td_probability).slice(0,6);
  if (type === 'rush') list = list.filter(p => Number(p.expected_carries) >= 1 || Number(p.expected_rush_yards) >= 5).sort((a,b) => b.expected_rush_yards - a.expected_rush_yards).slice(0,8);
  if (type === 'receive') list = list.filter(p => Number(p.expected_targets) >= 1 || Number(p.expected_receiving_yards) >= 8).sort((a,b) => b.expected_receiving_yards - a.expected_receiving_yards).slice(0,8);
  if (!list.length) return '<p class="muted">No meaningful eligible projections in this view.</p>';
  return list.map(player => {
    const value = type === 'td' ? `${pct(player.reconciled_td_probability)} • Fair ${num(player.model_fair_odds,2)}` : type === 'rush' ? `${num(player.expected_carries)} carries • ${num(player.expected_rush_yards,0)} yd • ${num(player.rush_yards_low,0)}–${num(player.rush_yards_high,0)}` : `${num(player.expected_targets)} targets • ${num(player.expected_receiving_yards,0)} yd • ${num(player.receiving_yards_low,0)}–${num(player.receiving_yards_high,0)}`;
    const flag = player.availability_status === 'QUESTIONABLE' ? ' · Questionable' : player.role_status === 'ROLE_UNCERTAIN' ? ' · Role uncertain' : player.role_status === 'EXPECTED_STARTER' ? ' · Expected starter' : '';
    return `<div class="projection-row"><div><strong>${esc(player.player_name)}</strong><br><small>${esc(player.team)} • ${esc(player.position)}${esc(flag)}</small>${type === 'td' ? `<div class="bar"><i style="width:${Math.min(100, Number(player.reconciled_td_probability || 0) * 100)}%"></i></div>` : ''}</div><span>${value}</span></div>`;
  }).join('');
}

function valueCompact(rows) {
  if (!rows?.length) return '<p class="muted">Market data unavailable for this game.</p>';
  return rows.slice(0,5).map(value => `<div class="projection-row"><div><strong>${esc(value.selection)}</strong><br><small>${esc(value.market_type.replaceAll('_',' '))} · ${esc(value.bookmaker)} · ${esc(value.value_confidence)} confidence</small></div><span>${value.line == null ? '' : `${value.side} ${num(value.line)} · `}${pct(value.model_probability)} · ${num(value.book_odds,2)} · ${Number(value.ev_pct) >= 0 ? '+' : ''}${num(value.ev_pct,1)}% EV<br><b>${esc(value.value_class.replaceAll('_',' '))}</b></span></div>`).join('');
}

function gameCard(game, index, valueRows = [], market = null) {
  const winner = Number(game.home_win_probability) >= .5 ? game.home_team : game.away_team;
  const winProbability = Math.max(Number(game.home_win_probability || 0), Number(game.away_win_probability || 0));
  const margin = Number(game.model_margin || 0);
  const spread = margin >= 0 ? `${game.home_team} -${num(Math.abs(margin))}` : `${game.away_team} -${num(Math.abs(margin))}`;
  const factors = (() => { try { return JSON.parse(game.main_factors || '[]'); } catch { return []; } })();
  const warning = game.data_quality_warnings ? `<p class="muted"><b>Data note:</b> ${esc(game.data_quality_warnings)}</p>` : '';
  const marketPanel = market ? `<div class="market-strip"><div class="market-title"><span>MARKET-IMPLIED SCORE</span><small>Consensus main spread + total</small></div><div class="market-score"><b>${esc(game.away_team)} ${num(market.market_away_points,0)}</b><i>—</i><b>${num(market.market_home_points,0)} ${esc(game.home_team)}</b></div><div class="market-lines"><span>${esc(market.market_spread_label)}</span><span>O/U ${num(market.market_total)}</span><span>${Math.min(market.spread_book_count,market.total_book_count)}+ books</span></div></div>` : `<div class="market-strip unavailable"><span>Market-implied score unavailable</span></div>`;
  const scoreRange = Number.isFinite(Number(game.away_score_p25)) && Number.isFinite(Number(game.home_score_p25)) ? `<p><b>Middle 50% score range:</b> ${esc(game.away_team)} ${num(game.away_score_p25,0)}–${num(game.away_score_p75,0)} · ${esc(game.home_team)} ${num(game.home_score_p25,0)}–${num(game.home_score_p75,0)}</p>` : '';
  return `<article class="game-card"><div class="game-head"><span><b>WEEK ${esc(game.week)}</b> · ${when(game.kickoff_utc)}</span><span>${esc(game.stadium || 'Venue TBC')}</span></div><div class="matchup scoreboard"><div class="team"><span class="field-label">AWAY</span><span class="team-badge" aria-label="${esc(TEAM_NAMES[game.away_team] || game.away_team)}">${esc(game.away_team)}</span><span class="team-name">${esc(TEAM_NAMES[game.away_team] || game.away_team)}</span><strong class="team-score">${esc(game.display_away_score)}</strong></div><div class="fixture-at"><span>@</span><small>MODEL<br>SCORE</small></div><div class="team"><span class="field-label">HOME</span><span class="team-badge" aria-label="${esc(TEAM_NAMES[game.home_team] || game.home_team)}">${esc(game.home_team)}</span><span class="team-name">${esc(TEAM_NAMES[game.home_team] || game.home_team)}</span><strong class="team-score">${esc(game.display_home_score)}</strong></div></div><div class="quick-stats"><div><span>MODEL LINE</span><strong>${esc(spread)}</strong></div><div><span>MODEL O/U</span><strong>${num(game.model_total)}</strong></div><div><span>WIN CHANCE</span><strong>${esc(winner)} ${pct(winProbability)}</strong></div></div>${marketPanel}<details><summary>Open matchup board <span>＋</span></summary><div class="details-body"><div class="tabbar" role="tablist"><button class="active" data-tab="overview-${index}">Overview</button><button data-tab="td-${index}">TD scorers</button><button data-tab="rush-${index}">Rushing</button><button data-tab="receive-${index}">Receiving</button><button data-tab="value-${index}">Value</button><button data-tab="insight-${index}">Insight</button></div><div id="overview-${index}" class="tab-panel active"><p><b>Model mean:</b> ${esc(game.away_team)} ${num(game.away_expected_points)} — ${num(game.home_expected_points)} ${esc(game.home_team)}</p>${scoreRange}<p><b>Confidence:</b> ${esc(game.confidence_rating)} · <span class="muted">${esc(game.data_quality_status)}</span></p><p class="muted"><b>Weather:</b> ${esc(game.weather_summary || 'Forecast unavailable')} · Context only</p>${warning}</div><div id="td-${index}" class="tab-panel">${playerRows(game.players,'td')}</div><div id="rush-${index}" class="tab-panel">${playerRows(game.players,'rush')}</div><div id="receive-${index}" class="tab-panel">${playerRows(game.players,'receive')}</div><div id="value-${index}" class="tab-panel"><p class="muted">Downstream comparison. Market prices did not create this prediction.</p>${valueCompact(valueRows)}</div><div id="insight-${index}" class="tab-panel"><h3>Model leans ${esc(winner)}</h3><ul>${factors.map(x => `<li>${esc(x)}</li>`).join('')}</ul>${warning}</div></div></details></article>`;
}

function wireTabs() {
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-tab]');
    if (!button) return;
    const body = button.closest('.details-body');
    body.querySelectorAll('[data-tab]').forEach(item => item.classList.toggle('active', item === button));
    body.querySelectorAll('.tab-panel').forEach(item => item.classList.toggle('active', item.id === button.dataset.tab));
  });
}

async function gamesPage() {
  try {
    const [data, value] = await Promise.all([loadJSON('data/current_week.json'), loadOptionalJSON('data/value.json', {values:[],metadata:{status:'MARKET_DATA_UNAVAILABLE'}})]);
    const meta = data.metadata || {};
    const byGame = (value.values || []).reduce((grouped, row) => ((grouped[row.game_id] ??= []).push(row), grouped), {});
    const marketByGame = value.market_game_context || {};
    $('#week-title').textContent = `NFL ${meta.season || 2026} · Week ${meta.week ?? '—'}`;
    $('#status-panel').classList.remove('skeleton');
    $('#status-panel').innerHTML = `<span class="status-dot"></span><b>Current data: ${esc(meta.data_quality_result)}</b><br>Model updated ${when(meta.created_at_utc)}<br>Market updated ${value.metadata?.created_at_utc ? when(value.metadata.created_at_utc) : 'unavailable'}<br>Stage ${esc(meta.weekly_stage || '—')} · ${esc(meta.run_status)}`;
    if (meta.data_quality_result === 'READY_WITH_WARNINGS') notice('Official Week 1 forecast: some club injury reports are not yet due. Current depth, roster and QB checks passed.');
    if (meta.data_quality_result === 'BLOCKED') notice('Publication blocked: one or more critical current-data checks failed.');
    $('#games').innerHTML = (data.games || []).map((game,index) => gameCard(game,index,byGame[game.game_id] || [],marketByGame[game.game_id] || null)).join('') || '<p class="empty">No fixtures are available.</p>';
  } catch (error) { notice(`Unable to load the locked prediction bundle: ${error.message}`); }
}

function playerCard(player) {
  const useful = player.availability_status === 'QUESTIONABLE' ? 'Questionable' : player.role_status === 'EXPECTED_STARTER' ? 'Expected starter' : player.role_status === 'ROLE_UNCERTAIN' ? 'Role uncertain' : 'Active';
  return `<article class="player-card"><div class="player-top"><div><h2>${esc(player.player_name)}</h2><p class="muted">${esc(player.team)} • ${esc(player.position)} vs ${esc(player.opponent)}</p></div><span class="pill">${pct(player.reconciled_td_probability)} TD</span></div><div class="player-stats"><div><span>CARRIES</span><b>${num(player.expected_carries)}</b></div><div><span>RUSH YD</span><b>${num(player.expected_rush_yards,0)}</b></div><div><span>TARGETS</span><b>${num(player.expected_targets)}</b></div><div><span>REC YD</span><b>${num(player.expected_receiving_yards,0)}</b></div><div><span>ROLE CONF.</span><b>${esc(player.role_confidence_label || pct(player.role_confidence))}</b></div><div><span>STATUS</span><b>${esc(useful)}</b></div></div></article>`;
}

async function playersPage() {
  try {
    const data = await loadJSON('data/current_week.json');
    const all = (data.games || []).flatMap(game => (game.players || []).map(player => ({...player, game_id:game.game_id, game_label:`${game.away_team} @ ${game.home_team}`})));
    $('#players-timestamp').textContent = `Model updated ${when(data.metadata?.created_at_utc)} · Stage ${data.metadata?.weekly_stage || '—'}`;
    const gameOptions = [...new Map(all.map(player => [player.game_id, player.game_label])).entries()];
    $('#game-filter').insertAdjacentHTML('beforeend', gameOptions.map(([id,label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join(''));
    const teams = [...new Set(all.map(player => player.team))].sort();
    $('#team-filter').insertAdjacentHTML('beforeend', teams.map(team => `<option>${esc(team)}</option>`).join(''));
    const render = () => {
      const query = $('#player-search').value.toLowerCase();
      const game = $('#game-filter').value;
      const team = $('#team-filter').value;
      const position = $('#position-filter').value;
      const sort = $('#player-sort').value;
      let list = all.filter(player => (!query || `${player.player_name} ${player.team}`.toLowerCase().includes(query)) && (!game || player.game_id === game) && (!team || player.team === team) && (!position || player.position === position));
      const top = key => [...list].sort((a,b) => Number(b[key] || 0) - Number(a[key] || 0)).slice(0,6).map(playerCard).join('') || '<p class="empty">No matching players.</p>';
      $('#top-td').innerHTML = top('reconciled_td_probability');
      $('#top-rush').innerHTML = top('expected_rush_yards');
      $('#top-receiving').innerHTML = top('expected_receiving_yards');
      const key = {td:'reconciled_td_probability', rush:'expected_rush_yards', receiving:'expected_receiving_yards', targets:'expected_targets'}[sort];
      list.sort((a,b) => Number(b[key] || 0) - Number(a[key] || 0));
      $('#players').innerHTML = list.map(playerCard).join('') || '<p class="empty">No matching players.</p>';
    };
    ['#player-search','#game-filter','#team-filter','#position-filter','#player-sort'].forEach(selector => $(selector).addEventListener('input',render));
    render();
    if (data.metadata?.data_quality_result === 'READY_WITH_WARNINGS') notice('Some club injury reports are not yet due; exact depth roles are used where available and model-inferred roles are labelled.');
  } catch (error) { notice(`Unable to load player projections: ${error.message}`); }
}

async function performancePage() {
  try {
    const [data, week] = await Promise.all([loadJSON('data/performance.json'), loadJSON('data/current_week.json')]);
    $('#performance-timestamp').textContent = `Model updated ${when(week.metadata?.created_at_utc)}`;
    const labels = {team_score_mae:'Team score MAE',margin_mae:'Margin MAE',total_mae:'Total MAE',winner_accuracy:'Winner accuracy',rushing_yards_mae:'Rush yards MAE',receiving_yards_mae:'Receiving yards MAE',td_brier:'TD Brier'};
    $('#historical').innerHTML = Object.entries(data.historical || {}).map(([key,value]) => `<div class="metric"><strong>${key.includes('accuracy') ? pct(value) : num(value,3)}</strong><span>${esc(labels[key] || key)}</span></div>`).join('');
    $('#live-status').textContent = data.live_2026?.status || 'No live result data';
    $('#live-results').innerHTML = (data.live_2026?.weeks || []).map(row => `<div class="projection-row"><strong>Week ${esc(row.week)}</strong><span>${row.games} games · Score MAE ${num(row.score_mae,2)} · Winner ${pct(row.winner_accuracy)}</span></div>`).join('') || '<p class="muted">The season results populate only after independent completed-game actuals are ingested.</p>';
    $('#market-performance-status').textContent = data.market_value?.status || 'No settled ledger selections';
    $('#market-performance').innerHTML = (data.market_value?.rows || []).map(row => `<div class="projection-row"><strong>${esc(row.market_type)}</strong><span>${row.selections} selections · ${num(row.roi * 100,1)}% ROI</span></div>`).join('') || '<p class="muted">Value performance is separate from predictive accuracy and starts only after official pre-kickoff ledger entries settle.</p>';
  } catch (error) { $('#live-status').textContent = `Unavailable: ${error.message}`; }
}

function valueCard(value) {
  const line = value.line == null ? '' : `<span>${esc(value.side)} ${num(value.line)}</span>`;
  const captured = value.market_timestamp_utc ? ` · Captured ${when(value.market_timestamp_utc)}` : '';
  return `<article class="value-card"><div class="value-card-head"><div><small>${esc(value.market_type.replaceAll('_',' '))} · ${esc(value.bookmaker)}</small><h2>${esc(value.selection)}</h2></div><span class="pill">${esc(value.value_class.replaceAll('_',' '))}</span></div><div class="value-stats">${line}<span>Model ${pct(value.model_probability)}</span><span>Fair ${num(value.model_fair_odds,2)}</span><span>Book ${num(value.book_odds,2)}</span><span>Market ${pct(value.no_vig_probability ?? value.book_implied_probability)}</span><span>Edge ${Number(value.edge_pp) >= 0 ? '+' : ''}${num(value.edge_pp,1)}pp</span><span>EV ${Number(value.ev_pct) >= 0 ? '+' : ''}${num(value.ev_pct,1)}%</span></div><p class="muted">${esc(value.team || '')} · ${esc(value.value_confidence)} confidence · Score ${num(value.value_score,0)}${esc(captured)}</p></article>`;
}

async function valuePage() {
  const [data, week] = await Promise.all([loadOptionalJSON('data/value.json',{metadata:{status:'MARKET_DATA_UNAVAILABLE'},values:[],message:'Market data unavailable'}), loadJSON('data/current_week.json')]);
  $('#value-timestamps').textContent = `Model updated ${when(week.metadata?.created_at_utc)} · Market updated ${data.metadata?.created_at_utc ? when(data.metadata.created_at_utc) : 'unavailable'}`;
  const status = $('#value-status');
  const all = data.values || [];
  if (data.metadata?.status === 'MARKET_DATA_UNAVAILABLE') { status.className = 'notice'; status.textContent = 'Market data unavailable. Independent game and player predictions remain available.'; }
  else { status.className = 'notice value-notice'; status.textContent = data.message || 'Current timestamped market comparison.'; }
  const render = () => {
    const market = $('#value-market').value, team = $('#value-team').value.toLowerCase(), confidence = $('#value-confidence').value, book = $('#value-book').value.toLowerCase();
    const rows = all.filter(value => (!market || value.market_type === market) && (!team || String(value.team || '').toLowerCase().includes(team)) && (!confidence || value.value_confidence === confidence) && (!book || String(value.bookmaker || '').toLowerCase().includes(book)));
    const visible = rows.slice(0,60);
    if (data.metadata?.status !== 'MARKET_DATA_UNAVAILABLE') status.textContent = `${data.message || 'Current timestamped market comparison.'} Showing top ${visible.length} of ${rows.length} matching qualifying selections.`;
    $('#value-list').innerHTML = visible.map(valueCard).join('') || '<p class="empty">No active qualifying market comparisons are available.</p>';
  };
  ['#value-market','#value-team','#value-confidence','#value-book'].forEach(selector => $(selector).addEventListener('input',render));
  render();
}

async function modelPage() {
  const data = await loadOptionalJSON('data/current_week.json',{});
  const element = $('#model-timestamp');
  if (element) element.textContent = `Model updated ${when(data.metadata?.created_at_utc)} · ${esc(data.metadata?.weekly_stage || '—')}`;
}

const page = document.body.dataset.page;
wireTabs();
if (page === 'games') gamesPage();
if (page === 'players') playersPage();
if (page === 'value') valuePage();
if (page === 'performance') performancePage();
if (page === 'model') modelPage();
