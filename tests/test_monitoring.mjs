import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chartPaths, ranges, monitoringPanel } from '../app/static/monitoring.js';

test('charts break across unavailable samples and collection downtime', () => {
  const paths = chartPaths([[10,10,100,5,15],[20,20,100,20,20],[30,null,100,null,null],[40,30,100,30,30],[100,40,100,40,40]], 0, 120, 100, 10);
  assert.equal((paths.line.match(/M/g)||[]).length, 3);
  assert.equal((paths.line.match(/L/g)||[]).length, 1);
  assert.ok(paths.peaks.includes('L'));
  assert.doesNotMatch(paths.line, /NaN|Infinity/);
});

test('all requested ranges and each disk appear, with escaped names', () => {
  assert.equal(ranges.length, 8);
  const html = monitoringPanel('incus:demo', ['root', 'disk1', '<bad>']);
  assert.match(html, /Last 12 Months/);
  assert.match(html, /Disk root/);
  assert.match(html, /Disk disk1/);
  assert.match(html, /Disk &lt;bad&gt;/);
  assert.doesNotMatch(html, /<bad>/);
  assert.equal((html.match(/class="monitor-chart"/g)||[]).length, 5);
});
