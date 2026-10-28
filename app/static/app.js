import {
  state,
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
const pageNames = {
  overview: "Overview",
  vms: "Virtual machines",
  networks: "Networks",
  organization: "Projects & tags",
  resources: "Resource view",
  isos: "ISO library",
  storage: "Storage",
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
  ["users", "♧", "Access control"],
];
let busy = false,
  taskBusy = false,
  selectedNetwork = null,
  renderedPath = "";
const seenTasks = new Map();
let vmMetrics = null, metricsBusy = false;
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
const poolOptions = () =>
  state.inventory.pools.map((p) => [p.name, `${p.name} · ${p.driver}`]);
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
function organizationChips(resource) {
  const project = state.inventory.projects.find(
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
    `<main class="login-screen"><form class="login-card" id="login-form"><div class="login-brand">▣ CARRTECH</div><span class="product">VM MANAGER</span><h1>Sign in to your infrastructure</h1><p class="host-address">ctlnxdbn001 · 192.168.8.235</p>${field("Username", "username", "", { placeholder: "Your username" })}${field("Password", "password", "", { type: "password", placeholder: "Your password" })}<div class="form-error" role="alert" ${message ? "" : "hidden"}>${esc(message)}</div><button class="button primary" type="submit">Sign in</button><p class="login-foot">Local accounts · Session protected</p></form></main>`;
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
      )}</nav><div class="nav-heading">RESOURCE TREE</div><div class="resource-tree" id="resource-tree">${renderTree()}</div><div class="host-online"><strong>● HOST CONNECTED</strong><small>${esc(inv.host.address)}</small></div></aside><section class="workspace"><header class="topbar"><button class="icon-button mobile-menu" data-action="toggle-sidebar" aria-label="Toggle navigation">☰</button><div class="breadcrumb"><a href="#overview">Datacenter</a><span>/</span><span>${esc(inv.host.name)}</span><span>/</span><strong>${esc(state.page === "vm" ? vm?.name || "VM" : pageNames[state.page] || "Overview")}</strong></div>${can() ? btn("+ Create VM", "create-vm", "", "primary") : ""}<button class="icon-button" data-action="help" aria-label="Help">?</button><button class="account-button" data-action="account" aria-label="Your account"><span class="avatar">${esc(state.user.username.slice(0, 2).toUpperCase())}</span><span class="account-name">${esc(state.user.username)}</span><span class="role-label">${esc(state.user.role)}</span></button></header><main class="content" id="main-content">${inv.warnings.length ? `<div class="error-banner"><strong>Some host information is unavailable.</strong><br>${inv.warnings.map(esc).join("<br>")}</div>` : ""}${renderOrganizationFilters()}${renderPage()}</main><section class="task-dock" aria-label="Recent tasks" id="task-dock">${renderDock()}</section></section></div>`;
  bindPage();
  if ($("#resource-tree")) $("#resource-tree").scrollTop = treeScroll;
  if ($("#main-content")) $("#main-content").scrollTop = contentScroll;
}
function renderTree() {
  return `<a class="tree-host" href="#overview">⌄ &nbsp;${esc(state.inventory.host.name)}</a>${[
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
      return `<details class="tree-group" open><summary>${provider === "incus" ? "Incus" : "libvirt"} <span class="subtle">(${vms.length})</span></summary>${vms.map((v) => `<a class="tree-vm ${state.page === "vm" && state.vm === v.id ? "active" : ""}" href="#vm/${encodeURIComponent(v.id)}/summary" title="${esc(v.name)}"><span class="dot ${v.status}"></span><span>${esc(v.name)}</span></a>`).join("")}${!vms.length ? '<div class="tree-vm subtle">No matching VMs</div>' : ""}</details>`;
    })
    .join("")}`;
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
  const total = pools.reduce((n, p) => n + (p.resources?.space?.total || 0), 0);
  const used = pools.reduce((n, p) => n + (p.resources?.space?.used || 0), 0);
  return (
    title(
      "Overview",
      "Host metrics and virtual machines in the current resource view",
      btn("↻ Refresh", "refresh"),
    ) +
    `<div class="resource-cards">${stat("CPU usage", host.cpu_percent.toFixed(1) + "%", host.cpu_count + " logical CPUs", host.cpu_percent)}${stat("Memory", Math.round(host.memory_used / 1024 ** 3) + " / " + Math.round(host.memory_total / 1024 ** 3) + " GiB", host.memory_percent.toFixed(1) + "% used", host.memory_percent)}${stat("Storage", size(used, 0) + " / " + size(total, 1), pools.length + " storage pools", total ? (used / total) * 100 : 0)}${stat("Virtual machines", running + " / " + vms.length, "running / total", vms.length ? (running / vms.length) * 100 : 0)}</div><div class="overview-columns">${panel(
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
        ["Hostname", esc(host.name)],
        ["Address", esc(host.address)],
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
function vmTable(vms, compact = false) {
  return table(
    compact
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
        ],
    vms.map(
      (v) =>
        `<tr class="clickable" data-action="open-vm" data-id="${esc(v.id)}"><td class="vm-name-cell"><a class="name" href="#vm/${encodeURIComponent(v.id)}/summary">${esc(v.name)}</a>${organizationChips(v)}${!compact && v.description ? `<small>${esc(v.description.slice(0, 70))}</small>` : ""}</td><td>${badge(v.provider)}</td><td>${badge(v.status)}</td><td class="numeric">${esc(v.cpu)}</td><td class="numeric">${size(v.memory)}</td>${compact ? "" : `<td class="numeric">${Object.values(v.devices).filter((d) => d.type === "disk" && d.readonly !== "true" && !d.source?.endsWith(".iso") && d.kind !== "cdrom").length} disk(s)</td><td class="mono">${esc(v.addresses.join(", ") || "Not reported")}</td><td><div class="actions">${smallAction("Open", "open-vm", v.id)}${can() ? smallAction(v.status === "running" ? "Shutdown" : "Start", v.status === "running" ? "shutdown" : "start", v.id, !["stopped", "running"].includes(v.status)) : ""}</div></td>`}</tr>`,
    ),
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
function renderVMs() {
  const vms = filteredVMs();
  return (
    title(
      "Virtual machines",
      "Manage Incus and libvirt guests",
      can() ? btn("+ Create VM", "create-vm", "", "primary") : "",
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
      )}</select><span class="result-count" id="vm-result-count">${vms.length} virtual machines</span></div><div id="vm-table">${panel("Virtual machines", vmTable(vms))}</div>`
  );
}
function renderVM() {
  const v = currentVM();
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
    `<span class="title-status">${esc(v.name)} ${badge(v.status)}</span>`,
    `${v.provider === "incus" ? "Incus" : "libvirt"} virtual machine${v.managed ? " · Created in VM Manager" : ""}`,
    can()
      ? `${btn("Console", "console", v.id, "secondary", !running)}${btn(running ? "Shutdown" : "Start", running ? "shutdown" : "start", v.id, "primary", !["running", "stopped"].includes(v.status))}${btn(v.pending?.length ? "Restart & apply" : "Restart", "restart", v.id, "secondary", !running)}${btn("Force stop", "force-stop", v.id, "danger", !running)}`
      : "",
  );
  html += `<div class="tabs">${["summary", "hardware", "console", "options"].map((t) => `<button class="tab ${state.tab === t ? "active" : ""}" data-action="vm-tab" data-id="${t}" ${t === "console" && !can() ? "disabled" : ""}>${t[0].toUpperCase() + t.slice(1)}</button>`).join("")}</div>`;
  if (state.tab === "console") {
    return (
      html +
      (running
        ? `<div class="console-intro"><span>Graphical console · ${v.provider === "incus" ? "SPICE" : "VNC"} · Click inside to capture keyboard input</span><a class="button small" href="/console?vm=${encodeURIComponent(v.id)}" target="_blank" rel="noopener">Open in new tab ↗</a></div><iframe class="console-frame" title="${esc(v.name)} console" src="/console?vm=${encodeURIComponent(v.id)}" allow="fullscreen"></iframe>`
        : empty(
            "Start the VM to connect",
            "The graphical console is available while the VM is running.",
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
          ["Name", esc(v.name)],
          ["Hypervisor", badge(v.provider)],
          ["Status", badge(v.status)],
          [
            "IP addresses",
            esc(v.addresses.join(", ") || "Not reported by guest"),
          ],
          ["Description", esc(v.description || "No description")],
          ["Project & tags", organizationChips(v) || "Unassigned · No tags"],
        ]),
      )}${panel("Quick actions", `<div class="storage-body"><p class="muted">Configure disks, network adapters and installation media from the Hardware tab.</p><div class="actions" style="margin-top:16px">${btn("View hardware", "vm-tab", "hardware")}${can() ? btn("Assign project & tags", "organize-vm", v.id) : ""}${can() ? btn("Edit resources", "edit-vm", v.id, "secondary") : ""}</div></div>`)}</div>`
    );
  }
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
    const isNIC = d.type === "nic";
    const isISO =
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
      if (isNIC)
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
      value,
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
    `<p class="subtle" style="font-size:11px">Disk expansion increases the virtual device. Extend partitions and filesystems inside the guest afterwards.</p>`
  );
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
            `<tr class="clickable" data-action="network-detail" data-id="${esc(n.id)}"><td><span class="name">${esc(n.name)}</span>${organizationChips(n)}${n.description ? `<small>${esc(n.description)}</small>` : ""}</td><td>${badge(n.provider)}</td><td>${badge(networkMode(n), "incus")}</td><td class="mono">${esc(n.config["ipv4.address"] || "None")}</td><td>${n.config["ipv4.dhcp"] === "false" ? "Disabled" : "Enabled"}</td><td>${n.used_by.length}</td><td><div class="actions">${smallAction("Details", "network-detail", n.id)}${can() ? smallAction("Organize", "organize-network", n.id) : ""}${can() && n.provider === "incus" ? `${smallAction("Edit", "edit-network", n.id, !!n.used_by.length)}${smallAction("Delete", "delete-network", n.id, !!n.used_by.length)}` : ""}</div></td></tr>`,
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
            ["Gateway", esc(selected.config["ipv4.address"] || "None")],
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
              esc(
                selected.config["ipv4.dhcp.ranges"] || "Managed automatically",
              ),
            ],
            [
              "DNS domain",
              esc(selected.config["dns.domain"] || "Managed by hypervisor"),
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
      ? `<label class="dropzone" id="dropzone" tabindex="0" role="button" aria-label="Upload ISO"><span class="upload-symbol">↑</span><strong>Drag ISO files here or click to browse</strong><small>ISO9660 / UDF · Up to 32 GiB per file · ${size(state.inventory.host.disk_free)} free on host</small><input id="iso-input" type="file" accept=".iso" multiple><div id="upload-progress" class="upload-progress" hidden><progress id="upload-bar" max="100" value="0"></progress><small id="upload-label"></small></div></label>`
      : "") +
    panel(
      "ISO images",
      table(
        ["Name", "Size", "Storage", "Uploaded", "Used by", "Actions"],
        isos.map(
          (i) =>
            `<tr><td class="iso-name-cell"><span class="name">${esc(i.name)}</span>${i.sha256 ? `<small title="SHA-256 ${esc(i.sha256)}">SHA-256 ${esc(i.sha256.slice(0, 20))}…</small>` : ""}</td><td class="numeric">${size(i.size)}</td><td>${esc(i.pool || "ISO library")}</td><td class="numeric">${i.created ? when(i.created) : "—"}</td><td>${i.used_by?.length || 0} reference(s)</td><td>${can() ? smallAction("Delete", "delete-iso", i.id, !!i.used_by?.length) : '<span class="subtle">Read only</span>'}</td></tr>`,
        ),
      ),
      `<small>${isos.length} images</small>`,
    ) +
    `<p class="subtle" style="font-size:11px">Uploaded images are stored persistently and verified with SHA-256. Existing Incus ISO volumes are discovered automatically. Eject media from all VMs before deleting it.</p>`
  );
}
function renderStorage() {
  return (
    title(
      "Storage",
      "Existing hypervisor pools and local application storage",
    ) +
    `<div class="storage-cards">${state.inventory.pools
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
      `<tr class="clickable" data-action="task-detail" data-id="${t.id}"><td class="numeric">${when(t.started)}</td><td>${esc(t.username)}</td><td title="${esc(t.resource)}">${esc(t.resource)}</td><td>${esc(t.action.replaceAll(".", " → "))}</td><td>${badge(t.status)}</td></tr>`,
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
            `<tr><td class="numeric">${when(a.at)}</td><td>${esc(a.username)}</td><td>${esc(a.action)}</td><td class="vm-name-cell">${esc(a.resource)}</td><td>${esc(a.detail)}</td></tr>`,
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
function bindPage() {
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
  $("#vm-table").innerHTML = panel("Virtual machines", vmTable(vms));
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
  const body = `<div class="wizard-layout"><aside class="wizard-rail"><strong>1 &nbsp; General</strong><span>2 &nbsp; Hardware</span><span>3 &nbsp; Network & media</span><span>4 &nbsp; Create</span><small>Complete the configuration, then create your stopped VM. Start it when you are ready to install the OS.</small></aside><div class="wizard-form"><div class="form-grid"><div class="form-section">General</div>${field("VM name", "name", "", { placeholder: "my-temporary-vm", hint: "Unique name on this host. Letters, digits and hyphens." })}${field("Hypervisor", "provider", "Incus", { readonly: true })}<div class="form-section">Hardware</div>${field("CPU cores", "cpu", 2, { type: "number", min: 1, max: 128 })}${field("Memory (GiB)", "memory_gib", 4, { type: "number", min: 0.5, max: 256, step: 0.5 })}${select("Storage pool", "pool", pools, pools.some((p) => p[0] === "hdd-pool") ? "hdd-pool" : pools[0][0])}${field("Root disk (GiB)", "disk_gib", 32, { type: "number", min: 4, max: 4096 })}<div class="extra-disks"><label class="field"><span>Additional disks</span></label><div id="extra-disk-list"></div><button type="button" class="button small" id="extra-disk-add" style="margin-top:8px">+ Add another disk</button></div><div class="form-section">Network & installation media</div><div class="full">${select("Network", "network", networks, networks.some((n) => n[0] === "incusbr0") ? "incusbr0" : networks[0][0])}</div><div class="full">${select("Installation ISO", "iso_id", isoOptions(), "", "Optional. The VM boots from selected media until you eject it.")}</div>${check("Secure Boot", "secure_boot", false)}${check("Start with host", "autostart", false)}${textarea("Description (optional)", "description")}</div></div></div>`;
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
  );
}
function addDisk(v) {
  modal(
    "Add disk · " + v.name,
    `<div class="form-grid">${field("Disk size (GiB)", "size_gib", 16, { type: "number", min: 1, max: 4096 })}${v.provider === "incus" ? select("Storage pool", "pool", poolOptions(), "hdd-pool") : field("Storage", "storage", "Application disk storage", { readonly: true })}</div><div class="hint" style="margin-top:18px">A new blank disk will be attached. Partition and format it inside the guest.</div>`,
    async (f) =>
      queue(ref(v.id) + "/disks", "POST", {
        size_gib: +f.get("size_gib"),
        pool: f.get("pool") || "hdd-pool",
      }),
    { submit: "Add disk" },
  );
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
    { submit: device ? "Save network" : "Add adapter" },
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
    { submit: "Apply media" },
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
  try {
    for (const file of files) {
      if (!file.name.toLowerCase().endsWith(".iso"))
        throw new Error(file.name + ": choose an .iso file");
      if (file.size > 32 * 1024 ** 3)
        throw new Error("Each ISO must be smaller than 32 GiB");
      const form = new FormData();
      form.append("file", file);
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
        !(state.page === "vm" && state.tab === "console"))
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
    case "start":
    case "shutdown":
    case "restart":
      await queue(ref(id) + "/action", "POST", { action: name });
      break;
    case "force-stop":
      confirmAction(
        "Force stop",
        v.name,
        "The VM will stop immediately. Unsaved guest data may be lost.",
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
      const dialog = modal(
        "Task details",
        properties([
          ["Operation", esc(t.action)],
          ["Resource", esc(t.resource)],
          ["User", esc(t.username)],
          ["Started", when(t.started)],
          ["Finished", t.finished ? when(t.finished) : "In progress"],
          ["Status", badge(t.status)],
          ["Result", esc(t.message || "Waiting for the host")],
        ]),
        async () => {},
        { submit: "Close" },
      );
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
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  if (button.tagName === "TR" && event.target.closest("a,button")) return;
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
  state.inventory = null;
  $("#modal").close();
  renderLogin("Your session expired. Sign in to continue.");
});
window.addEventListener("refresh-tasks", () => refreshTasks());
async function init() {
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
init();
