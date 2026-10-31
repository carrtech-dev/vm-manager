import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import * as core from "../app/static/core.js";
import { renderEmbeddedConsole } from "../app/static/console-view.js";
import { hostPicker } from "../app/static/cluster-ui.js";

// Run the actual page renderer and refresh path without browser startup/timers.
const source = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8")
  .replace(/^import[\s\S]*?from "[^"]+";\n/gm, "")
  .replace("\ninit();\n", "\n");

function consolePage(provider = "incus") {
  const vm = { id: `${provider}:test`, name: "test", provider, status: "running" };
  const inventory = { vms: [vm], projects: [], tags: [], warnings: [], host: { name: "host", address: "127.0.0.1" } };
  Object.assign(core.state, {
    user: { username: "operator", role: "operator" }, inventory, tasks: [],
    page: "vm", vm: vm.id, tab: "console", search: "", project: "all", tag: "all",
  });
  let draws = 0, html = "", nextInventory = inventory;
  const app = { set innerHTML(value) { html = value; draws++; } };
  const modal = { open: false };
  const document = { activeElement: { tagName: "IFRAME" }, addEventListener() {} };
  const context = createContext({
    ...core, renderEmbeddedConsole, hostPicker, document, window: { addEventListener() {} }, setInterval() {},
    bindCopyButtons() {}, refreshMonitoring() {},
    $: selector => ({ "#app": app, "#modal": modal })[selector] || null,
    api: async path => {
      if (path === '/cluster/hosts') return { local: { name: 'host' }, hosts: [] };
      assert.equal(path, "/inventory");
      return structuredClone(nextInventory);
    },
    toast: message => assert.fail(message),
  });
  runInContext(source, context);
  const page = runInContext("({render, refresh})", context);
  page.render();
  return {
    ...page, modal,
    get html() { return html; }, get draws() { return draws; },
    status(value) { nextInventory = { ...inventory, vms: value == null ? [] : [{ ...vm, status: value }] }; },
  };
}

for (const provider of ["incus", "libvirt"]) {
  test(`${provider} console survives polling and updates after stop/start`, async () => {
    const page = consolePage(provider);
    assert.match(page.html, /badge running/);
    assert.match(page.html, /<iframe/);
    if (provider === "incus") {
      assert.match(page.html, /data-mode="text"/);
      assert.match(page.html, /src="\/terminal\?vm=incus%3Atest&amp;embedded=1"/);
    } else {
      assert.match(page.html, /data-mode="graphical"/);
      assert.doesNotMatch(page.html, /data-console-frame="text"/);
    }
    await page.refresh();
    assert.equal(page.draws, 1, "unchanged running VM must retain its console");
    page.status("stopped");
    await page.refresh();
    assert.equal(page.draws, 2);
    assert.match(page.html, /badge stopped/);
    assert.match(page.html, /Start the VM to connect/);
    assert.match(page.html, /data-action="start"/);
    assert.doesNotMatch(page.html, /<iframe|badge running/);
    await page.refresh();
    assert.equal(page.draws, 2);
    page.status("running");
    await page.refresh();
    assert.equal(page.draws, 3);
    assert.match(page.html, /badge running/);
    assert.match(page.html, /<iframe/);
  });
}

test("a power change deferred by a modal is rendered after it closes", async () => {
  const page = consolePage();
  page.modal.open = true;
  page.status("stopped");
  await page.refresh();
  assert.equal(page.draws, 1);
  page.modal.open = false;
  await page.refresh();
  assert.equal(page.draws, 2, "compare with the rendered state, not the previous poll");
  assert.match(page.html, /badge stopped/);
});

test("a deleted VM removes its stale console and running badge", async () => {
  const page = consolePage();
  page.status(null);
  await page.refresh();
  assert.equal(page.draws, 2);
  assert.match(page.html, /Virtual machine unavailable/);
  assert.doesNotMatch(page.html, /<iframe|badge running/);
});
