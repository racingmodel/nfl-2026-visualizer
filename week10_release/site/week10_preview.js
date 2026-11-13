/* Week 10 presentation and Week 9 results overlay. */
'use strict';
const Week10 = (() => {
  let data, cData, publication;
  const e = value => escapeHTML(String(value ?? ''));
  const n = (value, decimals=2) => Number.isFinite(value) ? value.toFixed(decimals) : 'Unknown';
  const uk = stamp => new Date(stamp).toLocaleString('en-GB', {
    timeZone:'Europe/London', dateStyle:'medium', timeStyle:'short'
  });

  async function load() {
    const response = await fetch('data/week10_context.json', {cache:'no-store'});
    if (!response.ok) throw Error('Week 10 context unavailable');
    const bytes = await response.arrayBuffer();
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)))
      .map(x => x.toString(16).padStart(2,'0')).join('');
    if (hash !== '67e690a077b50e9d75dba4af03f176dcdd4d42b325c553027e59b907e9ee7821') throw Error('Week 10 context hash differs');
    data = JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
    if (data.schema !== 'week10-context-v1' || data.actionable !== false ||
        data.matches.length !== 14 || data.completed.length !== 15) {
      throw Error('Week 10 context contract differs');
    }

    try {
      const response = await fetch('data/model_c_week10.json', {cache:'no-store'});
      if (!response.ok) throw Error('Model C asset unavailable');
      const bytes = await response.arrayBuffer();
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)))
        .map(x=>x.toString(16).padStart(2,'0')).join('');
      if (digest !== '71e819237db60e7b1bbf861a7db49b97e4ddcc749cf1abb558e3fc369aedcf2f') throw Error('Model C asset hash differs');
      const candidate = JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
      if (candidate.schema !== 'week10-model-c-preview-v1' || candidate.actionable !== false ||
          candidate.parent_a_sha256 !== '4a37a8c515ea9d00b299471c5ecac95d86e76d66c3f77a8037b43a1fa7a6454e' || candidate.fixtures.length !== 14)
        throw Error('Model C parent or scope differs');
      cData = candidate;
    } catch {
      cData = null;
    }
    const publicationResponse=await fetch('data/week10_publication_context.json',{cache:'no-store'});
    if(!publicationResponse.ok)throw Error('Publication context unavailable');
    const publicationBytes=await publicationResponse.arrayBuffer();
    const publicationHash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',publicationBytes))).map(x=>x.toString(16).padStart(2,'0')).join('');
    if(publicationHash!=='3ac25116409abf80ef923e1f8de9d09473d737555f693c9f03d5ab5e7480510d')throw Error('Publication context hash differs');
    publication=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(publicationBytes));
    if(publication.schema!=='week10-mixed-publication-context-v1'||publication.release_id!=='frontend-week10-recovery-bd0913766051f62c'||publication.publication_mode!=='MIXED_ARCHIVE_AND_PREGAME'||publication.archived.length!==1||publication.archived[0].game_id!=='2026_10_WAS_NYG'||publication.pregame_fixture_ids.length!==13)throw Error('Publication scope differs');
    if (document.body.dataset.page === 'integrated-performance') {
      document.querySelector('#metrics').insertAdjacentHTML('beforebegin', evaluation());
    }
  }

  function archiveFor(gid){return publication?.archived.find(row=>row.game_id===gid);}
  function isArchived(gid){return Boolean(archiveFor(gid));}
  function game(g) {
    const final=data.completed.find(x=>x.game_id===g.game_id);
    return final?{...g,state:'FINAL',actuals:{away_score:final.away_score,home_score:final.home_score}}:g;
  }
  function publicationNotice(){return ' WAS @ NYG is an archived pregame forecast. Its predictions and captured prices were issued before kickoff, with first publication after cutoff. Market observations are references, not live prices.';}
  function archiveNote(gid,kind='all'){
    const item=archiveFor(gid);if(!item)return '';
    const stamp=kind==='td'?item.td_issued_at_utc:item.forecast_issued_at_utc;
    return '<p class="comparison-market-note archived-pregame-note" data-original-issued-at="'+e(stamp)+'"><b>Archived pregame</b> · '+(kind==='td'?'TD issue ':'Forecast issue ')+e(shortTime(stamp))+'. Original cutoff '+e(shortTime(item.original_pregame_cutoff_utc))+'. Not a post-kickoff forecast or live price.</p>';
  }

  const shortTime = stamp => stamp ? new Intl.DateTimeFormat('en-GB', {
    timeZone:'Europe/London',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',hour12:false
  }).format(new Date(stamp))+' GMT' : 'Unknown';
  const pair = (values, decimals=1) => values.map(value=>n(value,decimals)).join(' – ');
  const range = values => values.map(value=>n(value,1)).join(' to ');
  const pct = value => n(100*value,1)+'%';
  const fixtureC = g => cData?.fixtures.find(row=>row.game_id===g.game_id);
  const flatDetails = markup => markup
    .replace(/<details\b[^>]*>/g,'<section class="w10-detail-group">')
    .replace(/<summary>([\s\S]*?)<\/summary>/g,'<h4>$1</h4>')
    .replace(/<\/details>/g,'</section>');

  function modelCPanel(g) {
    const f=fixtureC(g);
    if(!f)return '<div class="w10-model-c-summary"><small>MODEL C</small><b>Unavailable</b></div>';
    return '<div class="w10-model-c-summary" aria-label="Model C expected scores and total range">'+
      '<div><small>MODEL C · EXPECTED</small><b>'+e(g.away_team)+' '+n(f.C.expected[0],1)+
      ' – '+n(f.C.expected[1],1)+' '+e(g.home_team)+'</b></div>'+
      '<span>80% total <strong>'+pair(f.C.total_interval80)+'</strong></span></div>';
  }

  function distributionDetails(g) {
    const f=fixtureC(g);
    if(!f)return '<p>Model C asset unavailable. Existing predictions remain preserved.</p>';
    const metrics=[
      ['Expected scores',s=>pair(s.expected)],['Median scores',s=>pair(s.median)],
      ['Typical scenario',s=>pair(s.typical,0)],['Low-total scenario',s=>pair(s.low,0)],
      ['High-total scenario',s=>pair(s.high,0)],
      [g.away_team+' 80% range',s=>range(s.away_interval80)],
      [g.home_team+' 80% range',s=>range(s.home_interval80)],
      ['Total 80% range',s=>range(s.total_interval80)],
      ['Home margin 80% range',s=>range(s.home_margin_interval80)],
      ['Absolute margin 80% range',s=>range(s.absolute_margin_interval80)],
      ['Expected total',s=>n(s.expected_total)],['Expected home margin',s=>n(s.expected_home_margin)],
      ['Score correlation',s=>n(s.score_correlation,3)],['Draws',s=>String(s.draws)]
    ];
    const probabilities=[
      ['Total 60+', 'total_60'],['Total below 30','total_under_30'],
      ['Margin 17+','margin_17'],['Margin 24+','margin_24'],['Within 3 points','within_3'],
      ['Either team 35+','either_35'],['Either team 10 or fewer','either_10'],
      [g.away_team+' 35+','away_35'],[g.home_team+' 35+','home_35'],
      [g.away_team+' 10 or fewer','away_10'],[g.home_team+' 10 or fewer','home_10']
    ];
    const rows=metrics.map(([label,value])=>'<tr><th scope="row">'+e(label)+'</th><td>'+e(value(f.C))+ '</td><td>'+e(value(f.NB))+'</td></tr>').join('')+
      probabilities.map(([label,key])=>'<tr data-probability="'+key+'"><th scope="row">'+e(label)+'</th><td>'+pct(f.C.probabilities[key])+'</td><td>'+pct(f.NB.probabilities[key])+'</td></tr>').join('');
    return '<section class="w10-distributions"><h4>Model C distributions &amp; score scenarios</h4>'+
      '<p>Scores are '+e(g.away_team)+' – '+e(g.home_team)+'. Negative binomial remains a separate research challenger.</p>'+
      '<table class="w10-detail-table"><thead><tr><th scope="col">Measure</th><th scope="col">Model C</th><th scope="col">Negative binomial</th></tr></thead><tbody>'+rows+'</tbody></table>'+
      '<p>Prior scoring SD '+n(f.context_score_sd,1)+' · Prior drives '+n(f.context_drives,1)+
      '. Model A inputs '+e(uk(f.a_features_asof_utc))+' GMT. Model C edition '+e(cData.model_c_id)+'.</p>'+
      '<p>Research joint-score distributions. Standalone win probabilities remain unvalidated. Model C uses the pinned Model A parent and eligible lagged scoring and drive history.</p></section>';
  }

  function comparisonDetails(g,m) {
    const d=m.disagreement,signed=value=>(value>0?'+':'')+n(value);
    return '<section class="w10-comparison-details"><h4 id="w10-comparison-'+e(g.game_id)+'" tabindex="-1">Model A / B comparison</h4>'+
      '<p>'+(d.mean_disagreement?'Model means lean different ways':'Model means lean the same way')+
      ': A '+e(d.a_mean_lean)+' / B '+e(d.b_mean_lean)+'. Home margin A '+signed(d.a_home_margin)+
      ' / B '+signed(d.b_home_margin)+'. Total A '+n(d.a_total)+' / B '+n(d.b_total)+'.</p>'+
      '<p>'+(d.mean_disagreement!==d.scenario_disagreement?'Representative score scenarios tell a different story. ':'')+
      'Expected-score leans and simulated scenarios are separate; this is not a betting recommendation.</p></section>';
  }

  function availabilityDetails(g,m) {
    const news=m.news.map(row=>'<p><b>'+e(row.team||'')+' '+e(row.player_name)+'</b> '+e(row.note)+
      ' <a href="'+e(row.source_url)+'">Source</a> · Published '+e(row.published_at_utc?uk(row.published_at_utc)+' GMT':'Unknown')+
      ' · Modified '+e(row.modified_at_utc?uk(row.modified_at_utc)+' GMT':'Unknown')+
      ' · Observed '+e(row.observed_at_utc?uk(row.observed_at_utc)+' GMT':'Unknown')+'</p>').join('');
    const practice=m.practice.map(row=>'<tr><th scope="row">'+e(row.team)+' · '+e(row.player_name)+'</th><td>'+e(row.injury)+
      '</td><td>'+e(row.wednesday||row.tuesday||row.monday||'Unknown')+'</td><td>'+e(row.designation)+'</td></tr>').join('');
    const sources=[...new Map(m.practice.map(row=>[row.source_url,row])).values()].map(row=>'<p><a href="'+e(row.source_url)+
      '">Captured injury report</a> · Published '+e(row.published_at_utc?uk(row.published_at_utc)+' GMT':'Unknown')+
      ' · Modified '+e(row.modified_at_utc?uk(row.modified_at_utc)+' GMT':'Unknown')+
      ' · Observed '+e(row.observed_at_utc?uk(row.observed_at_utc)+' GMT':'Unknown')+'</p>').join('');
    const qbs=g.quarterbacks.map(q=>e(q.team)+': '+e(q.display_name||q.expected_qb_name||'Unverified')+' · '+e(q.status.replaceAll('_',' '))+
      ' · medical '+e(q.medical_status)+' · '+(q.starter_confirmed?'starter confirmed':'starter unconfirmed')).join('<br>');
    const weather=m.weather&&m.weather.forecast_status==='AVAILABLE'?'<p>Venue forecast: '+e(m.weather.weather_condition)+', '+n(m.weather.temperature_f,0)+
      '°F, wind '+n(m.weather.wind_speed_mph,0)+' mph. Display context.</p>':'';
    return '<section class="w10-availability"><h4>Team news, injuries &amp; availability</h4><p>'+qbs+'</p>'+news+weather+
      (m.td_held?'<p class="w10-hold-reason"><b>Anytime TD withheld:</b> '+e(m.td_held.replaceAll('_',' '))+'</p>':'')+
      (practice?'<table class="w10-detail-table w10-injury-table"><thead><tr><th scope="col">Player</th><th scope="col">Injury</th><th scope="col">Practice</th><th scope="col">Status</th></tr></thead><tbody>'+practice+'</tbody></table>'+sources:
       '<p>No additional captured injury table for this fixture. See the retained source audit below.</p>')+
      '<p>Practice participation, blank/dash designations and roster eligibility are not game-day clearance. Unknown remains unknown; no starter promotion is inferred.</p></section>';
  }

  function advanced(g,audit) {
    const m=data.matches.find(row=>row.game_id===g.game_id);
    return '<details class="w10-advanced" id="w10-advanced-'+e(g.game_id)+'"><summary>Advanced details · models, team news &amp; sources</summary>'+
      '<div class="w10-advanced-body">'+archiveNote(g.game_id)+distributionDetails(g)+comparisonDetails(g,m)+availabilityDetails(g,m)+
      '<section class="w10-forecast-history">'+flatDetails(audit)+'</section></div></details>';
  }

  function market(g) {
    const m=data.matches.find(row=>row.game_id===g.game_id);
    if(!m)return '';
    const spread=m.bookmaker_quotes.find(row=>row.market==='Spread'),total=m.bookmaker_quotes.filter(row=>row.market==='Total');
    const odds=value=>(value>0?'+':'')+value;
    return '<div class="comparison-bookmaker"><small>BOOKMAKER SPREAD</small><strong>'+e(g.home_team)+' '+
      (Number.isFinite(m.bookmaker_home_line)?odds(m.bookmaker_home_line):'Unavailable')+'</strong><small>'+ (spread?odds(spread.american_odds):'')+'</small></div>'+
      '<div class="comparison-bookmaker"><small>BOOKMAKER TOTAL</small><strong>'+n(m.bookmaker_total,1)+'</strong><small>'+total.map(row=>e(row.selection)+' '+odds(row.american_odds)).join(' · ')+'</small></div>'+
      '<p class="comparison-market-note market-source-reference" data-provider-updated-at="'+e(spread?.provider_updated_at_utc||'')+
      '" data-retrieved-at="'+e(spread?.retrieved_at_utc||'')+'"><a href="'+e(spread?.source_url||'#')+'" target="_blank" rel="noopener noreferrer">'+
      e(spread?.bookmaker||'Bookmaker')+' via '+e((spread?.source_provider||'Captured source').replace(' reference feed',''))+'</a> · '+(isArchived(g.game_id)?'Archived reference':'Reference')+'<br>'+
      '<span title="'+e(spread?.provider_updated_at_utc||'Unknown')+'">Feed '+e(shortTime(spread?.provider_updated_at_utc))+'</span> · '+
      '<span title="'+e(spread?.retrieved_at_utc||'Unknown')+'">Retrieved '+e(shortTime(spread?.retrieved_at_utc))+'</span><br>Bookmaker update unknown</p>';
  }

  function compactCard(g,parts) {
    const m=data.matches.find(row=>row.game_id===g.game_id),d=m.disagreement;
    const indicator=d.mean_disagreement||d.scenario_disagreement?
      '<button type="button" class="w10-disagreement" aria-controls="w10-advanced-'+e(g.game_id)+'" aria-expanded="false" data-w10-comparison="'+e(g.game_id)+'">A/B differ</button>':'';
    return '<article class="card card-v31 research-card week10-compact" data-game-id="'+e(g.game_id)+'" data-forecast-kind="experimental-research" data-actionable="false">'+
      '<div class="game-meta"><b>'+e(g.away_team)+' @ '+e(g.home_team)+'</b><span>'+e(shortTime(g.kickoff_utc))+'</span><span>'+e(parts.stadium||'')+'</span><span class="tag">'+e(isArchived(g.game_id)?'ARCHIVED PREGAME':parts.state)+'</span></div>'+
      '<p class="primary-model-label">MODEL A · PREDICTED FINAL</p>'+parts.scoreboard+parts.modelB+
      '<div class="quick week9-comparisons"><div><small>MODEL SPREAD</small><strong>'+e(parts.spread)+'</strong></div><div><small>MODEL TOTAL</small><strong>'+n(g.forecast.model_total)+'</strong></div>'+market(g)+'</div>'+
      modelCPanel(g)+'<div class="w10-card-meta"><span>Forecast '+e(shortTime(g.original_created_at_utc))+'</span><span class="w10-meta-indicators">'+
      (m.td_held?'<span class="w10-hold-tag" title="'+e(m.td_held)+'">TD withheld</span>':'')+indicator+'</span></div>'+
      '<details class="week9-quarter-details"><summary>Independent Model B · Quarter Simulation &amp; sources</summary><div class="expanded">'+flatDetails(parts.modelBDetails)+'</div></details>'+
      '<details class="player-props"><summary>Player Props · conditional research</summary><div class="expanded">'+archiveNote(g.game_id)+parts.props+'</div></details>'+advanced(g,parts.audit)+'</article>';
  }

  function context(g) { return modelCPanel(g); }

  document.addEventListener('click',event=>{
    const control=event.target.closest('[data-w10-comparison]');
    if(!control)return;
    const panel=document.getElementById('w10-advanced-'+control.dataset.w10Comparison);
    if(!panel)return;
    panel.open=true;
    document.getElementById('w10-comparison-'+control.dataset.w10Comparison)?.focus();
  });
  document.addEventListener('toggle',event=>{
    if(!event.target.matches?.('.w10-advanced'))return;
    const id=event.target.id;
    document.querySelectorAll('.w10-disagreement').forEach(control=>{
      if(control.getAttribute('aria-controls')===id)control.setAttribute('aria-expanded',String(event.target.open));
    });
  },true);

  function evaluation() {
    const rows = data.metrics.map(x=>'<tr><th>'+e(x.cohort)+'</th><td>'+x.games+'</td><td>'+n(x.mae)+
      '</td><td>'+n(x.margin_mae)+'</td><td>'+n(x.total_mae)+'</td><td>'+x.winners_correct+'/'+x.winner_denominator+'</td></tr>').join('');
    const finals = data.completed.map(x=>'<tr id="result-'+e(x.game_id)+'"><th>'+e(x.away_team)+' @ '+e(x.home_team)+
      '</th><td>'+x.away_score+'–'+x.home_score+(x.overtime?' OT':'')+'</td><td>'+n(x.A_away)+'–'+n(x.A_home)+
      '</td><td>'+x.B_away+'–'+x.B_home+'</td><td>'+n(x.A_score_mae)+'</td><td>'+n(x.A_margin_error)+
      '</td><td>'+n(x.A_total_error)+'</td><td>'+ (x.A_winner_correct?'Correct':'Incorrect')+'</td><td>'+e(uk(x.A_created))+' UK</td></tr>').join('');
    const header = names => '<thead><tr>'+names.map(s=>'<th>'+e(s)+'</th>').join('')+'</tr></thead>';
    return '<section id="week9-results" class="panel local-week8-results"><h2>Week 9 · completed results</h2>'+
      '<p>15 reconciled finals. Score MAE is per team. Each frozen forecast vintage is scored separately.</p>'+
      '<div class="local-table-wrap"><table>'+header(['Forecast cohort','Games','Score MAE','Margin MAE','Total MAE','Winners'])+
      '<tbody>'+rows+'</tbody></table></div><details><summary>All 15 final scores and errors</summary>'+
      '<div class="local-table-wrap"><table>'+header(['Fixture','Actual','A original mean','B scenario','A score MAE','A margin error','A total error','A winner lean','A original issue'])+
      '<tbody>'+finals+'</tbody></table></div></details>'+
      '<p><a href="data/week9_quarter_metrics.csv">Quarter performance</a> · <a href="data/week9_yardage_metrics.csv">Player performance</a> · '+
      '<a href="data/week9_td_metrics.csv">TD performance</a> · <a href="data/weeks1_9_team_metrics.csv">Weeks 1–9 forecast cohorts</a></p></section>';
  }

  function completedDetails(g) {
    return data.completed.some(x => x.game_id === g.game_id) ?
      '<p class="muted"><a href="performance.html?week=9#result-'+e(g.game_id)+'">Week 9 score, quarter and player evaluation</a></p>' : '';
  }

  function summary() {
    return '<details class="source-panel"><summary>Week 10 preview and source coverage</summary><div class="expanded">'+
      '<p>All 14 matchups. Final history scores, independent quarter simulations and participation-conditional player forecasts. '+
      'First cutoff 13 November 01:14 GMT. Bye teams CHI, DEN, PHI and TB.</p>'+
      '<p>All 15 Week 9 games cleared the history rule and reconciled before this issue. Model C is the primary joint distribution. Models A and B remain independent. Negative binomial is a separate challenger.</p>'+
      '<p>Match and selected player prices are reference observations. Bookmaker update times and qualified value are unavailable.</p>'+
      '<p><a href="data/week10_fixture_comparison.csv">Matchup comparison</a> · <a href="data/week10_market_observations.csv">Reference prices</a> · <a href="data/week10_public_prop_observations.csv">Public player price observations</a></p></div></details>';
  }

  return {load,game,context,market,compactCard,completedDetails,summary,isArchived,archiveNote,publicationNotice,
          get roles(){return data.roles;},get display(){return data.display;}};
})();
