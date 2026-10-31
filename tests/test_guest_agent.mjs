import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
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
  return { ...runInContext("({agentButton, installAgent, agentInstallCommand, agentSELinuxSetup})", context), nodes, listeners,
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
  const inner = agentInstallCommand.slice("sudo sh -c '".length, -1);
  assert.equal(spawnSync("sh", ["-n"], { input: inner, encoding: "utf8" }).status, 0);
  assert.match(agentInstallCommand, /systemctl restart incus-agent/);
  assert.ok(agentInstallCommand.length <= 4096);
  assert.doesNotMatch(agentInstallCommand, /setenforce|SELINUX=disabled|selinux=0/);
});

test("SELinux bootstrap installs missing tools and uses the canonical persistent context", (t) => {
  const { agentSELinuxSetup } = setup();
  for (const manager of ["dnf", "apt-get"]) {
    const directory = mkdtempSync(join(tmpdir(), "vmm-agent-tools-"));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    writeFileSync(join(directory, "getenforce"), "#!/bin/sh\necho Enforcing\n", { mode: 0o700 });
    writeFileSync(join(directory, manager), `#!/bin/sh
echo "packages: $*"
/bin/cat > "$VMM_TEST_BIN/semanage" <<'SH'
#!/bin/sh
echo "policy: $*"
SH
/bin/cat > "$VMM_TEST_BIN/restorecon" <<'SH'
#!/bin/sh
echo "label: $*"
SH
/bin/chmod +x "$VMM_TEST_BIN/semanage" "$VMM_TEST_BIN/restorecon"
`, { mode: 0o700 });
    const result = spawnSync("/bin/sh", ["-c", "set -eu; " + agentSELinuxSetup], {
      env: { PATH: directory, VMM_TEST_BIN: directory }, encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /packages: install -y policycoreutils policycoreutils-python-utils/);
    assert.match(result.stdout, /policy: fcontext -a -t bin_t \/run\/incus_agent\/incus-agent/);
  }
  const disabled = spawnSync("/bin/sh", ["-c", "set -eu; getenforce() { echo Disabled; }; " + agentSELinuxSetup], {
    env: { PATH: "" }, encoding: "utf8",
  });
  assert.equal(disabled.status, 0, disabled.stderr);
  assert.equal(disabled.stdout, "");
  const missing = spawnSync("/bin/sh", ["-c", "set -eu; getenforce() { echo Enforcing; }; " + agentSELinuxSetup], {
    env: { PATH: "" }, encoding: "utf8",
  });
  assert.equal(missing.status, 1);
  assert.match(missing.stderr, /Install semanage and restorecon/);
});

test("cached-agent recovery modifies an existing context and relabels before restarting without CD access", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "vmm-agent-label-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  writeFileSync(join(directory, "incus-agent"), "cached agent", { mode: 0o700 });
  const { agentInstallCommand } = setup();
  const body = agentInstallCommand.slice("sudo sh -c '".length, -1).replaceAll("/run/incus_agent", directory);
  const functions = `
    getenforce() { echo Enforcing; }
    semanage() { if [ "$2" = -a ]; then return 1; fi; echo "policy: $*"; }
    restorecon() { echo "label: $*"; }
    systemctl() { echo "service: $*" >&2; }
    mount() { echo "must not remount CD" >&2; return 77; }
  `;
  const result = spawnSync("/bin/sh", ["-c", functions + body], { env: { PATH: "" }, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /policy: fcontext -m -t bin_t/);
  assert.match(result.stdout, /label: -v/);
  assert.match(result.stderr, /service: reset-failed incus-agent/);
  assert.match(result.stderr, /service: restart incus-agent/);
  assert.doesNotMatch(result.stderr, /must not remount CD/);
});

test("agent check offers CD repair when its hardware ID is incompatible", async () => {
  const page = setup("operator", true);
  page.installAgent({ id: "incus:test", name: "test" });
  await page.nodes.get("#agent-check").onclick();
  assert.match(page.nodes.get("#agent-prepare-status").textContent, /CD needs repair/);
  assert.match(page.html, /Prepare optional installer CD/);
});
