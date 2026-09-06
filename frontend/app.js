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

/* The stat row describes the chain currently open, with the queue-wide figure
   kept in each sub-line for context. It used to show only portfolio totals,
   which never moved as you worked through chains and read as frozen. */
function renderStats() {
  const o = state.overview;
  const t = state.trace;
  const portfolio = o.funds_recoverable + o.funds_lost || 1;

  if (!t) {
    $('stats').innerHTML = statCards([
      { label: 'Active chains', value: o.active_chains,
        sub: `${o.freezable_chains} freezable · ${o.cashed_out_chains} cashed out`,
        pct: 100, bar: 'blue', pips: [['#4f8f86', 'C']] },
      { label: 'Recoverable across queue', value: inr(o.funds_recoverable),
        sub: `${inr(o.funds_lost)} already cashed out`,
        pct: o.funds_recoverable / portfolio * 100, bar: '', pips: [['#6aa88f', '₹']] },
      { label: 'Median chain duration', value: mins(o.median_chain_minutes),
        sub: 'across the whole queue', pct: 50, bar: 'amber', pips: [['#c99a4e', '⏱']] },
      { label: 'Accounts flagged', value: o.accounts_flagged,
        sub: `of ${o.accounts.toLocaleString('en-IN')} on the network`,
        pct: o.accounts_flagged / o.accounts * 100, bar: 'red', pips: [['#bf5f66', '!']] },
    ]);
    return;
  }

  const cash = t.end_reason === 'cash_out';
  const mulesOnPath = t.nodes.filter(n => n.mule_score != null && n.mule_score >= 0.5).length;
  const share = o.funds_recoverable ? t.amount_at_end / o.funds_recoverable * 100 : 0;
  const vsMedian = o.median_chain_minutes
    ? t.elapsed_minutes / (o.median_chain_minutes * 2) * 100 : 50;

  const cards = [
    {
      label: cash ? 'Lost at cash-out' : 'Held at this end node',
      value: inr(t.amount_at_end),
      sub: `${inr(o.funds_recoverable)} recoverable across ${o.active_chains} active chains`,
      pct: share, bar: cash ? 'red' : '',
      pips: [[cash ? '#bf5f66' : '#6aa88f', '₹']],
    },
    {
      label: 'This chain took',
      value: mins(t.elapsed_minutes),
      sub: `queue median ${mins(o.median_chain_minutes)} · `
         + `${t.elapsed_minutes <= o.median_chain_minutes ? 'faster than' : 'slower than'} typical`,
      pct: Math.min(100, vsMedian), bar: 'amber',
      pips: [['#c99a4e', '⏱']],
    },
    {
      label: 'Hops traced',
      value: t.hop_count,
      sub: `${t.nodes.length} accounts on the path · ${mulesOnPath} scored as mules`
         + (t.splits_detected ? ` · ${t.splits_detected} split` : ''),
      pct: Math.min(100, t.hop_count / 8 * 100), bar: 'blue',
      pips: [['#4f8f86', 'H']],
    },
    {
      label: 'Recovery priority',
      value: `${t.risk.score}`,
      sub: `${t.risk.band} · ${o.accounts_flagged} accounts flagged network-wide`,
      pct: t.risk.score, bar: t.risk.band === 'critical' ? 'red' : 'amber',
      pips: [[bandColor(t.risk.band), '!']],
    },
  ];

  $('stats').innerHTML = statCards(cards);
}

function statCards(cards) {
  return cards.map(c => `
    <div class="card stat">
      <div class="stat-top">
        <span class="stat-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19h16M7 16V9M12 16V5M17 16v-4"/></svg>
        </span>${c.label}
      </div>
      <div class="stat-value">${c.value}</div>
      <div class="stat-sub">${c.sub}</div>
      <div class="bar ${c.bar}"><span style="width:${Math.max(4, Math.min(100, c.pct)).toFixed(0)}%"></span></div>
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

function graphData(t) {
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

  return { nodes, edges };
}

function renderGraph(t) {
  if (state.network) state.network.destroy();
  state.network = mountGraph($('graph'), graphData(t), { onNodeClick: showAccount });
}

/* Shared mount so the money trail can be drawn in more than one place - the
   console panel and the freeze report both render the same chain. */
function mountGraph(container, data, { onNodeClick } = {}) {
  const network = new vis.Network(container, data, {
    physics: { enabled: true, solver: 'forceAtlas2Based',
      forceAtlas2Based: { gravitationalConstant: -62, springLength: 150, springConstant: 0.06 },
      stabilization: { iterations: 220 } },
    interaction: { hover: true, dragView: true, zoomView: true, tooltipDelay: 120 },
    layout: { improvedLayout: true },
  });
  network.once('stabilizationIterationsDone', () => {
    network.setOptions({ physics: false });
    network.fit({ animation: { duration: 400 } });
  });
  if (onNodeClick) network.on('click', p => { if (p.nodes.length) onNodeClick(p.nodes[0]); });
  return network;
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

async function showAccount(id) {
  try {
    const a = await get('/api/risk-score/' + encodeURIComponent(id));
    const lines = a.signals.map(s => `${s.label}: ${s.value} (p${s.percentile})`).join('\n');
    alert(`${a.account_id}\n${a.bank} · ${a.account_type} · age ${a.account_age_days} d\n` +
          `mule score ${(a.score * 100).toFixed(0)}/100\n\n` +
          `in ${a.in_count} (${inr(a.total_in)}) · out ${a.out_count} (${inr(a.total_out)})\n\n${lines}`);
  } catch (e) { /* unknown account - nothing to show */ }
}

// ---------- trace ----------

/* The walk thresholds the console traces with. These are the values the
   evaluation reports against; the API still accepts overrides per request. */
const WALK = { min_forward_pct: 0.70, max_gap_hours: 48, max_hops: 10 };

function walkQuery() {
  return `min_forward_pct=${WALK.min_forward_pct.toFixed(2)}`
       + `&max_gap_hours=${WALK.max_gap_hours}`
       + `&max_hops=${WALK.max_hops}`;
}

async function runTrace(txnId) {
  try {
    const t = await get(`/api/trace/${encodeURIComponent(txnId)}?${walkQuery()}`);
    state.trace = t;
    renderEndnode(t);
    renderStats();          // the stat row describes the open chain, so it moves too
    renderGraph(t);
    renderTimeline(t);
    renderChains();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (e) {
    $('endnode-sub').textContent = `Could not trace ${txnId} — ${e.message}`;
  }
}

// ---------- views ----------

// ---------- boot ----------

/* Every figure on screen is derived from the API on each refresh - nothing is
   cached across a data change, so regenerating the dataset moves all of it. */
async function refresh({ retrace = false } = {}) {
  state.overview = await get('/api/overview');

  const { chains } = await get('/api/chains?limit=40');
  state.chains = chains;
  state.index = 0;
  renderChains();

  // a trace from a previous dataset must not be described against new totals
  if (retrace && state.trace && !chains.some(c => c.entry_txn_id === state.trace.entry_txn_id)) {
    state.trace = null;
  }
  renderStats();

  const badge = $('dataset-badge');
  if (badge) {
    badge.textContent = state.overview.dataset;
    badge.title = `Dataset ${state.overview.dataset} (seed ${state.overview.seed}) — `
                + 'every session gets its own network';
  }

  if (retrace) {
    if (state.trace) await runTrace(state.trace.entry_txn_id);
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
  renderStats();
}

async function boot() {
  await refresh({ retrace: true });
  $('loading').remove();
}

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

$('search-go').addEventListener('click', doSearch);
$('search-input').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

/* The search box is now the only way to trace an arbitrary transaction, so it
   accepts a txn id directly as well as an account. */
async function doSearch() {
  const q = $('search-input').value.trim();
  if (!q) return;
  if (/^TXN\d+$/i.test(q)) { runTrace(q.toUpperCase()); return; }
  const r = await get('/api/search?q=' + encodeURIComponent(q));
  if (r.accounts.length) showAccount(r.accounts[0]);
  else if (r.transactions.length) runTrace(r.transactions[0].txn_id);
}

// ---------- freeze request report ----------

/* Replaces the old confirm()/alert() pair. Everything below is derived from the
   open trace - nothing is written in. Fields a real freeze request needs but
   this feed does not carry (account number, IFSC, holder name, RRN/UTR) are
   shown as unavailable rather than invented, because a fabricated identifier on
   a freeze request is worse than a gap. */

const FR = { network: null, submitted: false };

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const NA = '<b class="na">not carried in this feed</b>';

function freezeRequestId(t) {
  // deterministic per chain, so reopening the report shows the same reference
  let h = 0;
  for (const ch of t.entry_txn_id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
  return `FRZ-${stamp}-${h.toString(36).toUpperCase().padStart(6, '0').slice(-6)}`;
}

function field(label, value, { na = false } = {}) {
  return `<div class="fr-field"><i>${esc(label)}</i>${na ? NA : `<b>${value}</b>`}</div>`;
}

function fraudIndicators(t) {
  const end = t.nodes[t.nodes.length - 1];
  const gap = t.risk.median_gap_minutes;
  const fwd = t.risk.avg_forward_pct;
  const out = [];

  if (gap != null && gap < 180) {
    out.push({
      sev: gap < 30 ? 'high' : 'medium',
      title: 'Rapid onward movement',
      detail: `Median ${mins(gap)} between hops. Funds were moved on faster than an `
            + `account holder would normally react to an unauthorised debit.`,
      tag: gap < 30 ? 'strong' : 'present',
    });
  }
  if (fwd != null && fwd >= 0.7) {
    out.push({
      sev: fwd >= 0.9 ? 'high' : 'medium',
      title: 'High pass-through ratio',
      detail: `${(fwd * 100).toFixed(0)}% of each received amount was forwarded on, `
            + `consistent with a conduit account rather than a beneficiary.`,
      tag: fwd >= 0.9 ? 'strong' : 'present',
    });
  }
  if (t.hop_count >= 3) {
    out.push({
      sev: t.hop_count >= 5 ? 'high' : 'medium',
      title: 'Multi-hop layering',
      detail: `${t.hop_count} sequential transfers across ${t.nodes.length} accounts, `
            + `distancing the beneficiary from the victim transaction.`,
      tag: `${t.hop_count} hops`,
    });
  }
  if (end.mule_score != null && end.mule_score >= 0.5) {
    out.push({
      sev: end.mule_score >= 0.75 ? 'high' : 'medium',
      title: 'Beneficiary scored as a probable mule',
      detail: `Classifier score ${(end.mule_score * 100).toFixed(0)}/100 on behavioural `
            + `features — turnaround speed, forwarding ratio and counterparty spread.`,
      tag: `score ${(end.mule_score * 100).toFixed(0)}`,
    });
  }
  if (end.account_age_days != null && end.account_age_days < 180) {
    out.push({
      sev: end.account_age_days < 60 ? 'high' : 'medium',
      title: 'Recently opened beneficiary account',
      detail: `Account age ${end.account_age_days} days at the time of receipt.`,
      tag: `${end.account_age_days} d old`,
    });
  }
  if (t.splits_detected > 0) {
    out.push({
      sev: 'high',
      title: 'Split transfers detected',
      detail: `${t.splits_detected} hop(s) were broken into multiple smaller legs, a `
            + `pattern used to stay under per-transaction review thresholds.`,
      tag: 'structuring',
    });
  }
  if (end.kyc_tier != null && end.kyc_tier <= 1) {
    out.push({
      sev: 'medium',
      title: 'Minimal KYC tier on beneficiary',
      detail: `Beneficiary account is at KYC tier ${end.kyc_tier}, limiting the `
            + `identity assurance behind the account.`,
      tag: `tier ${end.kyc_tier}`,
    });
  }
  const mules = t.nodes.filter(n => n.mule_score != null && n.mule_score >= 0.5).length;
  if (mules >= 2) {
    out.push({
      sev: mules >= 4 ? 'high' : 'medium',
      title: 'Multiple flagged accounts on one path',
      detail: `${mules} of ${t.nodes.length} accounts on this trail independently score `
            + `as probable mules, indicating a coordinated ring rather than one bad account.`,
      tag: `${mules} accounts`,
    });
  }
  return out;
}

function requestedAction(t) {
  const urgency = { critical: 'Immediate — same working day',
                    high: 'Same working day',
                    medium: 'Within T+1',
                    low: 'Routine queue' }[t.risk.band] || 'Routine queue';
  const partial = t.leakage_pct > 0.25;
  return {
    urgency,
    type: partial ? 'Partial lien — traced residue only' : 'Full lien on the traced amount',
    note: partial
      ? `${(t.leakage_pct * 100).toFixed(1)}% of the original victim amount was skimmed `
        + `across intermediate hops, so only the traced residue is claimed here.`
      : `Substantially the whole victim amount reached this account, so the lien is `
        + `requested against the full traced sum.`,
  };
}

function buildFreezeReport(t) {
  const o = state.overview;
  const end = t.nodes[t.nodes.length - 1];
  const entry = t.hops[0];
  const terminal = t.hops[t.hops.length - 1];
  const cash = t.end_reason === 'cash_out';
  const reqId = freezeRequestId(t);
  const caseId = t.ground_truth_chain || t.entry_txn_id;
  const now = new Date();
  const action = requestedAction(t);
  const indicators = fraudIndicators(t);

  $('fr-meta').innerHTML = [
    `Request <b>${esc(reqId)}</b>`,
    `Case <b>${esc(caseId)}</b>`,
    `Generated <b>${esc(now.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }))}</b>`,
    `Dataset <b>${esc(o.dataset)}</b>`,
  ].join('');

  const risk = $('fr-risk');
  risk.textContent = `${t.risk.band.toUpperCase()} · ${t.risk.score}/100`;
  risk.className = 'chip ' + (t.risk.band === 'critical' ? 'red'
                            : t.risk.band === 'low' ? 'green' : 'amber');

  const sections = [];

  // ---- cash-out warning, when the money is already gone ----
  if (cash) {
    sections.push(`
      <div class="fr-notice critical" style="margin-bottom:20px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="9"/></svg>
        <div><b>This trail terminates at a cash-out.</b> The traced funds left the banking
        channel at the final hop, so an account freeze cannot recover them. This report is
        retained as an evidence pack for law-enforcement escalation; the freeze request is
        not submittable.</div>
      </div>`);
  }

  // ---- summary ----
  sections.push(`
    <div class="fr-summary">
      <div class="fr-sum"><i>${cash ? 'Amount lost' : 'Freeze amount requested'}</i>
        <b>${inr(t.amount_at_end)}</b><span>of ${inr(t.amount_in)} originally debited</span></div>
      <div class="fr-sum"><i>Hops traced</i>
        <b>${t.hop_count}</b><span>${t.nodes.length} accounts on the path</span></div>
      <div class="fr-sum"><i>Chain duration</i>
        <b>${mins(t.elapsed_minutes)}</b><span>victim debit to final hop</span></div>
      <div class="fr-sum"><i>Risk score</i>
        <b style="color:${bandColor(t.risk.band)}">${t.risk.score}</b><span>${esc(t.risk.band)} priority</span></div>
    </div>`);

  // ---- 1. request details ----
  sections.push(`
    <div class="fr-section"><h4 data-n="1">Request Details</h4>
      <div class="fr-grid">
        ${field('Freeze request ID', esc(reqId))}
        ${field('Case / chain reference', esc(caseId))}
        ${field('Raised at', esc(now.toISOString()))}
        ${field('Raised by', 'Fraud Desk — MuleTrace Investigation Console')}
        ${field('Priority', `${esc(t.risk.band)} · ${t.risk.score}/100`)}
        ${field('Detection mode', t.ground_truth_chain ? 'Proactive scan (no complaint filed)' : 'Complaint-triggered trace')}
      </div>
    </div>`);

  // ---- 2. target account ----
  sections.push(`
    <div class="fr-section"><h4 data-n="2">Target Account — Freeze Beneficiary</h4>
      <div class="fr-grid">
        ${field('Virtual payment address', esc(end.account_id))}
        ${field('Bank', esc(end.bank || '—'))}
        ${field('Account number (masked)', '', { na: true })}
        ${field('IFSC', '', { na: true })}
        ${field('Account holder name', '', { na: true })}
        ${field('Account age at receipt', end.account_age_days != null ? `${end.account_age_days} days` : '—')}
        ${field('KYC tier', end.kyc_tier != null ? `Tier ${end.kyc_tier}` : '—')}
        ${field('Mule probability', end.mule_score != null ? `${(end.mule_score * 100).toFixed(0)}/100` : '—')}
      </div>
    </div>`);

  // ---- 3. transaction details ----
  sections.push(`
    <div class="fr-section"><h4 data-n="3">Transaction Details</h4>
      <div class="fr-grid">
        ${field('Originating UPI txn ID', esc(t.entry_txn_id))}
        ${field('Terminal UPI txn ID', esc(terminal.txn_id))}
        ${field('RRN / UTR', '', { na: true })}
        ${field('Victim debit at', esc(when(t.first_seen)))}
        ${field('Funds settled at target', esc(when(t.last_seen)))}
        ${field('Original amount debited', inr(t.amount_in))}
        ${field('Amount at target account', inr(t.amount_at_end))}
        ${field('Payer (victim VPA)', esc(t.victim))}
        ${field('First beneficiary', esc(entry.target))}
        ${field('Channel', esc(terminal.mode))}
      </div>
    </div>`);

  // ---- 4. basis ----
  sections.push(`
    <div class="fr-section"><h4 data-n="4">Basis for Freeze</h4>
      <div class="fr-prose">
        Funds debited from <b>${esc(t.victim)}</b> on ${esc(when(t.first_seen))} were traced
        forward through <b>${t.hop_count} sequential transfers</b> to
        <b>${esc(end.account_id)}</b>, where movement stopped
        ${cash ? 'at a cash-out point' : `after ${mins(t.elapsed_minutes)}`}.
        Each hop was admitted only where it occurred after the funds arrived, inside a
        48-hour window, and forwarded between 70% and 125% of the amount received —
        ${esc(t.risk.explanation)}.
        ${cash ? '' : `<b>${inr(t.amount_at_end)}</b> is assessed as still resting at the
        target account and is the subject of this request.`}
      </div>
    </div>`);

  // ---- 5. indicators ----
  sections.push(`
    <div class="fr-section"><h4 data-n="5">Fraud Indicators</h4>
      ${indicators.length ? `<div class="fr-indicators">${indicators.map(i => `
        <div class="fr-ind ${i.sev}">
          <div class="fr-ind-body">
            <strong>${esc(i.title)}</strong>
            <span>${i.detail}</span>
          </div>
          <span class="fr-ind-tag">${esc(i.tag)}</span>
        </div>`).join('')}</div>`
        : `<div class="fr-prose">No individual indicator crossed its threshold on this
           chain; the request rests on the traced path itself.</div>`}
    </div>`);

  // ---- 6. money trail ----
  sections.push(`
    <div class="fr-section"><h4 data-n="6">Money Trail</h4>
      <div id="fr-graph"></div>
    </div>`);

  // ---- 7. hop ledger ----
  sections.push(`
    <div class="fr-section"><h4 data-n="7">Hop Ledger</h4>
      <div class="fr-table-wrap">
        <table>
          <thead><tr>
            <th>Hop</th><th>Receiving account</th><th>Bank</th><th>Amount</th>
            <th>Timestamp</th><th>Gap</th><th>Forwarded</th><th>Mule score</th>
          </tr></thead>
          <tbody>${t.hops.map((h, i) => {
            const node = t.nodes[i + 1] || {};
            const last = i === t.hops.length - 1;
            return `<tr class="${last ? 'is-end' : ''}">
              <td>${h.hop}</td>
              <td class="vpa">${esc(h.target)}</td>
              <td>${esc(node.bank || '—')}</td>
              <td>${inr(h.amount)}</td>
              <td>${esc(when(h.timestamp))}</td>
              <td>${h.hop === 0 ? '—' : esc(mins(h.gap_minutes))}</td>
              <td>${h.hop === 0 ? '—' : (h.forward_pct * 100).toFixed(0) + '%'}</td>
              <td>${node.mule_score != null ? (node.mule_score * 100).toFixed(0) : '—'}</td>
            </tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    </div>`);

  // ---- 8. requested action ----
  sections.push(`
    <div class="fr-section"><h4 data-n="8">Requested Action</h4>
      <div class="fr-grid">
        ${field('Action sought', cash ? 'No freeze — law-enforcement escalation' : 'Freeze / lien on credit balance')}
        ${field('Amount to be held', inr(t.amount_at_end))}
        ${field('Scope', esc(action.type))}
        ${field('Urgency', esc(action.urgency))}
      </div>
      <div class="fr-prose" style="margin-top:12px">${action.note}</div>
    </div>`);

  // ---- 9. evidence ----
  const txnIds = t.hops.map(h => h.txn_id);
  sections.push(`
    <div class="fr-section"><h4 data-n="9">Evidence & References</h4>
      <div class="fr-grid">
        ${field('Chain reference', esc(caseId))}
        ${field('Transactions in trail', `${txnIds.length} — ${esc(txnIds.join(', '))}`)}
        ${field('Accounts in trail', esc(t.path.join(' → '))) }
        ${field('Detection rule', 'Temporal chain walk — forward ≥70% and ≤125%, ≤48 h gap, ≤10 hops')}
        ${field('Termination reason', esc(t.end_reason.replace(/_/g, ' ')))}
        ${field('Complaint reference', '', { na: true })}
        ${field('Dataset', `${esc(o.dataset)} (seed ${o.seed})`)}
        ${field('Split legs observed', t.splits_detected ? `${t.splits_detected}` : 'none')}
      </div>
    </div>`);

  // ---- 10. regulatory notice ----
  sections.push(`
    <div class="fr-section"><h4 data-n="10">Regulatory Notice</h4>
      <div class="fr-notice">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="9"/></svg>
        <div><b>This is an operational fraud-intervention request, not a suspicious
        transaction report.</b> It seeks a precautionary freeze or lien on the identified
        account so that traced funds are preserved pending investigation. It does not
        constitute an STR filing under the PMLA, and any applicable FIU-IND reporting
        obligation remains with the authorised reporting entity. Freeze action requires
        review and authorisation by the receiving bank; a chain surfaced by automated
        detection is a lead, not a determination of guilt.</div>
      </div>
    </div>`);

  $('fr-body').innerHTML = sections.join('');

  // reuse the same money-trail rendering the console panel uses
  if (FR.network) { FR.network.destroy(); FR.network = null; }
  FR.network = mountGraph($('fr-graph'), graphData(t));

  // cash-out chains are evidence packs, not freeze requests
  const submit = $('fr-submit');
  submit.disabled = cash;
  submit.style.display = cash ? 'none' : '';
  $('fr-foot-note').textContent = cash
    ? 'Funds left the banking channel — escalate to law enforcement rather than the bank.'
    : 'Submitting logs this chain as confirmed fraud and queues the request for bank action.';
}

function openFreezeReport() {
  const t = state.trace;
  if (!t) return;
  FR.submitted = false;
  buildFreezeReport(t);

  const overlay = $('fr-overlay');
  overlay.hidden = false;
  requestAnimationFrame(() => overlay.classList.add('open'));
  $('fr-body').scrollTop = 0;
  document.body.style.overflow = 'hidden';
  $('fr-close').focus();
}

function closeFreezeReport() {
  const overlay = $('fr-overlay');
  overlay.classList.remove('open');
  document.body.style.overflow = '';
  setTimeout(() => {
    overlay.hidden = true;
    if (FR.network) { FR.network.destroy(); FR.network = null; }
  }, 220);
}

async function submitFreezeRequest() {
  const t = state.trace;
  if (!t || FR.submitted) return;
  const submit = $('fr-submit');
  submit.disabled = true;
  submit.textContent = 'Submitting…';

  try {
    // the existing feedback API - unchanged
    const body = await recordVerdict('confirmed_fraud', 'freeze requested from console');
    FR.submitted = true;
    await refresh();

    const reqId = freezeRequestId(t);
    $('fr-foot-note').innerHTML = '';
    $('fr-body').insertAdjacentHTML('afterbegin', `
      <div class="fr-sent" style="margin-bottom:18px">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m5 13 4 4L19 7"/></svg>
        <div><b>Request sent — awaiting bank acknowledgement.</b>
        ${esc(reqId)} has been queued to ${esc(t.nodes[t.nodes.length - 1].bank || 'the receiving bank')}.
        No freeze is in force until the bank confirms; ${body.chains_reviewed} chain(s)
        reviewed on this dataset so far.</div>
      </div>`);
    $('fr-body').scrollTop = 0;
    submit.style.display = 'none';
    $('fr-cancel').textContent = 'Close';
  } catch (e) {
    submit.disabled = false;
    submit.textContent = 'Submit Freeze Request';
    $('fr-foot-note').innerHTML =
      `<span style="color:var(--red)">Could not submit — ${esc(e.message)}</span>`;
  }
}

$('btn-freeze').addEventListener('click', openFreezeReport);
$('fr-cancel').addEventListener('click', closeFreezeReport);
$('fr-close').addEventListener('click', closeFreezeReport);
$('fr-submit').addEventListener('click', submitFreezeRequest);
$('fr-overlay').addEventListener('click', e => {
  if (e.target === $('fr-overlay')) closeFreezeReport();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('fr-overlay').hidden) closeFreezeReport();
});

boot().catch(err => {
  /* On a static host there is no API behind the console. Say what is missing and
     where the working demo is, rather than showing a bare fetch error. */
  const staticHost = !/^(localhost|127\.0\.0\.1)$/.test(location.hostname);
  $('loading').innerHTML = staticHost
    ? `<div style="max-width:460px;text-align:center;line-height:1.6">
         <p style="color:var(--amber);font-weight:700;margin:0 0 10px">
           No detection backend is connected</p>
         <p style="color:var(--muted);font-size:12.5px;margin:0 0 14px">
           The console traces chains against a live FastAPI service. This host
           serves the interface only, so there is nothing to query yet.</p>
         <p style="color:var(--muted-2);font-size:11.5px;margin:0 0 18px">
           Point <code>/api/*</code> at a deployed backend in
           <code>netlify.toml</code>, or run it locally:<br>
           <code>uvicorn app:app --app-dir backend</code></p>
         <a class="btn btn-primary" href="/">Back to the overview</a>
       </div>`
    : `<p style="color:#bf5f66">Could not reach the API — ${err.message}</p>`;
});
