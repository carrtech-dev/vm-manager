import { state, esc, when, can, api, toast, modal, field, select, check, confirmAction, matchesOrganization } from "./core.js";

const selected = new Set();
const data = () => state.inventory?.patching || { guests: {}, schedules: [], runs: [] };
const button = (label, action, id = "", kind = "secondary") =>
  `<button type="button" class="button ${kind}" data-action="patch-${action}" data-id="${esc(id)}">${esc(label)}</button>`;
const table = (headers, rows) => `<div class="table-wrap"><table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.join("") : `<tr><td colspan="${headers.length}">No results</td></tr>`}</tbody></table></div>`;
const panel = (title, body) => `<section class="panel"><div class="panel-header"><h3>${esc(title)}</h3></div>${body}</section>`;
const visible = () => (state.inventory?.vms || []).filter(v => matchesOrganization(v) && (!state.search || v.name.toLowerCase().includes(state.search.toLowerCase())));
const vmName = id => state.inventory.vms.find(v => v.id === id)?.name || id;
const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export function clearOperationSelection() { selected.clear(); }

export function selectOperation(event) {
  if (event.target.matches(".patch-vm-select")) {
    if (event.target.checked) selected.add(event.target.value); else selected.delete(event.target.value);
  }
  if (event.target.id === "patch-select-all") {
    document.querySelectorAll(".patch-vm-select").forEach(input => {
      input.checked = event.target.checked;
      if (input.checked) selected.add(input.value); else selected.delete(input.value);
    });
  }
}

export function renderOperations() {
  const { guests, schedules, runs } = data();
  const vms = visible();
  const active = new Set(runs.filter(r => ["pending", "dispatched", "running", "reconnecting"].includes(r.status)).map(r => r.vm_id));
  const actions = can() ? button("Check selected", "check") + button("Patch selected now", "now", "", "primary") + button("Create schedule", "schedule") : "";
  return `<div class="page-title"><div><h1>Operations</h1><p>Linux patching · immediate updates and scheduled maintenance</p></div><div class="actions">${actions}</div></div>` +
    `<div class="hint">Select VMs and use Check selected to verify Linux, the guest agent, Python 3 and systemd. Supported guests: Debian 12+, Ubuntu 22.04+, Rocky Linux, AlmaLinux and RHEL 8+. Patching installs available package updates from each guest’s configured repositories. Updates may restart services. Stopped or unavailable VMs are skipped; expired maintenance windows are skipped. Windows guests cannot be patched.</div>` +
    panel("Linux patching", table([can() ? '<input type="checkbox" id="patch-select-all" aria-label="Select all visible VMs for Linux checks">' : "", "VM", "Power", "Linux readiness", "Last checked", "Patch status"], vms.map(v => {
      const ready = guests[v.id];
      return `<tr><td>${can() ? `<input type="checkbox" class="patch-vm-select" value="${esc(v.id)}" aria-label="Select ${esc(v.name)}" ${selected.has(v.id) ? "checked" : ""}>` : ""}</td><td><a href="#vm/${encodeURIComponent(v.id)}/summary">${esc(v.name)}</a><small>${esc(v.provider)}</small></td><td>${esc(v.status)}</td><td>${ready ? (ready.ready ? esc(ready.os) + " · Ready" : esc(ready.message || "Unavailable")) : "Not checked"}</td><td>${ready ? when(ready.checked) : "—"}</td><td>${active.has(v.id) ? "Active patch operation" : "—"}</td></tr>`;
    }))) +
    panel("Patch schedules", table(["Name", "VMs", "When", "Window", "Reboot", "Next run", "Last outcome", "Actions"], schedules.map(s =>
      `<tr><td>${esc(s.name)}<small>${s.enabled ? "Enabled" : "Disabled"}</small></td><td>${s.vm_ids.map(id => esc(vmName(id))).join(", ")}</td><td>${s.frequency === "weekly" ? weekdays[s.weekday] : "Daily"} ${esc(s.time)}<small>${esc(s.timezone)}</small></td><td>${s.window_minutes} min</td><td>${s.reboot === "never" ? "Report required" : "When required"}</td><td>${s.next_run ? when(s.next_run) : "—"}</td><td>${esc(s.last_message || "—")}</td><td><div class="actions">${can() ? button("Edit", "schedule", s.id) + button("Delete", "delete-schedule", s.id) : "Read only"}</div></td></tr>`))) +
    panel("Patch runs", table(["VM", "Started", "Status", "Result", "Reboot", "Actions"], runs.map(r =>
      `<tr><td>${esc(vmName(r.vm_id))}</td><td>${when(r.created)}<small>${esc(r.username)}</small></td><td>${esc(r.status)}</td><td>${esc(r.result.message || "Waiting for a worker and this VM")}${r.result.changed_count != null ? `<small>${r.result.changed_count} packages updated</small>` : ""}</td><td>${r.result.rebooted ? "Reboot verified" : r.result.reboot_required === true ? "Required" : r.result.reboot_required === false ? "Not required" : "Unknown"}</td><td><div class="actions">${button("Details", "details", r.id)}${can() && r.status === "pending" ? button("Cancel pending batch", "cancel", r.batch_id) : ""}</div></td></tr>`)));
}

function selection() {
  const existing = new Set(state.inventory.vms.map(v => v.id));
  const ids = [...selected].filter(id => existing.has(id));
  if (!ids.length) throw new Error("Select at least one VM first");
  return ids;
}

function policyFields(value = {}) {
  return select("Reboot policy", "reboot", [["never", "Report reboot required; do not reboot"], ["if-required", "Reboot automatically when required"]], value.reboot || "never") +
    field("VMs patched at once", "concurrency", value.concurrency || 1, {type: "number", min: 1, max: 4}) +
    check("Stop remaining VMs after a failure", "stop_on_failure", value.stop_on_failure !== false);
}

function policy(form) {
  return {reboot: form.get("reboot"), concurrency: +form.get("concurrency"), stop_on_failure: form.has("stop_on_failure")};
}

export function patchForm(scheduleId, refresh) {
  const current = data().schedules.find(s => s.id === scheduleId);
  const ids = current?.vm_ids || selection();
  const ready = data().guests;
  if (!current && ids.some(id => !ready[id]?.ready)) throw new Error("Check Linux readiness for every selected VM first");
  const schedule = scheduleId !== null;
  const picker = schedule ? `<fieldset class="tag-picker"><legend>Linux VMs in this schedule</legend>${state.inventory.vms.filter(v => ready[v.id]?.ready || ids.includes(v.id)).map(v =>
    `<label class="check"><input type="checkbox" name="vm_ids" value="${esc(v.id)}" ${ids.includes(v.id) ? "checked" : ""}><span>${esc(v.name)}</span></label>`).join("")}</fieldset>` : `<p>${ids.map(id => esc(vmName(id))).join(", ")}</p>`;
  modal(schedule ? (current ? "Edit patch schedule" : "Create patch schedule") : "Patch Linux VMs now",
    `<div class="warning">Package updates can restart services. Automatic reboot requires the reboot policy below. Guest repositories supply updates; distribution release upgrades are outside this operation.</div>` + picker +
    `<div class="form-grid">${schedule ? field("Schedule name", "name", current?.name || "Linux maintenance") +
      select("Frequency", "frequency", [["weekly", "Weekly"], ["daily", "Daily"]], current?.frequency || "weekly") +
      select("Weekday (weekly schedules)", "weekday", weekdays.map((d, i) => [String(i), d]), String(current?.weekday ?? 6)) +
      field("Start time", "time", current?.time || "02:00", {type: "time"}) +
      field("Timezone", "timezone", current?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone) +
      field("Maintenance window (minutes)", "window_minutes", current?.window_minutes || 120, {type: "number", min: 15, max: 1440}) : ""}${policyFields(current)}${schedule ? check("Schedule enabled", "enabled", current?.enabled !== false) : ""}</div>` +
    `<p class="hint">A patch transaction that starts within the window is allowed to finish. Automatic reboot may occur after the window. Reboot need can be unknown on some RPM guests; those guests require manual review.</p>`,
    async form => {
      const body = {...policy(form), vm_ids: schedule ? form.getAll("vm_ids") : ids};
      if (!body.vm_ids.length) throw new Error("Select at least one Linux VM");
      if (schedule) Object.assign(body, {name: form.get("name"), frequency: form.get("frequency"), weekday: +form.get("weekday"), time: form.get("time"), timezone: form.get("timezone"), window_minutes: +form.get("window_minutes"), enabled: form.has("enabled")});
      await api("/operations/patching/" + (schedule ? "schedules" + (current ? "/" + current.id : "") : "runs"), current ? "PUT" : "POST", body);
      toast(schedule ? "Patch schedule saved" : "Patch batch queued");
      await refresh(true);
    }, {submit: schedule ? "Save schedule" : "Patch selected VMs", wide: true});
}

export async function operationAction(action, id, refresh) {
  if (action === "patch-details") {
    const run = await api("/operations/patching/runs/" + id);
    modal("Patch result · " + vmName(run.vm_id), `<p>${esc(run.status)} · ${esc(run.result.message || "Waiting")}</p>` +
      `<p>${run.result.rebooted ? "Reboot verified" : run.result.reboot_required === true ? "Reboot required" : run.result.reboot_required === false ? "Reboot not required" : "Reboot requirement unknown"}</p>` +
      `<details><summary>Updated packages (${run.result.changed_count ?? 0})</summary><pre>${esc((run.result.packages || []).join("\n"))}</pre></details>` +
      `<details open><summary>Latest package output</summary><pre class="task-output-text">${esc(run.result.output || "Output becomes available as the guest transaction progresses.")}</pre></details>`, async () => {}, {submit: "Close", wide: true});
    return;
  }
  if (!can()) throw new Error("Your role does not permit this action");
  if (action === "patch-check") {
    await api("/operations/patching/check", "POST", {vm_ids: selection()});
    toast("Linux readiness checks queued");
    window.dispatchEvent(new CustomEvent("refresh-tasks"));
  } else if (action === "patch-now") patchForm(null, refresh);
  else if (action === "patch-schedule") patchForm(id || "", refresh);
  else if (action === "patch-delete-schedule") {
    const schedule = data().schedules.find(s => s.id === id);
    confirmAction("Delete patch schedule", schedule.name, "Remove future runs of this schedule. Already queued and running patches continue.", async () => {
      await api("/operations/patching/schedules/" + id, "DELETE"); await refresh(true);
    });
  } else if (action === "patch-cancel") {
    await api("/operations/patching/batches/" + id + "/cancel", "POST", {});
    toast("Pending patches cancelled; dispatched and running transactions continue");
    await refresh(true);
  }
}
