/* MuleTrace investigator console - talks to the FastAPI backend in backend/app.py */

const API = '';
const $ = (id) => document.getElementById(id);

const state = {
  overview: null,
  chains: [],
  band: '',
  trace: null,
  index: 0,
  network: null,
};

// ---------- formatting ----------

const inr = (n) => {
  if (n >= 1e7) return '₹' + (n / 1e7).toFixed(2) + ' Cr';
  if (n >= 1e5) return '₹' + (n / 1e5).toFixed(2) + ' L';
  return '₹' + Math.round(n).toLocaleString('en-IN');
};

const mins = (m) => {
  if (m < 60) return m.toFixed(0) + ' min';
  if (m < 1440) return (m / 60).toFixed(1) + ' h';
  return (m / 1440).toFixed(1) + ' d';
};

const short = (vpa, n = 26) => (vpa && vpa.length > n ? vpa.slice(0, n - 1) + '…' : vpa || '—');

const when = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
};

const bandColor = (b) => ({ critical: '#bf5f66', high: '#c9834e', medium: '#c99a4e', low: '#6aa88f' }[b] || '#8ea39e');

/* A seed on the page URL pins the dataset, so it has to travel with every API
   call the page makes - otherwise the page is pinned and its data is not. */
const PINNED_SEED = new URLSearchParams(location.search).get('seed');

function url(path) {
  if (!PINNED_SEED) return API + path;
  return API + path + (path.includes('?') ? '&' : '?') + 'seed=' + encodeURIComponent(PINNED_SEED);
}

async function get(path) {
  const res = await fetch(url(path));
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function post(path, body) {
  const res = await fetch(url(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

// ---------- stat cards ----------

function renderStats() {
  const o = state.overview;
  const total = o.funds_recoverable + o.funds_lost || 1;

  const cards = [
    {
      label: 'Active chains', value: o.active_chains,
      sub: `${o.freezable_chains} still freezable · ${o.cashed_out_chains} cashed out`
         + (o.dismissed_chains ? ` · ${o.dismissed_chains} dismissed` : ''),
      pct: Math.min(100, o.active_chains / 60 * 100), bar: 'blue',
      pips: [['#4f8f86', 'C'], ['#3d6f68', 'H'], ['#c99a4e', 'M']],
    },
    {
      label: 'Recoverable right now', value: inr(o.funds_recoverable),
      sub: `${inr(o.funds_lost)} already cashed out`,
      pct: o.funds_recoverable / total * 100, bar: '',
      pips: [['#6aa88f', '₹']],
    },
    {
      label: 'Median chain duration', value: mins(o.median_chain_minutes),
      sub: 'complaint window before cash-out',
      pct: Math.min(100, o.median_chain_minutes / 240 * 100), bar: 'amber',
      pips: [['#c99a4e', '⏱']],
    },
    {
      label: 'Accounts flagged', value: o.accounts_flagged,
      sub: `of ${o.accounts.toLocaleString('en-IN')} on the network`
         + ` · ${o.transactions.toLocaleString('en-IN')} transactions`,
      pct: o.accounts_flagged / o.accounts * 100, bar: 'red',
      pips: [['#bf5f66', '!']],
    },
  ];

  $('stats').innerHTML = cards.map(c => `
    <div class="card stat">
      <div class="stat-top">
        <span class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19h16M7 16V9M12 16V5M17 16v-4"/></svg>
        </span>${c.label}
      </div>
      <div class="stat-value">${c.value}</div>
      <div class="stat-sub">${c.sub}</div>
      <div class="bar ${c.bar}"><span style="width:${Math.max(4, c.pct).toFixed(0)}%"></span></div>
      <div class="stat-foot">
        <span class="pips">${c.pips.map(p => `<span class="pip" style="background:${p[0]}">${p[1]}</span>`).join('')}</span>
      </div>
    </div>`).join('');
}

// ---------- end-node banner ----------

function renderEndnode(t) {
  const end = t.nodes[t.nodes.length - 1];
  const cash = t.end_reason === 'cash_out';

  $('crumb-chain').textContent = t.ground_truth_chain || t.entry_txn_id;
  $('endnode-vpa').textContent = end.account_id;
  $('endnode-sub').textContent = cash
    ? 'Trail ends at a cash-out — funds are beyond a freeze'
    : `Funds stopped moving here · ${end.bank} · account age ${end.account_age_days ?? '—'} d`;

  $('endnode-badge').className = 'node-badge' + (cash ? '' : '');
  $('endnode-chips').innerHTML = [
    `<span class="chip ${cash ? 'red' : 'green'}">${inr(t.amount_at_end)} at end node</span>`,
    `<span class="chip">${t.hop_count} hops</span>`,
    `<span class="chip">${mins(t.elapsed_minutes)} end to end</span>`,
    `<span class="chip">${(t.leakage_pct * 100).toFixed(1)}% skimmed en route</span>`,
    t.splits_detected ? `<span class="chip violet">${t.splits_detected} split transfer${t.splits_detected > 1 ? 's' : ''}</span>` : '',
    end.mule_score != null ? `<span class="chip ${end.mule_score >= .5 ? 'amber' : ''}">mule score ${(end.mule_score * 100).toFixed(0)}</span>` : '',
  ].join('');

  const v = $('endnode-verdict');
  const review = t.review;
  if (review && review.verdict === 'confirmed_fraud') {
    v.textContent = 'CONFIRMED — freeze requested';
    v.className = 'chip green';
  } else if (review && review.verdict === 'false_positive') {
    v.textContent = 'dismissed — false positive';
    v.className = 'chip';
  } else {
    v.textContent = cash ? 'unrecoverable — cash-out' : 'FREEZE RECOMMENDED';
    v.className = 'chip ' + (cash ? 'red' : 'green');
  }

  $('endnode-entry').textContent = `entry ${t.entry_txn_id} · priority ${t.risk.score}/100 (${t.risk.band})`;
  $('graph-sub').textContent = t.risk.explanation;
}

// ---------- graph ----------

const NODE_STYLE = {
  victim: { bg: 'rgba(191,95,102,.22)', border: '#bf5f66', size: 22 },
  mule: { bg: 'rgba(201,154,78,.18)', border: '#c99a4e', size: 17 },
  end_node: { bg: 'rgba(155,200,189,.24)', border: '#9bc8bd', size: 26 },
  cashout: { bg: 'rgba(99,115,111,.22)', border: '#63736f', size: 24 },
};

function renderGraph(t) {
  const nodes = t.nodes.map((n, i) => {
    const s = NODE_STYLE[n.role] || NODE_STYLE.mule;
    const tag = n.role === 'victim' ? 'VICTIM'
      : n.role === 'end_node' ? 'END NODE'
      : n.role === 'cashout' ? 'CASH-OUT'
      : 'HOP ' + i;
    return {
      id: n.account_id,
      label: `${tag}\n${short(n.account_id, 22)}`,
      shape: 'dot',
      size: s.size,
      color: { background: s.bg, border: s.border, highlight: { background: s.bg, border: '#fff' } },
      borderWidth: n.role === 'end_node' ? 3 : 2,
      font: { color: '#c3cfcb', size: 11, face: 'Plus Jakarta Sans', multi: false, vadjust: -2 },
      shadow: { enabled: n.role === 'end_node', color: 'rgba(155,200,189,.55)', size: 26, x: 0, y: 0 },
    };
  });

  const edges = t.hops.map(h => ({
    from: h.source,
    to: h.target,
    label: `${inr(h.amount)}${h.hop ? `\n${mins(h.gap_minutes)} · ${(h.forward_pct * 100).toFixed(0)}%` : ''}`,
    arrows: { to: { enabled: true, scaleFactor: 0.65 } },
    color: { color: h.split_of ? 'rgba(155,200,189,.7)' : 'rgba(79,143,134,.75)', highlight: '#9bc8bd' },
    width: 1.6,
    dashes: h.split_of ? [6, 4] : false,
    font: { color: '#8ea39e', size: 10, face: 'Plus Jakarta Sans', strokeWidth: 0, align: 'middle' },
    smooth: { type: 'curvedCW', roundness: 0.16 },
  }));

  const container = $('graph');
  if (state.network) state.network.destroy();
  state.network = new vis.Network(container, { nodes, edges }, {
    physics: { enabled: true, solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -62, springLength: 150, springConstant: 0.06 },
      stabilization: { iterations: 220 } },
    interaction: { hover: true, dragView: true, zoomView: true, tooltipDelay: 120 },
    layout: { improvedLayout: true },
  });
  state.network.once('stabilizationIterationsDone', () => {
    state.network.setOptions({ physics: false });
    state.network.fit({ animation: { duration: 400 } });
  });
  state.network.on('click', p => {
    if (p.nodes.length) showAccount(p.nodes[0]);
  });
}

// ---------- hop ledger ----------

function renderTimeline(t) {
  $('timeline').innerHTML = t.hops.map((h, i) => {
    const last = i === t.hops.length - 1;
    const node = t.nodes[i + 1] || {};
    return `
      <div class="hop ${last ? 'end' : ''}">
        <div class="hop-rail"><span class="hop-num">${h.hop}</span>${last ? '' : '<i></i>'}</div>
        <div class="hop-body">
          <div class="to">${short(h.target, 34)}</div>
          <div class="meta">
            ${h.hop === 0 ? '<span>victim transfer</span>' : `<span>+${mins(h.gap_minutes)} later</span><span>${(h.forward_pct * 100).toFixed(0)}% forwarded</span>`}
            <span>${h.mode}</span>
            ${h.split_of ? `<span style="color:#9bc8bd">split into ${h.split_of}</span>` : ''}
            ${node.mule_score != null ? `<span>mule ${(node.mule_score * 100).toFixed(0)}</span>` : ''}
          </div>
        </div>
        <div class="hop-amt">${inr(h.amount)}<small>${when(h.timestamp)}</small></div>
      </div>`;
  }).join('');
}

// ---------- chains table ----------

function renderChains() {
  const rows = state.band ? state.chains.filter(c => c.risk.band === state.band) : state.chains;
  $('chains-sub').textContent = `${rows.length} chain${rows.length === 1 ? '' : 's'} · ranked by recovery priority`;

  if (!rows.length) { $('chains-body').innerHTML = '<tr><td colspan="8" class="empty">No chains in this band.</td></tr>'; return; }

  $('chains-body').innerHTML = rows.map(c => {
    const cash = c.end_reason === 'cash_out';
    return `
    <tr data-txn="${c.entry_txn_id}" class="${state.trace && state.trace.entry_txn_id === c.entry_txn_id ? 'selected' : ''}">
      <td class="mono">${c.entry_txn_id}</td>
      <td class="vpa">${short(c.victim, 22)}</td>
      <td class="vpa">${short(c.end_node, 24)}</td>
      <td class="mono">${c.hop_count}</td>
      <td class="mono"><b>${inr(c.amount_at_end)}</b></td>
      <td class="mono">${mins(c.elapsed_minutes)}</td>
      <td>
        <div class="score-cell">
          <span class="score-track"><span style="width:${c.risk.score}%;background:${bandColor(c.risk.band)}"></span></span>
          <b class="mono">${c.risk.score}</b>
        </div>
      </td>
      <td><span class="chip ${cash ? 'red' : 'green'}">${cash ? 'cashed out' : 'freezable'}</span></td>
    </tr>`;
  }).join('');

  $('chains-body').querySelectorAll('tr[data-txn]').forEach(tr => {
    tr.addEventListener('click', () => runTrace(tr.dataset.txn));
  });
}

// ---------- watchlist ----------

async function renderWatchlist() {
  const { accounts } = await get('/api/watchlist?limit=30&min_score=0.4');
  $('watchlist-body').innerHTML = accounts.map(a => `
    <tr data-acc="${a.account_id}">
      <td class="vpa">${short(a.account_id, 28)}</td>
      <td>${a.bank}</td>
      <td class="mono">${a.account_age_days} d</td>
      <td class="mono">${(a.forward_ratio * 100).toFixed(0)}%</td>
      <td class="mono">${mins(a.median_response_min)}</td>
      <td class="mono">${inr(a.total_in)}</td>
      <td>
        <div class="score-cell">
          <span class="score-track"><span style="width:${a.score * 100}%;background:${a.score >= .75 ? '#bf5f66' : '#c99a4e'}"></span></span>
          <b class="mono">${(a.score * 100).toFixed(0)}</b>
        </div>
      </td>
    </tr>`).join('');
  $('watchlist-body').querySelectorAll('tr[data-acc]').forEach(tr =>
    tr.addEventListener('click', () => showAccount(tr.dataset.acc)));
}

async function showAccount(id) {
  try {
    const a = await get('/api/risk-score/' + encodeURIComponent(id));
    const lines = a.signals.map(s => `${s.label}: ${s.value} (p${s.percentile})`).join('\n');
    alert(`${a.account_id}\n${a.bank} · ${a.account_type} · age ${a.account_age_days} d\n` +
          `mule score ${(a.score * 100).toFixed(0)}/100\n\n` +
          `in ${a.in_count} (${inr(a.total_in)}) · out ${a.out_count} (${inr(a.total_out)})\n\n${lines}`);
  } catch (e) { /* unknown account - nothing to show */ }
}

// ---------- model ----------

async function renderModel() {
  const m = state.overview.model, b = state.overview.baseline;
  $('model-metrics').innerHTML = `
    <div class="kv"><span>Algorithm</span><span>${m.algorithm}</span></div>
    <div class="kv"><span>Accounts scored</span><span>${m.n_accounts.toLocaleString('en-IN')}</span></div>
    <div class="kv"><span>Known mules</span><span>${m.n_mules}</span></div>
    <div class="kv"><span>Precision</span><span>${(m.precision * 100).toFixed(1)}%</span></div>
    <div class="kv"><span>Recall</span><span>${(m.recall * 100).toFixed(1)}%</span></div>
    <div class="kv"><span>F1</span><span>${m.f1.toFixed(3)}</span></div>
    <div class="kv"><span>ROC AUC</span><span>${m.roc_auc.toFixed(3)}</span></div>
    <div class="kv"><span>Baseline (logistic)</span><span>F1 ${b.f1.toFixed(3)} · AUC ${b.roc_auc.toFixed(3)}</span></div>`;

  const max = Math.max(...m.top_features.map(f => f.importance)) || 1;
  $('model-features').innerHTML = m.top_features.map(f => `
    <div style="padding:9px 0;border-bottom:1px solid var(--line)">
      <div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:7px">
        <span>${f.label}</span><b class="mono">${f.importance.toFixed(3)}</b>
      </div>
      <div class="bar blue"><span style="width:${(f.importance / max * 100).toFixed(0)}%"></span></div>
    </div>`).join('');

  // validation figures for the dataset currently loaded, computed on demand
  const box = $('model-validation');
  box.innerHTML = '<div class="empty">Evaluating current dataset…</div>';
  try {
    const ev = await get('/api/evaluation');
    const w = ev.chain_walk, s = ev.proactive_scan;
    box.innerHTML = `
      <div class="kv"><span>Chains in dataset</span><span>${ev.dataset.injected_chains}</span></div>
      <div class="kv"><span>End-node accuracy</span><span>${(w.end_node_accuracy * 100).toFixed(1)}%</span></div>
      <div class="kv"><span>Full-path recovery</span><span>${(w.full_path_recovery * 100).toFixed(1)}%</span></div>
      <div class="kv"><span>Scan recall</span><span>${(s.recall_vs_injected * 100).toFixed(1)}%</span></div>
      <div class="kv"><span>Precision @10 / @20</span><span>${(s.precision_at_10 * 100).toFixed(0)}% / ${(s.precision_at_20 * 100).toFixed(0)}%</span></div>
      <div class="kv"><span>Precision, whole set</span><span>${(s.precision * 100).toFixed(1)}%</span></div>
      <div style="margin-top:14px;font-size:11px;color:var(--muted-2);font-weight:600;letter-spacing:.05em">
        WALK SENSITIVITY
      </div>
      ${ev.sensitivity.map(r => `
        <div class="kv"><span>${r.setting}</span><span>${(r.end_node_accuracy * 100).toFixed(1)}%</span></div>
      `).join('')}`;
  } catch (e) {
    box.innerHTML = `<div class="empty">Evaluation unavailable — ${e.message}</div>`;
  }
}

// ---------- complaints ----------

async function renderComplaints() {
  const { complaints } = await get('/api/complaints?limit=14');
  $('complaints-body').innerHTML = complaints.map(c => `
    <tr data-txn="${c.txn_id}">
      <td class="mono">${c.txn_id}</td>
      <td class="vpa">${short(c.victim, 20)}</td>
      <td class="mono">${inr(c.amount)}</td>
      <td class="vpa">${when(c.timestamp)}</td>
    </tr>`).join('');
  $('complaints-body').querySelectorAll('tr[data-txn]').forEach(tr =>
    tr.addEventListener('click', () => { runTrace(tr.dataset.txn); switchView('chains'); }));
}

// ---------- trace ----------

function walkQuery() {
  return `min_forward_pct=${(+$('p-pct').value / 100).toFixed(2)}`
       + `&max_gap_hours=${$('p-gap').value}`
       + `&max_hops=${$('p-hops').value}`;
}

async function runTrace(txnId) {
  try {
    const t = await get(`/api/trace/${encodeURIComponent(txnId)}?${walkQuery()}`);
    state.trace = t;
    renderEndnode(t);
    renderGraph(t);
    renderTimeline(t);
    renderChains();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    $('endnode-sub').textContent = `Could not trace ${txnId} — ${e.message}`;
  }
}

// ---------- views ----------

function moveNavPill(button, instant) {
  const pill = $('nav-pill');
  if (!pill || !button) return;
  if (instant) pill.classList.add('instant');
  pill.style.width = button.offsetWidth + 'px';
  pill.style.transform = 'translateX(' + button.offsetLeft + 'px)';
  if (instant) requestAnimationFrame(() => pill.classList.remove('instant'));
}

function switchView(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('active', b.dataset.view === name));
  moveNavPill(document.querySelector('#nav button[data-view="' + name + '"]'));
  if (name === 'watchlist') renderWatchlist();
  if (name === 'model') renderModel();
  if (name === 'trace') renderComplaints();
}

// ---------- boot ----------

/* Every figure on screen is derived from the API on each refresh - nothing is
   cached across a data change, so regenerating the dataset moves all of it. */
async function refresh({ retrace = false } = {}) {
  state.overview = await get('/api/overview');
  renderStats();

  const { chains } = await get('/api/chains?limit=40');
  state.chains = chains;
  state.index = 0;
  renderChains();

  const badge = $('dataset-badge');
  if (badge) {
    badge.textContent = state.overview.dataset;
    badge.title = `Dataset ${state.overview.dataset} (seed ${state.overview.seed}) — `
                + 'every session gets its own network';
  }

  if (retrace) {
    const stillThere = state.trace && chains.some(c => c.entry_txn_id === state.trace.entry_txn_id);
    if (stillThere) await runTrace(state.trace.entry_txn_id);
    else if (chains.length) await runTrace(chains[0].entry_txn_id);
    else clearTrace();
  }
  return state.overview;
}

function clearTrace() {
  state.trace = null;
  $('crumb-chain').textContent = '—';
  $('endnode-vpa').textContent = '—';
  $('endnode-sub').textContent = 'No chains in the queue';
  $('endnode-chips').innerHTML = '';
  $('timeline').innerHTML = '<div class="empty">No trace loaded.</div>';
  if (state.network) { state.network.destroy(); state.network = null; }
}

async function boot() {
  await refresh({ retrace: true });
  $('loading').remove();
}

document.querySelectorAll('#nav button').forEach(b =>
  b.addEventListener('click', () => switchView(b.dataset.view)));

// place the indicator once fonts have settled, and keep it aligned on resize
const placeNavPill = () => moveNavPill(document.querySelector('#nav button.active'), true);
placeNavPill();
if (document.fonts && document.fonts.ready) document.fonts.ready.then(placeNavPill);
window.addEventListener('resize', placeNavPill);

document.querySelectorAll('.card-head .pill-btn[data-band]').forEach(b =>
  b.addEventListener('click', () => { state.band = b.dataset.band; renderChains(); }));

$('btn-fit').addEventListener('click', () => state.network && state.network.fit({ animation: true }));

$('btn-next').addEventListener('click', () => {
  if (!state.chains.length) return;
  state.index = (state.index + 1) % state.chains.length;
  runTrace(state.chains[state.index].entry_txn_id);
});

async function recordVerdict(verdict, note) {
  const t = state.trace;
  if (!t) return null;
  const res = await fetch('/api/feedback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entry_txn_id: t.entry_txn_id, verdict, note, reviewer: 'console' }),
  });
  if (!res.ok) throw new Error(`feedback ${res.status}`);
  const body = await res.json();
  state.trace.review = body.recorded;
  renderEndnode(state.trace);
  return body;
}

$('btn-freeze').addEventListener('click', async () => {
  const t = state.trace;
  if (!t) return;
  if (t.end_reason === 'cash_out') {
    alert('This trail ends at a cash-out. A freeze cannot recover these funds — escalate to law enforcement instead.');
    return;
  }
  const ok = confirm(
    `Raise a freeze request?\n\nAccount: ${t.end_node}\nAmount held: ${inr(t.amount_at_end)}\n` +
    `Chain: ${t.hop_count} hops over ${mins(t.elapsed_minutes)}\nPriority: ${t.risk.score}/100 (${t.risk.band})\n\n` +
    `Basis: ${t.risk.explanation}\n\n` +
    `This logs the chain as confirmed fraud. In production it queues for a human ` +
    `reviewer before any freeze is applied, and the verdict becomes training data.`);
  if (!ok) return;

  try {
    const body = await recordVerdict('confirmed_fraud', 'freeze requested from console');
    await refresh();
    alert(`Freeze request logged for ${t.end_node}.\n\n` +
          `${body.chains_reviewed} chain(s) reviewed so far — confirmed cases feed the next retrain.`);
  } catch (e) {
    alert(`Could not record the verdict — ${e.message}`);
  }
});

$('btn-dismiss').addEventListener('click', async () => {
  if (!state.trace) return;
  if (!confirm('Dismiss this chain as a false positive? It will drop out of the queue.')) return;
  try {
    await recordVerdict('false_positive', 'dismissed from console');
    await refresh({ retrace: true });
  } catch (e) {
    alert(`Could not record the verdict — ${e.message}`);
  }
});

/* Reloads the dataset if it changed on disk, then re-runs the scan. This is the
   one control that brings the whole console back in line with the data. */
$('btn-rescan').addEventListener('click', async () => {
  const btn = $('btn-rescan');
  btn.disabled = true;
  btn.classList.add('busy');
  try {
    if (PINNED_SEED) {
      await post('/api/rescan');
      await refresh({ retrace: true });
      alert(`This session is pinned to seed ${PINNED_SEED}, so the dataset stays fixed.\n\n` +
            `Drop ?seed= from the URL to get a new network on each load.`);
      return;
    }
    const fresh = await post('/api/reload');
    const o = await refresh({ retrace: true });
    alert(`New dataset generated: ${fresh.dataset}\n\n` +
          `${o.transactions.toLocaleString('en-IN')} transactions · ` +
          `${o.accounts.toLocaleString('en-IN')} accounts · ` +
          `${o.injected_chains} fraud chains · ${o.active_chains} in the queue.`);
  } catch (e) {
    alert(`Could not generate a new dataset — ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.classList.remove('busy');
  }
});

$('btn-trace').addEventListener('click', () => {
  const v = $('txn-input').value.trim();
  if (v) { runTrace(v); switchView('chains'); }
});

$('search-go').addEventListener('click', doSearch);
$('search-input').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

async function doSearch() {
  const q = $('search-input').value.trim();
  if (!q) return;
  if (/^TXN\d+$/i.test(q)) { runTrace(q.toUpperCase()); switchView('chains'); return; }
  const r = await get('/api/search?q=' + encodeURIComponent(q));
  if (r.accounts.length) showAccount(r.accounts[0]);
  else if (r.transactions.length) runTrace(r.transactions[0].txn_id);
}

[['p-pct', v => v + '%'], ['p-gap', v => v + ' h'], ['p-hops', v => v]].forEach(([id, fmt]) => {
  const el = $(id);
  el.addEventListener('input', () => {
    $(id + '-v').textContent = fmt(el.value);
    if (state.trace) runTrace(state.trace.entry_txn_id);
  });
});

boot().catch(err => {
  $('loading').innerHTML = `<p style="color:#bf5f66">Could not reach the API — ${err.message}</p>`;
});
