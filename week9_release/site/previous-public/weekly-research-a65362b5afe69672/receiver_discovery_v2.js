"use strict";
// Separately versioned presentation. The legacy module and frozen payloads stay intact.
const receiverEvidenceNode=document.getElementById('receiver-discovery-evidence');
let receiverEvidenceV2;
try{receiverEvidenceV2=JSON.parse(receiverEvidenceNode?.textContent||'{}')}catch{receiverEvidenceV2={};}
function receiverGroupsV2(players,game,contract=receiverEvidenceV2){
 const all=players.filter(p=>p.position==='WR'&&expectedStarter(p,game)&&p.expected_receiving_yards!=null&&Number.isFinite(Number(p.expected_receiving_yards)));
 const featured=[],radar=[];
 for(const p of all){
  const e=contract.players?.[`${game.game_id}|${p.player_id}|${p.team}`];
  const run=game.prediction_source_run_id||game.prediction_run_id;
  if(contract.schema!=='receiver-discovery-v2'||contract.rule!=='top2-targets-plus-prior3-growth-top3-v1'||contract.prediction_run_id!==run||!e||e.prediction_source_run_id!==run||e.game_id!==game.game_id||e.player_id!==p.player_id||e.team!==p.team)continue;
  const cutoff=Date.parse(e.cutoff_utc),known=Date.parse(e.usage_known_at_utc),forecast=Date.parse(game.prediction_cutoff_utc),kickoff=Date.parse(game.kickoff_utc);
  if(!Number.isFinite(cutoff)||!Number.isFinite(known)||!Number.isFinite(forecast)||known>cutoff||cutoff!==forecast||cutoff>=kickoff||!Number.isFinite(e.prior3_targets)||!Number.isFinite(e.prior8_targets)||!Number.isFinite(e.target_rank)||!Number.isFinite(e.recent_rank))continue;
  if(e.category==='FEATURED'&&e.target_rank<=2)featured.push(p);
  else if(e.category==='UNDER_THE_RADAR'&&e.target_rank>2&&e.prior3_targets>e.prior8_targets&&e.recent_rank<=3)radar.push(p);
 }
 const sort=(a,b)=>Number(b.expected_targets)-Number(a.expected_targets)||String(a.player_id).localeCompare(String(b.player_id));
 return {featured:featured.sort(sort),radar:radar.sort(sort),all:all.slice().sort(sort)};
}
function receiverProjectionRowsV2(rows){
 return `<div class="rows">${rows.map(p=>`<div class="row"><span><b>${escapeHTML(p.player_name)}</b><small>${escapeHTML(p.team)} · ${escapeHTML(p.position)} · Expected Starter</small></span><span>${number(p.expected_receiving_yards,0)} yards<small>${number(p.expected_targets)} targets</small><small>${number(p.receiving_yards_low,0)}–${number(p.receiving_yards_high,0)} yards · production band</small></span></div>`).join('')}</div>`;
}
function receivingRowsV2(players,game){
 const g=receiverGroupsV2(players,game);
 const other=players.filter(p=>p.position!=='WR'&&expectedStarter(p,game)&&(Number(p.expected_targets)>=1||Number(p.expected_receiving_yards)>=8)&&p.expected_receiving_yards!=null&&Number.isFinite(Number(p.expected_receiving_yards)));
 const empty='<p class="muted">No receivers with sufficient same-run usage evidence in this category.</p>';
 return `<section aria-label="Featured Receivers"><h3>Featured Receivers</h3><p class="muted">Leading projected target workloads with pregame usage evidence.</p>${g.featured.length?receiverProjectionRowsV2(g.featured):empty}</section><section aria-label="Under-the-Radar Receivers"><h3>Under-the-Radar Receivers</h3><p class="muted">Recent three-game targets exceed the eight-game average, with a top-three recent team role. Usage context, not a value recommendation.</p>${g.radar.length?receiverProjectionRowsV2(g.radar):empty}</section><details class="all-eligible-receivers"><summary>All Eligible Receivers (${g.all.length})</summary>${g.all.length?receiverProjectionRowsV2(g.all):'<p class="muted">No eligible receivers available.</p>'}</details>${other.length?`<section aria-label="Other receiving projections"><h3>Other receiving projections</h3>${receiverProjectionRowsV2(other)}</section>`:''}`;
}
const originalPropRowsV2=propRows;
propRows=function(players,game,kind){return kind==='receive'?receivingRowsV2(players,game):originalPropRowsV2(players,game,kind)};

"use strict";
// Presentation only. Existing frozen eligibility, projections and growth evidence remain authoritative.
function uniqueReceivingV3(players,game){
 const rows=players.filter(p=>['WR','TE','RB','FB'].includes(p.position)&&expectedStarter(p,game)&&p.expected_receiving_yards!=null&&Number.isFinite(Number(p.expected_receiving_yards)));
 const keys=new Set();for(const p of rows){const key=`${game.game_id}|${p.player_id}|${p.team}`;if(keys.has(key))throw new Error('Duplicate receiving forecast identity');keys.add(key)}
 return rows.slice().sort((a,b)=>Number(b.expected_receiving_yards)-Number(a.expected_receiving_yards)||String(a.player_id).localeCompare(String(b.player_id)));
}
function receivingListV3(players,game){
 const all=uniqueReceivingV3(players,game),radar=new Set(receiverGroupsV2(players,game).radar.map(p=>`${p.player_id}|${p.team}`));
 const filters=[['ALL','All'],['WR','WR'],['TE','TE'],['RB','RB'],['RADAR','Under the Radar']];
 const gaps=[game.away_team,game.home_team].filter(t=>players.some(p=>p.team===t&&p.position==='WR')&&!all.some(p=>p.team===t&&p.position==='WR'));
 const warning=gaps.length?`<p class="muted">${gaps.map(escapeHTML).join(', ')} WR starter evidence unavailable. Original projections remain on <a href="players.html">Players</a>.</p>`:'';
 return `<div class="receiving-list-v3"><div role="group" aria-label="Receiving filters" style="display:flex;flex-wrap:wrap;gap:.3rem;margin:.5rem 0">${filters.map(([key,label])=>`<button type="button" class="receiving-filter-v3" data-receiving-filter="${key}" aria-pressed="${key==='ALL'}">${label}</button>`).join('')}</div><p class="muted">Projected receiving yards · highest first. Rising usage is an opportunity signal, not a betting recommendation.</p>${warning}<div class="rows">${all.map(p=>{
 const signal=radar.has(`${p.player_id}|${p.team}`),e=receiverEvidenceV2.players?.[`${game.game_id}|${p.player_id}|${p.team}`];
 const why=signal?`<small>Targets trending up: ${number(e.prior3_targets)} recent 3-game average vs ${number(e.prior8_targets)} prior 8-game average.</small>`:'';
 return `<div class="row receiving-player-v3" data-receiver-key="${escapeHTML(p.player_id+'|'+p.team)}" data-position="${escapeHTML(p.position)}" data-radar="${signal}"><span><b>${escapeHTML(p.player_name)}</b><small>${escapeHTML(p.team)} · ${escapeHTML(p.position)} · Expected Starter</small>${signal?'<small class="warning">Under-the-Radar</small>':''}${why}</span><span>${number(p.expected_receiving_yards,0)} yards<small>${number(p.expected_targets)} targets</small><small>${number(p.receiving_yards_low,0)}–${number(p.receiving_yards_high,0)} yards · production band</small></span></div>`;
 }).join('')}</div><p class="muted receiving-empty-v3" ${all.length?'hidden':''}>No eligible receiving projections in this filter.</p></div>`;
}
const previousReceivingPropRowsV3=propRows;
propRows=function(players,game,kind){return kind==='receive'?receivingListV3(players,game):previousReceivingPropRowsV3(players,game,kind)};
document.addEventListener('click',event=>{
 const button=event.target.closest('.receiving-filter-v3');if(!button)return;
 const panel=button.closest('.receiving-list-v3'),filter=button.dataset.receivingFilter;
 for(const b of panel.querySelectorAll('.receiving-filter-v3'))b.setAttribute('aria-pressed',String(b===button));
 let count=0;for(const row of panel.querySelectorAll('.receiving-player-v3')){const show=filter==='ALL'||(filter==='RB'?['RB','FB'].includes(row.dataset.position):row.dataset.position===filter)||(filter==='RADAR'&&row.dataset.radar==='true');row.hidden=!show;row.style.display=show?'':'none';if(show)count++}
 panel.querySelector('.receiving-empty-v3').hidden=count>0;
});
