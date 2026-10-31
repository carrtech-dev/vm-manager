import { api, esc, size } from './core.js';

export const ranges = [['5m', '5 minutes'], ['1h', '1 Hour'], ['12h', '12 Hours'],
  ['1d', '1 Day'], ['1w', '1 Week'], ['1mo', '1 Month'], ['6mo', '6 Months'], ['12mo', '12 Months']];
const cache = new Map();
const selection = new Map();
const pending = new Set();
const format = (value, cpu) => value == null ? 'Unavailable' : cpu ? `${value.toFixed(1)}%` : value === 0 ? '0 B' : size(value);
const date = seconds => new Date(seconds * 1000).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});

export function chartPaths(points, start, end, maximum, step) {
  const x = t => 72 + (t - start) / (end - start) * 502;
  const y = value => 166 - Math.max(0, Math.min(1, value / maximum)) * 140;
  let line = '', capacity = '', peaks = '', previous = null, previousTotal = null;
  for (const [at, value, total, low, high] of points) {
    if (at < start) continue;
    if (value != null) {
      line += `${previous != null && at - previous <= step * 1.5 ? 'L' : 'M'}${x(at).toFixed(1)},${y(value).toFixed(1)} `;
      if (low != null && high != null && high > low)
        peaks += `M${x(at).toFixed(1)},${y(low).toFixed(1)} L${x(at).toFixed(1)},${y(high).toFixed(1)} `;
      previous = at;
    } else previous = null;
    if (total != null) {
      capacity += `${previousTotal != null && at - previousTotal <= step * 1.5 ? 'L' : 'M'}${x(at).toFixed(1)},${y(total).toFixed(1)} `;
      previousTotal = at;
    } else previousTotal = null;
  }
  return { line, capacity, peaks };
}

function chart(series, data) {
  const cpu = series.metric === 'cpu';
  const points = series.points || [];
  const available = points.filter(p => p[1] != null);
  const latest = series.latest;
  const fresh = latest && data.end - latest.at <= 30;
  const maximum = cpu ? 100 : Math.max(1, ...points.flatMap(p => [p[1] || 0, p[2] || 0, p[4] || 0]));
  const { line, capacity, peaks } = chartPaths(points, data.start, data.end, maximum, data.step);
  const summary = fresh ? format(latest.value, cpu) : 'Unavailable';
  return `<article class="monitor-chart" data-metric="${esc(series.metric)}" data-series="${esc(series.id)}">
    <div class="monitor-chart-heading"><h3>${esc(series.label)}</h3><strong>${esc(summary)}${!cpu && fresh && latest.total ? `<small> / ${esc(size(latest.total))}</small>` : ''}</strong></div>
    <p class="monitor-source">${esc(fresh ? latest.source : series.source)}</p>
    <div class="monitor-plot"><svg viewBox="0 0 600 210" role="img" aria-label="${esc(series.label)} history. ${esc(summary)}. ${esc(series.source)}" tabindex="0">
      ${[0, .5, 1].map(v => `<line class="monitor-gridline" x1="72" x2="574" y1="${166-v*140}" y2="${166-v*140}"/><text class="monitor-axis" x="65" y="${170-v*140}" text-anchor="end">${esc(format(v*maximum, cpu))}</text>`).join('')}
      <path class="monitor-capacity" d="${cpu ? '' : capacity}"/><path class="monitor-peaks" d="${peaks}"/><path class="monitor-line" d="${line}"/>
      ${available.length === 1 && available[0][0] >= data.start ? `<circle class="monitor-dot" cx="${72+(available[0][0]-data.start)/(data.end-data.start)*502}" cy="${166-available[0][1]/maximum*140}" r="3"/>` : ''}
      <text class="monitor-axis" x="72" y="194">${esc(date(data.start))}</text><text class="monitor-axis" x="574" y="194" text-anchor="end">${esc(date(data.end))}</text>
    </svg>${!available.length ? '<div class="monitor-empty">No measurements in this range</div>' : ''}<output class="monitor-tooltip" hidden></output></div>
    <div class="monitor-legend"><span>● Used${cpu ? ' · all allocated CPUs' : ''}</span>${!cpu ? '<span>┄ Capacity</span>' : ''}<span>│ Min–max</span></div>
  </article>`;
}

export function monitoringPanel(resource, disks = []) {
  const range = selection.get(resource) || '5m';
  return `<section id="monitoring" data-resource="${esc(resource)}" data-range="${range}">
    <div class="monitor-toolbar"><div><h2>${resource === 'host' ? 'Host monitoring' : 'Monitoring'}</h2><p class="subtle">Updates every 10 seconds · History retained for up to 12 months</p></div>
    <label>Time range <select id="monitor-range">${ranges.map(([key,label]) => `<option value="${key}" ${range === key ? 'selected' : ''}>Last ${label}</option>`).join('')}</select></label></div>
    <div id="monitor-status" class="subtle" role="status">${cache.get(resource + '/' + range) ? 'Last refreshed ' + new Date(cache.get(resource + '/' + range).at).toLocaleTimeString() : 'Loading monitoring…'}</div><div id="monitor-charts" class="monitor-charts">${chartContent(resource, range, disks)}</div>
    <p class="monitor-note subtle">History starts when monitoring is enabled. Gaps indicate stopped VMs, unavailable measurements, or service downtime. Longer ranges show averages and min–max peaks. VM disks show mounted filesystem usage; unmounted disks and filesystems spanning disks may be unavailable.</p></section>`;
}

function chartContent(resource, range, disks) {
  const data = cache.get(resource + '/' + range)?.data || {start:Date.now()/1000-300, end:Date.now()/1000, step:10, series:[]};
  const wanted = [['cpu','', 'CPU usage'], ['memory','','Memory usage'], ...disks.map(key => ['disk',key,resource === 'host' ? 'Disk space' : 'Disk ' + key])];
  return wanted.map(([metric,id,label]) => chart(data.series.find(s => s.metric === metric && s.id === id) ||
    {metric,id,label,source:'Waiting for measurements',points:[]}, data)).join('');
}

export async function refreshMonitoring() {
  const element = document.querySelector('#monitoring');
  if (!element) return;
  const {resource, range} = element.dataset;
  const disks = [...element.querySelectorAll('[data-metric="disk"]')].map(e => e.dataset.series);
  element.querySelector('#monitor-range').onchange = event => {
    selection.set(resource, event.target.value);
    element.dataset.range = event.target.value;
    element.querySelector('#monitor-charts').innerHTML = chartContent(resource, event.target.value, disks);
    refreshMonitoring();
  };
  bindTooltips(element, resource, range);
  const key = resource + '/' + range;
  if (pending.has(key) || Date.now() - (cache.get(key)?.at || 0) < 9500) return;
  pending.add(key);
  try {
    const endpoint = resource === 'host' ? '/monitoring/host' : '/vms/' + encodeURIComponent(resource) + '/monitoring';
    const data = await api(endpoint + '?range=' + range);
    cache.set(key, {data,at:Date.now()});
    // Bound browser memory when browsing many VMs/ranges.
    if (cache.size > 32) cache.delete(cache.keys().next().value);
    const current = document.querySelector('#monitoring');
    if (current?.dataset.resource !== resource || current.dataset.range !== range) return;
    current.querySelector('#monitor-charts').innerHTML = chartContent(resource, range, disks);
    current.querySelector('#monitor-status').textContent = 'Last refreshed ' + new Date().toLocaleTimeString();
    bindTooltips(current, resource, range);
  } catch (error) {
    const current = document.querySelector('#monitoring');
    if (current?.dataset.resource === resource && current.dataset.range === range)
      current.querySelector('#monitor-status').textContent = 'Could not refresh monitoring: ' + error.message;
  } finally { pending.delete(key); }
}

function bindTooltips(element, resource, range) {
  const data = cache.get(resource + '/' + range)?.data;
  if (!data) return;
  for (const card of element.querySelectorAll('.monitor-chart')) {
    const series = data.series.find(s => s.metric === card.dataset.metric && s.id === card.dataset.series);
    const plot = card.querySelector('svg');
    const tooltip = card.querySelector('output');
    const show = at => {
      const point = series?.points.reduce((best,p) => !best || Math.abs(p[0]-at) < Math.abs(best[0]-at) ? p : best, null);
      if (!point || Math.abs(point[0]-at) > data.step * 1.5) { tooltip.hidden = true; return; }
      tooltip.textContent = `${date(point[0])} · ${format(point[1], series.metric === 'cpu')} · Min ${format(point[3], series.metric === 'cpu')} / Max ${format(point[4], series.metric === 'cpu')}`;
      tooltip.hidden = false;
    };
    plot.onpointermove = event => {
      const bounds = plot.getBoundingClientRect();
      show(data.start + Math.max(0, Math.min(1, ((event.clientX-bounds.left)/bounds.width*600-72)/502))*(data.end-data.start));
    };
    plot.onpointerleave = () => { tooltip.hidden = true; };
    let cursor = data.end;
    plot.onkeydown = event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      cursor = event.key === 'Home' ? data.start : event.key === 'End' ? data.end : Math.max(data.start, Math.min(data.end, cursor + (event.key === 'ArrowLeft' ? -1 : 1) * data.step));
      show(cursor);
    };
    plot.onfocus = () => show(cursor);
    plot.onblur = () => { tooltip.hidden = true; };
  }
}
