"use strict";
// Presentation-only: original prediction payloads remain immutable.
let starterEvidence={};
let passingContract={schema:'certified-passing-v1',status:'NOT_CERTIFIED',predictions:[]};
function expectedStarter(p,g){
 const key=`${g.game_id}|${p.player_id}|${p.team}`,proof=starterEvidence[key];
 const declared=p.role_status==='EXPECTED_STARTER'&&p.depth_role==='STARTER',corrected=proof?.presentation_role_correction===true&&proof.depth_verified===true&&proof.depth_starter===true;
 const inactive=['OUT','IR','PUP','INACTIVE','RELEASED','WAIVED','PRACTICE_SQUAD','RESERVE'];
 return !!proof&&proof.expected_starter===true&&proof.eligible===true&&proof.prediction_source_run_id===(g.prediction_source_run_id||g.prediction_run_id)&&(declared||corrected)&&p.status_eligible!==false&&['QB','RB','FB','WR','TE'].includes(p.position)&&[g.home_team,g.away_team].includes(p.team)&&![p.injury_status,p.availability_status,p.roster_status].some(status=>inactive.includes(String(status).toUpperCase()));
}
function propRows(p,g,kind){
 const field={td:'reconciled_td_probability',rush:'expected_rush_yards',receive:'expected_receiving_yards'}[kind];
 const meaningful=x=>kind==='td'?Number(x.reconciled_td_probability)>=.05:kind==='rush'?Number(x.expected_carries)>=1||Number(x.expected_rush_yards)>=5:Number(x.expected_targets)>=1||Number(x.expected_receiving_yards)>=8;
 const data=p.filter(x=>expectedStarter(x,g)&&meaningful(x)&&x[field]!=null&&Number.isFinite(Number(x[field]))).sort((a,b)=>b[field]-a[field]);
 if(!data.length)return '<p class="muted">No expected-starter projections available.</p>';
 return `<div class="rows">${data.map(x=>`<div class="row"><span><b>${escapeHTML(x.player_name)}</b><small>${escapeHTML(x.team)} · ${escapeHTML(x.position)} · Expected Starter</small></span><span>${kind==='td'?`${percent(x.reconciled_td_probability)}<small>Fair decimal ${number(x.model_fair_odds,2)}</small>`:kind==='rush'?`${number(x.expected_rush_yards,0)} yards<small>${number(x.expected_carries)} carries</small><small>${number(x.rush_yards_low,0)}–${number(x.rush_yards_high,0)} yards · production band</small>`:`${number(x.expected_receiving_yards,0)} yards<small>${number(x.expected_targets)} targets</small><small>${number(x.receiving_yards_low,0)}–${number(x.receiving_yards_high,0)} yards · production band</small>`}</span></div>`).join('')}</div>`;
}
function passingRows(p,g,contract=passingContract){
 // Ingestion/certification gate is independent of the mean model and UI.
 if(contract.schema!=='certified-passing-v1'||contract.status!=='CERTIFIED_PRODUCTION'||!contract.certification_id||contract.prediction_run_id!==(g.prediction_source_run_id||g.prediction_run_id))return '<p class="muted">Passing projections are not yet available in the production model.</p>';
 const values=(contract.predictions||[]).filter(x=>x.game_id===g.game_id&&Number.isFinite(x.expected_passing_yards)&&p.some(y=>y.player_id===x.player_id&&y.team===x.team&&y.position==='QB'&&expectedStarter(y,g)));
 return values.length?`<div class="rows">${values.map(x=>`<div class="row"><span><b>${escapeHTML(x.player_name)}</b><small>${escapeHTML(x.team)} · QB · Expected Starter</small></span><span>${number(x.expected_passing_yards,0)} yards${x.expected_attempts!=null?`<small>${number(x.expected_attempts)} attempts</small>`:''}${x.expected_passing_tds!=null?`<small>${number(x.expected_passing_tds)} passing TDs</small>`:''}${x.interval_certified===true?`<small>${number(x.p10,0)}–${number(x.p90,0)} yards</small>`:''}</span></div>`).join('')}</div>`:'<p class="muted">No expected-starter projections available.</p>';
}
function propsTabs(g){
 const id='props-'+g.game_id.replace(/[^a-zA-Z0-9_-]/g,'-');
 const kinds=[['td','TD Scorers'],['rush','Rushing'],['receive','Receiving'],['pass','Passing']];
 return `<details class="player-props"><summary>Player Props</summary><div class="expanded"><div role="tablist" aria-label="Player Props categories" class="props-tabs">${kinds.map(([kind,label],i)=>`<button type="button" role="tab" id="${id}-${kind}-tab" aria-controls="${id}-${kind}" aria-selected="${i===0}" tabindex="${i===0?0:-1}">${label}</button>`).join('')}</div>${kinds.map(([kind,label],i)=>`<section role="tabpanel" id="${id}-${kind}" aria-labelledby="${id}-${kind}-tab" tabindex="0" ${i?'hidden':''}>${kind==='pass'?passingRows(g.players||[],g):propRows(g.players||[],g,kind)}</section>`).join('')}</div></details>`;
}
function modelWinner(g){
 const valid=v=>v!=null&&v!==''&&Number.isFinite(Number(v))&&Number(v)>=0&&Number(v)<=1;
 if(!valid(g.home_win_probability)||!valid(g.away_win_probability))return {team:'Unavailable',probability:null};
 const home=Number(g.home_win_probability)>=Number(g.away_win_probability);
 return {team:home?g.home_team:g.away_team,probability:home?g.home_win_probability:g.away_win_probability};
}
function marketV31(m,values){
 const complete=m&&[m.market_away_points,m.market_home_points,m.market_total].every(x=>x!=null&&Number.isFinite(Number(x)))&&m.market_spread_label;
 if(!complete)return '<div class="market-v31"><small>MARKET-IMPLIED SCORE ·</small><p class="muted">Unavailable</p></div>';
 const fresh=values.length&&values.every(v=>quoteFreshness(v)==='CAPTURED QUOTE · VERIFY PRICE');
 return `<section class="market-v31" aria-label="Market-derived context"><div class="market-heading"><small>MARKET-IMPLIED SCORE ·</small><span class="tag ${fresh?'':'warning'}">${fresh?'CAPTURED':'STALE / UNVERIFIED'}</span></div><p class="market-score"><b>${number(m.market_away_points,0)} — ${number(m.market_home_points,0)}</b></p><div class="market-fields"><div><small>MARKET LINE</small><b>${escapeHTML(m.market_spread_label)}</b></div><div><small>MARKET O/U</small><b>${number(m.market_total)}</b></div></div></section>`;
}
function cardV31(g,values,market=null){
 const winner=modelWinner(g),spread=g.model_margin==null?'Unavailable':g.model_margin>0?`${g.home_team} -${number(g.model_margin)}`:g.model_margin<0?`${g.away_team} -${number(-g.model_margin)}`:"PICK'EM";
 return `<article class="card card-v31"><div class="game-meta"><b>${escapeHTML(g.away_team)} @ ${escapeHTML(g.home_team)}</b><span>${ukTime(g.kickoff_utc)}</span><span>${escapeHTML(g.stadium||'Stadium unavailable')}</span></div><div class="scoreboard" aria-label="Original model prediction"><div><div class="team-code">${teamLogo(g.away_team)}${escapeHTML(g.away_team)}</div><div class="score">${escapeHTML(g.display_away_score)}</div><div>${escapeHTML(names[g.away_team]||g.away_team)}</div></div><span class="at" aria-label="at">@</span><div><div class="team-code">${teamLogo(g.home_team)}${escapeHTML(g.home_team)}</div><div class="score">${escapeHTML(g.display_home_score)}</div><div>${escapeHTML(names[g.home_team]||g.home_team)}</div></div></div><p class="winner">Model winner ${escapeHTML(winner.team)}</p><div class="quick"><div><small>MODEL SPREAD</small><strong>${escapeHTML(spread)}</strong></div><div><small>MODEL TOTAL</small><strong>${number(g.model_total)}</strong></div><div><small>MODEL WIN CHANCE</small><strong>${percent(winner.probability)}</strong></div></div>${g.result_status==='FINAL'?`<div class="final"><small>FINAL SCORE</small><b>${escapeHTML(g.away_team)} ${escapeHTML(g.actual_away_score)} – ${escapeHTML(g.actual_home_score)} ${escapeHTML(g.home_team)}</b></div>`:''}${marketV31(market,values)}${propsTabs(g)}</article>`;
}
function selectPropTab(tab){
 const list=tab.closest('[role="tablist"]'),root=list.parentElement;
 list.querySelectorAll('[role="tab"]').forEach(button=>{const selected=button===tab;button.setAttribute('aria-selected',String(selected));button.tabIndex=selected?0:-1;root.querySelector('#'+button.getAttribute('aria-controls')).hidden=!selected;});
}
document.addEventListener('click',event=>{const tab=event.target.closest('[role="tab"]');if(tab)selectPropTab(tab);});
document.addEventListener('keydown',event=>{
 const tab=event.target.closest('[role="tab"]');if(!tab||!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
 event.preventDefault();const tabs=[...tab.parentElement.querySelectorAll('[role="tab"]')],i=tabs.indexOf(tab);
 const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(i+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
 selectPropTab(tabs[next]);tabs[next].focus();
});
