/* Week 10 presentation and Week 9 results overlay. */
'use strict';
const Week10 = (() => {
  let data, cData;
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
    if (hash !== '0ef1119ad63c0ae5fbf2419a9af32c492be74136a0a609f73dfc8e7a96e1b7c3') throw Error('Week 10 context hash differs');
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
    if (document.body.dataset.page === 'integrated-performance') {
      document.querySelector('#metrics').insertAdjacentHTML('beforebegin', evaluation());
    }
  }

  function game(g) {
    const final = data.completed.find(x => x.game_id === g.game_id);
    return final ? {...g,state:'FINAL',actuals:{away_score:final.away_score,home_score:final.home_score}} : g;
  }


  function modelCPanel(g) {
    const f = cData?.fixtures.find(row=>row.game_id===g.game_id);
    if (!f) return '<section class="local-source-note model-c-panel"><p>Model C unavailable.</p></section>';
    const c=f.C, p=c.probabilities, nb=f.NB;
    const pct=x=>n(100*x,1)+'%';
    const pair=(values,d=1)=>values.map(x=>n(x,d)).join('–');
    const range=values=>values.map(x=>n(x,1)).join(' to ');
    const section=label=>'<p>'+label+'</p>';
    const alert=f.alerts.length?'<p><b>Volatility Alert</b> · 17+ margin exceeds the held-out threshold.</p>':'';
    return '<section class="local-source-note model-c-panel"><p><b>Model C · paired residuals</b> · Final history edition</p>'+
      section('Expected '+pair(c.expected)+' · Total 80% range '+range(c.total_interval80))+
      section('60+ total '+pct(p.total_60)+' · Below 30 '+pct(p.total_under_30)+' · 17+ margin '+pct(p.margin_17))+
      alert+'<details><summary>Model C ranges, scenarios and sources</summary>'+
      section('Median '+pair(c.median)+' · Typical scenario '+pair(c.typical,0))+
      section('Low-total scenario '+pair(c.low,0)+' · High-total scenario '+pair(c.high,0))+
      section('80% team ranges. '+e(g.away_team)+' '+range(c.away_interval80)+' · '+e(g.home_team)+' '+range(c.home_interval80))+
      section('80% home margin '+range(c.home_margin_interval80)+' · Absolute margin '+range(c.absolute_margin_interval80))+
      section('24+ margin '+pct(p.margin_24)+' · Either team 35+ '+pct(p.either_35)+' · Either team at most 10 '+pct(p.either_10))+
      section('Scores are away–home. Scenarios are supported examples. Standalone win probabilities remain unvalidated.')+
      section('Prior scoring SD '+n(f.context_score_sd,1)+' points · Prior drives '+n(f.context_drives,1)+' · '+c.draws+' paired draws')+
      section('Model A input issue '+e(uk(f.a_features_asof_utc))+' GMT · Model C edition '+e(cData.model_c_id))+
      section('Research probabilities. The distribution uses the pinned A score and eligible lagged scoring and drive history.')+
      '</details><details><summary>Separate negative-binomial challenger</summary>'+
      section('Expected '+pair(nb.expected)+' · Total 80% range '+range(nb.total_interval80))+
      section('60+ total '+pct(nb.probabilities.total_60)+' · Below 30 '+pct(nb.probabilities.total_under_30)+
              ' · 17+ margin '+pct(nb.probabilities.margin_17))+'</details></section>';
  }

  function context(g) {
    const match = data.matches.find(x => x.game_id === g.game_id);
    if (!match) return '';
    const nowLocked = Date.now() >= Date.parse(g.cutoff_utc);
    const details = match.news.map(x => '<p><b>'+e(x.player_name)+'</b> '+e(x.note)+
      ' <a href="'+e(x.source_url)+'">NFL report</a> · '+e(uk(x.modified_at_utc))+'</p>').join('');
    const weather = match.weather && match.weather.forecast_status === 'AVAILABLE' ?
      '<p>Venue forecast: '+e(match.weather.weather_condition)+', '+n(match.weather.temperature_f,0)+'°F, wind '+
      n(match.weather.wind_speed_mph,0)+' mph. Display context.</p>' : '';
    const practice = match.practice.filter(p => p.player_id && ['QB','RB','WR','TE'].includes(p.position));
    const practiceNote = practice.length ?
      '<details><summary>Latest captured injury reports</summary><p>'+
      practice.map(p=>e(p.player_name)+' · '+e(p.wednesday||p.tuesday||p.monday||'Unknown')+' · '+e(p.designation)+' · '+e(p.injury)).join('<br>')+
      '</p><p><a href="'+e(practice[0].source_url)+'">Official club report</a> · '+
      e(uk(practice[0].modified_at_utc))+' UK. Final designations pending.</p></details>' : '';
    return '<section class="local-source-note"><p><b>'+ (nowLocked?'Pregame cutoff reached':'Final history · experimental research')+
      '</b> · '+e(uk(g.created_at_utc))+' UK. Fixed history-only Model A.</p>'+details+weather+practiceNote+
      (match.td_held?'<p>Anytime TD withheld · '+e(match.td_held.replaceAll('_',' ').toLowerCase())+'</p>':'')+
      '<p>Game-day participation remains unconfirmed. Injury source coverage varies by team.</p></section>'+modelCPanel(g);
  }

  function market(g) {
    const m = data.matches.find(x => x.game_id === g.game_id);
    if (!m) return '';
    const d = m.disagreement;
    const signed = v => (v>0?'+':'')+n(v);
    const comparison = '<p class="comparison-market-note week10-model-comparison"><b>'+
      (d.mean_disagreement?'Models lean different ways':'Model means lean the same way')+'</b> · '+
      'A '+e(d.a_mean_lean)+' / B '+e(d.b_mean_lean)+'<br>'+
      'Home margin A '+e(signed(d.a_home_margin))+' / B '+e(signed(d.b_home_margin))+
      ' · Total A '+n(d.a_total)+' / B '+n(d.b_total)+
      (d.mean_disagreement!==d.scenario_disagreement?'<br>Representative scores tell a different story.':'')+
      '<br>Analytical comparison only.</p>';
    const line = m.bookmaker_home_line;
    const quotes = m.bookmaker_quotes;
    const spread = quotes.find(x => x.market === 'Spread');
    const total = quotes.filter(x => x.market === 'Total');
    const odds = x => (x>0?'+':'')+x;
    return '<div class="comparison-bookmaker"><small>BOOKMAKER SPREAD</small><strong>'+
      e(g.home_team)+' '+odds(line)+'</strong><small>'+odds(spread.american_odds)+'</small></div>'+
      '<div class="comparison-bookmaker"><small>BOOKMAKER TOTAL</small><strong>'+n(m.bookmaker_total,1)+
      '</strong><small>'+total.map(x=>e(x.selection)+' '+odds(x.american_odds)).join(' · ')+'</small></div>'+
      '<p class="comparison-market-note market-source-reference" data-provider-updated-at="'+e(spread.provider_updated_at_utc||'')+
      '" data-retrieved-at="'+e(spread.retrieved_at_utc||'')+'"><a href="'+e(spread.source_url)+
      '" target="_blank" rel="noopener noreferrer">DraftKings via AgentBets</a><br>Feed updated '+
      e(spread.provider_updated_at_utc?uk(spread.provider_updated_at_utc)+' GMT':'Unknown')+
      ' · Retrieved '+e(spread.retrieved_at_utc?uk(spread.retrieved_at_utc)+' GMT':'Unknown')+
      '<br>Bookmaker quote-update time unknown. Reference only.</p>'+comparison;
  }

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

  return {load,game,context,market,completedDetails,summary,
          get roles(){return data.roles;},get display(){return data.display;}};
})();
