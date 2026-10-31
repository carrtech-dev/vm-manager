import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import { spawnSync } from "node:child_process";
import * as core from "../app/static/core.js";

const source = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8")
  .replace(/^import[\s\S]*?from "[^"]+";\n/gm, "")
  .replace("\ninit();\n", "\n");

function setup(role = "operator", needsRepair = false) {
  core.state.user = { role };
  const nodes = new Map();
  const listeners = new Map();
  const dialog = { open: true, addEventListener(event, fn) { listeners.set(event, fn); } };
  let html = "";
  const context = createContext({
    ...core, document: { addEventListener() {} }, window: { addEventListener() {} },
    bindCopyButtons() {},
    setInterval() {}, setTimeout() {}, clearTimeout() {},
    $: selector => { if (!nodes.has(selector)) nodes.set(selector, {}); return nodes.get(selector); },
    modal(title, content) { html = content; return dialog; },
    api: async path => path.includes("/tasks/")
      ? { status: "completed", message: "Installer CD ready" }
      : { message: "Agent connected", installer_attached: true, installer_needs_repair: needsRepair },
    queue: async () => ({ task_id: "task" }),
  });
  runInContext(source, context);
  return { ...runInContext("({agentButton, installAgent, agentInstallCommand})", context), nodes, listeners,
    get html() { return html; } };
}

test("agent installation is offered for stable Incus VMs to operators only", () => {
  const page = setup();
  for (const status of ["running", "stopped"])
    assert.match(page.agentButton({ provider: "incus", id: "incus:test", status }), /Install Incus agent/);
  assert.match(page.agentButton({ provider: "incus", status: "starting" }), /disabled/);
  assert.equal(page.agentButton({ provider: "libvirt" }), "");
  assert.equal(setup("viewer").agentButton({ provider: "incus" }), "");
});

test("guided install prepares media and reports the real agent check", async () => {
  const page = setup();
  page.installAgent({ id: "incus:test", name: "test" });
  assert.match(page.html, /First-time installation needs a command inside the VM/);
  assert.match(page.html, /data-copy=/);
  assert.match(page.html, /Open VM console/);
  await page.nodes.get("#agent-prepare").onclick();
  assert.equal(page.nodes.get("#agent-prepare-status").textContent, "Installer CD ready");
  await page.nodes.get("#agent-check").onclick();
  assert.equal(page.nodes.get("#agent-status").textContent, "Agent connected");
  assert.equal(page.nodes.get("#agent-check").disabled, false);
});

test("copied installer command is valid shell and avoids shared mount directories", () => {
  const { agentInstallCommand } = setup();
  const shell = spawnSync("sh", ["-n"], { input: agentInstallCommand, encoding: "utf8" });
  assert.equal(shell.status, 0, shell.stderr);
  assert.match(agentInstallCommand, /mktemp -d \/run\/incus-agent/);
  assert.match(agentInstallCommand, /systemctl start incus-agent/);
});

test("agent check offers CD repair when its hardware ID is incompatible", async () => {
  const page = setup("operator", true);
  page.installAgent({ id: "incus:test", name: "test" });
  await page.nodes.get("#agent-check").onclick();
  assert.match(page.nodes.get("#agent-prepare-status").textContent, /CD needs repair/);
  assert.match(page.html, /Prepare optional installer CD/);
});
