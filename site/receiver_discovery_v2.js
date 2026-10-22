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
