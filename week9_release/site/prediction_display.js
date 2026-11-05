/* Derived display adapter. No model fitting, forecast mutation or market inference. */
const PredictionDisplay=(()=>{
 let data=null;
 async function load(){const r=await fetch('data/prediction_display.json',{cache:'no-store'});if(!r.ok)throw Error('Display artifact unavailable');data=await r.json();if(data.schema!=='prediction-first-display-v1')throw Error('Unsupported display schema');}
 function score(g){const r=data?.scorelines.find(x=>x.game_id===g.game_id);if(!r)return null;if(r.away_team!==g.away_team||r.home_team!==g.home_team||r.source_model_sha256!==g.model_sha256||Object.keys(r.raw_forecast).some(k=>r.raw_forecast[k]!==g.forecast[k]))return null;return r;}
 function passing(g){return (data?.passing||[]).filter(p=>p.game_id===g.game_id&&g.quarterbacks.some(q=>q.team===p.team&&q.expected_qb_id===p.player_id&&q.status==='EXPECTED_STARTER')&&!['OUT','IR','INACTIVE','RELEASED','PRACTICE_SQUAD'].includes(g.availability[p.team+'|'+p.player_id]?.status));}
 return {load,score,passing};})();
