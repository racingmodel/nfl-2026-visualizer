'use strict';
const PlayerRoles = (() => {
  const records = new Map();
  const key = player => [player.game_id, player.team, player.player_id].join('|');
  const rowFor = player => records.get(key(player));
  const escape = value => escapeHTML(String(value ?? ''));
  const options = (label, values) => '<option value="">' + escape(label) + '</option>' +
    values.map(value => '<option>' + escape(value) + '</option>').join('');

  async function load() {
    const response = await fetch('data/player_roles.json');
    if (!response.ok) throw Error('Role evidence unavailable');
    for (const row of await response.json()) records.set(key(row), row);
  }

  function roleStatus(role) {
    return role?.depth_status ?? 'Unavailable';
  }

  function rankOf(role) {
    return roleStatus(role) === 'Verified' ? role.depth_role : null;
  }

  function name(player) {
    const role = rowFor(player);
    const status = roleStatus(role);
    const rank = rankOf(role);
    const label = rank ?? (status === 'Uncertain' ? 'Role uncertain' : 'Role unverified');
    const title = rank ? 'Official depth-chart rank ' + rank + ' · captured before kickoff' : label;
    return '<span class="prop-player-name"><span class="prop-name-text">' + escape(player.player_name) +
      '</span><span class="role-badge" data-status="' + escape(status) + '" title="' + escape(title) +
      '" aria-label="' + escape(title) + '">' + escape(label) + '</span></span>';
  }

  function details(player, kind) {
    const role = rowFor(player);
    if (!role) return '';
    const usage = kind === 'receive' ? ['target', 'carry'] : ['carry', 'target'];
    const recent = usage.map(metric => {
      const label = metric === 'target' ? 'Targets' : 'Carries';
      const rank = role[metric + '_rank'];
      return rank == null ? label + ' rank unavailable' :
        label + ' #' + rank + ' · ' + role[metric + '_mean'].toFixed(1) + '/appearance (' + role.usage_games + ')';
    });
    recent.push(role.snap_rank == null ? 'Snap rank unavailable' :
      'Snap share #' + role.snap_rank + ' · ' + (role.snap_mean * 100).toFixed(0) + '% (' + role.snap_games + ')');
    const slot = role.slot_rank == null ? '' : ' · ' + role.formation + ' · slot rank ' + role.slot_rank;
    const depth = role.depth_role ? 'Reported depth rank ' + role.depth_role + slot : 'Depth rank unavailable';
    return '<details class="prop-role-details"><summary>Role evidence &amp; usage</summary>' +
      '<small class="role-detail">' + escape(depth + ' · ' + role.reason) + '</small>' +
      '<small class="role-detail">' + escape('Recent usage · ' + recent.join(' · ')) + '</small>' +
      '<small class="role-detail">' + escape('Game-day starter ' + role.starter.toLowerCase()) + '</small></details>';
  }

  function controls(kind, fixture = false) {
    const receiving = ['WR1','WR2','WR3','WR4+','TE1','TE2','TE3+','RB1','RB2','RB3','RB4+'];
    const quarterbacks = ['QB1','QB2','QB3','QB4'];
    const roles = kind === 'pass' ? quarterbacks : kind === 'receive' ? receiving : [...receiving, ...quarterbacks];
    const yards = kind === 'receive' || kind === 'rush';
    return '<div class="role-controls" ' + (fixture ? 'data-role-fixture' : 'id="role-global"') + '>' +
      '<label>Depth rank<select data-role-rank>' + options('All ranks', roles) + '</select></label>' +
      '<label>Role evidence<select data-role-status><option value="">All statuses</option>' +
      '<option value="Verified">Verified rank</option><option value="Uncertain">Role uncertain</option>' +
      '<option value="Unavailable">Role unverified</option></select></label>' +
      (fixture ? '<label>Team<select data-role-team><option value="">Both teams</option></select></label>' : '') +
      '<label>Opponent<select data-role-opponent><option value="">All opponents</option></select></label>' +
      (fixture && yards ? '<label>Projected yards<select data-role-sort><option value="desc">High to low</option><option value="asc">Low to high</option></select></label>' : '') +
      (fixture && kind === 'td' ? '<label class="role-show-all"><input type="checkbox" data-role-show-all> Show all eligible TD players</label>' : '') +
      '<small class="role-legend">Badges show captured depth rank. Usage and starter status are separate.</small></div>';
  }

  function matches(player, controlsNode) {
    if (!controlsNode) return true;
    const role = rowFor(player);
    const rank = controlsNode.querySelector('[data-role-rank]').value;
    const status = controlsNode.querySelector('[data-role-status]').value;
    const opponent = controlsNode.querySelector('[data-role-opponent]').value;
    return (!rank || rank === rankOf(role)) && (!status || status === roleStatus(role))
      && (!opponent || opponent === player.opponent);
  }

  function globalControls(visible, kind, players, rerender) {
    let controlsNode = document.querySelector('#role-global');
    if (!visible) {
      if (controlsNode) controlsNode.hidden = true;
      return;
    }
    if (!controlsNode || controlsNode.dataset.kind !== kind) {
      if (controlsNode) controlsNode.remove();
      document.querySelector('#player-panel').insertAdjacentHTML('beforebegin', controls(kind));
      controlsNode = document.querySelector('#role-global');
      controlsNode.dataset.kind = kind;
      controlsNode.querySelector('[data-role-opponent]').innerHTML =
        options('All opponents', [...new Set(players.map(player => player.opponent))].sort());
      controlsNode.addEventListener('change', rerender);
    }
    controlsNode.hidden = false;
  }

  function attributes(player) {
    const role = rowFor(player);
    return 'data-role-rank-value="' + escape(rankOf(role)) + '" data-role-status-value="' +
      escape(roleStatus(role)) + '" data-role-opponent-value="' + escape(player.opponent) + '"';
  }

  function initializeFixtures() {
    for (const controlsNode of document.querySelectorAll('[data-role-fixture]')) {
      const panel = controlsNode.closest('[role=tabpanel]');
      const rows = [...panel.querySelectorAll('.rows > .row')];
      const showAll = controlsNode.querySelector('[data-role-show-all]');
      const sort = controlsNode.querySelector('[data-role-sort]');
      controlsNode.querySelector('[data-role-team]').innerHTML = options('Both teams', [...new Set(rows.map(row => row.dataset.team))]);
      controlsNode.querySelector('[data-role-opponent]').innerHTML = options('All opponents', [...new Set(rows.map(row => row.dataset.roleOpponentValue))]);
      controlsNode.addEventListener('change', () => {
        const rank = controlsNode.querySelector('[data-role-rank]').value;
        const status = controlsNode.querySelector('[data-role-status]').value;
        const team = controlsNode.querySelector('[data-role-team]').value;
        const opponent = controlsNode.querySelector('[data-role-opponent]').value;
        const expandTD = showAll?.checked || rank || status || team || opponent;
        for (const row of rows) {
          row.classList.toggle('role-hidden', !!((rank && rank !== row.dataset.roleRankValue) ||
            (status && status !== row.dataset.roleStatusValue) || (team && team !== row.dataset.team) ||
            (opponent && opponent !== row.dataset.roleOpponentValue) ||
            (showAll && !expandTD && row.dataset.featured === 'false')));
        }
        const container = panel.querySelector('.rows');
        if (sort) {
          for (const row of sort.value === 'asc' ? [...rows].reverse() : rows) container.append(row);
        }
        const empty = panel.querySelector('.role-empty');
        if (empty) empty.hidden = rows.some(row => !row.classList.contains('role-hidden'));
      });
    }
  }

  return {load, name, details, controls, globalControls, matches, attributes, initializeFixtures};
})();
