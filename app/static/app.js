import {
  state,
  copyValue,
  copyAddresses,
  bindCopyButtons,
  $,
  $$,
  esc,
  size,
  when,
  duration,
  can,
  api,
  toast,
  modal,
  field,
  select,
  check,
  textarea,
  confirmAction,
  queue,
  matchesOrganization,
} from "./core.js";
import { watchTaskDetail } from "./task-detail.js";
import { renderEmbeddedConsole, switchEmbeddedConsole, toggleConsoleFullscreen, bindEmbeddedConsoleEvents } from "./console-view.js";
import { monitoringPanel, refreshMonitoring } from "./monitoring.js";
const pageNames = {
  overview: "Overview",
  vms: "Virtual machines",
  networks: "Networks",
  organization: "Projects & tags",
  resources: "Resource view",
  isos: "ISO library",
  storage: "Storage",
  backups: "Backups",
  templates: "Templates",
  users: "Access control",
  tasks: "Task history",
  audit: "Audit trail",
  account: "Account",
  vm: "Virtual machine",
};
const navItems = [
  ["overview", "▦", "Overview"],
  ["vms", "▣", "Virtual machines"],
  ["networks", "◇", "Networks"],
  ["organization", "◈", "Projects & tags"],
  ["isos", "◉", "ISO library"],
  ["storage", "▤", "Storage"],
  ["backups", "↺", "Backups"],
  ["templates", "▧", "Templates"],
  ["users", "♧", "Access control"],
];
let busy = false,
  taskBusy = false,
  selectedNetwork = null,
  renderedPath = "";
const selectedVMs = new Set();
const collapsedVMProjects = new Set();
let hostTreeCollapsed = false;
const seenTasks = new Map();
let vmMetrics = null, metricsBusy = false;
let vmSnapshots = null, snapshotsLoading = null;
let renderedVMStatus = null;
const badge = (text, style = text) =>
  `<span class="badge ${esc(style)}"><span aria-hidden="true">●</span>${esc(text)}</span>`;
const btn = (
  label,
  action,
  id = "",
  kind = "secondary",
  disabled = false,
  extra = "",
) =>
  `<button type="button" class="button ${kind}" data-action="${action}" data-id="${esc(id)}" ${disabled ? "disabled" : ""} ${extra}>${label}</button>`;
const empty = (title, description, button = "") =>
  `<div class="empty"><strong>${esc(title)}</strong>${esc(description)}${button ? `<div>${button}</div>` : ""}</div>`;
const table = (headers, rows, options = "") =>
  `<div class="table-wrap"><table ${options}><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.join("") : `<tr><td colspan="${headers.length}">${empty("No results", "Nothing matches this view.")}</td></tr>`}</tbody></table></div>`;
const panel = (title, body, aside = "") =>
  `<section class="panel"><div class="panel-header"><h3>${esc(title)}</h3>${aside}</div>${body}</section>`;
const properties = (entries) =>
  `<dl class="properties">${entries.map(([k, v]) => `<div class="property"><dt>${esc(k)}</dt><dd>${v}</dd></div>`).join("")}</dl>`;
const title = (name, subtitle, actions = "") =>
  `<div class="page-title"><div><h1>${name}</h1>${subtitle ? `<p>${subtitle}</p>` : ""}</div><div class="actions">${actions}</div></div>`;
const networkMode = (n) =>
  n.config["ipv4.nat"] === "true"
    ? "NAT"
    : n.config["ipv4.routing"] === "false"
      ? "Isolated"
      : "Routed";
const storageBlocks = () => state.inventory.storage_blocks || [];
const externalSources = () => state.inventory.external_storage?.sources || [];
const externalMounts = (vmId) => (state.inventory.external_storage?.mounts || []).filter(m => !vmId || m.vm_id === vmId);
const externalSource = id => externalSources().find(s => s.id === id);
const externalLocation = s => s.kind === "smb" ? `//${s.server}/${s.share}${s.prefix ? "/" + s.prefix : ""}` : `${s.endpoint}/${s.bucket}${s.prefix ? "/" + s.prefix : ""}`;

const storagePurposes = [["vm", "VM disks (Incus)"], ["iso", "ISO images"], ["backup", "VM & configuration backups"]];
const storageOptions = (purpose) => [
  ["", purpose === "vm" ? "Existing pool selection" : "Original application storage"],
  ...storageBlocks().filter(b => b.purpose === purpose && b.status === "ready")
    .map(b => [b.id, `${b.name} · ${b.size_gib} GiB`]),
];
const poolOptions = () =>
  state.inventory.pools.filter(p => !p.name.startsWith("vmm-block-") || storageBlocks().some(b => b.pool === p.name && b.status === "ready"))
    .map((p) => [p.name, `${storageBlocks().find(b => b.pool === p.name)?.name || p.name} · ${p.driver}`]);
const defaultVMPool = () => storageBlocks().find(b => b.id === state.inventory.storage_defaults?.vm && b.status === "ready")?.pool || (poolOptions().some(p => p[0] === "hdd-pool") ? "hdd-pool" : poolOptions()[0]?.[0]);
const networkOptions = () =>
  state.inventory.networks
    .filter((n) => n.provider === "incus")
    .map((n) => [
      n.name,
      `${n.name} · ${networkMode(n)} · ${n.config["ipv4.address"] || "IPv4 disabled"}`,
    ]);
const isoOptions = (vm = null) => [
  ["", "No media / boot from disk"],
  ...state.inventory.isos
    .filter((i) => !vm || vm.provider === "incus" || i.source === "upload")
    .map((i) => [i.id, `${i.name}${i.pool ? " · " + i.pool : ""}`]),
];
const ref = (id) => "/vms/" + encodeURIComponent(id);
const currentVM = (id) =>
  state.inventory?.vms.find((v) => v.id === (id || state.vm));
const resourceName = (id) => /^(incus|libvirt):[a-zA-Z][\w-]{0,62}$/.test(id)
  ? copyValue(id.split(":")[1], "hostname", esc(id)) : esc(id);
const smallAction = (label, action, id, disabled = false) =>
  btn(label, action, id, "small link", disabled);
function normalizeOrganizationFilters() {
  if (
    ![
      "all",
      "unassigned",
      ...state.inventory.projects.map((p) => p.id),
    ].includes(state.project)
  )
    state.project = "all";
  if (
    !["all", "untagged", ...state.inventory.tags.map((t) => t.id)].includes(
      state.tag,
    )
  )
    state.tag = "all";
}
function renderOrganizationFilters() {
  const inv = state.inventory;
  return `<div class="organization-filters" aria-label="Resource filters">${select("Project", "project-filter", [["all", "All projects"], ["unassigned", "Unassigned resources"], ...inv.projects.map((p) => [p.id, p.name])], state.project)}${select("Tag", "tag-filter", [["all", "All tags"], ["untagged", "Untagged resources"], ...inv.tags.map((t) => [t.id, t.name])], state.tag)}${state.project !== "all" || state.tag !== "all" ? btn("Clear filters", "clear-filters") : ""}<small>Filters apply to VM and network lists and the resource tree.</small></div>`;
}
function organizationChips(resource, showProject = true) {
  const project = showProject && state.inventory.projects.find(
    (p) => p.id === resource.project_id,
  );
  const tags = state.inventory.tags.filter((t) =>
    resource.tag_ids?.includes(t.id),
  );
  if (!project && !tags.length) return "";
  return `<div class="organization-chips">${project ? `<span class="resource-project" title="Project">◈ ${esc(project.name)}</span>` : ""}${tags.map((t) => `<span class="resource-tag" title="Tag">${esc(t.name)}</span>`).join("")}</div>`;
}
function renderOrganization() {
  const section = (kind, singular) =>
    panel(
      kind === "projects" ? "Projects" : "Tags",
      table(
        ["Name", "Virtual machines", "Networks", "Actions"],
        state.inventory[kind].map((item) => {
          const belongs = (r) =>
            kind === "projects"
              ? r.project_id === item.id
              : r.tag_ids?.includes(item.id);
          return `<tr><td class="name">${esc(item.name)}</td><td>${state.inventory.vms.filter(belongs).length}</td><td>${state.inventory.networks.filter(belongs).length}</td><td><div class="actions">${smallAction("View resources", "view-" + singular, item.id)}${can() ? smallAction("Rename", "rename-" + singular, item.id) + smallAction("Delete", "delete-" + singular, item.id) : ""}</div></td></tr>`;
        }),
      ),
      can()
        ? btn("+ Create " + singular, "create-" + singular, "", "primary")
        : "",
    );
  return (
    title(
      "Projects & tags",
      "Group resources by project and label them with multiple tags. Counts cover all resources.",
    ) +
    `<div class="hint">Each VM or network can belong to one project and have multiple tags. Assign them from a VM’s summary or a network’s Organize action. Projects organize this manager’s inventory; they do not change network connectivity or access permissions.</div>` +
    section("projects", "project") +
    section("tags", "tag")
  );
}
function renderResources() {
  const vms = state.inventory.vms.filter(matchesOrganization);
  const networks = state.inventory.networks.filter(matchesOrganization);
  const project = state.inventory.projects.find((p) => p.id === state.project);
  const tag = state.inventory.tags.find((t) => t.id === state.tag);
  return (
    title(
      esc(project?.name || tag?.name || "Resource view"),
      `${vms.length} virtual machines · ${networks.length} networks`,
    ) +
    panel("Virtual machines", vmTable(vms)) +
    panel(
      "Networks",
      table(
        ["Name", "Provider", "Project & tags", "Actions"],
        networks.map(
          (n) =>
            `<tr><td class="name">${esc(n.name)}</td><td>${badge(n.provider)}</td><td>${organizationChips(n) || "Unassigned · No tags"}</td><td><div class="actions">${smallAction("Details", "network-detail", n.id)}${can() ? smallAction("Organize", "organize-network", n.id) : ""}</div></td></tr>`,
        ),
      ),
    )
  );
}
async function updateOrganization() {
  state.inventory = await api("/inventory");
  normalizeOrganizationFilters();
  render();
}
function collectionForm(kind, id = "") {
  const item = state.inventory[kind].find((c) => c.id === id);
  const singular = kind === "projects" ? "project" : "tag";
  const dialog = modal(
    (item ? "Rename " : "Create ") + singular,
    field("Name", "name", item?.name || "", {
      hint: "Up to 64 characters. Names must be unique, ignoring case.",
    }),
    async (f) => {
      await api(
        "/collections/" + kind + (item ? "/" + item.id : ""),
        item ? "PUT" : "POST",
        { name: f.get("name") },
      );
      await updateOrganization();
      toast(singular === "project" ? "Project saved" : "Tag saved");
    },
    { submit: item ? "Save name" : "Create " + singular },
  );
  $("input[name=name]", dialog).maxLength = 64;
}
function deleteCollection(kind, id) {
  const item = state.inventory[kind].find((c) => c.id === id);
  if (!item) return;
  const singular = kind === "projects" ? "project" : "tag";
  confirmAction(
    "Delete " + singular,
    item.name,
    `This removes the ${singular} and its assignments. VMs, networks and their data are retained.`,
    async () => {
      await api("/collections/" + kind + "/" + id, "DELETE");
      await updateOrganization();
      toast(singular === "project" ? "Project deleted" : "Tag deleted");
    },
  );
}
function organizeResource(kind, resource) {
  if (!resource) return;
  modal(
    "Project & tags · " + resource.name,
    select(
      "Project",
      "project_id",
      [
        ["", "Unassigned"],
        ...state.inventory.projects.map((p) => [p.id, p.name]),
      ],
      resource.project_id || "",
    ) +
      `<fieldset class="tag-picker"><legend>Tags</legend>${state.inventory.tags.length ? state.inventory.tags.map((t) => check(t.name, "tag_ids", resource.tag_ids?.includes(t.id)).replace('name="tag_ids"', `name="tag_ids" value="${esc(t.id)}"`)).join("") : '<p class="muted">Create tags in Projects & tags first.</p>'}</fieldset><p class="hint">Assignments can be changed while VMs are running. Create projects and tags in Projects & tags.</p>`,
    async (f) => {
      await api(
        "/organization/" + kind + "/" + encodeURIComponent(resource.id),
        "PUT",
        {
          project_id: f.get("project_id") || null,
          tag_ids: f.getAll("tag_ids"),
        },
      );
      await updateOrganization();
      toast("Project and tags saved");
    },
  );
}
function navigate() {
  const parts = location.hash.slice(1).split("/");
  state.page = parts[0] || "overview";
  if (state.page === "vm") {
    state.vm = decodeURIComponent(parts[1] || "");
    state.tab = parts[2] || "summary";
  }
  if (state.user) {
    render();
    refreshVMMetrics();
  }
}
function renderLogin(message = "") {
  $("#app").innerHTML =
    `<main class="login-screen"><form class="login-card" id="login-form"><div class="login-brand">▣ CARRTECH</div><span class="product">VM MANAGER</span><h1>Sign in to your infrastructure</h1><p class="host-address">${copyValue("ctlnxdbn001")} · ${copyAddresses("192.168.8.235")}</p>${field("Username", "username", "", { placeholder: "Your username" })}${field("Password", "password", "", { type: "password", placeholder: "Your password" })}<div class="form-error" role="alert" ${message ? "" : "hidden"}>${esc(message)}</div><button class="button primary" type="submit">Sign in</button><p class="login-foot">Local accounts · Session protected</p></form></main>`;
  $("input[name=username]").autocomplete = "username";
  $("input[name=password]").autocomplete = "current-password";
  $("#login-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const button = $("button[type=submit]");
    button.disabled = true;
    button.textContent = "Signing in…";
    try {
      state.user = await api("/login", "POST", Object.fromEntries(form));
      state.page = "overview";
      location.hash = "#overview";
      await refresh(true);
    } catch (e) {
      const error = $(".form-error");
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      if (button.isConnected) {
        button.disabled = false;
        button.textContent = "Sign in";
      }
    }
  };
}
function render() {
  if (!state.user) {
    renderLogin();
    return;
  }
  if (!state.inventory) {
    $("#app").innerHTML =
      '<div class="initial-loading">Loading host resources…</div>';
    return;
  }
  const treeScroll = $("#resource-tree")?.scrollTop || 0;
  const viewKey = [state.page, state.vm, state.tab].join("/");
  const contentScroll =
    renderedPath === viewKey ? $("#main-content")?.scrollTop || 0 : 0;
  renderedPath = viewKey;
  const inv = state.inventory;
  const active = state.page === "vm" ? "vms" : state.page;
  const vm = currentVM();
  $("#app").innerHTML =
    `<div class="app-shell"><aside class="sidebar" aria-label="Resource navigation"><a class="brand" href="#overview"><strong>▣ CARRTECH</strong><small>VM MANAGER</small></a><div class="resource-search"><input id="resource-search" aria-label="Search resources" placeholder="⌕  Search resources" value="${esc(state.search)}"></div><div class="nav-heading">WORKSPACE</div><nav>${navItems
      .filter((n) => n[0] !== "users" || can("admin"))
      .map(
        ([key, icon, label]) =>
          `<a class="nav-link ${active === key ? "active" : ""}" href="#${key}"><span class="nav-symbol" aria-hidden="true">${icon}</span>${label}${key === "vms" ? `<span class="nav-count">${inv.vms.filter(matchesOrganization).length}</span>` : ""}</a>`,
      )
      .join(
        "",
      )}</nav><div class="nav-heading">RESOURCE TREE</div><div class="resource-tree" id="resource-tree">${renderTree()}</div><div class="host-online"><strong>● HOST CONNECTED</strong><small>${copyAddresses(inv.host.address)}</small></div></aside><section class="workspace"><header class="topbar"><button class="icon-button mobile-menu" data-action="toggle-sidebar" aria-label="Toggle navigation">☰</button><div class="breadcrumb"><a href="#overview">Datacenter</a><span>/</span><span>${copyValue(inv.host.name)}</span><span>/</span><strong>${state.page === "vm" && vm ? copyValue(vm.name) : esc(pageNames[state.page] || "Overview")}</strong></div>${can() ? btn("+ Create VM", "create-vm", "", "primary") : ""}<button class="icon-button" data-action="help" aria-label="Help">?</button><button class="account-button" data-action="account" aria-label="Your account"><span class="avatar">${esc(state.user.username.slice(0, 2).toUpperCase())}</span><span class="account-name">${esc(state.user.username)}</span><span class="role-label">${esc(state.user.role)}</span></button></header><main class="content" id="main-content">${inv.warnings.length ? `<div class="error-banner"><strong>Some host information is unavailable.</strong><br>${inv.warnings.map(esc).join("<br>")}</div>` : ""}${renderOrganizationFilters()}${renderPage()}</main><section class="task-dock" aria-label="Recent tasks" id="task-dock">${renderDock()}</section></section></div>`;
  bindPage();
  refreshMonitoring();
  if ($("#resource-tree")) $("#resource-tree").scrollTop = treeScroll;
  if ($("#main-content")) $("#main-content").scrollTop = contentScroll;
}
function renderTree() {
  return `<div class="tree-host"><button type="button" class="tree-host-toggle" data-action="toggle-host-tree" aria-expanded="${!hostTreeCollapsed}" aria-controls="host-tree-resources" aria-label="${hostTreeCollapsed ? "Expand" : "Collapse"} ${esc(state.inventory.host.name)} resources"><span aria-hidden="true">${hostTreeCollapsed ? "▸" : "▾"}</span></button>${copyValue(state.inventory.host.name, "hostname", `<a href="#overview">${esc(state.inventory.host.name)}</a>`)}</div><div id="host-tree-resources"${hostTreeCollapsed ? " hidden" : ""}>${[
    "incus",
    "libvirt",
  ]
    .map((provider) => {
      const vms = state.inventory.vms.filter(
        (v) =>
          v.provider === provider &&
          matchesOrganization(v) &&
          (!state.search ||
            v.name.toLowerCase().includes(state.search.toLowerCase())),
      );
      return `<details class="tree-group" open><summary>${provider === "incus" ? "Incus" : "libvirt"} <span class="subtle">(${vms.length})</span></summary>${vms.map((v) => `<div class="tree-vm ${state.page === "vm" && state.vm === v.id ? "active" : ""}"><span class="dot ${v.status}"></span>${copyValue(v.name, "hostname", `<a href="#vm/${encodeURIComponent(v.id)}/summary" title="${esc(v.name)}">${esc(v.name)}</a>`)}</div>`).join("")}${!vms.length ? '<div class="tree-vm subtle">No matching VMs</div>' : ""}</details>`;
    })
    .join("")}</div>`;
}
function renderPage() {
  switch (state.page) {
    case "vms":
      return renderVMs();
    case "vm":
      return renderVM();
    case "networks":
      return renderNetworks();
    case "organization":
      return renderOrganization();
    case "resources":
      return renderResources();
    case "isos":
      return renderISOs();
    case "storage":
      return renderStorage();
    case "backups":
      return renderBackups();
    case "templates":
      return renderTemplates();
    case "users":
      return renderUsers();
    case "tasks":
      return renderTasks();
    case "audit":
      return renderAudit();
    case "account":
      return renderAccount();
    default:
      return renderOverview();
  }
}
function stat(label, value, caption, percent) {
  return `<article class="stat-card"><div class="label">${label}</div><div class="value">${value}</div><small>${caption}</small><div class="usage-bar"><i style="width:${Math.max(0, Math.min(100, percent || 0))}%"></i></div></article>`;
}
function renderOverview() {
  const { host, pools, isos } = state.inventory;
  const vms = state.inventory.vms.filter(matchesOrganization);
  const networks = state.inventory.networks.filter(matchesOrganization);
  const running = vms.filter((v) => v.status === "running").length;
  const underlying = pools.filter(p => !p.name.startsWith("vmm-block-"));
  const total = underlying.reduce((n, p) => n + (p.resources?.space?.total || 0), 0);
  const used = underlying.reduce((n, p) => n + (p.resources?.space?.used || 0), 0);
  return (
    title(
      "Overview",
      "Host metrics and virtual machines in the current resource view",
      btn("↻ Refresh", "refresh"),
    ) +
    `<div class="resource-cards">${stat("CPU usage", host.cpu_percent.toFixed(1) + "%", host.cpu_count + " logical CPUs", host.cpu_percent)}${stat("Memory", Math.round(host.memory_used / 1024 ** 3) + " / " + Math.round(host.memory_total / 1024 ** 3) + " GiB", host.memory_percent.toFixed(1) + "% used", host.memory_percent)}${stat("Storage", size(used, 0) + " / " + size(total, 1), pools.length + " storage pools", total ? (used / total) * 100 : 0)}${stat("Virtual machines", running + " / " + vms.length, "running / total", vms.length ? (running / vms.length) * 100 : 0)}</div>${monitoringPanel("host", [""])}<div class="overview-columns">${panel(
      "Virtual machines",
      vmTable(
        vms
          .slice()
          .sort((a, b) => (b.status === "running") - (a.status === "running"))
          .slice(0, 9),
        true,
      ),
      '<a href="#vms">View all →</a>',
    )}${panel(
      "Host summary",
      properties([
        ["Hostname", copyValue(host.name)],
        ["Address", copyAddresses(host.address)],
        ["Uptime", duration(host.uptime)],
        ["Load average", host.load.map((v) => v.toFixed(2)).join(" / ")],
        ["Hypervisors", "Incus + libvirt"],
        ["Networks", String(networks.length)],
        ["ISO images", String(isos.length)],
        ["App storage free", size(host.disk_free)],
      ]),
    )}</div><p class="subtle" style="font-size:11px">Live host values · Updated ${when(state.inventory.updated)} · Guest addresses appear when reported by the hypervisor.</p>`
  );
}
function vmProjectId(vm) {
  return state.inventory.projects.some(p => p.id === vm.project_id) ? vm.project_id : "";
}
function visibleVMs(vms = filteredVMs()) {
  return vms.filter(v => !collapsedVMProjects.has(vmProjectId(v)));
}
function vmProjectGroups(vms) {
  const groups = new Map();
  for (const vm of vms) {
    const id = vmProjectId(vm);
    if (!groups.has(id)) groups.set(id, {
      id, name: state.inventory.projects.find(p => p.id === id)?.name || "Unassigned", vms: [],
    });
    groups.get(id).vms.push(vm);
  }
  return [...groups.values()].sort((a, b) =>
    (a.id === "") - (b.id === "") || a.name.localeCompare(b.name));
}
function vmTable(vms, compact = false, selectable = false, grouped = false) {
  const selectableVMs = grouped ? visibleVMs(vms) : vms;
  const selectionHeader = '<input type="checkbox" id="vm-select-all" aria-label="Select all visible virtual machines" ' + (selectableVMs.length && selectableVMs.every(v => selectedVMs.has(v.id)) ? "checked" : "") + (!selectableVMs.length ? " disabled" : "") + '>';
  const renderRow = (v) =>
        `<tr class="clickable${grouped ? " vm-project-member" : ""}" data-action="open-vm" data-id="${esc(v.id)}">${selectable ? `<td class="vm-select-cell"><input type="checkbox" class="vm-select" value="${esc(v.id)}" aria-label="Select ${esc(v.name)} (${esc(v.provider)})" ${selectedVMs.has(v.id) ? "checked" : ""}></td>` : ""}<td class="vm-name-cell">${copyValue(v.name, "hostname", `<a class="name" href="#vm/${encodeURIComponent(v.id)}/summary">${esc(v.name)}</a>`)}${organizationChips(v, !grouped)}${!compact && v.description ? `<small>${esc(v.description.slice(0, 70))}</small>` : ""}</td><td>${badge(v.provider)}</td><td>${badge(v.status)}</td><td class="numeric">${esc(v.cpu)}</td><td class="numeric">${size(v.memory)}</td>${compact ? "" : `<td class="numeric">${Object.values(v.devices).filter((d) => d.type === "disk" && d.readonly !== "true" && !d.source?.endsWith(".iso") && d.kind !== "cdrom").length} disk(s)</td><td class="mono">${copyAddresses(v.addresses, "Not reported")}</td><td><div class="actions">${smallAction("Open", "open-vm", v.id)}${can() ? smallAction(v.status === "running" ? "Shutdown" : "Start", v.status === "running" ? "shutdown" : "start", v.id, !["stopped", "running"].includes(v.status)) : ""}</div></td>`}</tr>`;
  const rows = grouped ? vmProjectGroups(vms).flatMap(group => {
    const expanded = !collapsedVMProjects.has(group.id);
    const folder = `<tr class="vm-project-folder"><td colspan="${(compact ? 5 : 8) + Number(selectable)}"><button type="button" class="vm-project-toggle" data-action="toggle-vm-project" data-id="${esc(group.id)}" aria-expanded="${expanded}"><span class="vm-project-chevron" aria-hidden="true">${expanded ? "▾" : "▸"}</span><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/></svg><span>${esc(group.name)}</span><span class="vm-project-count">${group.vms.length} VM${group.vms.length === 1 ? "" : "s"}</span></button></td></tr>`;
    return [folder, ...(expanded ? group.vms.map(renderRow) : [])];
  }) : vms.map(renderRow);
  return table(
    [...(selectable ? [selectionHeader] : []), ...(compact
      ? ["Name", "Hypervisor", "Status", "vCPUs", "Memory"]
      : [
          "Name",
          "Hypervisor",
          "Status",
          "vCPUs",
          "Memory",
          "Disks",
          "IP address",
          "Actions",
        ])],
    rows,
  );
}
function filteredVMs() {
  return state.inventory.vms.filter(
    (v) =>
      matchesOrganization(v) &&
      (!state.search ||
        v.name.toLowerCase().includes(state.search.toLowerCase()) ||
        v.description.toLowerCase().includes(state.search.toLowerCase())) &&
      (state.status === "all" || v.status === state.status) &&
      (state.provider === "all" || v.provider === state.provider),
  );
}
function vmListContent(vms) {
  // Selections always describe visible rows, including after filters/refreshes.
  const visibleIds = new Set(visibleVMs(vms).map(v => v.id));
  for (const id of selectedVMs) if (!visibleIds.has(id)) selectedVMs.delete(id);
  return (can() && selectedVMs.size ? `<div class="toolbar bulk-vm-toolbar" aria-label="Selected VM operations"><strong>${selectedVMs.size} selected</strong><select id="bulk-vm-action" aria-label="Bulk VM operation"><option value="">Choose operation…</option><option value="shutdown">Shutdown</option><option value="restart">Restart</option><option value="stop">Force Stop</option><option value="delete">Delete</option></select>${btn("Clear selection", "clear-vm-selection")}</div>` : "") + panel("Virtual machines", vmTable(vms, false, can(), true));
}
function bulkVMAction(operation) {
  const vms = visibleVMs().filter(v => selectedVMs.has(v.id));
  if (!vms.length) return;
  const labels = {shutdown: "Shutdown", restart: "Restart", stop: "Force Stop", delete: "Delete"};
  const deleting = operation === "delete";
  const explanation = deleting
    ? "Permanently delete these stopped VMs, all their snapshots and their backups. Check Preserve backups for each VM whose independent backups should remain available in Backups. Incus root disks and attached app-created disks are removed; libvirt disk files and detached volumes are retained. Running VMs fail deletion; shut them down first."
    : operation === "stop" ? "Power off these VMs immediately. Unsaved guest data may be lost. Earlier queued power requests are cancelled; pending hardware changes are retained."
    : operation === "restart" ? "Gracefully shut down and restart these VMs, applying pending hardware changes. Guests that do not shut down within 60 seconds will report a failed task."
    : "Ask these VMs to shut down gracefully. Guests that do not shut down within 60 seconds will report a failed task; they will not be forced off.";
  modal(`${labels[operation]} ${vms.length} virtual machine${vms.length === 1 ? "" : "s"}`, `<div class="${deleting || operation === "stop" ? "warning" : "hint"}">${explanation}</div>` +
    table(["Virtual machine", "Hypervisor", "Status", ...(deleting ? ["Preserve backups"] : [])], vms.map(v => `<tr><td>${esc(v.name)}</td><td>${esc(v.provider)}</td><td>${esc(v.status)}</td>${deleting ? `<td><input type="checkbox" name="preserve_backups" value="${esc(v.id)}" aria-label="Preserve backups for ${esc(v.name)} (${esc(v.provider)})"></td>` : ""}</tr>`)) +
    `<p>Confirm once for all listed VMs. Each VM has its own task and result. Operations run on up to four VMs at a time; a failure does not stop the other VMs.${deleting ? ' Deletion cleanup cannot be undone and may be partially completed if a task fails.' : ''}</p>`, async f => {
      const result = await api("/vms/bulk/action", "POST", {action: operation, vm_ids: vms.map(v => v.id), confirmed: true, preserve_backups: f.getAll("preserve_backups")});
      vms.forEach(v => selectedVMs.delete(v.id));
      if (state.page === "vms") updateVMTable();
      toast(`${result.tasks.length} VM tasks queued. Follow each result in the task log.`);
      window.dispatchEvent(new CustomEvent("refresh-tasks"));
    }, {wide: true, submit: `${labels[operation]} ${vms.length} VM${vms.length === 1 ? "" : "s"}`, danger: deleting || operation === "stop"});
}
function renderVMs() {
  const vms = filteredVMs();
  return (
    title(
      "Virtual machines",
      "Manage Incus and libvirt guests",
      can() ? btn("From template", "templates") + btn("+ Create VM", "create-vm", "", "primary") : "",
    ) +
    `<div class="toolbar"><input id="vm-search" aria-label="Search virtual machines" placeholder="⌕  Search virtual machines" value="${esc(state.search)}"><select id="provider-filter" aria-label="Hypervisor filter">${[
      ["all", "All hypervisors"],
      ["incus", "Incus"],
      ["libvirt", "libvirt"],
    ]
      .map(
        ([v, l]) =>
          `<option value="${v}" ${v === state.provider ? "selected" : ""}>${l}</option>`,
      )
      .join(
        "",
      )}</select><select id="status-filter" aria-label="Status filter">${[
      ["all", "All states"],
      ["running", "Running"],
      ["stopped", "Stopped"],
      ["paused", "Paused"],
    ]
      .map(
        ([v, l]) =>
          `<option value="${v}" ${v === state.status ? "selected" : ""}>${l}</option>`,
      )
      .join(
        "",
      )}</select><span class="result-count" id="vm-result-count">${vms.length} virtual machines</span></div><div id="vm-table">${vmListContent(vms)}</div>`
  );
}
async function refreshSnapshots(vmId, repaint = true) {
  if (snapshotsLoading === vmId) return;
  snapshotsLoading = vmId;
  try {
    const data = await api(ref(vmId) + "/snapshots");
    vmSnapshots = { vm: vmId, ...data };
  } catch (e) {
    vmSnapshots = { vm: vmId, error: e.message };
  } finally {
    if (snapshotsLoading === vmId) snapshotsLoading = null;
    if (repaint && state.page === "vm" && state.tab === "snapshots" && state.vm === vmId && !$("#modal").open) render();
  }
}
function renderSnapshots(v, data) {
  if (data.error) return `<div class="error-banner">${esc(data.error)}</div>${btn("Retry", "refresh-snapshots", v.id)}`;
  const working = state.tasks.some(t => t.resource === v.id && ["queued", "running"].includes(t.status));
  const incomplete = data.snapshots.some(s => s.managed && s.status !== "ready");
  const disabled = v.status !== "stopped" || !!v.pending?.length || working;
  const actions = `${can() ? btn("Take snapshot", "create-snapshot", v.id, "primary", (!!v.pending?.length || working || !["running", "stopped"].includes(v.status)) || incomplete || !data.supported) : ""}${btn("Refresh", "refresh-snapshots", v.id)}`;
  return `<div class="hint">Snapshots share unchanged disk blocks; subsequent snapshots store changes. Each snapshot records its parent. Take snapshots live, or choose graceful shutdown and restart. Live capture may briefly pause the VM. Restore and delete require a stopped VM. Snapshots include configuration and writable disks, and remain on the same storage as the VM.</div>
    ${!data.supported ? `<div class="error-banner">${esc(data.reason)}</div>` : ""}
    ${v.pending?.length ? '<div class="pending-banner">Apply or discard pending hardware changes before taking or restoring a snapshot.</div>' : ""}
    ${panel("Snapshot history", data.snapshots.length ? table(["Snapshot", "Parent", "Created", "Disks", "Status", "Actions"], data.snapshots.map(s => {
      const retry = s.status === "incomplete" && s.operation === "restore";
      const restoreDisabled = disabled || !data.supported || !(s.status === "ready" || retry);
      const deleteDisabled = v.status !== "stopped" || working || !!s.children?.length || retry;
      return `<tr><td class="vm-name-cell"><strong>${esc(s.name)}</strong>${s.description ? `<small>${esc(s.description)}</small>` : ""}${s.current ? '<small>Current snapshot base</small>' : ""}${s.managed ? `<small>${s.consistency === "crash-consistent" ? "Live · crash-consistent" : "Offline"}</small>` : ""}${s.error ? `<small role="alert">${esc(s.error)}</small>` : ""}${!s.managed ? '<small>Created outside VM Manager; manage through the hypervisor.</small>' : ""}</td><td>${esc(s.parent || (s.managed ? "Base" : "—"))}</td><td>${s.created ? when(s.created) : "—"}</td><td>${s.disks ? esc(s.disks.map(d => d.device).join(", ")) : "—"}</td><td>${badge(s.status)}</td><td><div class="actions">${can() && s.managed ? `${btn(retry ? "Retry restore" : "Restore", "restore-snapshot", v.id + "|" + s.name, "small", restoreDisabled)}${btn("Delete", "delete-snapshot", v.id + "|" + s.name, "small danger", deleteDisabled)}` : "—"}</div>${s.children?.length ? '<small>Delete children first</small>' : ""}</td></tr>`;
    })) : empty("No snapshots yet", "Take a snapshot before making changes so you can return to this state."), actions)}
    <div class="hint">Restoring replaces current disk contents and configuration and leaves the VM stopped. On ZFS, explicitly delete newer snapshots first. Delete snapshots newest first to preserve the chain. Snapshots do not protect against storage failure; use backups for a separate recoverable copy.</div>`;
}
function createSnapshot(v) {
  modal("Take snapshot · " + v.name,
    field("Snapshot name", "name", "snap-" + new Date().toISOString().replace(/[-:]/g, "").slice(0, 15).replace("T", "-"), { hint: "1–63 letters, digits, underscores or hyphens; start with a letter." }) +
    textarea("Description (optional)", "description", "") +
    protectionMode("live") +
    (v.provider === "libvirt" ? '<div class="hint">Live libvirt snapshots also save memory and may pause the VM while saving it. Restoring leaves the VM stopped.</div>' : "") +
    '<div class="hint">Includes VM configuration and supported writable disks. Live capture is crash-consistent: unsaved application data may be absent. Shutdown and restart allows the guest to finish writes. A VM that is already stopped stays stopped.</div>',
    f => queue(ref(v.id) + "/snapshots", "POST", { name: f.get("name"), description: f.get("description"), mode: f.get("mode") }),
    { submit: "Take snapshot", copyTitle: v.name });
}
function renderVM() {
  const v = currentVM();
  renderedVMStatus = v?.status;
  if (!v)
    return (
      title(
        "Virtual machine unavailable",
        "Refresh the inventory or select another VM.",
      ) + btn("Refresh", "refresh")
    );
  const running = v.status === "running";
  const editDisabled = v.status !== "stopped";
  let html = title(
    `<span class="title-status">${copyValue(v.name)} ${badge(v.status)}</span>`,
    `${v.provider === "incus" ? "Incus" : "libvirt"} virtual machine${v.managed ? " · Created in VM Manager" : ""}`,
    can()
      ? `${btn("Console", "console", v.id, "secondary", !running)}${btn("Create template", "capture-template", v.id, "secondary", !["running", "stopped"].includes(v.status))}${btn(running ? "Shutdown" : "Start", running ? "shutdown" : "start", v.id, "primary", !["running", "stopped"].includes(v.status))}${btn(v.pending?.length ? "Restart & apply" : "Restart", "restart", v.id, "secondary", !running)}${btn("Force stop", "force-stop", v.id, "danger", !running)}`
      : "",
  );
  html += `<div class="tabs">${["summary", "monitor", "hardware", "snapshots", "backups", "console", "options"].map((t) => `<button class="tab ${state.tab === t ? "active" : ""}" data-action="vm-tab" data-id="${t}" ${t === "console" && !can() ? "disabled" : ""}>${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div>`;
  if (state.tab === "monitor") return html + monitoringPanel(v.id, Object.entries(v.devices).filter(([key,d]) => d.type === "disk" && key !== "cdrom" && (d.kind || "disk") === "disk" && d.source !== "agent:config" && !d.source?.toLowerCase().endsWith(".iso") && !state.inventory.isos.some(i => i.pool === d.pool && i.name === d.source)).map(([key]) => key));
  if (state.tab === "snapshots") {
    if (vmSnapshots?.vm !== v.id) {
      refreshSnapshots(v.id);
      return html + '<div class="empty">Loading snapshots…</div>';
    }
    return html + renderSnapshots(v, vmSnapshots);
  }
  if (state.tab === "console") {
    return (
      html +
      (running
        ? renderEmbeddedConsole(v)
        : empty(
            "Start the VM to connect",
            "The console is available while the VM is running.",
            can() ? btn("Start VM", "start", v.id, "primary") : "",
          ))
    );
  }
  if (state.tab === "summary") {
    return (
      html +
      `<div id="vm-live-metrics" class="resource-cards">${renderVMMetrics(v)}</div><div class="overview-columns">${panel(
        "VM information",
        properties([
          ["Name", copyValue(v.name)],
          ["Hypervisor", badge(v.provider)],
          ["Status", badge(v.status)],
          [
            "IP addresses",
            copyAddresses(v.addresses, "Not reported by guest"),
          ],
          ["Description", esc(v.description || "No description")],
          ["Project & tags", organizationChips(v) || "Unassigned · No tags"],
        ]),
      )}${panel("Quick actions", `<div class="storage-body"><p class="muted">Configure disks, network adapters and installation media from the Hardware tab.</p><div class="actions" style="margin-top:16px">${btn("View hardware", "vm-tab", "hardware")}${agentButton(v)}${can() ? btn("Assign project & tags", "organize-vm", v.id) : ""}${can() ? btn("Edit resources", "edit-vm", v.id, "secondary") : ""}</div></div>`)}</div>`
    );
  }
  if (state.tab === "backups") return html + renderVMBackups(v);
  if (state.tab === "options") {
    return (
      html +
      panel(
        "VM options",
        properties([
          [
            "Secure Boot",
            v.provider === "incus"
              ? v.config["security.secureboot"] === "false"
                ? "Disabled"
                : "Enabled"
              : "Managed by libvirt firmware",
          ],
          [
            "Autostart",
            v.provider === "incus"
              ? v.config["boot.autostart"] === "true"
                ? "Enabled"
                : "Disabled"
              : "Managed by libvirt",
          ],
          [
            "Configuration",
            v.managed
              ? "Managed by VM Manager"
              : "Existing host virtual machine",
          ],
        ]),
      ) +
      renderBackupPlan(v) +
      (can()
        ? `<div class="actions">${btn("Edit resources and options", "edit-vm", v.id, "secondary")}${btn("Delete virtual machine", "delete-vm", v.id, "danger", editDisabled)}</div><div class="hint" style="margin-top:18px">${v.provider === "incus" ? "Deleting this VM removes its root disk and attached extra disks created by this app. Detached volumes remain stored." : "Deleting this existing libvirt VM removes its definition and retains its disk files."} A stopped VM and exact name confirmation are required.</div>`
        : "")
    );
  }
  html += running
    ? '<div class="hint">Changes are saved for Restart in VM Manager, or the next Start after shutdown. A reboot inside the guest does not apply pending hardware.</div>'
    : "";
  if (can())
    html += `<div class="toolbar">${btn("Edit CPU / memory", "edit-vm", v.id, "secondary")}${btn("+ Add disk", "add-disk", v.id, "secondary")}${btn("+ Network adapter", "add-nic", v.id, "secondary")}${btn("Mount / eject ISO", "cdrom", v.id, "secondary")}</div>`;
  const hardware = [
    [
      "Processors",
      `${esc(v.cpu)} vCPU`,
      can() ? smallAction("Edit", "edit-vm", v.id) : "",
    ],
    [
      "Memory",
      size(v.memory),
      can() ? smallAction("Edit", "edit-vm", v.id) : "",
    ],
    [
      "Firmware",
      v.provider === "incus"
        ? `UEFI · Secure Boot ${v.config["security.secureboot"] === "false" ? "disabled" : "enabled"}`
        : "Existing libvirt firmware",
      "",
    ],
  ];
  for (const [key, d] of Object.entries(v.devices)) {
    if (!["disk", "nic"].includes(d.type)) continue;
    const isAgent = d.type === "disk" && d.source === "agent:config";
    const isNIC = d.type === "nic";
    const isISO =
      isAgent ||
      d.kind === "cdrom" ||
      key === "cdrom" ||
      d.source?.endsWith(".iso") ||
      state.inventory.isos.some(
        (i) => i.pool === d.pool && i.name === d.source,
      );
    const isRoot =
      d.path === "/" || (v.provider === "libvirt" && key === "vda");
    const value = isNIC
      ? `${esc(d.network || d.parent || d.nictype || "Network")} ${d.hwaddr ? `<span class="subtle">· ${esc(d.hwaddr)}</span>` : ""}`
      : `${esc(d.source ? d.source.split("/").pop() : d.pool || "Disk")} ${d.size ? `<span class="subtle">· ${esc(d.size)}</span>` : ""}${isRoot ? ' <span class="subtle">· Root disk</span>' : ""}`;
    let actions = "";
    if (can()) {
      if (isAgent) actions += agentButton(v);
      else if (isNIC)
        actions += smallAction(
          "Edit",
          "edit-nic",
          v.id + "|" + key,
          false,
        );
      else if (isISO)
        actions += smallAction("Change / eject", "cdrom", v.id);
      else if (v.provider === "incus" && d.readonly !== "true")
        actions += smallAction(
          "Expand",
          "resize-disk",
          v.id + "|" + key,
          false,
        );
      if (!isRoot && !isISO)
        actions += smallAction(
          "Detach",
          "detach-device",
          v.id + "|" + key,
          false,
        );
    }
    hardware.push([
      `${isNIC ? "Network adapter" : isISO ? "CD/DVD drive" : "Hard disk"} (${esc(key)})`,
      isAgent ? "Incus agent CD · Needed at boot only when the guest uses the CD fallback" : value,
      actions,
    ]);
  }
  return (
    html +
    renderPending(v) +
    panel(
      "Hardware",
      hardware
        .map(
          ([label, value, actions]) =>
            `<div class="hardware-row"><span class="hardware-label">${label}</span><span class="hardware-value">${value}</span><div class="actions">${actions}</div></div>`,
        )
        .join(""),
    ) +
    renderExternalMounts(v) +
    (v.provider === "incus" ? panel("Guest integration", `<div class="storage-body"><p class="muted">Install the Incus agent to enable guest commands and automatic storage mounting. Linux mounts also require Python 3 and systemd.</p><div class="actions">${agentButton(v)}</div></div>`) : "") +
    `<p class="subtle" style="font-size:11px">Disk expansion increases the virtual device. Extend partitions and filesystems inside the guest afterwards.</p>`
  );
}

function agentButton(v) {
  return can() && v.provider === "incus"
    ? btn("Install Incus agent", "install-agent", v.id, "secondary", !["running", "stopped"].includes(v.status)) : "";
}

// Register the canonical /run path before the host's installer runs. Older
// Incus installers only try /var/run, which SELinux treats as an alias, and
// silently skip policy setup when semanage is absent on minimal RHEL guests.
const agentSELinuxSetup = `if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" != Disabled ]; then if ! command -v semanage >/dev/null 2>&1 || ! command -v restorecon >/dev/null 2>&1; then if command -v dnf >/dev/null 2>&1; then dnf install -y policycoreutils policycoreutils-python-utils; elif command -v apt-get >/dev/null 2>&1; then apt-get install -y policycoreutils policycoreutils-python-utils; else echo "Install semanage and restorecon before installing the agent" >&2; exit 1; fi; fi; semanage fcontext -a -t bin_t /run/incus_agent/incus-agent 2>/dev/null || semanage fcontext -m -t bin_t /run/incus_agent/incus-agent; if [ -e /run/incus_agent/incus-agent ]; then restorecon -v /run/incus_agent/incus-agent; fi; fi`;
const agentInstallCommand = `sudo sh -c 'set -eu; ${agentSELinuxSetup}; if [ ! -x /run/incus_agent/incus-agent ] || ! systemctl cat incus-agent >/dev/null 2>&1; then d=$(mktemp -d /run/incus-agent.XXXXXX); trap "cd /; umount $d 2>/dev/null || true; rmdir $d" EXIT; mount -t 9p -o ro config "$d" || mount -o ro /dev/disk/by-id/scsi-0QEMU_QEMU_CD-ROM_incus_agent "$d"; cd "$d"; ./install.sh; cd /; umount "$d"; rmdir "$d"; trap - EXIT; fi; systemctl reset-failed incus-agent; systemctl restart incus-agent'`;

function installAgent(v) {
  const dialog = modal("Install Incus agent · " + v.name,
    `<p>First-time installation needs a command inside the VM. Linux with systemd is supported by this installer. The command first uses Incus’s built-in shared filesystem (9p), which requires no CD.</p><ol><li><p>Optional CD fallback: try the command below first. Prepare the installer CD if the shared filesystem is unavailable, then run the command again. Preparing also repairs older installer CDs with an incompatible device ID. Guests using the CD need it attached at boot for agent files and connection credentials; it does not need to stay mounted inside Linux.</p><button type="button" class="button secondary" id="agent-prepare">Prepare optional installer CD</button><p id="agent-prepare-status" role="status"></p></li><li><p>Start the VM if needed, open its console and log in. Run this command in a Linux terminal with sudo access, or omit sudo in a root shell. On SELinux guests it configures the agent permissions and installs SELinux management tools if needed, keeping SELinux enabled. It can also repair an already installed agent without remounting its CD.</p><div class="hint" style="overflow-wrap:anywhere">${copyValue(agentInstallCommand, "installation command", `<code style="white-space:pre-wrap">${esc(agentInstallCommand)}</code>`)}</div><p><a class="button secondary" href="/console?vm=${encodeURIComponent(v.id)}" target="_blank" rel="noopener">Open VM console ↗</a></p><p class="muted">If the CD is not visible after attaching it, restart the VM and try again. This installs the guest agent; automatic storage mounts also need /usr/bin/python3.</p></li><li><p>After installation, verify the guest connection and mounting prerequisites.</p><button type="button" class="button secondary" id="agent-check">Check agent</button><p id="agent-status" role="status" aria-live="polite"></p></li></ol>`,
    async () => {}, { wide: true, submit: "Done" });
  $("footer .close-modal", dialog).textContent = "Close";
  let disposed = false, timer;
  const active = () => !disposed && dialog.open;
  const prepare = $("#agent-prepare", dialog);
  const note = $("#agent-prepare-status", dialog);
  const check = $("#agent-check", dialog);
  const status = $("#agent-status", dialog);
  const dispose = () => { disposed = true; clearTimeout(timer); };
  dialog.addEventListener("close", dispose, { once: true });
  dialog.addEventListener("modal-replace", dispose, { once: true });
  check.onclick = async () => {
    check.disabled = true;
    status.textContent = "Checking guest connection…";
    try {
      const result = await api(ref(v.id) + "/agent");
      if (!active()) return;
      status.textContent = result.message;
      if (result.installer_attached && !prepare.disabled) note.textContent = result.installer_needs_repair
        ? "Installer CD needs repair. Use Prepare optional installer CD, then rerun the installation command."
        : "Optional installer CD is attached. Keep it attached if the guest relies on the CD at boot.";
    } catch (error) {
      if (active()) status.textContent = error.message;
    } finally {
      if (active()) check.disabled = false;
    }
  };
  prepare.onclick = async () => {
    prepare.disabled = true;
    note.textContent = "Preparing installer CD…";
    try {
      const result = await queue(ref(v.id) + "/agent/prepare", "POST", {});
      const update = async () => {
        if (!active()) return;
        try {
          const task = await api("/tasks/" + result.task_id);
          if (!active()) return;
          if (["queued", "running"].includes(task.status)) {
            note.textContent = "Preparing installer CD · " + task.status;
            timer = setTimeout(update, 1000);
          } else {
            note.textContent = task.message;
            prepare.disabled = false;
          }
        } catch (error) {
          if (active()) { note.textContent = error.message; prepare.disabled = false; }
        }
      };
      await update();
    } catch (error) {
      if (active()) { note.textContent = error.message; prepare.disabled = false; }
    }
  };
}
function renderVMMetrics(v) {
  const m = vmMetrics?.vm_id === v.id ? vmMetrics : null;
  const fresh = m && Date.now() / 1000 - m.updated < 20;
  const cpu = fresh && m.cpu_percent != null ? `${m.cpu_percent.toFixed(1)}%` : "—";
  const memory = fresh && m.memory_used != null ? size(m.memory_used) : "—";
  const cpuLabel = !fresh ? "Waiting for live sample" : !m.running ? "VM stopped" : m.cpu_percent == null ? "Sampling CPU usage…" : `${m.cpu_source} · 100% = all allocated vCPUs`;
  const memLabel = !fresh ? "Waiting for live sample" : !m.running ? "VM stopped" : m.memory_source || "Guest memory unavailable";
  return stat("CPU usage", cpu, `${esc(v.cpu)} vCPU allocated · ${esc(cpuLabel)}`, fresh ? m.cpu_percent || 0 : 0)
    + stat("Memory usage", memory, `${size(v.memory)} configured · ${esc(memLabel)}`, fresh && m.memory_total ? Math.min(100, (m.memory_used || 0) / m.memory_total * 100) : 0)
    + stat("Power state", esc(v.status), "Live usage refreshes every 5 seconds", v.status === "running" ? 100 : 0)
    + stat("Network adapters", Object.values(v.devices).filter(d => d.type === "nic").length, "Connected virtual interfaces", 0);
}
async function refreshVMMetrics() {
  if (metricsBusy || !state.user || state.page !== "vm" || state.tab !== "summary") return;
  const v = currentVM();
  if (!v) return;
  metricsBusy = true;
  try {
    vmMetrics = await api(ref(v.id) + "/metrics");
  } catch {
    vmMetrics = null;
  } finally {
    metricsBusy = false;
    if (state.page === "vm" && state.tab === "summary" && currentVM()?.id === v.id && $("#vm-live-metrics"))
      $("#vm-live-metrics").innerHTML = renderVMMetrics(currentVM());
  }
}
function renderPending(v) {
  if (!v.pending?.length) return "";
  const rows = v.pending.flatMap(p => {
    const b = p.body;
    if (p.kind === "resources") {
      const values = [["Processors", `${v.cpu} vCPU`, `${b.cpu} vCPU`], ["Memory", size(v.memory), size(b.memory_gib * 1024 ** 3)], ["Description", v.description || "None", b.description || "None"]];
      if (v.provider === "incus") values.push(["Secure Boot", v.config["security.secureboot"] === "false" ? "Disabled" : "Enabled", b.secure_boot ? "Enabled" : "Disabled"], ["Start with host", v.config["boot.autostart"] === "true" ? "Enabled" : "Disabled", b.autostart ? "Enabled" : "Disabled"]);
      return values.filter(([,a,b]) => a !== b);
    }
    if (p.kind === "disk.add") return [["New disk", "Not attached", `${b.size_gib} GiB · ${b.pool}`]];
    if (p.kind === "disk.resize") return [[`Disk ${p.device}`, v.devices[p.device]?.size || "Current disk size", `${b.size_gib} GiB`]];
    if (p.kind === "device.detach") {
      const d = v.devices[p.device];
      const current = d?.type === "nic" ? d.network || d.parent || "Network adapter" : [d?.source?.split("/").pop() || d?.pool || "Disk", d?.size].filter(Boolean).join(" · ");
      return [[p.device, current, "Detached (storage retained)"]];
    }
    if (p.kind === "nic") return [[b.device || "New network adapter", v.devices[b.device]?.network || "Not attached", b.network]];
    if (p.kind === "cdrom") return [["CD/DVD drive", Object.values(v.devices).filter(d => d.kind === "cdrom" || d.source?.endsWith(".iso")).map(d => d.source).join(", ") || "No ISO", b.iso_id ? `${state.inventory.isos.find(i => i.id === b.iso_id)?.name || b.iso_id}${b.boot ? " · Boot first" : ""}` : "Ejected"]];
    return [];
  });
  return panel("Pending hardware changes", `<div class="hint">Apply with Restart in VM Manager, or Start after shutdown. Changes apply in saved order. If a change fails, the VM stays stopped and unapplied changes remain here.</div>` + table(["Hardware", "Current", "After restart"], rows.map(row => `<tr>${row.map(value => `<td>${esc(value)}</td>`).join("")}</tr>`)), can() ? btn("Discard pending", "discard-pending", v.id, "secondary") : "");
}
function renderNetworks() {
  const nets = state.inventory.networks.filter(matchesOrganization);
  const selected = nets.find((n) => n.id === selectedNetwork) || nets[0];
  return (
    title(
      "Networks",
      "Managed virtual networks, DHCP and outbound connectivity",
      can() ? btn("+ Create network", "create-network", "", "primary") : "",
    ) +
    panel(
      "Networks",
      table(
        [
          "Name",
          "Provider",
          "Mode",
          "Gateway / subnet",
          "DHCP",
          "References",
          "Actions",
        ],
        nets.map(
          (n) =>
            `<tr class="clickable" data-action="network-detail" data-id="${esc(n.id)}"><td><span class="name">${esc(n.name)}</span>${organizationChips(n)}${n.description ? `<small>${esc(n.description)}</small>` : ""}</td><td>${badge(n.provider)}</td><td>${badge(networkMode(n), "incus")}</td><td class="mono">${copyAddresses(n.config["ipv4.address"])}</td><td>${n.config["ipv4.dhcp"] === "false" ? "Disabled" : "Enabled"}</td><td>${n.used_by.length}</td><td><div class="actions">${smallAction("Details", "network-detail", n.id)}${can() ? smallAction("Organize", "organize-network", n.id) : ""}${can() && n.provider === "incus" ? `${smallAction("Edit", "edit-network", n.id, !!n.used_by.length)}${smallAction("Delete", "delete-network", n.id, !!n.used_by.length)}` : ""}</div></td></tr>`,
        ),
      ),
    ) +
    (selected
      ? panel(
          `${selected.name} · Network settings`,
          properties([
            [
              "Mode",
              networkMode(selected) === "NAT"
                ? "NAT · outbound access"
                : networkMode(selected) === "Isolated"
                  ? "Isolated · no IP forwarding"
                  : "Routed · No NAT",
            ],
            ["Gateway", copyAddresses(selected.config["ipv4.address"])],
            [
              "Project & tags",
              organizationChips(selected) || "Unassigned · No tags",
            ],
            [
              "DHCP",
              selected.config["ipv4.dhcp"] === "false" ? "Disabled" : "Enabled",
            ],
            [
              "DHCP range",
              copyAddresses(selected.config["ipv4.dhcp.ranges"], "Managed automatically"),
            ],
            [
              "DNS domain",
              selected.config["dns.domain"] ? copyValue(selected.config["dns.domain"], "DNS domain") : "Managed by hypervisor",
            ],
            [
              "References",
              selected.used_by.length
                ? selected.used_by
                    .map((u) => `<div class="mono">${esc(u)}</div>`)
                    .join("")
                : "Not in use",
            ],
          ]),
        )
      : "") +
    `<div class="hint">New networks are created with DHCP and a free private subnet automatically. NAT provides outbound connectivity; isolated networks keep guest traffic local. Referenced networks must be detached before editing or deletion. Existing libvirt networks are shown for visibility.</div>`
  );
}
function renderISOs() {
  const isos = state.inventory.isos;
  return (
    title(
      "ISO library",
      "Upload installation media and attach it to virtual CD-ROM drives",
    ) +
    (can()
      ? `${select("Upload destination", "iso-storage", storageOptions("iso"), state.inventory.storage_defaults?.iso || "", "Applies to new uploads. Existing ISO files stay in their current storage.")}<label class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Upload ISO"><span class="upload-symbol">↑</span><strong>Drag ISO files here or click to browse</strong><small>ISO9660 / UDF · Up to 32 GiB per file · Choose a destination above</small><input id="iso-input" type="file" accept=".iso" multiple><div id="upload-progress" class="upload-progress" hidden><progress id="upload-bar" max="100" value="0"></progress><small id="upload-label"></small></div></label>`
      : "") +
    panel(
      "ISO images",
      table(
        ["Name", "Size", "Storage", "Uploaded", "Used by", "Actions"],
        isos.map(
          (i) =>
            `<tr><td class="iso-name-cell"><span class="name">${esc(i.name)}</span>${i.sha256 ? `<small title="SHA-256 ${esc(i.sha256)}">SHA-256 ${esc(i.sha256.slice(0, 20))}…</small>` : ""}</td><td class="numeric">${size(i.size)}</td><td>${esc(storageBlocks().find(b => b.path && i.path?.startsWith(b.path + "/"))?.name || i.pool || "Original ISO library")}</td><td class="numeric">${i.created ? when(i.created) : "—"}</td><td>${i.used_by?.length || 0} reference(s)</td><td>${can() ? smallAction("Delete", "delete-iso", i.id, !!i.used_by?.length) : '<span class="subtle">Read only</span>'}</td></tr>`,
        ),
      ),
      `<small>${isos.length} images</small>`,
    ) +
    `<p class="subtle" style="font-size:11px">Uploaded images are stored persistently and verified with SHA-256. Existing Incus ISO volumes are discovered automatically. Eject media from all VMs before deleting it.</p>`
  );
}
const templateRows = () => state.inventory.vm_templates || [];
function renderTemplates() {
  const rows = templateRows().filter(t => !state.search || `${t.name} ${t.description} ${t.vm_id}`.toLowerCase().includes(state.search.toLowerCase()));
  return title("VM templates", "Capture a configured VM once, then create independent VMs from it", can() ? btn("Create template", "capture-template", "", "primary", !state.inventory.vms.length) : "") +
    `<div class="hint">Templates include VM configuration, root disks and supported private data disks. They remain available after the source VM is deleted. Each new VM uses the same hypervisor as its template and starts stopped, with independent disks and new VM IDs and MAC addresses. Prepare a generalized guest before capture, or review its hostname, static IPs, credentials and automatic external mounts before starting each copy. Shared data and ISO media are excluded. CPU, memory and network settings can be edited on the new VM before starting.</div>` +
    panel("Template library", table(["Template", "Source VM", "Created", "Storage", "Status", "Size", "Actions"], rows.map(t => `<tr><td class="name">${esc(t.name)}${t.description ? `<small>${esc(t.description)}</small>` : ""}</td><td>${resourceName(t.vm_id)}<small>${esc(t.vm_id.split(":")[0])}</small></td><td>${when(t.created)}</td><td>${esc(backupBlockName(t.block_id))}</td><td>${badge(t.status)}${t.message ? `<small>${esc(t.message)}</small>` : ""}</td><td>${size(t.size)}</td><td><div class="actions">${can() ? smallAction("Create VM", "use-template", t.id, t.status !== "completed") + smallAction("Delete", "delete-template", t.id, ["queued", "running"].includes(t.status)) : "Read only"}</div></td></tr>`)));
}
function captureTemplate(vmId = "") {
  if (!backupDestinations().length) throw new Error("Add a backup block or writable external source in Storage first");
  const vms = state.inventory.vms.filter(v => ["running", "stopped"].includes(v.status));
  if (!vms.length) throw new Error("A running or stopped VM is required to create a template");
  const v = vms.find(v => v.id === vmId) || vms[0];
  modal("Create VM template", select("Source VM", "vm_id", vms.map(v => [v.id, `${v.name} · ${v.provider} · ${v.status}`]), v.id) +
    field("Template name", "name", v.name + " template", {hint: "A unique template name, up to 100 characters."}) + textarea("Description (optional)", "description") +
    select("Template storage", "block_id", backupDestinations(), state.inventory.storage_defaults?.backup || backupDestinations()[0][0]) +
    select("Capture mode", "mode", [["stopped", "Require VM to be stopped"], ["shutdown", "Gracefully shut down, capture, then restart"], ["live", "Live capture (crash-consistent)"]], "stopped") +
    `<div class="warning">Prepare the guest for reuse before capture. Templates retain guest hostnames, static IPs, secrets and automatic external mounts. Live capture excludes memory and unsaved data; shutdown mode allows downtime during capture. The source VM is retained. Templates use full archive space and remain until explicitly deleted.</div>`,
    async f => { await queue("/templates", "POST", {vm_id: f.get("vm_id"), name: f.get("name"), description: f.get("description"), block_id: f.get("block_id"), mode: f.get("mode")}); location.hash = "templates"; },
    {submit: "Capture template", wide: true});
}
function useTemplate(id) {
  const t = templateRows().find(t => t.id === id);
  if (!t || t.status !== "completed") throw new Error("Choose a completed template");
  modal("Create VM from template", `<p>Template: <strong>${esc(t.name)}</strong> · ${esc(t.vm_id.split(":")[0])}</p><div class="warning">Creates an independent, stopped VM with new VM IDs and MAC addresses. Autostart is disabled and ISO media is ejected. Review copied hostnames, static IPs, application identities and external mounts/credentials before starting. Failed creation may leave partial resources for inspection.</div>` +
    field("New VM name", "name", "", {hint: "A unique name, up to 63 letters, digits, underscores or hyphens; start with a letter."}) +
    (t.vm_id.startsWith("incus:") ? select("VM disk storage", "pool", poolOptions(), defaultVMPool()) : `<div class="hint">Libvirt disks use application disk storage.</div>`),
    f => queue("/templates/" + id + "/vms", "POST", {name: f.get("name"), pool: f.get("pool") || ""}),
    {submit: "Create VM", wide: true});
}

const backupPlans = () => state.inventory.backup_plans || [];
const backupRows = () => state.inventory.vm_backups || [];
const selectedBackupVMs = new Set();
const backupDestinations = () => [
  ...storageBlocks().filter(b => b.purpose === "backup" && b.status === "ready").map(b => [b.id, `${b.name} · ${b.size_gib} GiB`]),
  ...externalSources().filter(s => s.status === "ready" && !s.readonly).map(s => [s.id, `${s.name} · ${s.kind.toUpperCase()}`]),
];
const backupBlockName = id => storageBlocks().find(b => b.id === id)?.name || externalSource(id)?.name || "Unavailable storage";
const backupPlan = id => backupPlans().find(p => p.vm_id === id);
const planSummary = p => !p ? "Not configured" : !p.enabled ? "Schedule disabled" : `${p.frequency === "weekly" ? ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][p.weekday] : "Daily"} at ${p.time} · ${p.timezone}`;
function renderBackupPlan(v) {
  const p = backupPlan(v.id);
  const mode = p?.mode || (p?.shutdown ? "shutdown" : "stopped");
  return panel("Backup settings", properties([
    ["Schedule", esc(planSummary(p))],
    ["Destination", p ? esc(backupBlockName(p.block_id)) : "Choose backup storage"],
    ["Next run", p?.next_run ? when(p.next_run) : "—"],
    ["Retention", p ? `Keep ${p.keep} scheduled backups; manual backups retained until deleted` : "—"],
    ["Running VM", mode === "live" ? "Live backup" : mode === "shutdown" ? "Gracefully shut down, back up, restart" : "Require stopped VM"],
  ]), can() ? btn("Configure backups", "backup-settings", v.id) : "");
}
function backupTable(rows) {
  return table(["Source VM", "Created", "Storage", "Status", "Size", "Actions"], rows.map(b => `<tr><td class="name">${resourceName(b.vm_id)}<small>${esc(b.trigger)} · ${esc(b.id.slice(0, 8))}</small></td><td>${when(b.created)}</td><td>${esc(backupBlockName(b.block_id))}</td><td>${badge(b.status)}${b.message ? `<small title="${esc(b.message)}">${esc(b.message)}</small>` : ""}</td><td>${size(b.size)}</td><td><div class="actions">${can() ? smallAction("Restore", "restore-backup", b.id, b.status !== "completed") + smallAction("Delete", "delete-backup", b.id, ["running", "queued"].includes(b.status)) : ""}</div></td></tr>`));
}
function renderVMBackups(v) {
  return `<div class="hint">Full backups include the root disk, attached private data disks and VM configuration. ISO media, snapshots, shared storage and passthrough devices are excluded or require manual backup. Restores create a separate, stopped VM.</div>` +
    renderBackupPlan(v) + panel("Restore points", backupTable(backupRows().filter(b => b.vm_id === v.id)), can() ? btn("Back up now", "backup-now", v.id, "primary") : "");
}
function renderBackups() {
  const vms = state.inventory.vms.filter(matchesOrganization);
  return title("Backups", "Create restore points and manage each VM’s automatic backup schedule", can() ? btn("Back up selected", "backup-selected", "", "primary") + btn("Configure selected", "backup-settings-selected") : "") +
    `<div class="hint">Backups use local backup blocks or writable SMB/S3 sources configured in Storage. External backups require local staging space and are verified remotely before completion. Choose live backup or graceful shutdown and restart. Existing schedules retain their saved capture mode. Schedules run while this manager is available; after downtime, one missed run is queued. Times below use your browser timezone; schedules use their configured timezone.</div>` +
    panel("Virtual machines & schedules", table([can() ? '<input type="checkbox" id="backup-select-all" aria-label="Select all visible VMs">' : "", "VM", "Schedule", "Destination", "Next run", "Last scheduled task", "Actions"], vms.map(v => {
      const p = backupPlan(v.id), last = state.tasks.find(t => t.id === p?.last_task);
      return `<tr><td>${can() ? `<input type="checkbox" class="backup-vm-select" value="${esc(v.id)}" aria-label="Select ${esc(v.name)}" ${selectedBackupVMs.has(v.id) ? "checked" : ""}>` : ""}</td><td>${copyValue(v.name, "hostname", `<a class="name" href="#vm/${encodeURIComponent(v.id)}/backups">${esc(v.name)}</a>`)}<small>${esc(v.provider)} · ${esc(v.status)}</small></td><td>${esc(planSummary(p))}</td><td>${p ? esc(backupBlockName(p.block_id)) : "—"}</td><td>${p?.next_run ? when(p.next_run) : "—"}</td><td>${last ? badge(last.status) : p?.last_task ? smallAction("View task history", "tasks", "") : "—"}</td><td><div class="actions">${can() ? smallAction("Back up", "backup-now", v.id) + smallAction("Settings", "backup-settings", v.id) + (p ? smallAction("Remove settings", "remove-backup-settings", v.id) : "") : ""}</div></td></tr>`;
    }))) +
    (backupPlans().some(p => !state.inventory.vms.some(v => v.id === p.vm_id)) ? panel("Unavailable VM schedules", table(["VM", "Schedule", "Actions"], backupPlans().filter(p => !state.inventory.vms.some(v => v.id === p.vm_id)).map(p => `<tr><td>${resourceName(p.vm_id)}</td><td>${esc(planSummary(p))}</td><td>${can() ? smallAction("Remove settings", "remove-backup-settings", p.vm_id) : ""}</td></tr>`))) : "") +
    panel("Restore points", backupTable(backupRows().filter(b => { const v = state.inventory.vms.find(v => v.id === b.vm_id); return v ? matchesOrganization(v) : state.project === "all" && state.tag === "all"; })));
}
function protectionMode(value) {
  return select("Capture mode", "mode", [["live", "Live — keep the VM running"], ["shutdown", "Gracefully shut down, capture, then restart"], ["stopped", "Require the VM to be stopped"]], value);
}
function backupForm(vmIds, settings = false) {
  vmIds = [...new Set(vmIds)].filter(id => currentVM(id) && matchesOrganization(currentVM(id)));
  if (!vmIds.length) throw new Error("Select at least one VM");
  if (!backupDestinations().length) throw new Error("Add a backup block or writable external source in Storage first");
  const p = vmIds.length === 1 ? backupPlan(vmIds[0]) : null;
  const content = `<p>${vmIds.map(id => copyValue(currentVM(id).name)).join(", ")}</p><div class="warning">Live backups capture a point in time while the VM keeps running; a brief pause may occur. They are crash-consistent and exclude memory, so unsaved application data may be absent. Choose shutdown and restart for a clean guest shutdown. Force stop cancels automatic restart. Already stopped VMs stay stopped; pending hardware edits remain saved.</div>` +
    select("Backup storage", "block_id", [...(p && !backupDestinations().some(b => b[0] === p.block_id) ? [[p.block_id, "Unavailable destination — select another block"]] : []), ...backupDestinations()], p?.block_id || state.inventory.storage_defaults?.backup || backupDestinations()[0][0]) +
    protectionMode(p ? p.mode || (p.shutdown ? "shutdown" : "stopped") : "live") +
    (settings ? check("Enable automatic backups", "enabled", p?.enabled || false) +
      `<div class="form-grid">${select("Frequency", "frequency", [["daily", "Daily"], ["weekly", "Weekly"]], p?.frequency || "daily")}${field("Time", "time", p?.time || "02:00", {type: "time"})}${select("Day (weekly only)", "weekday", ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((d,i) => [String(i),d]), String(p?.weekday ?? 6))}${field("Timezone", "timezone", p?.timezone || "Europe/London", {hint: "IANA name, for example Europe/London or UTC"})}${field("Scheduled backups to keep", "keep", p?.keep || 7, {type: "number", min: 1, max: 365, hint: "Pruned after a successful scheduled backup, for this VM and destination. Manual backups are retained."})}</div>` : "");
  modal((settings ? "Backup settings" : "Back up now") + ` · ${vmIds.length} VM${vmIds.length === 1 ? "" : "s"}`, content, async f => {
    const body = {vm_ids: vmIds, block_id: f.get("block_id"), mode: f.get("mode"), shutdown: f.get("mode") === "shutdown"};
    if (settings) {
      Object.assign(body, {enabled: f.has("enabled"), frequency: f.get("frequency"), time: f.get("time"), timezone: f.get("timezone"), weekday: +f.get("weekday"), keep: +f.get("keep")});
      await api("/backups/plans", "PUT", body);
      toast("Backup settings saved");
    } else {
      await queue("/backups", "POST", body);
    }
    await refresh(true);
  }, {wide: true, submit: settings ? "Save backup settings" : "Queue backups"});
}
function restoreBackup(id) {
  const b = backupRows().find(b => b.id === id);
  if (!b) return;
  modal("Restore VM backup", `<div class="warning">Creates a separate, stopped VM with independent disks and new network MAC addresses. ISO media is ejected and autostart is disabled. Guest hostnames, static IPs, application identities and automatic external mounts/credentials remain in the disks; review them before starting. Failed restores may leave partial resources for inspection.</div>` +
    field("Restored VM name", "name", "", {hint: "A unique name, up to 63 letters, digits, underscores or hyphens; start with a letter."}) +
    (b.vm_id.startsWith("incus:") ? select("VM disk storage", "pool", poolOptions(), defaultVMPool()) : `<div class="hint">Restored libvirt disks use application disk storage.</div>`),
    f => queue("/backups/" + id + "/restore", "POST", {name: f.get("name"), pool: f.get("pool") || ""}), {submit: "Restore as new VM", wide: true});
}

function renderStorage() {
  return (
    title(
      "Storage",
      "Local storage and reusable SMB / S3 sources for mounted drives and backups",
      can() ? btn("Add external source", "add-external-source", "", "primary", !!state.inventory.external_storage_error) + btn("Create storage block", "create-storage", "", "secondary", !!state.inventory.storage_error) + btn("Assign defaults", "storage-defaults") : "",
    ) +
    (state.inventory.storage_error ? `<div class="warning">${esc(state.inventory.storage_error)}</div>` : "") +
    renderExternalSources() +
    panel("Storage blocks", table(["Name", "Purpose", "Underlying pool", "Used / capacity", "Status", "Actions"], storageBlocks().map(b =>
      `<tr><td class="name">${esc(b.name)}${Object.values(state.inventory.storage_defaults || {}).includes(b.id) ? ' <small>Default destination</small>' : ""}</td><td>${esc(storagePurposes.find(p => p[0] === b.purpose)?.[1])}</td><td>${esc(b.parent)}</td><td>${size(b.used)} / ${size(b.capacity || b.size_gib * 1024 ** 3)}<small>${size(b.available)} available</small></td><td>${badge(b.status)}${b.error ? `<small>${esc(b.error)}</small>` : ""}</td><td>${can() ? smallAction("Expand", "grow-storage", b.id, b.status !== "ready") + smallAction("Delete", "delete-storage", b.id, b.status !== "ready") : "Read only"}</td></tr>`))) +
    `<div class="hint">Blocks reserve capacity on the underlying ZFS storage and enforce a hard limit. Create blocks from a ZFS pool; existing Btrfs pools remain available for VM disks. Defaults direct new writes. Existing disks and files stay in place. Backup blocks store full VM backups and configuration snapshots.</div>` +
    `<div class="storage-cards">${state.inventory.pools.filter(p => !p.name.startsWith("vmm-block-"))
      .map((p) => {
        const space = p.resources?.space || {};
        return panel(
          p.name,
          `<div class="storage-body">${badge(p.driver, "incus")}<div class="storage-value">${size(space.used, 0)} <span class="muted">/ ${size(space.total)}</span></div><div class="usage-bar"><i style="width:${space.total ? (space.used / space.total) * 100 : 0}%"></i></div></div>${properties(
            [
              ["Driver", esc(p.driver)],
              ["Status", esc(p.status || "Created")],
              ["Free", size((space.total || 0) - (space.used || 0))],
              ["References", String(p.used_by?.length || 0)],
            ],
          )}`,
        );
      })
      .join("")}</div>` +
    panel(
      "Application storage",
      properties([
        ["Available", size(state.inventory.host.disk_free)],
        ["Capacity", size(state.inventory.host.disk_total)],
        ["ISO images", "Persisted outside application releases"],
        ["Configuration backups", "Saved automatically before VM operations"],
      ]),
    ) +
    `<div class="hint">Choose an existing pool when creating a VM or adding a disk. Application deployment preserves VM storage and uploaded ISO files.</div>`
  );
}
function renderExternalSources() {
  const warning = state.inventory.external_storage_error ? `<div class="warning">${esc(state.inventory.external_storage_error)}</div>` : "";
  return warning + panel("External storage sources", table(["Name", "Type / location", "Connection", "VM attachments", "Actions"], externalSources().map(s => {
    const mounts = externalMounts().filter(m => m.source_id === s.id);
    return `<tr><td class="name">${esc(s.name)}${s.readonly ? "<small>Read only</small>" : ""}</td><td>${esc(s.kind.toUpperCase())}<small>${esc(externalLocation(s))}</small></td><td>${badge(s.status)}<small>${s.tested ? "Checked " + when(s.tested) : "Not tested"}</small>${s.error ? `<small>${esc(s.error)}</small>` : ""}</td><td>${mounts.map(m => `<a href="#vm/${encodeURIComponent(m.vm_id)}/hardware">${esc(m.vm_id.split(":")[1])}</a>`).join(", ") || "None"}</td><td><div class="actions">${can() ? smallAction("Test connection", "test-external-source", s.id) + smallAction("Edit", "edit-external-source", s.id, !!mounts.length) + smallAction("Remove", "delete-external-source", s.id, !!mounts.length) : "Read only"}</div></td></tr>`;
  }))) + `<div class="hint">Configure SMB shares or S3-compatible buckets, including Amazon S3 and Cloudflare R2. Reuse a source in multiple VMs through Add disk, or select a writable source for backups. Limits remain with the storage provider. Credentials are stored on the host and in root-only files inside attached guests.</div>`;
}
function renderExternalMounts(v) {
  const mounts = externalMounts(v.id);
  return panel("Mounted external storage", table(["Source", "Guest path", "Access", "Last result", "Actions"], mounts.map(m => `<tr><td class="name">${esc(externalSource(m.source_id)?.name || "Unavailable source")}</td><td>${esc(m.path)}</td><td>${m.readonly ? "Read only" : "Read / write"}</td><td>${badge(m.status)}<small>${m.checked ? "Last verified " + when(m.checked) : "Setup incomplete"}</small></td><td><div class="actions">${can() ? smallAction("Check mount", "test-external-mount", v.id + "|" + m.id, v.status !== "running") + (m.status !== "mounted" ? smallAction("Retry setup", "retry-external-mount", v.id + "|" + m.id, v.status !== "running") : "") + smallAction("Detach", "detach-external-mount", v.id + "|" + m.id, v.status !== "running") : "Read only"}</div></td></tr>`))) + `<div class="hint">Add mounted storage through Add disk. Checks and detach require a running Linux guest and its agent. A mounted result records the last successful check; use Check mount to verify current access. Remote files are shared independently of VM disks and snapshots.</div>`;
}
function externalSourceForm(source = null) {
  const value = (key, fallback = "") => source?.[key] ?? fallback;
  const dialog = modal(source ? "Edit external source" : "Add external source",
    `<div class="form-grid">${field("Source name", "name", value("name"))}${select("Type", "kind", [["smb", "SMB share"], ["s3", "S3-compatible bucket"]], value("kind", "smb"))}</div>` +
    `<div id="smb-source-fields" class="form-grid">${field("Server", "server", value("server"), {placeholder: "nas.example.local"})}${field("Share", "share", value("share"), {placeholder: "documents"})}${field("Username", "username", value("username"))}${field("Password", "password", "", {type: "password", required: !source, hint: source ? "Leave blank to retain the saved password." : ""})}${field("Domain (optional)", "domain", value("domain"), {required: false})}${field("Port", "port", value("port", 445), {type: "number", min: 1, max: 65535})}</div>` +
    `<div id="s3-source-fields" class="form-grid">${field("S3 endpoint", "endpoint", value("endpoint"), {type: "url", placeholder: "https://s3.eu-west-2.amazonaws.com"})}${field("Bucket", "bucket", value("bucket"))}${field("Access key", "access_key", "", {type: "password", required: !source, hint: source ? "Leave blank to retain the saved key." : ""})}${field("Secret key", "secret_key", "", {type: "password", required: !source, hint: source ? "Leave blank to retain the saved secret." : ""})}${field("Region", "region", value("region", "us-east-1"), {hint: "Use auto for Cloudflare R2."})}</div>` +
    field("Subfolder / prefix (optional)", "prefix", value("prefix"), {required: false, hint: "Relative path without a leading or trailing slash."}) + check("Read-only source", "readonly", value("readonly", false)) +
    `<div class="hint">Use credentials restricted to the selected share or bucket. Connection details cannot change while attached to a VM. Removing a source retains remote files.</div>`,
    f => {
      const body = {name: f.get("name"), kind: f.get("kind") || source.kind, prefix: f.get("prefix") || "", readonly: f.has("readonly")};
      const fields = body.kind === "smb" ? ["server", "share", "username", "password", "domain", "port"] : ["endpoint", "bucket", "access_key", "secret_key", "region"];
      for (const key of fields) body[key] = key === "port" ? +f.get(key) : f.get(key) || "";
      return queue("/storage/sources" + (source ? "/" + source.id : ""), source ? "PUT" : "POST", body);
    }, {wide: true, submit: "Save and test"});
  const choose = () => {
    const kind = $('[name="kind"]', dialog).value;
    for (const type of ["smb", "s3"]) {
      const section = $("#" + type + "-source-fields", dialog);
      section.hidden = kind !== type;
      $$("input", section).forEach(el => el.disabled = kind !== type);
    }
  };
  $('[name="kind"]', dialog).onchange = choose;
  if (source) $('[name="kind"]', dialog).disabled = true;
  choose();
}
function createStorageBlock() {
  const parents = state.inventory.pools.filter(p => p.driver === "zfs" && !p.name.startsWith("vmm-block-"));
  if (!parents.length) return toast("A ZFS storage pool is required to create capacity-limited blocks.", "warning");
  modal("Create storage block", `<div class="form-grid">${field("Block name", "name", "", { placeholder: "vm-storage" })}${select("Underlying storage", "parent", parents.map(p => [p.name, p.name]))}${select("Purpose", "purpose", storagePurposes)}${field("Capacity (GiB)", "size_gib", 100, { type: "number", min: 1, max: 65536 })}</div><div class="hint">Capacity is reserved from the same physical ZFS storage as the selected pool. The purpose is fixed when created. VM blocks appear in the storage selector when creating VMs or adding disks.</div>`,
    f => queue("/storage/blocks", "POST", { name: f.get("name"), parent: f.get("parent"), purpose: f.get("purpose"), size_gib: +f.get("size_gib") }), { submit: "Create block" });
}
function storageDefaultsForm() {
  modal("Assign default storage", `<div class="form-grid">${storagePurposes.map(([purpose, label]) => select(label, purpose, storageOptions(purpose), state.inventory.storage_defaults?.[purpose] || "")).join("")}</div><div class="hint">Applies to new VM selections, ISO uploads and VM configuration backups. Existing data stays in place. A full or unavailable destination reports an error.</div>`,
    async f => {
      for (const [purpose] of storagePurposes)
        await api("/storage/defaults/" + purpose, "PUT", { block_id: f.get(purpose) });
      await refresh(true);
    });
}
const roleDescriptions = [
  [
    "Administrator",
    "Full management of VMs, networks, ISO media and local accounts.",
    "All operations · User administration · Audit trail",
  ],
  [
    "Operator",
    "Create and manage virtual machines, networks and ISO media.",
    "VM lifecycle · Hardware · Console · Networks · ISO uploads",
  ],
  [
    "Viewer",
    "Read-only visibility across virtual machines and host resources.",
    "Inventory · Resource metrics · Task history · No console access",
  ],
];
function renderUsers() {
  if (!can("admin"))
    return empty(
      "Administrator access required",
      "Your role does not include user administration.",
    );
  return (
    title(
      "Access control",
      "Local users and server-enforced permissions",
      btn("+ Add user", "add-user", "", "primary"),
    ) +
    `<div class="access-columns">${panel(
      "Local accounts",
      table(
        ["User", "Role", "Status", "Actions"],
        state.users.map(
          (u) =>
            `<tr><td><span class="name">${esc(u.username)}</span>${u.username === state.user.username ? "<small>Your account</small>" : ""}</td><td>${badge(u.role)}</td><td>${badge(u.enabled ? "active" : "disabled")}</td><td>${smallAction("Edit", "edit-user", u.id)}</td></tr>`,
        ),
      ),
    )}<div>${roleDescriptions.map(([n, d, p]) => `<section class="panel role-card"><div class="panel-header"><h3>${n}</h3></div><div class="role-body">${d}<small>${p}</small></div></section>`).join("")}</div></div>`
  );
}
function taskRows(items) {
  return items.map(
    (t) =>
      `<tr class="clickable" data-action="task-detail" data-id="${t.id}"><td class="numeric">${when(t.started)}</td><td>${esc(t.username)}</td><td title="${esc(t.resource)}">${resourceName(t.resource)}</td><td>${esc(t.action.replaceAll(".", " → "))}</td><td>${badge(t.status)}</td></tr>`,
  );
}
function renderDock() {
  const active = state.tasks.filter((t) =>
    ["queued", "running"].includes(t.status),
  ).length;
  return `<div class="dock-tabs"><button class="active" data-action="tasks">Task log${active ? ` (${active} active)` : ""}</button>${can("admin") ? '<button data-action="audit">Audit trail</button>' : ""}<small>Live updates · <button data-action="tasks">View all ↗</button></small></div>${state.tasks.length ? table(["Time", "User", "Resource", "Operation", "Status"], taskRows(state.tasks.slice(0, 3))) : '<div class="empty">No management tasks yet. Your operations will appear here.</div>'}`;
}
function renderTasks() {
  let tasks = state.tasks.filter(
    (t) => state.taskFilter === "all" || t.status === state.taskFilter,
  );
  return (
    title(
      "Task history",
      "Management operations, completion status and failure details",
      btn("↻ Refresh", "refresh"),
    ) +
    `<div class="toolbar"><select id="task-filter" aria-label="Task status">${["all", "completed", "failed", "running", "queued", "interrupted"].map((v) => `<option value="${v}" ${v === state.taskFilter ? "selected" : ""}>${v === "all" ? "All tasks" : v}</option>`).join("")}</select><span class="result-count">Latest 100 operations</span></div>` +
    panel(
      "Task history",
      table(
        ["Time", "User", "Resource", "Operation", "Status"],
        taskRows(tasks),
      ),
    ) +
    `<p class="subtle" style="font-size:11px">Select a task to see its result. Interrupted operations require resource inspection before retrying.</p>`
  );
}
function renderAudit() {
  if (!can("admin"))
    return empty(
      "Administrator access required",
      "Audit records are restricted to administrators.",
    );
  return (
    title(
      "Audit trail",
      "Account activity and infrastructure operations",
      btn("↻ Refresh", "refresh"),
    ) +
    panel(
      "Latest 200 events",
      table(
        ["Time", "User", "Action", "Resource", "Detail"],
        state.audit.map(
          (a) =>
            `<tr><td class="numeric">${when(a.at)}</td><td>${esc(a.username)}</td><td>${esc(a.action)}</td><td class="vm-name-cell">${resourceName(a.resource)}</td><td>${esc(a.detail)}</td></tr>`,
        ),
      ),
    )
  );
}
function renderAccount() {
  return (
    title("Your account", "Local authentication and session settings") +
    panel(
      "Account",
      `<div class="storage-body"><div class="account-summary"><span class="avatar">${esc(state.user.username.slice(0, 2).toUpperCase())}</span><div><h2>${esc(state.user.username)}</h2>${badge(state.user.role)}</div></div><div class="actions">${btn("Change password", "change-password")}${btn("Sign out", "logout", "", "secondary")}</div></div>`,
    ) +
    panel(
      "Your permissions",
      `<div class="storage-body"><p class="muted">${roleDescriptions.find((r) => r[0].toLowerCase() === state.user.role)?.[1] || "Read-only access"}</p></div>`,
    )
  );
}
function syncVMSelection() {
  const all = $("#vm-select-all");
  if (all) all.indeterminate = selectedVMs.size > 0 && selectedVMs.size < visibleVMs().length;
}
function bindPage() {
  syncVMSelection();
  const resource = $("#resource-search");
  if (resource)
    resource.oninput = (e) => {
      state.search = e.target.value;
      $("#resource-tree").innerHTML = renderTree();
      if (state.page === "vms") updateVMTable();
    };
  const search = $("#vm-search");
  if (search)
    search.oninput = (e) => {
      state.search = e.target.value;
      resource.value = state.search;
      $("#resource-tree").innerHTML = renderTree();
      updateVMTable();
    };
  for (const [id, key] of [
    ["provider-filter", "provider"],
    ["status-filter", "status"],
    ["task-filter", "taskFilter"],
  ]) {
    const el = $("#" + id);
    if (el)
      el.onchange = (e) => {
        state[key] = e.target.value;
        if (state.page === "vms") updateVMTable();
        else render();
      };
  }
  for (const key of ["project", "tag"]) {
    const el = $(`[name="${key}-filter"]`);
    if (el)
      el.onchange = (event) => {
        state[key] = event.target.value;
        render();
      };
  }
  const input = $("#iso-input");
  if (input) input.onchange = (e) => uploadFiles([...e.target.files]);
  const zone = $("#dropzone");
  if (zone) {
    zone.onkeydown = (e) => {
      if (["Enter", " "].includes(e.key)) {
        e.preventDefault();
        input.click();
      }
    };
    zone.ondragover = (e) => {
      e.preventDefault();
      zone.classList.add("dragging");
    };
    zone.ondragleave = () => zone.classList.remove("dragging");
    zone.ondrop = (e) => {
      e.preventDefault();
      zone.classList.remove("dragging");
      uploadFiles([...e.dataTransfer.files]);
    };
  }
}
function updateVMTable() {
  const vms = filteredVMs();
  $("#vm-table").innerHTML = vmListContent(vms);
  syncVMSelection();
  $("#vm-result-count").textContent = vms.length + " virtual machines";
}
function createVM() {
  const pools = poolOptions(),
    networks = networkOptions();
  if (!pools.length || !networks.length) {
    toast(
      "A storage pool and an Incus network are required. Create a network first.",
      "error",
    );
    return;
  }
  const steps = ["General", "Hardware", "Network & media", "Create"];
  const panels = [
    `${field("VM name", "name", "", { placeholder: "my-temporary-vm", hint: "Unique name on this host. Start with a letter; use letters, digits, underscores or hyphens." })}${field("Hypervisor", "provider", "Incus", { readonly: true })}${textarea("Description (optional)", "description")}${check("Start with host", "autostart", false)}`,
    `${field("CPU cores", "cpu", 2, { type: "number", min: 1, max: 128 })}${field("Memory (GiB)", "memory_gib", 4, { type: "number", min: 0.5, max: 256, step: 0.5 })}${select("Storage pool", "pool", pools, defaultVMPool())}${field("Root disk (GiB)", "disk_gib", 32, { type: "number", min: 4, max: 4096 })}<div class="extra-disks"><label class="field"><span>Additional disks</span></label><div id="extra-disk-list"></div><button type="button" class="button small" id="extra-disk-add" style="margin-top:8px">+ Add another disk</button></div>${check("Secure Boot", "secure_boot", false)}`,
    `<div class="full">${select("Network", "network", networks, networks.some((n) => n[0] === "incusbr0") ? "incusbr0" : networks[0][0])}</div><div class="full">${select("Installation ISO", "iso_id", isoOptions(), "", "Optional. The VM boots from selected media until you eject it.")}</div>`,
    `<div class="full hint">Review the configuration, then create your stopped VM. Start it when you are ready to install the OS.</div><div class="full" id="vm-create-review"></div>`,
  ];
  const body = `<div class="wizard-layout"><nav class="wizard-rail" aria-label="VM creation steps">${steps.map((label, index) => `<button type="button" class="wizard-step" data-step="${index}" aria-controls="vm-create-step-${index}">${index + 1} &nbsp; ${label}</button>`).join("")}<small>Complete the configuration, then create your stopped VM. Start it when you are ready to install the OS.</small></nav><div class="wizard-form">${panels.map((content, index) => `<section id="vm-create-step-${index}" aria-labelledby="vm-create-heading-${index}" ${index ? "hidden" : ""}><div class="form-grid"><h3 class="form-section" id="vm-create-heading-${index}" tabindex="-1">${steps[index]}</h3>${content}</div></section>`).join("")}</div></div>`;
  const dialog = modal(
    "Create: Virtual machine",
    body,
    async (f) => {
      const spec = {
        name: f.get("name"),
        cpu: +f.get("cpu"),
        memory_gib: +f.get("memory_gib"),
        disk_gib: +f.get("disk_gib"),
        pool: f.get("pool"),
        network: f.get("network"),
        iso_id: f.get("iso_id"),
        secure_boot: f.has("secure_boot"),
        autostart: f.has("autostart"),
        description: f.get("description"),
        extra_disks: f.getAll("extra_disk").map(Number),
      };
      await queue("/vms", "POST", spec);
      location.hash = "#vms";
    },
    { wide: true, submit: "Create VM" },
  );
  const form = $("#dialog-form", dialog);
  const submit = $("button[type=submit]", form);
  const back = document.createElement("button");
  back.type = "button";
  back.className = "button secondary";
  back.textContent = "Back";
  submit.before(back);
  const name = form.elements.namedItem("name");
  name.pattern = String.raw`[a-zA-Z][a-zA-Z0-9_\-]{0,62}`;
  name.maxLength = 63;
  // Validate the relevant panels ourselves so hidden invalid fields can be shown.
  form.noValidate = true;
  let step = 0, submitting = false;
  const showStep = (index, focus = true) => {
    step = index;
    panels.forEach((_, i) => {
      $("#vm-create-step-" + i, dialog).hidden = i !== step;
      const button = $(`.wizard-step[data-step="${i}"]`, dialog);
      if (i === step) button.setAttribute("aria-current", "step");
      else button.removeAttribute("aria-current");
    });
    back.hidden = step === 0;
    submit.textContent = step === 3 ? "Create VM" : "Next";
    $(".form-error", dialog).hidden = true;
    if (step === 3) {
      const value = (key) => esc(form.elements.namedItem(key).value);
      const selected = (key) => esc(form.elements.namedItem(key).selectedOptions[0].textContent);
      const enabled = (key) => form.elements.namedItem(key).checked ? "Enabled" : "Disabled";
      const review = (title, entries) => `<h4>${title}</h4>${properties(entries)}`;
      $("#vm-create-review", dialog).innerHTML =
        review("General", [
          ["VM name", copyValue(form.elements.namedItem("name").value)], ["Hypervisor", "Incus"],
          ["Description", value("description") || "—"], ["Start with host", enabled("autostart")],
        ]) + review("Hardware", [
          ["CPU cores", value("cpu")], ["Memory", value("memory_gib") + " GiB"],
          ["Storage pool", selected("pool")], ["Root disk", value("disk_gib") + " GiB"],
          ["Additional disks", new FormData(form).getAll("extra_disk").map(v => esc(v) + " GiB").join(", ") || "None"],
          ["Secure Boot", enabled("secure_boot")],
        ]) + review("Network & media", [["Network", selected("network")], ["Installation ISO", selected("iso_id")]]);
    }
    if (focus) $("#vm-create-heading-" + step, dialog).focus();
    $(".dialog-content", dialog).scrollTop = 0;
  };
  const validateThrough = (last) => {
    for (let i = 0; i <= last; i++) {
      const invalid = $$("input, select, textarea", $("#vm-create-step-" + i, dialog))
        .find(input => !input.checkValidity());
      if (invalid) {
        showStep(i);
        invalid.reportValidity();
        return false;
      }
    }
    return true;
  };
  $$(".wizard-step", dialog).forEach(button => {
    button.onclick = () => {
      if (submitting) return;
      const target = +button.dataset.step;
      if (target <= step || validateThrough(target - 1)) showStep(target);
    };
  });
  back.onclick = () => { if (!submitting) showStep(step - 1); };
  const create = form.onsubmit;
  form.onsubmit = async event => {
    event.preventDefault();
    if (submitting || !validateThrough(step === 3 ? 2 : step)) return;
    if (step < 3) return showStep(step + 1);
    submitting = true;
    back.disabled = true;
    $$(".wizard-step", dialog).forEach(button => button.disabled = true);
    try {
      await create(event);
    } finally {
      submitting = false;
      back.disabled = false;
      $$(".wizard-step", dialog).forEach(button => button.disabled = false);
    }
  };
  showStep(0, false);
  $("#extra-disk-add", dialog).onclick = () => {
    if ($$(".extra-disk-row", dialog).length >= 8) {
      toast(
        "A maximum of eight additional disks can be created at once.",
        "warning",
      );
      return;
    }
    const row = document.createElement("div");
    row.className = "extra-disk-row";
    row.innerHTML =
      '<input name="extra_disk" type="number" value="16" min="1" max="4096" required aria-label="Additional disk size in GiB"><small>GiB · Same storage pool</small><button type="button" class="icon-button" aria-label="Remove disk">×</button>';
    $("button", row).onclick = () => row.remove();
    $("#extra-disk-list", dialog).append(row);
  };
}
function editVM(v) {
  const planned = [...(v.pending || [])].reverse().find(p => p.kind === "resources")?.body;
  const current = v;
  if (planned) v = {...v, cpu: String(planned.cpu), memory: planned.memory_gib * 1024 ** 3,
    description: planned.description, config: {...v.config, "security.secureboot": String(planned.secure_boot), "boot.autostart": String(planned.autostart)}};
  modal(
    (current.status === "running" ? "Plan resources · " : "Edit resources · ") + v.name,
    `<div class="form-grid">${field("CPU cores", "cpu", /^\d+$/.test(v.cpu) ? v.cpu : 2, { type: "number", min: 1, max: 128 })}${field("Memory (GiB)", "memory_gib", Math.max(0.5, v.memory / 1024 ** 3 || 4), { type: "number", min: 0.5, max: 256, step: 0.5 })}${v.provider === "incus" ? check("Secure Boot", "secure_boot", v.config["security.secureboot"] !== "false") + check("Start with host", "autostart", v.config["boot.autostart"] === "true") : ""}${textarea("Description", "description", v.description)}</div>`,
    async (f) =>
      queue(ref(v.id), "PUT", {
        cpu: +f.get("cpu"),
        memory_gib: +f.get("memory_gib"),
        description: f.get("description"),
        secure_boot: f.has("secure_boot"),
        autostart: f.has("autostart"),
      }),
    { copyTitle: v.name },
  );
}
function addDisk(v) {
  const dialog = modal(
    "Add disk · " + v.name,
    select("Storage source", "source_id", [["", "Local virtual disk"], ...externalSources().map(s => [s.id, `${s.name} · ${s.kind.toUpperCase()}`])]) +
    `<div id="local-drive-fields"><div class="form-grid">${field("Disk size (GiB)", "size_gib", 16, { type: "number", min: 1, max: 4096 })}${v.provider === "incus" ? select("Storage pool", "pool", poolOptions(), defaultVMPool()) : field("Storage", "storage", "Application disk storage", { readonly: true })}</div><div class="hint">A new blank disk will be attached. Partition and format it inside the guest.</div></div>` +
    `<div id="external-drive-fields" hidden>${field("Mount path inside Linux", "path", "/mnt/shared", {hint: "An empty folder below /mnt or /media. Existing files are never covered."})}${check("Read-only access", "readonly")}<div class="hint">VM Manager installs the mount tools and enables automatic mounting after reboot. The Linux VM must be running with its guest agent, Python 3 and systemd.${v.provider === "incus" ? " Use Install Incus agent on the VM Summary or Hardware tab for first-time setup." : ""} Mounted data is separate from VM disk backups.</div><div id="external-drive-note" class="hint"></div></div>`,
    async f => {
      if (f.get("source_id")) {
        if (v.status !== "running") throw new Error("Start the Linux VM before adding mounted storage.");
        return queue(ref(v.id) + "/mounts", "POST", { source_id: f.get("source_id"), path: f.get("path"), readonly: f.has("readonly") });
      }
      return queue(ref(v.id) + "/disks", "POST", {size_gib: +f.get("size_gib"), pool: f.get("pool") || "hdd-pool"});
    },
    { submit: "Add drive", copyTitle: v.name },
  );
  const choose = () => {
    const source = externalSource($('[name="source_id"]', dialog).value);
    for (const [selector, active] of [["#local-drive-fields", !source], ["#external-drive-fields", !!source]]) {
      const section = $(selector, dialog);
      section.hidden = !active;
      $$("input, select", section).forEach(el => el.disabled = !active);
    }
    if (source) {
      $('[name="path"]', dialog).value = "/mnt/" + source.name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
      $('[name="readonly"]', dialog).checked = source.readonly;
      $('[name="readonly"]', dialog).disabled = source.readonly;
      $("#external-drive-note", dialog).textContent = source.kind === "s3" ? "S3 uses a local cache and background uploads. Avoid concurrent editing of the same file across VMs. Cache settings do not impose a remote quota." : "The same share can be attached to multiple VMs. Permissions and any quotas are enforced by the SMB server.";
    }
  };
  $('[name="source_id"]', dialog).onchange = choose;
  choose();
}

function editNIC(v, device = "") {
  modal(
    (device ? "Edit network adapter" : "Add network adapter") + " · " + v.name,
    select(
      "Network",
      "network",
      networkOptions(),
      v.devices[device]?.network || "incusbr0",
      "The adapter connects to an Incus-managed virtual network. Configure guest addressing manually if DHCP is disabled.",
    ),
    async (f) =>
      queue(ref(v.id) + "/nics", "POST", { network: f.get("network"), device }),
    { submit: device ? "Save network" : "Add adapter", copyTitle: v.name },
  );
}
function editCDROM(v) {
  modal(
    "CD/DVD drive · " + v.name,
    select(
      "Installation media",
      "iso_id",
      isoOptions(v),
      "",
      "Select no media to eject existing ISO images.",
    ) +
      `<div style="margin-top:18px">${check("Boot from this ISO first", "boot", true)}</div><div class="hint" style="margin-top:18px">After installation, shut down the VM and eject the ISO to boot from its disk.</div>`,
    async (f) =>
      queue(ref(v.id) + "/cdrom", "PUT", {
        iso_id: f.get("iso_id"),
        boot: f.has("boot"),
      }),
    { submit: "Apply media", copyTitle: v.name },
  );
}
function networkForm(net = null) {
  const conf = net?.config || {};
  modal(
    net ? "Edit network · " + net.name : "Create network",
    `<div class="form-grid">${field("Network name", "name", net?.name || "", { placeholder: "temporary-lab", max: 15, readonly: !!net })}${select(
      "Connectivity",
      "mode",
      [
        ["nat", "NAT · Outbound access"],
        ["isolated", "Isolated · Guest network only"],
      ],
      !net || conf["ipv4.nat"] === "true" ? "nat" : "isolated",
    )}<div class="full">${field("IPv4 gateway / prefix", "cidr", conf["ipv4.address"] || "auto", { hint: "Use auto for a free subnet, or enter a gateway such as 10.200.10.1/24." })}</div>${check("Enable DHCP", "dhcp", conf["ipv4.dhcp"] !== "false", "Automatically assign guest IP addresses.")}${textarea("Description", "description", net?.description || "")}</div><div class="hint" style="margin-top:18px">The bridge, DHCP and routing are configured by the app. Existing host and VM subnets are checked for overlaps.</div>`,
    async (f) =>
      queue(
        net ? "/networks/" + encodeURIComponent(net.name) : "/networks",
        net ? "PUT" : "POST",
        {
          name: f.get("name"),
          cidr: f.get("cidr"),
          nat: f.get("mode") === "nat",
          dhcp: f.has("dhcp"),
          description: f.get("description"),
        },
      ),
    { submit: net ? "Save network" : "Create network" },
  );
}
function userForm(user = null) {
  const roles = [
    ["administrator", "Administrator"],
    ["operator", "Operator"],
    ["viewer", "Viewer"],
  ];
  modal(
    user ? "Edit account · " + user.username : "Add local account",
    `<div class="form-grid">${field("Username", "username", user?.username || "", { readonly: !!user })}${select("Role", "role", roles, user?.role || "viewer")}<div class="full">${field(user ? "New password (optional)" : "Password", "password", "", { type: "password", required: !user, hint: "At least 12 characters. " + (user ? "Leave blank to keep the existing password." : "Provide this password securely to the user.") })}</div>${user ? check("Account enabled", "enabled", !!user.enabled) : ""}</div>`,
    async (f) => {
      await api(
        user ? "/users/" + user.id : "/users",
        user ? "PUT" : "POST",
        user
          ? {
              role: f.get("role"),
              password: f.get("password"),
              enabled: f.has("enabled"),
            }
          : {
              username: f.get("username"),
              password: f.get("password"),
              role: f.get("role"),
            },
      );
      toast("Account saved");
      state.users = await api("/users");
      render();
    },
    { submit: user ? "Save account" : "Create account" },
  );
}
let uploading = false;
async function uploadFiles(files) {
  if (uploading) {
    toast("An upload is already in progress.", "warning");
    return;
  }
  if (!files.length) return;
  uploading = true;
  const zone = $("#dropzone");
  const storageId = $('[name="iso-storage"]')?.value || "";
  try {
    for (const file of files) {
      if (!file.name.toLowerCase().endsWith(".iso"))
        throw new Error(file.name + ": choose an .iso file");
      if (file.size > 32 * 1024 ** 3)
        throw new Error("Each ISO must be smaller than 32 GiB");
      const form = new FormData();
      form.append("file", file);
      form.append("storage_id", storageId || "application");
      if ($("#upload-progress")) $("#upload-progress").hidden = false;
      if ($("#upload-label"))
        $("#upload-label").textContent = "Uploading " + file.name;
      await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/isos");
        xhr.setRequestHeader("X-CSRF-Token", state.user.csrf);
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) {
            const p = Math.round((e.loaded / e.total) * 100);
            const bar = $("#upload-bar");
            const label = $("#upload-label");
            if (bar) bar.value = p;
            if (label)
              label.textContent =
                p === 100
                  ? "Validating filesystem and calculating checksum…"
                  : `${file.name} · ${p}%`;
          }
        };
        xhr.onerror = () => reject(new Error("Upload connection failed"));
        xhr.onload = () => {
          let result;
          try {
            result = JSON.parse(xhr.responseText);
          } catch {
            result = { detail: "Upload failed" };
          }
          if (xhr.status >= 200 && xhr.status < 300) resolve(result);
          else reject(new Error(result.detail || "Upload failed"));
        };
        xhr.send(form);
      });
      toast(file.name + " uploaded and verified");
    }
  } catch (e) {
    toast(e.message, "error");
  } finally {
    uploading = false;
    if (zone?.isConnected && $("#upload-progress"))
      $("#upload-progress").hidden = true;
    await refresh(true);
  }
}
async function refresh(force = false) {
  if (busy || !state.user) return;
  busy = true;
  try {
    state.inventory = await api("/inventory");
    normalizeOrganizationFilters();
    if (state.page === "vm" && state.tab === "snapshots") await refreshSnapshots(state.vm, false);
    if (state.page === "vm" && state.tab === "summary") refreshVMMetrics();
    if (can("admin") && state.page === "users")
      state.users = await api("/users");
    if (can("admin") && state.page === "audit")
      state.audit = await api("/audit");
    if (
      force ||
      (!$("#modal").open &&
        !uploading &&
        !["INPUT", "SELECT", "TEXTAREA"].includes(
          document.activeElement?.tagName,
        ) &&
        // Keep a live console connected during ordinary polling, but redraw
        // after power changes (or deletion) so its badge and actions stay current.
        (!(state.page === "vm" && state.tab === "console") ||
          currentVM()?.status !== renderedVMStatus))
    )
      render();
  } catch (e) {
    if (state.user) toast(e.message, "error");
  } finally {
    busy = false;
  }
}
async function refreshTasks() {
  if (taskBusy || !state.user) return;
  taskBusy = true;
  try {
    state.tasks = await api("/tasks");
    let finished = false;
    for (const t of state.tasks) {
      const previous = seenTasks.get(t.id);
      if (
        previous &&
        ["queued", "running"].includes(previous) &&
        ["completed", "failed", "interrupted"].includes(t.status)
      ) {
        toast(
          `${t.resource}: ${t.status === "completed" && t.message === "Completed successfully" ? "completed" : t.message}`,
          t.status === "completed" ? "success" : "error",
        );
        finished = true;
      }
      seenTasks.set(t.id, t.status);
    }
    if ($("#task-dock")) $("#task-dock").innerHTML = renderDock();
    if (state.page === "tasks" && !$("#modal").open)
      $("#main-content").innerHTML = renderTasks();
    if (finished) await refresh();
  } catch (e) {
    if (state.user) console.warn(e.message);
  } finally {
    taskBusy = false;
  }
}
async function action(name, id) {
  const v = currentVM(id.split("|")[0]);
  switch (name) {
    case "install-agent": installAgent(v); break;
    case "refresh-snapshots":
      await refreshSnapshots(id);
      break;
    case "create-snapshot":
      createSnapshot(v);
      break;
    case "restore-snapshot": {
      const snapshot = id.split("|")[1];
      confirmAction("Restore snapshot · " + snapshot, v.name,
        "This replaces the VM's current disk contents and configuration with the selected snapshot. Changes since that snapshot will be lost. The VM must be stopped and will remain stopped. ZFS requires newer snapshots to be deleted first.",
        () => queue(ref(v.id) + "/snapshots/" + encodeURIComponent(snapshot) + "/restore", "POST", { confirm_name: v.name }));
      break;
    }
    case "delete-snapshot": {
      const snapshot = id.split("|")[1];
      confirmAction("Delete snapshot", snapshot, "This permanently removes this restore point from all included disks. The current VM disk contents are retained. Delete child snapshots first.",
        () => queue(ref(v.id) + "/snapshots/" + encodeURIComponent(snapshot), "DELETE", { confirm_name: snapshot }));
      break;
    }
    case "templates": location.hash = "templates"; break;
    case "capture-template": captureTemplate(id); break;
    case "use-template": useTemplate(id); break;
    case "delete-template": {
      const t = templateRows().find(t => t.id === id);
      if (!t) return;
      confirmAction("Delete VM template", t.name, "Permanently delete this template and its archive? The source VM and VMs created from this template are retained.", () => queue("/templates/" + id, "DELETE", {confirm_name: t.name}));
      break;
    }
    case "backup-now": backupForm([id]); break;
    case "backup-settings": backupForm([id], true); break;
    case "backup-selected": backupForm([...selectedBackupVMs]); break;
    case "backup-settings-selected": backupForm([...selectedBackupVMs], true); break;
    case "restore-backup": restoreBackup(id); break;
    case "delete-backup": {
      const b = backupRows().find(b => b.id === id), sourceName = b.vm_id.split(":")[1];
      confirmAction("Delete VM backup", sourceName, "Permanently delete this restore point and its files? The source VM is retained.", () => queue("/backups/" + id, "DELETE", {confirm_name: sourceName}));
      break;
    }
    case "remove-backup-settings":
      confirmAction("Remove backup settings", id, "Disable the schedule and remove these settings. Existing restore points are retained.", async () => { await api("/backups/plans/" + encodeURIComponent(id), "DELETE"); await refresh(true); });
      break;
    case "add-external-source": externalSourceForm(); break;
    case "edit-external-source": externalSourceForm(externalSource(id)); break;
    case "test-external-source": await queue("/storage/sources/" + id + "/test", "POST", {}); break;
    case "delete-external-source": {
      const source = externalSource(id);
      confirmAction("Remove external source", source.name, "Remove this connection from VM Manager? Remote files are retained. VM attachments and backup references must be removed first.", () => queue("/storage/sources/" + id, "DELETE", {confirm_name: source.name}));
      break;
    }
    case "test-external-mount": {
      const [vmId, mountId] = id.split("|");
      await queue(ref(vmId) + "/mounts/" + mountId + "/test", "POST", {});
      break;
    }
    case "retry-external-mount": {
      const [vmId, mountId] = id.split("|");
      const mount = externalMounts(vmId).find(m => m.id === mountId);
      await queue(ref(vmId) + "/mounts", "POST", {source_id: mount.source_id, path: mount.path, readonly: mount.readonly});
      break;
    }
    case "detach-external-mount": {
      const [vmId, mountId] = id.split("|");
      const mount = externalMounts(vmId).find(m => m.id === mountId);
      confirmAction("Detach mounted storage", mount.path, "Unmount this storage and disable automatic mounting in the guest. Remote files are retained. Close files first; pending S3 uploads prevent detachment.", () => queue(ref(vmId) + "/mounts/" + mountId, "DELETE", {confirm_name: mountId}));
      break;
    }
    case "create-storage":
      createStorageBlock();
      break;
    case "storage-defaults":
      storageDefaultsForm();
      break;
    case "grow-storage": {
      const b = storageBlocks().find(b => b.id === id);
      modal("Expand storage · " + b.name, field("Capacity (GiB)", "size_gib", b.size_gib + 1, { type: "number", min: b.size_gib + 1, max: 65536 }),
        f => queue("/storage/blocks/" + id, "PUT", { size_gib: +f.get("size_gib") }), { submit: "Expand block" });
      break;
    }
    case "delete-storage": {
      const b = storageBlocks().find(b => b.id === id);
      confirmAction("Delete empty storage block", b.name, "Only empty blocks can be deleted. Change any default assignment first. Files, VM volumes and pending hardware references prevent deletion.",
        () => queue("/storage/blocks/" + id, "DELETE", { confirm_name: b.name }));
      break;
    }
    case "discard-pending":
      confirmAction("Discard pending hardware", v.name, "Discard all unapplied hardware changes?", async () => queue(ref(v.id) + "/pending", "DELETE"));
      break;
    case "clear-filters":
      state.project = state.tag = "all";
      render();
      break;
    case "view-project":
      state.project = id;
      state.tag = "all";
      location.hash = "#resources";
      render();
      break;
    case "view-tag":
      state.tag = id;
      state.project = "all";
      location.hash = "#resources";
      render();
      break;
    case "create-project":
      collectionForm("projects");
      break;
    case "create-tag":
      collectionForm("tags");
      break;
    case "rename-project":
      collectionForm("projects", id);
      break;
    case "rename-tag":
      collectionForm("tags", id);
      break;
    case "delete-project":
      deleteCollection("projects", id);
      break;
    case "delete-tag":
      deleteCollection("tags", id);
      break;
    case "organize-vm":
      organizeResource("vm", v);
      break;
    case "organize-network":
      organizeResource(
        "network",
        state.inventory.networks.find((n) => n.id === id),
      );
      break;
    case "toggle-sidebar":
      $(".app-shell").classList.toggle("sidebar-open");
      break;
    case "account":
      location.hash = "#account";
      break;
    case "tasks":
      location.hash = "#tasks";
      break;
    case "audit":
      location.hash = "#audit";
      break;
    case "refresh":
      await refreshTasks();
      await refresh(true);
      break;
    case "create-vm":
      state.inventory = await api("/inventory");
    if (state.page === "vm" && state.tab === "summary") refreshVMMetrics();
      createVM();
      break;
    case "open-vm":
      location.hash = "#vm/" + encodeURIComponent(id) + "/summary";
      break;
    case "vm-tab":
      location.hash = "#vm/" + encodeURIComponent(state.vm) + "/" + id;
      break;
    case "console":
      location.hash = "#vm/" + encodeURIComponent(id) + "/console";
      break;
    case "console-mode":
      switchEmbeddedConsole(id);
      break;
    case "console-fullscreen":
      await toggleConsoleFullscreen();
      break;
    case "edit-vm":
      editVM(v);
      break;
    case "add-disk":
      addDisk(v);
      break;
    case "add-nic":
      editNIC(v);
      break;
    case "edit-nic":
      editNIC(v, id.split("|")[1]);
      break;
    case "cdrom":
      editCDROM(v);
      break;
    case "toggle-host-tree": {
      hostTreeCollapsed = !hostTreeCollapsed;
      const button = $(".tree-host-toggle");
      button.setAttribute("aria-expanded", String(!hostTreeCollapsed));
      button.setAttribute("aria-label", `${hostTreeCollapsed ? "Expand" : "Collapse"} ${state.inventory.host.name} resources`);
      button.firstElementChild.textContent = hostTreeCollapsed ? "▸" : "▾";
      $("#host-tree-resources").hidden = hostTreeCollapsed;
      break;
    }
    case "toggle-vm-project":
      if (collapsedVMProjects.has(id)) collapsedVMProjects.delete(id);
      else collapsedVMProjects.add(id);
      updateVMTable();
      $$(".vm-project-toggle").find(button => button.dataset.id === id)?.focus();
      break;
    case "clear-vm-selection":
      selectedVMs.clear();
      updateVMTable();
      break;
    case "start":
    case "shutdown":
    case "restart":
      await queue(ref(id) + "/action", "POST", { action: name });
      break;
    case "force-stop":
      confirmAction(
        "Force stop",
        v.name,
        "The VM will power off without waiting for the guest, even at a boot menu. Earlier queued power requests are cancelled. Unsaved guest data may be lost.",
        () =>
          queue(ref(v.id) + "/action", "POST", {
            action: "stop",
            confirm_name: v.name,
          }),
      );
      break;
    case "delete-vm":
      confirmAction(
        "Delete virtual machine",
        v.name,
        v.provider === "incus"
          ? "This permanently removes the VM, its root disk and attached extra disks created by the app. Detached volumes are retained."
          : "This removes the libvirt VM definition. Existing disk files are retained.",
        () => queue(ref(v.id), "DELETE", { confirm_name: v.name }),
      );
      break;
    case "resize-disk": {
      const device = id.split("|")[1];
      modal(
        "Expand disk · " + device,
        field("New size (GiB)", "size_gib", "", {
          type: "number",
          min: 1,
          max: 4096,
          hint: "Expansion only. Enter a size larger than the current disk.",
        }),
        (f) =>
          queue(ref(v.id) + "/disks/" + encodeURIComponent(device), "PUT", {
            size_gib: +f.get("size_gib"),
          }),
        { submit: "Expand disk" },
      );
      break;
    }
    case "detach-device": {
      const device = id.split("|")[1];
      confirmAction(
        "Detach device",
        device,
        "Detach this device from the VM. Disk data is retained in storage.",
        () =>
          queue(
            ref(v.id) + "/devices/" + encodeURIComponent(device),
            "DELETE",
            { confirm_name: device },
          ),
      );
      break;
    }
    case "create-network":
      networkForm();
      break;
    case "network-detail":
      selectedNetwork = id;
      location.hash = "#networks";
      render();
      break;
    case "edit-network":
      networkForm(state.inventory.networks.find((n) => n.id === id));
      break;
    case "delete-network": {
      const n = state.inventory.networks.find((n) => n.id === id);
      confirmAction(
        "Delete network",
        n.name,
        "Remove this virtual network and its DHCP configuration. The network must not be in use.",
        () =>
          queue("/networks/" + encodeURIComponent(n.name), "DELETE", {
            confirm_name: n.name,
          }),
      );
      break;
    }
    case "delete-iso": {
      const i = state.inventory.isos.find((i) => i.id === id);
      confirmAction(
        "Delete ISO",
        i.name,
        "Permanently remove this installation image. It must be ejected from all VMs first.",
        () =>
          queue("/isos/" + encodeURIComponent(i.id), "DELETE", {
            confirm_name: i.name,
          }),
      );
      break;
    }
    case "add-user":
      userForm();
      break;
    case "edit-user":
      userForm(state.users.find((u) => String(u.id) === id));
      break;
    case "change-password":
      modal(
        "Change your password",
        `<div class="form-grid"><div class="full">${field("Current password", "current", "", { type: "password" })}</div><div class="full">${field("New password", "password", "", { type: "password", hint: "At least 12 characters. Other sessions will be signed out." })}</div><div class="full">${field("Confirm new password", "confirm", "", { type: "password" })}</div></div>`,
        async (f) => {
          if (f.get("password") !== f.get("confirm"))
            throw new Error("New passwords do not match");
          await api("/password", "PUT", {
            current: f.get("current"),
            password: f.get("password"),
          });
          toast("Password changed");
        },
      );
      break;
    case "logout":
      await api("/logout", "POST");
      state.user = null;
      state.inventory = null;
      renderLogin();
      break;
    case "task-detail": {
      const t = state.tasks.find((t) => t.id === id);
      if (!t) break;
      const renderDetails = t => properties([
        ["Operation", esc(t.action)],
        ["Resource", resourceName(t.resource)],
        ["User", esc(t.username)],
        ["Started", when(t.started)],
        ["Finished", t.finished ? when(t.finished) : "In progress"],
        ["Status", badge(t.status)],
        ["Result", esc(t.message || (t.status === "queued" ? "Waiting for worker / resource" : "Waiting for the host"))],
      ]);
      const dialog = modal(
        "Task details",
        `<div class="task-detail-properties">${renderDetails(t)}</div><p class="muted task-status-note" role="status">Updating status…</p>
        <details class="task-output"><summary>Host output</summary>
        <p class="muted task-output-status" role="status">Expand to connect. Output stops streaming when collapsed.</p>
        <pre class="task-output-text" tabindex="0" aria-label="Host output"></pre></details>`,
        async () => {},
        { submit: "Close", wide: true },
      );
      watchTaskDetail(dialog, t, renderDetails);
      $("button[type=submit]", dialog).onclick = (e) => {
        e.preventDefault();
        dialog.close();
      };
      break;
    }
    case "help": {
      const dialog = modal(
        "Using VM Manager",
        '<div class="hint">Create a network, upload an OS ISO, then create a VM with the desired CPU, memory and disks. Start the VM and open Console to install the OS. Shut down and eject its ISO after installation.</div><p class="muted">Hardware changes can be saved while running and applied with Restart in VM Manager, or the next Start after shutdown. NAT networks provide outbound access; isolated networks keep guest traffic local. Every management operation appears in the task log. Select a task to inspect its result.</p>',
        async () => {},
        { submit: "Got it" },
      );
      $("button[type=submit]", dialog).onclick = (e) => {
        e.preventDefault();
        dialog.close();
      };
      break;
    }
  }
}
bindCopyButtons();
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  if (button.tagName === "TR" && event.target.closest("a,button,input,select,label")) return;
  event.preventDefault();
  action(button.dataset.action, button.dataset.id || "").catch((e) =>
    toast(e.message, "error"),
  );
});
window.addEventListener("hashchange", async () => {
  navigate();
  if (["users", "audit"].includes(state.page)) await refresh(true);
});
window.addEventListener("session-expired", () => {
  state.user = null;
  selectedVMs.clear();
  state.inventory = null;
  $("#modal").close();
  renderLogin("Your session expired. Sign in to continue.");
});
window.addEventListener("refresh-tasks", () => refreshTasks());
async function init() {
  bindEmbeddedConsoleEvents();
  try {
    state.user = await api("/me");
    navigate();
    await refresh(true);
    await refreshTasks();
  } catch {
    renderLogin();
  }
}
setInterval(() => {
  if (!document.hidden) refreshTasks();
}, 2000);
setInterval(() => {
  if (!document.hidden) refresh();
}, 10000);
setInterval(() => { if (!document.hidden) refreshVMMetrics(); }, 5000);
setInterval(() => { if (!document.hidden && state.user) refreshMonitoring(); }, 10000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.user) refreshMonitoring(); });
init();

 document.addEventListener("change", event => {
  if (event.target.matches(".vm-select")) {
    if (event.target.checked) selectedVMs.add(event.target.value);
    else selectedVMs.delete(event.target.value);
    updateVMTable();
  }
  if (event.target.id === "vm-select-all") {
    visibleVMs().forEach(v => {
      if (event.target.checked) selectedVMs.add(v.id); else selectedVMs.delete(v.id);
    });
    updateVMTable();
  }
  if (event.target.id === "bulk-vm-action" && event.target.value) {
    const operation = event.target.value;
    event.target.value = "";
    bulkVMAction(operation);
  }
  if (event.target.matches(".backup-vm-select")) {
    if (event.target.checked) selectedBackupVMs.add(event.target.value);
    else selectedBackupVMs.delete(event.target.value);
  }
  if (event.target.id === "backup-select-all") {
    $$(".backup-vm-select").forEach(input => {
      input.checked = event.target.checked;
      if (input.checked) selectedBackupVMs.add(input.value); else selectedBackupVMs.delete(input.value);
    });
  }
 });
