import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";
import * as core from "../app/static/core.js";

const source = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8")
  .replace(/^import[\s\S]*?from "[^"]+";\n/gm, "")
  .replace("\ninit();\n", "\n");

function page(role = "operator") {
  const state = { ...core.state, user: { role }, search: "", inventory: {
    vms: [{ id: "incus:source", name: "source", status: "stopped", provider: "incus" }],
    storage_blocks: [{ id: "block", name: "Images", status: "ready", purpose: "backup", size_gib: 10 }],
    pools: [{ name: "pool", driver: "zfs" }],
    vm_templates: [
      { id: "ready", name: "Ubuntu <base>", description: "Configured & ready", vm_id: "incus:deleted-source", block_id: "block", status: "completed", created: 1, size: 1024 },
      { id: "failed", name: "Failed", description: "", vm_id: "libvirt:source", block_id: "block", status: "failed", created: 2, size: 0, message: "Disk <full>" },
      { id: "busy", name: "Capturing", description: "", vm_id: "incus:source", block_id: "block", status: "running", created: 3, size: 0 },
    ],
  } };
  let modal, confirmation;
  const requests = [];
  const context = createContext({ ...core, state, can: () => role !== "viewer",
    document: { addEventListener() {} }, window: { addEventListener() {} }, location: {}, setInterval() {}, bindCopyButtons() {},
    modal: (title, html, submit) => { modal = { title, html, submit }; },
    confirmAction: (...args) => { confirmation = args; },
    queue: (...args) => { requests.push(args); },
  });
  runInContext(source, context);
  return { ...runInContext("({ renderTemplates, captureTemplate, useTemplate, action })", context),
    state, requests, get modal() { return modal; }, get confirmation() { return confirmation; } };
}

test("template library escapes metadata and gates incomplete template actions", () => {
  const ui = page(), html = ui.renderTemplates();
  assert.match(html, /Ubuntu &lt;base&gt;/);
  assert.match(html, /Configured &amp; ready/);
  assert.match(html, /Disk &lt;full&gt;/);
  assert.match(html, /data-action="use-template" data-id="ready"\s*>/);
  assert.match(html, /data-action="use-template" data-id="failed" disabled/);
  assert.match(html, /data-action="delete-template" data-id="busy" disabled/);
  assert.doesNotMatch(page("viewer").renderTemplates(), /data-action="(?:use-template|delete-template|capture-template)"/);
  ui.state.search = "deleted-source";
  assert.match(ui.renderTemplates(), /Ubuntu &lt;base&gt;/);
  assert.doesNotMatch(ui.renderTemplates(), />Failed</);
});

test("capture form submits source, storage and explicit capture policy", async () => {
  const ui = page();
  ui.captureTemplate("incus:source");
  assert.match(ui.modal.html, /Require VM to be stopped/);
  assert.match(ui.modal.html, /Live capture/);
  const fields = new Map(Object.entries({ vm_id: "incus:source", name: "Base", description: "OS", block_id: "block", mode: "shutdown" }));
  await ui.modal.submit(fields);
  assert.equal(ui.requests[0][0], "/templates");
  assert.deepEqual(JSON.parse(JSON.stringify(ui.requests[0][2])), Object.fromEntries(fields));
});

test("creation uses selected template and deletion confirms template name", async () => {
  const ui = page();
  ui.useTemplate("ready");
  assert.match(ui.modal.html, /independent, stopped VM/);
  assert.match(ui.modal.html, /VM disk storage/);
  await ui.modal.submit(new Map([["name", "new-vm"], ["pool", "pool"]]));
  assert.deepEqual(JSON.parse(JSON.stringify(ui.requests[0])), ["/templates/ready/vms", "POST", { name: "new-vm", pool: "pool" }]);
  assert.throws(() => ui.useTemplate("failed"), /completed template/);
  await ui.action("delete-template", "ready");
  assert.equal(ui.confirmation[1], "Ubuntu <base>");
  await ui.confirmation[3]();
  assert.deepEqual(JSON.parse(JSON.stringify(ui.requests[1])), ["/templates/ready", "DELETE", { confirm_name: "Ubuntu <base>" }]);
});
