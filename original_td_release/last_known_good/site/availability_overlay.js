"use strict";
// A separate dated eligibility layer. No frozen numeric field is modified.
let currentAvailability=null,availabilityInstalled=false,availabilityLoading=null;
const availabilityTarget={season:Number(document.body.dataset.availabilitySeason),week:Number(document.body.dataset.availabilityWeek),run:document.body.dataset.availabilityRun};
function availabilityScope(g){return Number(g?.season)===availabilityTarget.season&&Number(g?.week)===availabilityTarget.week;}
function availabilityState(p,g,now=Date.now()){
 if(!p||!g)return {state:'UNKNOWN',reason:'Player or fixture identity missing'};
 if(!availabilityScope(g))return {state:'HISTORICAL',reason:'Frozen historical forecast'};
 const cutoff=Date.parse(g.kickoff_utc)-60000;
 if(!Number.isFinite(cutoff)||now>=cutoff)return {state:'CLOSED',reason:'Game cutoff passed — original forecast only'};
 const a=currentAvailability,run=g.prediction_source_run_id||g.prediction_run_id||availabilityTarget.run;
 if(!a||a.schema!=='current-availability-v1'||a.season!==availabilityTarget.season||a.week!==availabilityTarget.week||a.prediction_run_id!==run||run!==availabilityTarget.run)return {state:'UNKNOWN',reason:'Current availability evidence unavailable'};
 if(!Number.isFinite(Date.parse(a.created_at_utc))||Date.parse(a.created_at_utc)>now)return {state:'UNKNOWN',reason:'Availability observation time is invalid/future'};
 const fixture=a.fixtures?.[g.game_id];
 if(!fixture||fixture.away_team!==g.away_team||fixture.home_team!==g.home_team)return {state:'UNKNOWN',reason:'Current fixture evidence missing/conflicting'};
 if(fixture.state!=='PRE_KICKOFF')return {state:'CLOSED',reason:'Fixture is not pre-kickoff — original forecast only'};
 const r=a.players?.[`${g.game_id}|${p.player_id}|${p.team}`];
 if(!r||r.game_id!==g.game_id||r.player_id!==p.player_id||r.team!==p.team||r.prediction_source_run_id!==run)return {state:'UNKNOWN',reason:'Exact current player identity evidence missing'};
 if(r.state==='UNAVAILABLE')return {state:'UNAVAILABLE',reason:r.reason||'Verified unavailable'};
 if(r.current_role==='NOT_EXPECTED_STARTER')return {state:'ROLE_CHANGED',reason:'Current evidence identifies a backup; original starter-only forecast withheld'};
 if(!Number.isFinite(Date.parse(a.created_at_utc))||Date.parse(a.created_at_utc)>now||!Number.isFinite(Date.parse(a.valid_until_utc))||now>=Date.parse(a.valid_until_utc))return {state:'UNKNOWN',reason:'Availability check expired — a new check is required'};
 if(r.current_role==='QB_SELECTION_UNRESOLVED')return {state:'UNKNOWN',reason:'Expected quarterback is unresolved'};
 if(r.state==='EXPECTED_ELIGIBLE')return {state:'ELIGIBLE',reason:'No current adverse designation; not confirmed healthy or game-day active'};
 return {state:r.state==='UNCERTAIN'?'UNCERTAIN':'UNKNOWN',reason:r.reason||'Availability unconfirmed; final report/inactives pending'};
}
function availabilityGameFor(p){const f=currentAvailability?.fixtures?.[p.game_id];return f?{...f,season:availabilityTarget.season,week:availabilityTarget.week,prediction_source_run_id:p.prediction_source_run_id||availabilityTarget.run}:{game_id:p.game_id,season:Number(String(p.game_id||'').slice(0,4)),week:Number(String(p.game_id||'').slice(5,7)),kickoff_utc:p.kickoff_utc,prediction_source_run_id:p.prediction_source_run_id};}
function availabilityPlayerMarket(v){return !!v.player_id||/player|anytime|touchdown|rushing|receiving/i.test(String(v.market_type||''));}
function availabilityReference(rows,g,kind,label){
 if(!rows.length)return '';
 const field={td:'reconciled_td_probability',rush:'expected_rush_yards',receive:'expected_receiving_yards'}[kind];
 const sorted=rows.slice().sort((a,b)=>Number(b[field])-Number(a[field]));
 return `<details class="availability-reference"><summary>${escapeHTML(label)} (${rows.length})</summary><p class="muted">Original frozen forecasts only. These rows are not current actionable selections. No opportunities or probabilities have been recalculated.</p><div class="rows">${sorted.map(p=>{
  const a=availabilityState(p,g),value=kind==='td'?percent(p[field]):number(p[field],1)+' yards';
  return `<div class="row" data-availability-reference="${escapeHTML(p.player_id+'|'+p.team)}" data-availability-state="${a.state}"><span><b>${escapeHTML(p.player_name)}</b><small>${escapeHTML(p.team)} · ${escapeHTML(p.position)} · Original Expected Starter</small><small class="availability-status">${escapeHTML(a.reason)}</small></span><span>${value}<small>Original forecast</small>${kind==='rush'?'<small>'+number(p.expected_carries)+' original carries</small>':kind==='receive'?'<small>'+number(p.expected_targets)+' original targets</small>':''}</span></div>`;
 }).join('')}</div></details>`;
}
function installAvailabilitySafety(){
 if(availabilityInstalled)return;availabilityInstalled=true;
 if(typeof cardV31==='function'){
  const originalCard=cardV31;
  cardV31=function(g,values,market=null){
   const rendered=originalCard(g,values,market);
   if(!availabilityScope(g))return rendered;
   const f=currentAvailability?.fixtures?.[g.game_id],r=f?.final_result;
   if(f?.state!=='FINAL'||!r||r.status!=='FINAL'||!Number.isInteger(r.away_points)||!Number.isInteger(r.home_points))return rendered;
   const result=`<section class="availability-reference" aria-label="Validated final result"><b>FINAL · ${escapeHTML(g.away_team)} ${r.away_points} – ${r.home_points} ${escapeHTML(g.home_team)}</b><p class="muted">Completed game. The original pregame forecast below is historical; player props and captured prices are closed.</p></section>`;
   return rendered.replace('<div class="game-meta">',result+'<div class="game-meta">');
  };
 }
 const originalStarter=typeof expectedStarter==='function'?expectedStarter:null;
 if(originalStarter){
  expectedStarter=function(p,g){if(!originalStarter(p,g))return false;const a=availabilityState(p,g);return ['HISTORICAL','ELIGIBLE'].includes(a.state);};
  const originalProps=propRows;
  propRows=function(players,g,kind){
   if(!availabilityScope(g))return originalProps(players,g,kind);
   const meaningful=p=>kind==='td'?Number(p.reconciled_td_probability)>=.05:kind==='rush'?Number(p.expected_carries)>=1||Number(p.expected_rush_yards)>=5:Number(p.expected_targets)>=1||Number(p.expected_receiving_yards)>=8;
   const population=players.filter(p=>originalStarter(p,g)&&meaningful(p));
   const ready=population.filter(p=>availabilityState(p,g).state==='ELIGIBLE');
   const pending=population.filter(p=>['UNKNOWN','UNCERTAIN'].includes(availabilityState(p,g).state));
   const held=population.filter(p=>['UNAVAILABLE','ROLE_CHANGED','CLOSED'].includes(availabilityState(p,g).state));
   return `<div class="availability-props"><p class="availability-status">${ready.length?'Current eligible original projections':'No current actionable selections: availability is unconfirmed or selections are withheld.'}</p><div class="availability-actionable">${ready.length?originalProps(ready,g,kind):''}</div>${availabilityReference(pending,g,kind,'Original forecasts — availability unconfirmed')}${availabilityReference(held,g,kind,'Unavailable, role changed or closed — original forecasts')}</div>`;
  };
 }
 const originalFreshness=quoteFreshness;
 quoteFreshness=function(v,now=Date.now()){
  if(availabilityPlayerMarket(v)){
   const a=availabilityState(v,availabilityGameFor(v),now);
   if(!['HISTORICAL','ELIGIBLE'].includes(a.state))return a.state==='UNAVAILABLE'?'PLAYER UNAVAILABLE · HISTORICAL COMPARISON ONLY':a.state==='ROLE_CHANGED'?'STARTER ROLE CHANGED · HISTORICAL COMPARISON ONLY':a.state==='CLOSED'?'GAME CLOSED · HISTORICAL COMPARISON ONLY':'AVAILABILITY UNCONFIRMED · HISTORICAL COMPARISON ONLY';
  }
  return originalFreshness(v,now);
 };
 if(typeof playerCard==='function'){
  const original=playerCard;
  playerCard=function(p){const g=availabilityGameFor(p);if(!availabilityScope(g))return original(p);const a=availabilityState(p,g);return original(p).replace('</h3>',`</h3><p class="availability-status" data-current-availability="${a.state}">${escapeHTML(a.reason)} · Original forecast reference</p>`).replace('<p class="muted">','<p class="muted">Frozen role at prediction time: ');};
 }
 if(typeof valueCard==='function'){
  const original=valueCard;
  valueCard=function(v){if(!availabilityPlayerMarket(v))return original(v);const a=availabilityState(v,availabilityGameFor(v));return original(v).replace('</h3>',`</h3><p class="availability-status">${escapeHTML(a.reason)} · Captured comparison only</p>`);};
 }
}
async function loadAvailabilityOverlay(){
 if(availabilityLoading)return availabilityLoading;
 availabilityLoading=(async()=>{
  try{const response=await fetch('data/current_availability.json',{cache:'no-store'});if(!response.ok)throw Error('Availability unavailable');const a=await response.json();if(a.schema!=='current-availability-v1'||a.season!==availabilityTarget.season||a.week!==availabilityTarget.week||a.prediction_run_id!==availabilityTarget.run)throw Error('Availability identity differs');currentAvailability=a;}catch{currentAvailability=null;}
  installAvailabilitySafety();
  const notice=document.getElementById('availability-notice');
  if(notice){const expired=currentAvailability&&Date.now()>=Date.parse(currentAvailability.valid_until_utc);notice.textContent=currentAvailability?`Week ${currentAvailability.week} eligibility ${expired?'check expired; current selections are withheld. Last checked':'checked'} ${ukTime(currentAvailability.created_at_utc)}. Unavailable players are withheld; unknown availability is not clearance. Original forecast values and captured prices remain unchanged, including historical weeks. Next checkpoint ${ukTime(currentAvailability.next_checkpoint_utc)}.`:'Current availability evidence is unavailable. Current player selections are withheld. Original forecasts remain available as references.';}
 })();return availabilityLoading;
}
