/* Explicit presentation contracts. Research never enters the official renderer.
 * @typedef {{kind:'experimental-research',source:object,fixture:object}} ResearchView
 * @typedef {{kind:'official-historical',source:object,metadata:object}} OfficialView
 * @typedef {ResearchView|OfficialView} ForecastView
 * Runtime validation below is mandatory even when callers use static typing.
 */
(function(root){
 'use strict';
 const SOURCE_SHA='b196e9dad4f55f716e222aa939d2d88445eab7b84149f776e76ea2a7f99afdc3';
 const TEAM_SHA='3c31726c7ca9bea5a4683128b5b0befe0f7d4635608a1d6e7ac43af8b2268fec';
 const PLAYER_SHA='397b4353d921511f29252f28851578285ed6b6b82208a5c0bdc0da6ace171d3a';
 function requireValue(ok,message){if(!ok)throw Error(message);}
 function freeze(x){if(x&&typeof x==='object'&&!Object.isFrozen(x)){Object.freeze(x);Object.values(x).forEach(freeze);}return x;}
 function research(data,fixtures){
  requireValue(data.schema==='weekly-prospective-research-v1'&&data.model_certification==='NOT_PRODUCTION_CERTIFIED','Unsupported research contract');
  requireValue(data.games.length===14&&fixtures.length===14,'Research fixture population differs');
  const ids=new Set(),players=new Set();
  const result=data.games.map(g=>{
   const fixture=fixtures.find(f=>f.game_id===g.game_id);
   requireValue(fixture&&g.game_id===`2026_08_${g.away_team}_${g.home_team}`&&fixture.away_team===g.away_team&&fixture.home_team===g.home_team&&Date.parse(fixture.kickoff_utc)===Date.parse(g.kickoff_utc),'Research fixture identity differs');
   requireValue(!ids.has(g.game_id),'Duplicate research fixture');ids.add(g.game_id);
   requireValue(g.actionable===false&&g.model_certification==='NOT_PRODUCTION_CERTIFIED'&&g.model_sha256===TEAM_SHA&&g.player_model_sha256===PLAYER_SHA,'Research certification/model differs');
   for(const version of [g.forecast,g.original_forecast]){
    requireValue(version&&['away_expected_points','home_expected_points','model_margin','model_total'].every(k=>Number.isFinite(version[k])),'Missing research scores');
    requireValue(Math.abs(version.model_total-version.home_expected_points-version.away_expected_points)<1e-8&&Math.abs(version.model_margin-version.home_expected_points+version.away_expected_points)<1e-8,'Incoherent research scores');
   }
   requireValue(Date.parse(g.original_created_at_utc)<Date.parse(g.cutoff_utc)&&Date.parse(g.created_at_utc)<Date.parse(g.cutoff_utc)&&Date.parse(g.cutoff_utc)<Date.parse(g.kickoff_utc),'Invalid research clocks');
   g.players.forEach(p=>{
    const key=`${p.game_id}|${p.team}|${p.player_id}`;
    requireValue(p.game_id===g.game_id&&[g.away_team,g.home_team].includes(p.team)&&p.opponent===(p.team===g.away_team?g.home_team:g.away_team)&&!players.has(key),'Research player identity differs');players.add(key);
    requireValue(p.actionable===false&&p.verified_active===false&&p.model_sha256===PLAYER_SHA&&p.anytime_td_probability===null&&p.passing_yards===null&&p.quarter_scores===null,'Unsupported research player scope');
    requireValue(['research_rushing_yards','research_receiving_yards','research_carries','research_targets'].every(k=>Number.isFinite(p[k])&&p[k]>=0),'Missing conditional projection');
   });
   // The discriminant is outside the immutable source. No invented official run ID.
   return freeze({kind:'experimental-research',source:g,fixture});
  });
  requireValue(players.size===409,'Conditional player population differs');
  return freeze(result);
 }
 function official(data){
  requireValue(data.metadata?.run_status==='OFFICIAL_PRODUCTION'||data.metadata?.run_id,'Missing original forecast identity');
  requireValue(data.games.every(g=>g.week!==8),'Research week cannot use the official renderer');
  return data.games.map(g=>({kind:'official-historical',source:g,metadata:data.metadata}));
 }
 function state(g,now=Date.now()){
  if(['FINAL','LIVE','AMBIGUOUS'].includes(g.state))return g.state;
  return now>=Date.parse(g.cutoff_utc)?'CLOSED':g.state==='PRE_KICKOFF'?'PRE_KICKOFF':'AMBIGUOUS';
 }
 const api={SOURCE_SHA,TEAM_SHA,PLAYER_SHA,research,official,state};
 if(typeof module!=='undefined')module.exports=api;else root.ForecastAdapter=api;
})(globalThis);
