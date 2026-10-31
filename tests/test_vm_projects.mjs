import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import * as core from "../app/static/core.js";
import { selectOperation } from "../app/static/operations.js";

const source = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8")
  .replace(/^import[\s\S]*?from "[^"]+";\n/gm, "")
  .replace("\ninit();\n", "\n");

function list(role = "operator") {
  const vm = (id, project_id, status = "running", tag_ids = []) => ({
    id, name: id.split(":")[1], provider: id.split(":")[0], project_id, status,
    tag_ids, description: "", cpu: 2, memory: 1024 ** 3, devices: {}, addresses: [],
  });
  const state = { ...core.state, user: { role }, page: "vms", search: "", status: "all",
    provider: "all", project: "all", tag: "all", inventory: {
      projects: [{ id: "test", name: "Test <Project>" }, { id: "alpha", name: "Alpha" }, { id: "empty", name: "Empty" }],
      tags: [{ id: "web", name: "Web" }],
      host: { cpu_percent: 1, cpu_count: 4, memory_used: 1024 ** 3, memory_total: 4 * 1024 ** 3,
        memory_percent: 25, name: "host", address: "127.0.0.1", uptime: 60, load: [0, 0, 0], disk_free: 1024 ** 3 },
      pools: [], networks: [], isos: [], updated: 0,
      vms: [vm("incus:first", "test", "running", ["web"]), vm("incus:loose", null),
        vm("libvirt:second", "test", "stopped"), vm("incus:alpha", "alpha")],
    } };
  const elements = { "#overview-vm-table": { innerHTML: "" }, "#vm-table": { innerHTML: "" }, "#vm-result-count": {}, "#vm-select-all": {} };
  const listeners = {};
  let confirmation;
  const context = createContext({ ...core, state, selectOperation, can: () => role !== "viewer",
    document: { addEventListener(name, fn) { listeners[name] = fn; } },
    window: { addEventListener() {} }, setInterval() {}, bindCopyButtons() {}, monitoringPanel: () => "",
    $: selector => elements[selector] || null, $$: () => [],
    modal: (title, html) => { confirmation = { title, html }; },
  });
  context.matchesOrganization = runInContext(`(${core.matchesOrganization.toString()})`, context);
  runInContext(source, context);
  const page = runInContext("({ renderOverview, updateVMTable, action, filteredVMs, vmTable, selectedVMs, syncVMSelection, bulkVMAction })", context);
  page.updateVMTable();
  return { ...page, state, elements, get html() { return elements["#vm-table"].innerHTML; },
    get confirmation() { return confirmation; },
    selectAll() { listeners.change({ target: { id: "vm-select-all", checked: true, matches: () => false } }); },
  };
}

test("project folders keep cross-provider VMs together, escape names and put unassigned last", () => {
  const page = list();
  assert.equal((page.html.match(/class="vm-project-folder"/g) || []).length, 3);
  assert.match(page.html, /Test &lt;Project&gt;<\/span><span class="vm-project-count">2 VMs/);
  const positions = ["Alpha", "incus:alpha", "Test &lt;Project&gt;", "incus:first", "libvirt:second", "Unassigned", "incus:loose"].map(text => page.html.indexOf(text));
  assert.ok(positions.every((p, i) => p >= 0 && (!i || p > positions[i - 1])));
  assert.doesNotMatch(page.html, />Empty</);
});

test("collapsing clears hidden selections and select all and bulk confirmation use expanded rows", async () => {
  const page = list();
  page.selectAll();
  assert.equal(page.selectedVMs.size, 4);
  await page.action("toggle-vm-project", "test");
  assert.match(page.html, /data-id="test" aria-expanded="false"/);
  assert.doesNotMatch(page.html, /data-id="(?:incus:first|libvirt:second)"/);
  assert.equal(page.selectedVMs.size, 2);
  assert.equal(page.elements["#vm-select-all"].indeterminate, false);
  page.selectedVMs.clear();
  page.selectAll();
  assert.deepEqual([...page.selectedVMs].sort(), ["incus:alpha", "incus:loose"]);
  // Simulate stale selection too: the confirmation independently excludes collapsed rows.
  page.selectedVMs.add("incus:first");
  page.bulkVMAction("shutdown");
  assert.equal(page.confirmation.title, "Shutdown 2 virtual machines");
  assert.doesNotMatch(page.confirmation.html, /<td>(?:first|second)<\/td>/);
  page.updateVMTable();
  assert.equal(page.selectedVMs.size, 2);
  page.updateVMTable();
  assert.match(page.html, /data-id="test" aria-expanded="false"/);
  await page.action("toggle-vm-project", "test");
  assert.match(page.html, /data-id="libvirt:second"/);
  assert.equal(page.selectedVMs.size, 2);
  assert.equal(page.elements["#vm-select-all"].indeterminate, true);
});

test("project, tag, search, hypervisor and status filters determine folder members and counts", () => {
  const page = list();
  page.state.project = "test";
  page.state.provider = "libvirt";
  page.state.status = "stopped";
  page.state.search = "second";
  page.updateVMTable();
  assert.match(page.html, /vm-project-count">1 VM</);
  assert.match(page.html, /data-id="libvirt:second"/);
  assert.doesNotMatch(page.html, /Unassigned|data-id="incus:first"/);
  page.state.tag = "web";
  page.updateVMTable();
  assert.match(page.html, /No results/);
  assert.doesNotMatch(page.html, /vm-project-folder/);
});

test("unassigned folders collapse and select all is disabled when every folder is collapsed", async () => {
  const page = list();
  page.selectAll();
  for (const id of ["test", "alpha", ""]) await page.action("toggle-vm-project", id);
  assert.equal(page.selectedVMs.size, 0);
  assert.match(page.html, /id="vm-select-all"[^>]*disabled/);
  assert.doesNotMatch(page.html, /class="clickable/);
  page.selectAll();
  assert.equal(page.selectedVMs.size, 0);
});

test("viewers can expand folders without selection or power controls; ungrouped tables remain flat", async () => {
  const page = list("viewer");
  assert.match(page.html, /colspan="8"/);
  assert.doesNotMatch(page.html, /vm-select|data-action="start"|data-action="shutdown"/);
  await page.action("toggle-vm-project", "test");
  assert.match(page.html, /aria-expanded="false"/);
  assert.doesNotMatch(page.vmTable(page.filteredVMs(), true), /vm-project-folder|vm-project-member/);
});


test("overview groups every VM across providers and power states without truncating projects", () => {
  const page = list("viewer");
  page.state.page = "overview";
  const first = page.state.inventory.vms[0];
  for (let i = 0; i < 12; i++) page.state.inventory.vms.push({ ...first, id: `incus:extra-${i}`, name: `extra-${i}` });
  const html = page.renderOverview();
  assert.match(html, /id="overview-vm-table"/);
  assert.equal((html.match(/class="vm-project-folder"/g) || []).length, 3);
  assert.equal((html.match(/class="clickable vm-project-member"/g) || []).length, 16);
  assert.match(html, /Test &lt;Project&gt;<\/span><span class="vm-project-count">14 VMs/);
  assert.match(html, /colspan="5"/);
  const positions = ["Alpha", "incus:alpha", "Test &lt;Project&gt;", "incus:first", "libvirt:second", "incus:extra-11", "Unassigned", "incus:loose"].map(text => html.indexOf(text));
  assert.ok(positions.every((p, i) => p >= 0 && (!i || p > positions[i - 1])));
  assert.match(html, /Web/);
  assert.doesNotMatch(html, /vm-select|data-action="start"|data-action="shutdown"/);
});

test("overview folder toggles update only its table and persist through refreshes and navigation", async () => {
  const page = list();
  page.state.page = "overview";
  const mainTable = page.html;
  await page.action("toggle-vm-project", "test");
  const html = page.elements["#overview-vm-table"].innerHTML;
  assert.match(html, /data-id="test" aria-expanded="false"/);
  assert.doesNotMatch(html, /data-id="(?:incus:first|libvirt:second)"/);
  assert.equal(page.html, mainTable);
  assert.match(page.renderOverview(), /data-id="test" aria-expanded="false"/);
  page.state.page = "vms";
  page.updateVMTable();
  assert.match(page.html, /data-id="test" aria-expanded="false"/);
  await page.action("toggle-vm-project", "test");
  page.state.page = "overview";
  assert.match(page.renderOverview(), /data-id="libvirt:second"/);
  await page.action("toggle-vm-project", "");
  assert.doesNotMatch(page.elements["#overview-vm-table"].innerHTML, /data-id="incus:loose"/);
});

test("overview folder membership and counts follow project and tag filters", async () => {
  const page = list();
  page.state.page = "overview";
  page.state.project = "test";
  page.state.tag = "web";
  let html = page.renderOverview();
  assert.match(html, /vm-project-count">1 VM</);
  assert.match(html, /data-id="incus:first"/);
  assert.doesNotMatch(html, /Unassigned|data-id="libvirt:second"/);
  await page.action("toggle-vm-project", "test");
  await page.action("toggle-vm-project", "test");
  assert.match(page.elements["#overview-vm-table"].innerHTML, /vm-project-count">1 VM</);
  page.state.project = "alpha";
  html = page.renderOverview();
  assert.match(html, /No results/);
  assert.doesNotMatch(html, /vm-project-folder/);
});
