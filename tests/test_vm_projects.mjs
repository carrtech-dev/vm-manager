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
      vms: [vm("incus:first", "test", "running", ["web"]), vm("incus:loose", null),
        vm("libvirt:second", "test", "stopped"), vm("incus:alpha", "alpha")],
    } };
  const elements = { "#vm-table": { innerHTML: "" }, "#vm-result-count": {}, "#vm-select-all": {} };
  const listeners = {};
  let confirmation;
  const context = createContext({ ...core, state, selectOperation, can: () => role !== "viewer",
    document: { addEventListener(name, fn) { listeners[name] = fn; } },
    window: { addEventListener() {} }, setInterval() {}, bindCopyButtons() {},
    $: selector => elements[selector] || null, $$: () => [],
    modal: (title, html) => { confirmation = { title, html }; },
  });
  context.matchesOrganization = runInContext(`(${core.matchesOrganization.toString()})`, context);
  runInContext(source, context);
  const page = runInContext("({ updateVMTable, action, filteredVMs, vmTable, selectedVMs, syncVMSelection, bulkVMAction })", context);
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

test("viewers can expand folders without selection or power controls; other VM tables remain flat", async () => {
  const page = list("viewer");
  assert.match(page.html, /colspan="8"/);
  assert.doesNotMatch(page.html, /vm-select|data-action="start"|data-action="shutdown"/);
  await page.action("toggle-vm-project", "test");
  assert.match(page.html, /aria-expanded="false"/);
  assert.doesNotMatch(page.vmTable(page.filteredVMs(), true), /vm-project-folder|vm-project-member/);
});
