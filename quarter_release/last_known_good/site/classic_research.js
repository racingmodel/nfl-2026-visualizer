/* V3.1 presentation, separate official and research view models. No inference. */
'use strict';
const CLASSIC_RESEARCH_LABEL='Research / Experimental — Not Production Certified';
let hostedCatalog;let selectedWeek=8,selectedTab='rush',researchViews=[],historicalData,contextV32,marketData,receiverData;
const e=escapeHTML;
const finite=v=>typeof v==='number'&&Number.isFinite(v);
const dateLabel=v=>v?ukTime(v):'Not supplied';
const fullTeam={ARI:'Arizona Cardinals',ATL:'Atlanta Falcons',BAL:'Baltimore Ravens',BUF:'Buffalo Bills',CAR:'Carolina Panthers',CHI:'Chicago Bears',CIN:'Cincinnati Bengals',CLE:'Cleveland Browns',DAL:'Dallas Cowboys',DEN:'Denver Broncos',DET:'Detroit Lions',GB:'Green Bay Packers',HOU:'Houston Texans',IND:'Indianapolis Colts',JAX:'Jacksonville Jaguars',KC:'Kansas City Chiefs',LA:'Los Angeles Rams',LAC:'Los Angeles Chargers',LV:'Las Vegas Raiders',MIA:'Miami Dolphins',MIN:'Minnesota Vikings',NE:'New England Patriots',NO:'New Orleans Saints',NYG:'New York Giants',NYJ:'New York Jets',PHI:'Philadelphia Eagles',PIT:'Pittsburgh Steelers',SEA:'Seattle Seahawks',SF:'San Francisco 49ers',TB:'Tampa Bay Buccaneers',TEN:'Tennessee Titans',WAS:'Washington Commanders'};
function f2(v){if(!finite(v))return 'Withheld';return v.toFixed(2);}
function viewGames(){return selectedWeek>=8?researchViews:historicalData.games;}
function rawGame(view){return view.kind==='experimental-research'?view.source:view;}
function researchStatus(p,g){const a=g.availability[`${p.team}|${p.player_id}`];return a?.status||p.medical_status||'UNKNOWN';}
function nowState(g){return ForecastAdapter.state(g);}
function historicalSignal(p){
 const rows=Object.values(receiverData.players||{}).filter(x=>x.player_id===p.player_id&&x.team===p.team&&x.category==='UNDER_THE_RADAR');
 return rows.find(x=>finite(x.prior3_targets)&&finite(x.prior8_targets)&&x.prior3_targets>x.prior8_targets&&x.target_rank>2&&x.recent_rank<=3);
}
function historicalPlayers(){return historicalData.games.flatMap(g=>(g.players||[]).map(p=>({...p,game_label:`${g.away_team} @ ${g.home_team}`,prediction_source_run_id:g.prediction_source_run_id||g.prediction_run_id||historicalData.metadata.run_id,prediction_cutoff_utc:g.prediction_cutoff_utc||historicalData.metadata.cutoff_utc})));}
function researchPlayers(){return researchViews.flatMap(v=>v.source.players.map(p=>({...p,game_label:`${v.source.away_team} @ ${v.source.home_team}`,availability_display:researchStatus(p,v.source),research_td_probability:OriginalTD.probability(v.source,p)})));}
function updateNavigation(){for(const link of document.querySelectorAll('header a[href]')){const path=link.getAttribute('href').split('?')[0];link.href=path+'?week='+selectedWeek;}}
function provenance(){
 const r=selectedWeek>=8;
 query('#source-state').innerHTML=r?`<b>Experimental research</b><br>${researchViews.length} matchups · ${researchViews.reduce((n,v)=>n+v.source.players.length,0)} player references<br>Not production certified`:`<b>Original Week ${selectedWeek} forecasts</b><br>Frozen ${dateLabel(historicalData.metadata.created_at_utc)}<br>Historical references · all elapsed games closed`;
 const notice=query('#availability-notice');if(notice)notice.textContent=r?'Research / Experimental — Not Production Certified. Player yardage assumes participation; availability is unconfirmed and all selections are non-actionable.':'Historical forecasts · completed games and player props are closed. Original values preserved; the Week 7 availability overlay is expired.';
 const label=query('#week-nav-label');if(label)label.textContent=`2026 · Week ${selectedWeek}${r?' · Research':' · Historical'}`;
 if(query('#previous-week'))query('#previous-week').disabled=selectedWeek<=1;
 if(query('#next-week'))query('#next-week').disabled=selectedWeek>=Math.max(...Object.keys(hostedCatalog.periods).map(Number));
 updateNavigation();
}
function tabs(id,contents,active='rush'){
 const labels=[['td','Anytime TD'],['rush','Rushing'],['receive','Receiving'],['pass','QB Passing']];
 return `<div class="props-tabs" role="tablist" aria-label="Player Props">${labels.map(([k,l])=>`<button role="tab" id="${id}-${k}-tab" aria-controls="${id}-${k}" aria-selected="${k===active}" tabindex="${k===active?0:-1}">${l}</button>`).join('')}</div>${labels.map(([k])=>`<section role="tabpanel" id="${id}-${k}" aria-labelledby="${id}-${k}-tab" tabindex="0" ${k===active?'':'hidden'}>${contents[k]}</section>`).join('')}`;
}
const withheld={td:'Anytime TD estimates unavailable: the current player model has no compatible team-touchdown budget. Historical TD forecasts remain available.',pass:'Passing estimate unavailable: expected QB evidence or sufficient prior passing history is unresolved.'};
function passingRows(g){const rows=PredictionDisplay.passing(g);return rows.length?'<p class="muted">Conditional research · prior passing appearances, not confirmed starters or betting projections.</p>'+rows.map(p=>`<div class="row passing-player" data-player-id="${e(p.player_id)}" data-game-id="${e(p.game_id)}" data-team="${e(p.team)}" data-actionable="false"><span><b>${e(p.player_name)}</b><small>${e(p.team)} vs ${e(p.opponent)} · Expected QB</small><small>Participation unconfirmed</small></span><span><b>${p.passing_yards.toFixed(1)} yards</b><small>${p.passing_attempts.toFixed(1)} attempts</small></span></div>`).join('')+'<details><summary>Passing research details</summary><p class="muted">Mean of up to eight previous passing appearances; no fitted weights, game-script adjustment or calibrated intervals. Historical publication vintages unproved. Created '+dateLabel(rows[0].created_at_utc)+'. Not Production Certified; no probabilities or fair odds.</p></details>':'<p class="muted">'+withheld.pass+'</p>'; }
function originalTdRows(g){let rows=OriginalTD.rows(g);if(!rows.length)return '<p class="muted">TD estimates withheld: '+(g.quarterbacks.some(q=>q.status==='SOURCE_CONFLICT')?'paired quarterback/player-source evidence remains unresolved.':'a compatible original TD component is unavailable for this fixture.')+'</p>';const top=[];for(const team of [g.away_team,g.home_team])top.push(...rows.filter(p=>p.team===team).sort((a,b)=>b.experimental_td_probability-a.experimental_td_probability||a.player_id.localeCompare(b.player_id)).slice(0,6));rows=top.sort((a,b)=>b.experimental_td_probability-a.experimental_td_probability);return '<p class="td-research-label">Experimental TD Probability — Not Betting Certified</p><p class="muted">Original trained model · conditional on participation. Top six per team; full roster used for reconciliation. Not game-day cleared.</p><div class="rows">'+rows.map(p=>`<div class="row conditional-player td-research-player" data-player-id="${e(p.player_id)}" data-team="${e(p.team)}" data-game-id="${e(p.game_id)}" data-actionable="false"><span><b>${e(p.player_name)}</b><small>${e(p.team)} · ${e(p.position)} vs ${e(p.opponent)}</small>${p.medical_status==='UNKNOWN'?'':`<small class="availability-status">${e(p.medical_status)}</small>`}</span><span><b>${percent(p.experimental_td_probability)}</b><small>Conditional TD estimate</small></span></div>`).join('')+'</div><details><summary>TD model & probability details</summary><p class="muted">Original phase2-v2 logistic model, unchanged weights. Original raw-point budget and team logit adjustment; no fair odds or betting recommendation. Created '+dateLabel(rows[0].created_at_utc)+'. Medical/source history remains separately disclosed; final report certification is incomplete.</p></details>';}
function researchRows(g,kind){
 const receive=kind==='receive',field=receive?'research_receiving_yards':'research_rushing_yards';
 const rows=g.players.filter(p=>!receive||['WR','TE','RB','FB'].includes(p.position)).slice().sort((a,b)=>b[field]-a[field]||a.player_id.localeCompare(b.player_id));
 const filters=receive?`<div role="group" aria-label="Receiving filters" class="receiving-controls">${[['ALL','All'],['WR','WR'],['TE','TE'],['RB','RB'],['RADAR','Under the Radar']].map(([k,l])=>`<button class="receiving-filter-v3" data-receiving-filter="${k}" aria-pressed="${k==='ALL'}">${l}</button>`).join('')}</div><p class="muted">Single list · highest projected yards first. Under-the-Radar uses the preserved Week 7 pregame usage signal, not new Week 8 starter evidence.</p>`:'';
 return `<div class="${receive?'receiving-list-v3':'research-rushing-list'}">${filters}<p class="muted">Participation-conditional means, including reserves. No starter promotion, opportunity redistribution or calibrated ranges.</p><div class="rows">${rows.map(p=>{
  const a=researchStatus(p,g),signal=!!historicalSignal(p);
  return `<div class="row conditional-player ${receive?'receiving-player-v3':''}" data-player-id="${e(p.player_id)}" data-team="${e(p.team)}" data-receiver-key="${e(p.player_id+'|'+p.team)}" data-position="${e(p.position)}" data-radar="${signal}" data-actionable="false"><span><b>${e(p.player_name)}</b><small>${e(p.team)} · ${e(p.position)} vs ${e(p.opponent)}</small>${a==='UNKNOWN'?'':`<small class="availability-status">${e(a)}</small>`}${signal?'<small class="warning">Under-the-Radar · historical usage signal</small>':''}</span><span><b>${p[field].toFixed(1)} yards</b><small>${(receive?p.research_targets:p.research_carries).toFixed(1)} ${receive?'targets':'carries'}</small></span></div>`;
 }).join('')}</div><p class="muted receiving-empty-v3" ${rows.length?'hidden':''}>${g.quarterbacks.some(q=>q.status==='SOURCE_CONFLICT')?'Player forecasts withheld for unresolved QB-source evidence.':'No supported conditional projections in this filter.'}</p></div>`;
}
function researchCard(v){
 if(v.kind!=='experimental-research')throw Error('Wrong renderer for forecast kind');
 const g=v.source,f=g.forecast,state=nowState(g),display=PredictionDisplay.score(g),spread=f.model_margin===null?'Withheld':f.model_margin>0?`${g.home_team} −${f2(f.model_margin)}`:f.model_margin<0?`${g.away_team} −${f2(-f.model_margin)}`:'Pick’em';
 const qbs=g.quarterbacks.map(q=>`${q.team}: ${q.expected_qb_name||'Unverified'} (${q.status.replaceAll('_',' ')}; medical ${q.medical_status}; ${q.starter_confirmed?'starter confirmed':'starter unconfirmed'})`).join(' · ');
 const props=tabs('research-'+g.game_id,{td:originalTdRows(g),rush:researchRows(g,'rush'),receive:researchRows(g,'receive'),pass:passingRows(g)});
 return `<article class="card card-v31 research-card" data-game-id="${e(g.game_id)}" data-forecast-kind="experimental-research" data-actionable="false"><div class="game-meta"><b>${e(g.away_team)} @ ${e(g.home_team)}</b><span>${dateLabel(g.kickoff_utc)}</span><span>${e(v.fixture.stadium||'')}</span><span class="tag">${e(state)}</span></div><div class="scoreboard" aria-label="Plausible snapped scoreline — experimental research"><div><div class="team-code">${teamLogo(g.away_team)}${e(g.away_team)}</div><div class="score" data-side="away">${display?display.snapped_away:'—'}</div><div class="team-name">${e(fullTeam[g.away_team])}</div></div><span class="at" aria-label="at">@</span><div><div class="team-code">${teamLogo(g.home_team)}${e(g.home_team)}</div><div class="score" data-side="home">${display?display.snapped_home:'—'}</div><div class="team-name">${e(fullTeam[g.home_team])}</div></div></div><p class="exact-model">Exact model: ${e(g.away_team)} <b>${f2(f.away_expected_points)}</b> – ${e(g.home_team)} <b>${f2(f.home_expected_points)}</b></p><p class="scoreline-label">${display?'Plausible final-score scenario':'Display score awaiting compatible revision'}</p>${display?.toss_up?'<p class="toss-up-label">Toss-up · scenario only, not a winner pick</p>':''}<p class="research-label">${CLASSIC_RESEARCH_LABEL}</p><div class="quick"><div><small>MODEL SPREAD</small><strong>${e(spread)}</strong></div><div><small>MODEL TOTAL</small><strong>${f2(f.model_total)}</strong></div></div><p class="forecast-updated">Research forecast · ${dateLabel(g.original_created_at_utc)}${JSON.stringify(g.forecast)===JSON.stringify(g.original_forecast)?'<br>Team news checked — score unchanged.':'<br>Numerical forecast revised from compatible inputs.'}</p><p class="qb-summary">Expected QBs: ${g.quarterbacks.map(q=>e(q.expected_qb_name||'Unverified')+(q.status==='SOURCE_CONFLICT'?' · unresolved':'')).join(' / ')}</p><p class="quarter-unavailable">Quarter scores unavailable · regulation points not established.</p><details class="forecast-audit"><summary>Forecast details & sources</summary><div class="expanded"><p class="muted">Plausible final-score scenarios use historical scoring frequencies anchored to exact expected points, with final-draw rarity separated from regulation ties. They are not calibrated exact-score probabilities. Toss-up means the raw expected margin is below one point; a displayed winning side is a scenario, not a directional winner selection. Raw expectations, original forecasts and player projections are unchanged. ${e(qbs)}</p><div class="row"><span>Original · ${dateLabel(g.original_created_at_utc)}</span><b>${f2(g.original_forecast.away_expected_points)} @ ${f2(g.original_forecast.home_expected_points)}</b></div><div class="row"><span>Latest · ${dateLabel(g.created_at_utc)}</span><b>${f2(f.away_expected_points)} @ ${f2(f.home_expected_points)}</b></div><p class="muted">${e(g.reason)}. Original freeze: ${e(g.original_freeze_id||'First separately governed QB-neutral research issue; no prior freeze')}. Revision ${e(g.revision_id)}. Cutoff ${dateLabel(g.cutoff_utc)}.</p><p class="muted">Fixed history-only research model. Personnel evidence changes context, not arbitrary team scores. ${state==='PRE_KICKOFF'?'':'Forecast locked; historical reference only.'}</p><details><summary>Full revision and source audit</summary><div class="expanded"><ul>${g.audit.map(a=>`<li>${dateLabel(a.created_at_utc)} · ${e(a.reason)} · ${e(a.revision_id)}</li>`).join('')}</ul>${g.accepted_sources.map(s=>`<p class="muted source-record">${e(s.team||'')} · ${e(s.name||s.expected_qb_name||s.type)} · ${e(s.status||'')}<br>Observed ${dateLabel(s.observed_at_utc)} · Published ${dateLabel(s.published_at_utc)} · Modified ${dateLabel(s.modified_at_utc)}<br>${e(s.source_url)}<br>SHA-256 ${e(s.source_sha256)}</p>`).join('')}</div></details></div></details><details class="player-props"><summary>Player Props · conditional research</summary><div class="expanded">${props}</div></details>${g.state==='FINAL'&&g.actuals?`<div class="final"><small>FINAL · Separate actual result</small><b>${e(g.away_team)} ${e(g.actuals.away_score)} – ${e(g.actuals.home_score)} ${e(g.home_team)}</b></div>`:''}</article>`;
}
function officialGame(g){
 const result=contextV32.fixture_states[g.game_id];
 // Display-only result context. Original model fields and input payload are untouched.
 const display=result?.state==='FINAL'?{...g,result_status:'FINAL',actual_away_score:result.actual_away_score,actual_home_score:result.actual_home_score}:g;
 const values=(marketData.values||[]).filter(x=>x.game_id===g.game_id).map(x=>valueWithFixture(x,historicalData,marketData.metadata));
 const m=(marketData.market_game_context||[]);const market=Array.isArray(m)?m.find(x=>x.game_id===g.game_id):m[g.game_id];
 return cardV31(display,values,market).replace('<article class="card card-v31">',`<article class="card card-v31" data-game-id="${e(g.game_id)}" data-forecast-kind="official-historical" data-actionable="false">`).replace('<p class="winner">','<p class="forecast-label">Original historical forecast · closed</p><p class="winner">');
}
function renderGames(){
 const q=query('#search').value.toLowerCase().trim(),sort=query('#sort').value;
 const list=viewGames().filter(v=>{const g=rawGame(v);return `${fullTeam[g.away_team]} ${fullTeam[g.home_team]} ${g.away_team} ${g.home_team}`.toLowerCase().includes(q);});
 list.sort((a,b)=>{const x=rawGame(a),y=rawGame(b);return sort==='total'?(y.forecast?.model_total??y.model_total)-(x.forecast?.model_total??x.model_total):Date.parse(x.kickoff_utc)-Date.parse(y.kickoff_utc);});
 query('#games').innerHTML=list.map(g=>selectedWeek>=8?researchCard(g):officialGame(g)).join('')||'<p>No matching games.</p>';
 query('#title').textContent=`Week ${selectedWeek} matchups`;
 query('.hero .lede').textContent=selectedWeek>=8?'Matchup predictions and player yardage. Research forecasts, conditional on participation.':'Original pregame forecasts beside separately verified completed results.';
 query('#count').textContent=`${list.length} of ${viewGames().length} games · ${selectedWeek>=8?'Research / Experimental':'Historical official'}`;
 if(query('#source-health'))query('#source-health').textContent=selectedWeek>=8?'Exact published research snapshot. Both QB expectations and medical availability remain distinct. No live feed; full clocks and hashes are available in each forecast audit.':'Historical source evidence and original locks preserved. Current eligibility is closed.';
}
function renderPlayers(){
 const r=selectedWeek>=8,all=r?researchPlayers():historicalPlayers();
 const filters={search:query('#player-search').value,game:query('#game-filter').value,team:query('#team-filter').value,position:query('#position-filter').value};
 let list=filterPlayers(all,filters);
 if(selectedTab==='receive'){
  list=list.filter(p=>['WR','TE','RB','FB'].includes(p.position));
  if(!r&&query('#scope-filter').value!=='all'){
   const eligible=new Set(historicalData.games.flatMap(g=>uniqueReceivingV3(g.players,g).map(p=>`${g.game_id}|${p.player_id}|${p.team}`)));
   list=list.filter(p=>eligible.has(`${p.game_id}|${p.player_id}|${p.team}`));
  }
 }
 if(query('#radar-filter').value==='RADAR')list=list.filter(p=>!!historicalSignal(p));
 if(r&&selectedTab==='td')list=list.filter(p=>finite(p.research_td_probability));
 const key=r?(selectedTab==='td'?'research_td_probability':selectedTab==='receive'?(query('#player-sort').value==='targets'?'research_targets':'research_receiving_yards'):'research_rushing_yards'):({td:'reconciled_td_probability',rush:'expected_rush_yards',receive:query('#player-sort').value==='targets'?'expected_targets':'expected_receiving_yards'}[selectedTab]);
 list.sort((a,b)=>(b[key]??-Infinity)-(a[key]??-Infinity)||a.player_id.localeCompare(b.player_id));
 query('#player-tabs').innerHTML=[['td','Anytime TD'],['rush','Rushing'],['receive','Receiving'],['pass','QB Passing']].map(([k,l])=>`<button role="tab" id="global-${k}" aria-controls="player-panel" aria-selected="${selectedTab===k}" tabindex="${selectedTab===k?0:-1}" data-global-tab="${k}">${l}</button>`).join('');
 query('#player-panel').setAttribute('aria-labelledby','global-'+selectedTab);
 const isWithheld=(!r&&selectedTab==='pass');for(const id of ['position-filter','player-sort','scope-filter','radar-filter'])query('#'+id).closest('label').hidden=selectedTab==='pass';
 query('#props-explanation').textContent=r&&selectedTab==='td'?'Experimental TD Probability — Not Betting Certified. Original model, conditional on participation; no active clearance, fair odds or betting recommendations.':r&&selectedTab==='pass'?'Conditional passing research · prior-eight-appearance means. Expected QBs, not confirmed starters; all references non-actionable.':isWithheld?withheld[selectedTab]:r?'Participation-conditional research means, including reserves. Availability is separate; every reference is non-actionable.':'Original historical forecasts; completed fixtures and player props are closed. Frozen role and availability evidence are preserved.';
 query('#players').innerHTML=r&&selectedTab==='td'&&researchViews.every(v=>OriginalTD.rows(v.source).length===0)?'<p class="warning">TD estimates withheld: no compatible original TD component is available for this week. Other forecasts are preserved.</p>':r&&selectedTab==='pass'?researchViews.map(v=>{const g=v.source;if(filters.game&&g.game_id!==filters.game)return '';const ps=PredictionDisplay.passing(g).filter(p=>(!filters.team||p.team===filters.team)&&(!filters.search||p.player_name.toLowerCase().includes(filters.search.toLowerCase())));return ps.length?'<article class="player-card passing-fixture"><h3>'+e(g.away_team)+' @ '+e(g.home_team)+'</h3>'+passingRows({...g,quarterbacks:g.quarterbacks.filter(q=>ps.some(p=>p.player_id===q.expected_qb_id))})+'</article>':'';}).join('')||'<p>'+withheld.pass+'</p>':isWithheld?`<p class="warning">${withheld[selectedTab]}</p>`:list.map(p=>{
  const signal=historicalSignal(p),displayKey=r?(selectedTab==='td'?'research_td_probability':selectedTab==='receive'?'research_receiving_yards':'research_rushing_yards'):({td:'reconciled_td_probability',rush:'expected_rush_yards',receive:'expected_receiving_yards'}[selectedTab]),value=p[displayKey];
  const opportunities=selectedTab==='td'?'':`<p class="muted">${number(p[r?(selectedTab==='receive'?'research_targets':'research_carries'):(selectedTab==='receive'?'expected_targets':'expected_carries')],1)} projected ${selectedTab==='receive'?'targets':'carries'}</p>`;
  return `<article class="player-card conditional-player" data-player-id="${e(p.player_id)}" data-team="${e(p.team)}" data-game-id="${e(p.game_id)}" data-position="${e(p.position)}" data-actionable="false"><h3>${e(p.player_name)}</h3><p class="muted">${e(p.team)} · ${e(p.position)} vs ${e(p.opponent)}</p><p class="projection-number">${selectedTab==='td'?percent(value):number(value,1)+' yards'}</p>${opportunities}<p class="availability-status">${r?(p.availability_display==='UNKNOWN'?'Participation unconfirmed':e(p.availability_display)):'CLOSED · Historical forecast'}</p><p class="muted">${r?'Conditional reference · no starter promotion':e(p.role_status||'Frozen original role')}</p>${signal&&selectedTab==='receive'?`<p class="warning">Under-the-Radar · historical usage signal</p><p class="muted">${number(signal.prior3_targets)} recent 3-game targets vs ${number(signal.prior8_targets)} prior 8-game targets. Preserved Week 7 pregame evidence; not current Week 8 starter proof.</p>`:''}<details><summary>Original projection & source</summary><p class="muted">Created ${dateLabel(r&&selectedTab==='td'?OriginalTD.rows(researchViews.find(v=>v.source.game_id===p.game_id).source).find(x=>x.team===p.team&&x.player_id===p.player_id)?.created_at_utc:p.created_at_utc||p.prediction_cutoff_utc)}<br>${e(p.player_id)} · ${e(p.game_id)}<br>${r?'Model '+e(selectedTab==='td'?'530313ab8d23d642906943342d88541e27369515f1e0a87c35ad706b94d8011f':p.model_sha256):'Run '+e(p.prediction_source_run_id)}</p></details></article>`;
 }).join('')||'<p>No matching players. Try All positions or clear the filters.</p>';
 query('#player-count').textContent=r&&selectedTab==='pass'?`${query('#players').querySelectorAll('.passing-player').length} conditional QB estimates · separate from 409 original player records`:isWithheld?'Withheld — compatible certified source required':`${list.length} displayed · ${all.length} preserved ${r?'conditional research records':'original public player records'}`;
 query('#radar-filter').disabled=selectedTab!=='receive';
 query('#scope-filter').disabled=r||selectedTab!=='receive';
 query('[data-history-only]').hidden=r;
}
function setupPlayerFilters(){
 const all=selectedWeek>=8?researchPlayers():historicalPlayers();
 query('#game-filter').innerHTML='<option value="">All games</option>'+[...new Map(all.map(p=>[p.game_id,p.game_label]))].map(([id,l])=>`<option value="${e(id)}">${e(l)}</option>`).join('');
 query('#team-filter').innerHTML='<option value="">All teams</option>'+[...new Set(all.map(p=>p.team))].sort().map(t=>`<option value="${e(t)}">${e(fullTeam[t])} (${e(t)})</option>`).join('');
}
function renderValue(){
 const r=selectedWeek>=8;
 query('#value-count').textContent=r?'Week 8 comparisons withheld — no compatible calibrated research probabilities or current executable quotes.':'Historical / Expired — captured Week 7 comparisons, not current recommendations.';
 const all=r||selectedWeek!==7?[]:(marketData.values||[]).map(v=>valueWithFixture(v,historicalData,marketData.metadata));
 const list=filterValues(all,{market:query('#value-market').value,team:query('#value-team').value,confidence:query('#value-confidence').value,book:query('#value-book').value});
 query('#value-list').innerHTML=r?'<p>Research expected scores are independent model outputs. No bookmaker scores, probabilities, fair odds or value recommendations are substituted.</p>':list.slice(0,60).map(valueCard).join('')||'<p>No comparable captured market selections for this week and filter.</p>';
}
async function changeWeek(week){
 selectedWeek=Number(week);if(!Number.isInteger(selectedWeek)||selectedWeek<1||selectedWeek>Math.max(...Object.keys(hostedCatalog.periods).map(Number)))throw Error('Unsupported week');
 if(selectedWeek>=8){await loadHostedWeek(selectedWeek);}else{historicalData=await json(selectedWeek===7?'data/current_week.json':`data/week_${String(selectedWeek).padStart(2,'0')}.json`);ForecastAdapter.official(historicalData);}
 query('#week').value=selectedWeek;
 const url=new URL(location.href);url.searchParams.set('week',selectedWeek);history.replaceState({},'',url);provenance();
 const page=document.body.dataset.page;
 if(page==='integrated-index')renderGames();
 if(page==='integrated-players'){setupPlayerFilters();renderPlayers();}
 if(page==='integrated-value')renderValue();
}

async function loadHostedWeek(week){
 const entry=hostedCatalog.periods[String(week)];if(!entry||week<8||week>18)throw Error('Unsupported research period');
 const response=await fetch(entry.path,{cache:'no-store'});if(!response.ok)throw Error('Research period unavailable');const bytes=await response.arrayBuffer();
 const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(x=>x.toString(16).padStart(2,'0')).join('');if(digest!==entry.sha256)throw Error('Research source hash differs');
 const data=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));researchViews=ForecastAdapter.research(data,entry.fixtures,{week,expectedPlayers:entry.player_count});window.researchPayload=data;
}

async function classicStart(){
 hostedCatalog=await json('data/hosted_catalog.json');
 if(hostedCatalog.schema!=='hosted-classic-catalog-v1'||hostedCatalog.static_parent_release!=='frontend-classic-week8-974591f0e1ca648d'||hostedCatalog.model_hashes.team!==ForecastAdapter.TEAM_SHA||hostedCatalog.model_hashes.player!==ForecastAdapter.PLAYER_SHA)throw Error('Hosted scope/model differs');
 await OriginalTD.load();await PredictionDisplay.load();await loadHostedWeek(hostedCatalog.current_week);
 const digest=hostedCatalog.periods[String(hostedCatalog.current_week)].sha256;
 [contextV32,receiverData,historicalData,marketData]=await Promise.all(['dashboard_context_v32','receiver_discovery','current_week','value'].map(x=>json('data/'+x+'.json')));
 starterEvidence=(await json('data/player_presentation.json')).players;receiverEvidenceV2=receiverData;

 window.dashboardAudit={researchViews,sourceSha256:digest,originalData:historicalData,receivingWRCount:historicalData.games.reduce((n,g)=>n+uniqueReceivingV3(g.players,g).filter(p=>p.position==='WR').length,0)};
 query('#week').innerHTML=[...[1,2,3,4,5,6,7],...Object.keys(hostedCatalog.periods).map(Number)].map(w=>`<option value="${w}">Week ${w}${w>=8?' · Research':' · Historical'}</option>`).join('');
 const page=document.body.dataset.page;
 query('#week').addEventListener('change',()=>changeWeek(query('#week').value).catch(fail));
 if(page==='integrated-index'){
  query('#search').addEventListener('input',renderGames);query('#sort').addEventListener('change',renderGames);
  query('#sort').innerHTML='<option value="kickoff">Kickoff</option><option value="total">Model total</option>';
  query('#previous-week').onclick=()=>changeWeek(selectedWeek-1).catch(fail);query('#next-week').onclick=()=>changeWeek(selectedWeek+1).catch(fail);
 }
 if(page==='integrated-players'){
  if(matchMedia('(max-width:600px)').matches)query('.player-filter-panel').open=false;
  for(const id of ['player-search','game-filter','team-filter','position-filter','player-sort','radar-filter','scope-filter'])query('#'+id).addEventListener('input',renderPlayers);
  // The common V3.1 handler owns per-game panels. Global tabs share one panel
  // and must not be run through its one-panel-per-button handler.
  document.addEventListener('click',ev=>{const t=ev.target.closest('[data-global-tab]');if(t){ev.stopImmediatePropagation();selectedTab=t.dataset.globalTab;renderPlayers();query('#player-panel').hidden=false;query('#global-'+selectedTab).focus();}},true);
  document.addEventListener('keydown',ev=>{const t=ev.target.closest('[data-global-tab]');if(!t||!['ArrowLeft','ArrowRight','Home','End'].includes(ev.key))return;ev.preventDefault();ev.stopImmediatePropagation();const keys=['td','rush','receive','pass'],i=keys.indexOf(selectedTab);selectedTab=keys[ev.key==='Home'?0:ev.key==='End'?3:(i+(ev.key==='ArrowRight'?1:3))%4];renderPlayers();query('#player-panel').hidden=false;query('#global-'+selectedTab).focus();},true);
 }
 if(page==='integrated-value')for(const id of ['value-market','value-team','value-confidence','value-book'])query('#'+id).addEventListener('input',renderValue);
 if(page==='integrated-performance')await performance();
 if(page==='integrated-model'){
  query('#research-model-identity').textContent='Team '+ForecastAdapter.TEAM_SHA+' · Player '+ForecastAdapter.PLAYER_SHA;
  query('#model-contract').textContent='Historical original model versions: '+Object.entries(historicalData.metadata.model_ids).map(([k,v])=>k+': '+v.version).join(' · ')+'. These are separate from the Week 8 experimental team contract.';
 }
 const requested=Number(new URLSearchParams(location.search).get('week')||hostedCatalog.current_week);await changeWeek(requested);
 document.body.dataset.ready='true';
}
classicStart().catch(error=>{console.error(error);fail(error);document.body.dataset.failed='true';});
