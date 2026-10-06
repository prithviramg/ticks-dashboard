// NIFTY straddle dashboard: renders data/<date>.json (built by build_dashboard.py) with Plotly.js.
'use strict';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const DAY_END = '15:30:00'; // MARKET_CLOSE in build_dashboard.py
const ATM_COLOR = '#8250df';
const SIDE_STYLE = {
  SELL: { symbol: 'triangle-down', color: '#cf222e' },
  BUY: { symbol: 'triangle-up', color: '#1a7f37' },
};
const MIXED_STYLE = { symbol: 'diamond', color: '#9a6700' }; // SELL and BUY in the same bucket
const LEG_DASH = { CE: 'dash', PE: 'dot' }; // a single leg; the straddle (CE + PE) is solid
const CACHE_DAYS = 8; // parsed days kept in memory (~4 MB each); older ones are re-fetched
// Expiry slots in day.slots; weeklies first so a contract that is both prefers its weekly slot.
const SLOTS = ['cw', 'nw', 'cm', 'nm'];
const SLOT_NAMES = { cw: 'This week', nw: 'Next week', cm: 'This month', nm: 'Next month' };

/** "2026-10-06" -> "06 Oct" */
function shortDate(iso) {
  return `${iso.slice(8)} ${MONTHS[+iso.slice(5, 7) - 1]}`;
}

function weekday(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return WEEKDAYS[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
}

/** Blue below ATM, red above, purple at ATM; darker nearer ATM. */
function strikeColor(offset, nStrikes) {
  if (offset === 0) return ATM_COLOR;
  const far = Math.min((Math.abs(offset) - 1) / Math.max(nStrikes - 1, 1), 1);
  const lightness = Math.round(32 + 38 * far);
  return offset < 0 ? `hsl(212, 75%, ${lightness}%)` : `hsl(2, 75%, ${lightness}%)`;
}

/** Slot shown first: the one holding all of the day's trades, else this week. */
function defaultSlot(day) {
  const traded = new Set(day.trades.map((t) => t.expiry));
  if (traded.size === 1) {
    const [expiry] = traded;
    const slot = SLOTS.find((s) => day.slots[s] === expiry);
    if (slot) return slot;
  }
  return 'cw';
}

/**
 * Exponential moving average over `minutes` of a series sampled every `stepMs`
 * (span = minutes / step, alpha = 2 / (span + 1)). It starts at the first value; leading nulls stay null.
 */
function ema(y, minutes, stepMs) {
  const alpha = 2 / ((minutes * 60000) / stepMs + 1);
  let prev = null;
  return y.map((v) => {
    if (v != null) prev = prev == null ? v : prev + alpha * (v - prev);
    return prev;
  });
}

/** Nearest non-null value at or before bucket i (else after it). */
function valueAt(y, i) {
  for (let j = i; j >= 0; j--) if (y[j] != null) return y[j];
  for (let j = i + 1; j < y.length; j++) if (y[j] != null) return y[j];
  return null;
}

/** CE + PE per bucket; null where either leg has no price yet. */
function sumLegs(ce, pe) {
  return ce.map((v, i) => (v == null || pe[i] == null ? null : v + pe[i]));
}

/**
 * Strike rows for one expiry slot: ATM ± nStrikes plus every traded strike, highest first.
 * The chart and the strike panel both use it, so they always list the same strikes.
 * @returns {exp, rows: [{k, offset, color, trades}]}
 */
function strikeRows(day, slot, nStrikes) {
  const exp = day.expiries.find((e) => e.expiry === day.slots[slot]);
  const tradesByStrike = new Map();
  for (const t of day.trades) {
    if (t.expiry !== exp.expiry) continue;
    if (!tradesByStrike.has(t.strike)) tradesByStrike.set(t.strike, []);
    tradesByStrike.get(t.strike).push(t);
  }
  const rows = Object.keys(exp.legs)
    .filter((k) => Math.abs(+k - day.atm) <= nStrikes * day.step || tradesByStrike.has(k))
    .sort((a, b) => b - a)
    .map((k) => {
      const offset = Math.round((+k - day.atm) / day.step);
      return { k, offset, color: strikeColor(offset, nStrikes), trades: tradesByStrike.get(k) || [] };
    });
  return { exp, rows };
}

/** {CE, PE} legs plotted for a strike row: the user's pick, else both for a traded strike and neither otherwise. */
function legsFor(sel, row) {
  const traded = row.trades.length > 0;
  return sel[row.k] ?? { CE: traded, PE: traded };
}

/** ▼ SELL / ▲ BUY markers on a traded strike's line; orders in the same bucket share a marker. */
function tradeMarkers(day, strike, y, trades) {
  const buckets = new Map();
  for (const t of trades) {
    if (!buckets.has(t.i)) buckets.set(t.i, []);
    buckets.get(t.i).push(t);
  }
  const points = [...buckets.values()].map((group) => {
    const sides = new Set(group.map((t) => t.side));
    const style = sides.size === 1 ? SIDE_STYLE[group[0].side] || MIXED_STYLE : MIXED_STYLE;
    const text = group
      .map((t) => `${t.side} ${t.strike} ${t.type} @ ${t.price.toFixed(2)} · ${t.time} · ${t.strategy} (${t.mode})`)
      .join('<br>');
    return { x: `${day.date} ${group[0].time}`, y: valueAt(y, group[0].i), text, ...style };
  });
  return {
    type: 'scatter',
    mode: 'markers',
    yaxis: 'y2',
    name: `${strike} trades`,
    x: points.map((p) => p.x),
    y: points.map((p) => p.y),
    text: points.map((p) => p.text),
    marker: {
      symbol: points.map((p) => p.symbol),
      color: points.map((p) => p.color),
      size: 13,
      line: { width: 1, color: '#ffffff' },
    },
    hovertemplate: '%{text}<extra></extra>',
  };
}

/**
 * Plotly traces and layout for one day.
 * Spot is on the left axis (y); strike lines and trade markers are on the right axis (y2), drawn above spot.
 * A strike with both legs selected plots the straddle (CE + PE); with one leg, just that leg (dashed CE,
 * dotted PE) and only that leg's trades.
 * @param day       parsed data/<date>.json
 * @param slot      expiry slot: 'cw' this week, 'nw' next week, 'cm' this month, 'nm' next month
 * @param nStrikes  strikes shown on each side of ATM (traded strikes are always shown)
 * @param sel       {spot: visible, [strike]: {CE, PE}} picks from the strike panel; see legsFor() for defaults
 * @param theme     {fg, grid, bg} colours from the page's CSS
 * @param emaMinutes  0 = raw premiums, else their EMA over this many minutes (spot stays raw)
 */
function buildFigure(day, slot, nStrikes, sel = {}, theme = {}, emaMinutes = 0) {
  const grid = { x0: day.start, dx: day.step_ms }; // regular time grid, no per-trace timestamps
  const { exp, rows } = strikeRows(day, slot, nStrikes);

  const data = [
    {
      type: 'scatter',
      mode: 'lines',
      name: 'NIFTY spot',
      ...grid,
      y: day.spot,
      line: { color: theme.fg || '#1f2328', width: 2.2 },
      hovertemplate: '%{y:.2f}',
      visible: sel.spot ?? true,
    },
  ];
  for (const row of rows) {
    const { k, offset, trades } = row;
    const legs = legsFor(sel, row);
    if (!legs.CE && !legs.PE) continue;
    const leg = legs.CE && legs.PE ? null : legs.CE ? 'CE' : 'PE'; // null: the straddle
    const premium = leg ? exp.legs[k][leg] : sumLegs(exp.legs[k].CE, exp.legs[k].PE);
    const y = emaMinutes ? ema(premium, emaMinutes, day.step_ms) : premium;
    data.push({
      type: 'scatter',
      mode: 'lines',
      name: `${k} ${leg || 'CE+PE'}${offset === 0 ? ' (ATM)' : ''}${trades.length ? ' ★' : ''}`,
      ...grid,
      y,
      yaxis: 'y2',
      line: {
        color: row.color,
        width: trades.length ? 3 : offset === 0 ? 2.2 : 1.4,
        dash: leg ? LEG_DASH[leg] : 'solid',
      },
      hovertemplate: '%{y:.2f}',
    });
    const shown = leg ? trades.filter((t) => t.type === leg) : trades;
    if (shown.length) data.push(tradeMarkers(day, k, y, shown));
  }

  const layout = {
    uirevision: `${day.date}|${exp.expiry}`, // keep the user's zoom across strike toggles, reset per day/expiry
    showlegend: false, // strikes are picked in the side panel
    hovermode: 'x unified',
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { family: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif', color: theme.fg || '#1f2328' },
    hoverlabel: { bgcolor: theme.bg || '#ffffff', font: { color: theme.fg || '#1f2328' } },
    xaxis: {
      type: 'date',
      range: [day.start, `${day.date} ${DAY_END}`],
      tickformat: '%H:%M',
      gridcolor: theme.grid,
    },
    yaxis: { title: { text: 'NIFTY spot' }, tickformat: 'd', showgrid: false, zeroline: false },
    yaxis2: {
      title: { text: `Premium (₹)${emaMinutes ? ` · EMA ${emaMinutes}m` : ''}` },
      overlaying: 'y',
      side: 'right',
      gridcolor: theme.grid,
      zeroline: false,
    },
    margin: { l: 64, r: 64, t: 16, b: 32 },
  };
  return { data, layout };
}

/**
 * Get `key` from `cache`, a Map used as an LRU of at most `limit` entries; calls load(key) on a miss.
 * A Map keeps insertion order, so re-inserting marks an entry most recent and the first key is the oldest.
 */
function lruGet(cache, key, load, limit) {
  let value;
  if (cache.has(key)) {
    value = cache.get(key);
    cache.delete(key);
  } else {
    value = load(key);
  }
  cache.set(key, value);
  if (cache.size > limit) cache.delete(cache.keys().next().value);
  return value;
}

/** One-line description of the day shown above the chart. */
function summaryText(day, slot) {
  const expiry = day.slots[slot];
  const trades = day.trades.filter((t) => t.expiry === expiry);
  const strikes = [...new Set(trades.map((t) => t.strike))];
  const modes = [...new Set(trades.map((t) => t.mode))].join('/');
  return (
    `${weekday(day.date)} ${shortDate(day.date)} ${day.date.slice(0, 4)} · ATM ${day.atm}` +
    ` (spot ${day.open_spot.toFixed(2)} at ${day.open_time.slice(0, 5)}) · ${SLOT_NAMES[slot].toLowerCase()}'s` +
    ` expiry ${shortDate(expiry)} · ` +
    (trades.length ? `traded ${strikes.join(', ')} ★ (${trades.length} orders, ${modes})` : 'no trades in this expiry')
  );
}

if (typeof document !== 'undefined') {
  const $ = (id) => document.getElementById(id);
  const chart = $('chart');
  const daySelect = $('day');
  const panel = $('strikes');
  // sel: "date|expiry" -> {spot, [strike]: {CE, PE}}, so strike picks survive range/expiry switches.
  // rows: strikeRows() of what the panel currently lists.
  const state = { days: [], cache: new Map(), sel: new Map(), day: null, slot: 'cw', plotted: false, rows: [] };

  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const theme = () => ({ fg: css('--fg'), grid: css('--grid'), bg: css('--bg') });
  const nStrikes = () => +document.querySelector('input[name=range]:checked').value;
  const emaMinutes = () => +document.querySelector('input[name=ema]:checked').value;

  /** Strike picks for the current day and expiry, created empty (all defaults) on first use. */
  function selection() {
    const key = `${state.day.date}|${state.day.slots[state.slot]}`;
    if (!state.sel.has(key)) state.sel.set(key, {});
    return state.sel.get(key);
  }

  function showStatus(message) {
    if (state.plotted) Plotly.purge(chart);
    state.plotted = false;
    const p = document.createElement('p');
    p.id = 'status';
    p.textContent = message;
    chart.replaceChildren(p);
    panel.replaceChildren();
    state.rows = [];
    $('summary').textContent = '';
  }

  function checkbox(checked, label, data) {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = checked;
    box.setAttribute('aria-label', label);
    Object.assign(box.dataset, data);
    return box;
  }

  /** Check a strike row's CE / PE boxes to match `legs`; dim the row when neither is on. */
  function syncRow(el, legs) {
    for (const box of el.querySelectorAll('input[data-leg]')) box.checked = legs[box.dataset.leg];
    el.querySelector('.strike').setAttribute('aria-pressed', legs.CE && legs.PE);
    el.classList.toggle('off', !legs.CE && !legs.PE);
  }

  /** Strike list right of the chart: spot toggle, then per strike a button (both legs) and CE / PE boxes. */
  function renderPanel() {
    const sel = selection();
    const spot = document.createElement('label');
    spot.className = 'spot';
    spot.append(checkbox(sel.spot ?? true, 'NIFTY spot', { spot: '' }), 'NIFTY spot');
    const head = document.createElement('div');
    head.className = 'row head';
    head.append(...['Strike', 'CE', 'PE'].map((text) => Object.assign(document.createElement('span'), { textContent: text })));
    const rows = state.rows.map((row) => {
      const el = document.createElement('div');
      el.className = 'row';
      el.dataset.k = row.k;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'strike';
      button.title = 'Show or hide both legs (CE + PE)';
      button.style.setProperty('--swatch', row.color);
      button.textContent = `${row.k}${row.offset === 0 ? ' ATM' : ''}${row.trades.length ? ' ★' : ''}`;
      const cells = ['CE', 'PE'].map((leg) => {
        const cell = document.createElement('label');
        cell.className = 'cell';
        cell.append(checkbox(false, `${row.k} ${leg}`, { leg }));
        return cell;
      });
      el.append(button, ...cells);
      syncRow(el, legsFor(sel, row));
      return el;
    });
    panel.replaceChildren(spot, head, ...rows);
  }

  function drawChart() {
    const { data, layout } = buildFigure(state.day, state.slot, nStrikes(), selection(), theme(), emaMinutes());
    if (!state.plotted) chart.replaceChildren();
    Plotly.react(chart, data, layout, { responsive: true, displaylogo: false });
    state.plotted = true;
  }

  /** Strike panel + chart; for changes of day, expiry or strike range. */
  function render() {
    if (!state.day) return;
    state.rows = strikeRows(state.day, state.slot, nStrikes()).rows;
    renderPanel();
    drawChart();
    $('summary').textContent = summaryText(state.day, state.slot);
  }

  /** Panel input: the spot box, a CE / PE box (just that leg), or a strike button (both legs on, or both off). */
  function onPanelInput(e) {
    const sel = selection();
    if (e.target.matches('input[data-spot]')) {
      sel.spot = e.target.checked;
    } else {
      const el = e.target.closest('.row[data-k]');
      const row = el && state.rows.find((r) => r.k === el.dataset.k);
      if (!row) return;
      if (e.target.matches('input[data-leg]')) {
        sel[row.k] = { ...legsFor(sel, row), [e.target.dataset.leg]: e.target.checked };
      } else if (e.target.closest('.strike')) {
        const legs = legsFor(sel, row);
        const on = !(legs.CE && legs.PE);
        sel[row.k] = { CE: on, PE: on };
      } else {
        return;
      }
      syncRow(el, sel[row.k]);
    }
    drawChart(); // the panel stays as is, so keyboard focus isn't lost
  }

  function fetchDay(date) {
    return fetch(`data/${date}.json`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .catch((e) => {
        state.cache.delete(date); // retry on the next visit
        throw e;
      });
  }

  const loadDay = (date) => lruGet(state.cache, date, fetchDay, CACHE_DAYS);

  async function showDay(date) {
    daySelect.value = date;
    const i = state.days.indexOf(date);
    $('older').disabled = i >= state.days.length - 1;
    $('newer').disabled = i <= 0;
    if (location.hash.slice(1) !== date) history.replaceState(null, '', `#${date}`);
    let day;
    try {
      day = await loadDay(date);
    } catch (e) {
      showStatus(`Could not load ${date}: ${e.message}`);
      return;
    }
    if (daySelect.value !== date) return; // another day was picked while this one loaded
    state.day = day;
    state.slot = defaultSlot(day);
    for (const slot of SLOTS) {
      const input = $(`slot-${slot}`);
      const expiry = day.slots[slot];
      input.disabled = !expiry;
      input.checked = slot === state.slot;
      const date = document.createElement('small');
      date.textContent = expiry ? shortDate(expiry) : '—';
      input.nextElementSibling.replaceChildren(SLOT_NAMES[slot], date);
    }
    render();
  }

  function stepDay(delta) {
    const i = state.days.indexOf(daySelect.value) + delta;
    if (i >= 0 && i < state.days.length) showDay(state.days[i]);
  }

  async function init() {
    try {
      const r = await fetch('data/days.json', { cache: 'no-cache' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.days = (await r.json()).days;
    } catch (e) {
      showStatus(`Could not load the list of days: ${e.message}`);
      return;
    }
    if (!state.days.length) {
      showStatus('No days built yet.');
      return;
    }
    let group = null;
    for (const d of state.days) {
      const month = `${MONTHS[+d.slice(5, 7) - 1]} ${d.slice(0, 4)}`;
      if (!group || group.label !== month) {
        group = document.createElement('optgroup');
        group.label = month;
        daySelect.append(group);
      }
      group.append(new Option(`${shortDate(d)} · ${weekday(d)}`, d));
    }
    daySelect.addEventListener('change', () => showDay(daySelect.value));
    $('older').addEventListener('click', () => stepDay(+1)); // days are newest first
    $('newer').addEventListener('click', () => stepDay(-1));
    for (const el of document.querySelectorAll('input[name=slot]')) {
      el.addEventListener('change', () => {
        state.slot = el.value;
        render();
      });
    }
    for (const el of document.querySelectorAll('input[name=range]')) el.addEventListener('change', render);
    // EMA and theme don't change the strike list, so only the chart is redrawn.
    for (const el of document.querySelectorAll('input[name=ema]')) el.addEventListener('change', () => state.day && drawChart());
    panel.addEventListener('change', onPanelInput);
    panel.addEventListener('click', (e) => e.target.closest('.strike') && onPanelInput(e));
    window.addEventListener('hashchange', () => {
      const d = location.hash.slice(1);
      if (state.days.includes(d) && d !== daySelect.value) showDay(d);
    });
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => state.day && drawChart());
    const fromHash = location.hash.slice(1);
    showDay(state.days.includes(fromHash) ? fromHash : state.days[0]);
  }

  init();
}

if (typeof module !== 'undefined') {
  module.exports = { buildFigure, strikeRows, legsFor, sumLegs, defaultSlot, summaryText, strikeColor, ema, lruGet };
}
