import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import * as core from "../app/static/core.js";

const source = readFileSync(new URL("../app/static/operations.js", import.meta.url), "utf8")
  .replace(/^import .*;\n/gm, "").replace(/^export /gm, "");

function ui(role = "operator") {
  const state = {...core.state, user: {role}, inventory: {vms: [
    {id: "incus:linux", name: "Linux <web>", provider: "incus", status: "running"},
    {id: "libvirt:windows", name: "Windows", provider: "libvirt", status: "running"}], patching: {
    guests: {"incus:linux": {ready: true, os: "Ubuntu 24.04", checked: 1}, "libvirt:windows": {ready: false, message: "Linux agent unavailable"}},
    schedules: [{id: "s", name: "Weekend <patch>", vm_ids: ["incus:linux"], enabled: true, frequency: "weekly", weekday: 6, time: "02:00", timezone: "Europe/London", window_minutes: 120, reboot: "never"}],
    runs: [{id: "r", vm_id: "incus:linux", status: "completed", created: 1, username: "operator", result: {message: "Updated <2>", changed_count: 2, packages: ["curl"], output: "<script>alert(1)</script>", reboot_required: true}}],
  }}};
  state.inventory.patching.latest_runs = {"incus:linux": state.inventory.patching.runs[0]};
  let dialog;
  const requests = [];
  const context = createContext({...core, state, Intl, can: () => role !== "viewer",
    api: async (...args) => { requests.push(args); return args[0] === "/operations/patching/runs/r" ? state.inventory.patching.runs[0] : {}; }, toast() {},
    modal: (title, html, submit) => { dialog = {title, html, submit}; },
  });
  context.matchesOrganization = runInContext(`(${core.matchesOrganization.toString()})`, context);
  runInContext(source, context);
  const methods = runInContext("({renderOperations, selectOperation, patchForm, operationAction})", context);
  return {...methods, state, requests, get dialog() {return dialog;},
    select(id) {methods.selectOperation({target: {id: "", value: id, checked: true, matches: () => true}});}};
}

test("operations shows readiness, schedules and per-VM results with escaped metadata", () => {
  const page = ui();
  const html = page.renderOperations();
  assert.match(html, /Linux &lt;web&gt;/);
  assert.match(html, /Weekend &lt;patch&gt;/);
  assert.match(html, /Updated &lt;2&gt;/);
  assert.match(html, /Reboot|Required/);
  assert.match(html, /Linux agent unavailable/);
  assert.doesNotMatch(ui("viewer").renderOperations(), /data-action="patch-(?:now|schedule|check|delete-schedule)"/);
});

const linuxTable = page => page.renderOperations().split("<h3>Linux patching</h3>")[1].split("</section>")[0];

test("VM patch status keeps the latest completed result after history rolls off", () => {
  const page = ui("viewer");
  const run = page.state.inventory.patching.latest_runs["incus:linux"];
  run.result.rebooted = true;
  page.state.inventory.patching.runs = [];
  const html = linuxTable(page);
  assert.match(html, /Completed/);
  assert.match(html, /Started /);
  assert.match(html, /Updated &lt;2&gt;/);
  assert.match(html, /2 packages updated/);
  assert.match(html, /Reboot verified/);
  assert.match(html, /data-action="patch-details" data-id="r"/);
  assert.match(html, /No patch runs/);
  assert.doesNotMatch(html, /<script>/);
});

test("VM patch status follows queued, active and unsuccessful outcomes", () => {
  const page = ui();
  const run = page.state.inventory.patching.latest_runs["incus:linux"];
  for (const [status, label] of Object.entries({pending: "Queued", dispatched: "Starting", running: "Running", reconnecting: "Reconnecting", failed: "Failed", interrupted: "Interrupted", skipped: "Skipped"})) {
    run.status = status;
    run.result = {message: "Repository <unavailable>"};
    const html = linuxTable(page);
    assert.match(html, new RegExp(label));
    assert.match(html, /Repository &lt;unavailable&gt;/);
    assert.match(html, /Reboot requirement unknown/);
  }
  run.status = "completed";
  run.result = {changed_count: 0, reboot_required: false};
  assert.match(linuxTable(page), /0 packages updated/);
  assert.match(linuxTable(page), /Reboot not required/);
  run.result.reboot_required = true;
  assert.match(linuxTable(page), /Reboot required/);
});

test("patch now requires checked Linux and submits exact batch policies", async () => {
  const page = ui();
  assert.throws(() => page.patchForm(null), /Select at least/);
  page.select("incus:linux");
  page.patchForm(null, async () => {});
  assert.match(page.dialog.html, /do not reboot/);
  await page.dialog.submit(new Map([["reboot", "if-required"], ["concurrency", "2"], ["stop_on_failure", "on"]]));
  assert.deepEqual(JSON.parse(JSON.stringify(page.requests[0])), ["/operations/patching/runs", "POST", {
    vm_ids: ["incus:linux"], reboot: "if-required", concurrency: 2, stop_on_failure: true}]);
  page.select("libvirt:windows");
  assert.throws(() => page.patchForm(null), /Check Linux readiness/);
});

test("schedule editor preserves membership and submits timezone and maintenance window", async () => {
  const page = ui();
  page.patchForm("s", async () => {});
  assert.match(page.dialog.html, /value="incus:linux" checked/);
  const fields = new Map(Object.entries({name: "Weekly", frequency: "weekly", weekday: "0", time: "03:00", timezone: "Europe/London", window_minutes: "90", reboot: "never", concurrency: "1", enabled: "on"}));
  fields.getAll = () => ["incus:linux"];
  await page.dialog.submit(fields);
  assert.equal(page.requests[0][0], "/operations/patching/schedules/s");
  assert.equal(page.requests[0][1], "PUT");
  assert.equal(page.requests[0][2].window_minutes, 90);
  assert.equal(page.requests[0][2].timezone, "Europe/London");
});

test("result details escape package output and viewer cannot initiate patching", async () => {
  const page = ui("viewer");
  await page.operationAction("patch-details", "r");
  assert.match(page.dialog.html, /&lt;script&gt;/);
  assert.doesNotMatch(page.dialog.html, /<script>/);
  await assert.rejects(page.operationAction("patch-now", ""), /role does not permit/);
});
