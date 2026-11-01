import {state, $, $$, esc, can, api, modal, field, select, check, confirmAction, toast, queue} from "./core.js";
import {clusterScreen} from "./kubernetes.js";

const button = (label, action, id, disabled = false) => `<button type="button" class="button secondary" data-action="${action}" data-id="${esc(id)}" ${disabled ? "disabled" : ""}>${label}</button>`;
const clusters = () => state.devops?.clusters || [];
const active = status => ["queued", "deploying", "deleting"].includes(status);
export function renderDevops() {
  if (state.inventory?.features?.devops?.enabled === false || state.user?.features?.devops?.enabled === false) return `<div class="page-title"><div><h1>DevOps</h1><p>DevOps is not enabled on this manager.</p></div></div>`;
  if (state.devopsCluster) {
    const cluster = clusters().find(c => c.id === state.devopsCluster);
    if (cluster?.status === "ready") return clusterScreen(cluster);
    return `<div class="page-title"><div><a href="#devops">← DevOps clusters</a><h1>Cluster management unavailable</h1><p>${cluster ? "The cluster must be Ready before managing workloads." : "This cluster could not be found on this manager."}</p></div></div>`;
  }
  return `<div class="page-title"><div><h1>DevOps</h1><p>Deploy software and services across linked VM Manager hosts.</p></div>${can() ? button("Deploy Kubernetes cluster", "devops-create", "") : ""}</div>
    <section class="panel"><div class="panel-header"><h3>Kubernetes · K3s</h3></div><p class="hint">Ubuntu VMs, a K3s control plane, worker nodes and a built-in kubectl terminal. Deployments are coordinated by this manager, with a host chosen for every VM.</p>
    ${clusters().length ? clusters().map(c => `<article class="devops-cluster"><div class="panel-header"><div><h3>${esc(c.name)}</h3><p>${esc(c.version)} · ${c.spec.masters.length} master(s) · ${c.spec.workers.length} worker(s)</p></div><span class="badge ${c.status === "ready" ? "running" : ""}">${esc(c.status)}</span></div>
      <p role="status">${esc(c.message)}</p><div class="table-wrap"><table><thead><tr><th>VM</th><th>Role</th><th>Host</th><th>Specification</th><th>IP address</th><th>Deployment</th></tr></thead><tbody>${c.nodes.map(n => `<tr><td>${esc(n.name)}</td><td>${esc(n.role)}</td><td>${esc(n.host_name || (n.host_id ? "Linked host" : "This host"))}</td><td>${n.cpu} vCPU · ${n.memory_gib} GiB RAM · ${n.disk_gib} GiB disk</td><td>${esc(n.address || "Pending")}</td><td>${esc(n.status)}</td></tr>`).join("")}</tbody></table></div>
      <div class="actions">${c.task_id ? button("Task output", "task-detail", c.task_id) : ""}${c.status === "ready" ? button("Manage cluster", "devops-manage", c.id) : ""}${can() && c.status === "ready" ? button("kubectl terminal", "devops-terminal", c.id) + button("Download kubeconfig", "devops-kubeconfig", c.id) : ""}${can() && ["failed", "interrupted"].includes(c.status) ? button("Retry deployment", "devops-retry", c.id) : ""}${can() && !active(c.status) && c.status !== "deleted" ? button("Delete cluster VMs", "devops-delete", c.id) : ""}</div></article>`).join("") : `<div class="empty"><strong>No Kubernetes clusters yet</strong>Request a cluster to provision its VMs and install K3s automatically.</div>`}</section>`;
}

export function nodeFields(node, key, hosts) {
  const host = hosts.find(h => h.id === (node.host_id || "")) || hosts[0];
  const pools = host.options.pools.map(p => [p.name, `${p.name} · ${p.driver}`]);
  const networks = host.options.networks.filter(n => n.managed && ["bridge", "macvlan"].includes(n.type)).map(n => [n.name, `${n.name} · ${n.type} · ${n.config["ipv4.address"] || "LAN DHCP"}`]);
  return `<fieldset data-node="${key}"><legend>${key.startsWith("master") ? "Master" : "Worker"} ${+key.split("-")[1] + 1}</legend><div class="form-grid">${select("VM Manager host", key + "_host", hosts.map(h => [h.id, h.name]), host.id)}${field("vCPUs", key + "_cpu", node.cpu || 2, {type:"number", min:key.startsWith("master") ? 2 : 1, max:128})}${field("Memory (GiB)", key + "_memory", node.memory_gib || 4, {type:"number", min:key.startsWith("master") ? 2 : 1, max:512})}${field("System disk (GiB)", key + "_disk", node.disk_gib || 30, {type:"number", min:20, max:4096})}${select("Storage pool", key + "_pool", pools, node.pool || (pools.some(p => p[0] === "hdd-pool") ? "hdd-pool" : pools[0]?.[0]))}<div class="devops-existing-network">${select("Existing network", key + "_network", networks, node.network || networks[0]?.[0])}</div></div></fieldset>`;
}

export async function clusterForm(refresh) {
  const options = await api("/devops/options");
  const hosts = options.hosts.filter(h => h.options);
  if (!hosts.length) throw new Error("No host is available for Kubernetes deployment");
  const saved = new Map();
  const dialog = modal("Deploy Kubernetes cluster", `<p>K3s ${esc(options.version)} on Ubuntu 24.04. Every VM has its own host and hardware settings.</p><div class="form-grid">${field("Cluster name", "name", "", {hint:"Lowercase letters, numbers and hyphens; starts with a letter."})}${select("Masters / control plane", "master_count", [["1", "1 · single master"], ["3", "3 · HA control plane"], ["5", "5 · HA control plane"]], "1")}${field("Workers", "worker_count", 2, {type:"number", min:0, max:20})}${select("Cluster network", "network_mode", [["existing", "Use existing networks"], ["new", "Create a network"]])}${field("Pod CIDR", "pod_cidr", "10.42.0.0/16")}${field("Service CIDR", "service_cidr", "10.43.0.0/16")}</div>
    <div id="devops-network-settings" hidden><h3>New network settings</h3><div class="form-grid">${field("Network name", "network_name", "k3s-net")}${select("Network type", "network_kind", [["nat", "Private bridge · one host"], ["lan", "Shared LAN · multiple hosts"]])}<div class="devops-private-network">${field("IPv4 gateway / subnet", "network_subnet", "10.210.0.1/24")}${check("Enable DHCP (required)", "network_dhcp", true)}${check("Enable NAT for outbound internet", "network_nat", true)}</div></div><div id="devops-parent-interfaces"></div></div>
    <p class="hint">Nodes need outbound internet for Ubuntu packages, K3s and container images. Across hosts, choose shared LAN or mutually routed networks: TCP 6443, 10250, 2379–2380 and UDP 8472 must pass between the relevant nodes. Keep VXLAN private. Shared LAN uses macvlan and requires LAN DHCP; select the parent interface on each host. Reserve DHCP leases for stable node addresses.</p>
    <div id="devops-nodes"></div><p id="devops-totals" role="status"></p><p class="hint">HA masters should run on different physical hosts with fast disks. The kubeconfig initially targets the first master; an external load balancer is needed for a highly available API endpoint. Failed deployments retain VMs for inspection and retry.</p>${options.hosts.filter(h => h.error).map(h => `<p class="warning">${esc(h.name)}: ${esc(h.error)}</p>`).join("")}`,
    async form => {
      const spec = {name:form.get("name"), masters:[], workers:[], pod_cidr:form.get("pod_cidr"), service_cidr:form.get("service_cidr")};
      for (const element of $$("[data-node]", dialog)) {
        const key = element.dataset.node;
        spec[key.startsWith("master") ? "masters" : "workers"].push(readNode(form, key));
      }
      if (form.get("network_mode") === "new") {
        spec.new_network = {name:form.get("network_name"), kind:form.get("network_kind"), subnet:form.get("network_subnet"), dhcp:form.has("network_dhcp"), nat:form.has("network_nat"), parents:{}};
        if (spec.new_network.kind === "lan") for (const host of new Set([...spec.masters, ...spec.workers].map(n => n.host_id))) spec.new_network.parents[host] = form.get("parent_" + host) || "";
      }
      await queue("/devops/clusters", "POST", spec);
      state.devops = await api("/devops/clusters");
      await refresh(true);
    }, {wide:true, submit:"Deploy cluster"});
  function readNode(form, key) { return {host_id:form.get(key + "_host"), cpu:+form.get(key + "_cpu"), memory_gib:+form.get(key + "_memory"), disk_gib:+form.get(key + "_disk"), pool:form.get(key + "_pool"), network:form.get(key + "_network")}; }
  function networkSettings() {
    const creating = $("[name=network_mode]", dialog).value === "new";
    const lan = $("[name=network_kind]", dialog).value === "lan";
    $("#devops-network-settings", dialog).hidden = !creating;
    $$(".devops-existing-network", dialog).forEach(e => {e.hidden = creating;});
    $$(".devops-private-network", dialog).forEach(e => {e.hidden = lan;});
    $$("#devops-network-settings input, #devops-network-settings select", dialog).forEach(e => { e.disabled = !creating; });
    const parentFields = $("#devops-parent-interfaces", dialog);
    const previous = new FormData($("#dialog-form", dialog));
    const used = new Set($$("[data-node]", dialog).map(e => $("[name$=_host]", e).value));
    parentFields.innerHTML = creating && lan ? `<div class="form-grid">${hosts.filter(h => used.has(h.id)).map(h => select(`LAN interface · ${h.name}`, "parent_" + h.id, h.options.interfaces, previous.get("parent_" + h.id) || h.options.interfaces[0])).join("")}</div>` : "";
  }
  function rebuild() {
    const form = new FormData($("#dialog-form", dialog));
    for (const e of $$("[data-node]", dialog)) saved.set(e.dataset.node, readNode(form, e.dataset.node));
    const masters = +form.get("master_count"), workers = +form.get("worker_count");
    if (!Number.isInteger(workers) || workers < 0 || workers > 20) return;
    $("#devops-nodes", dialog).innerHTML = [["master", masters], ["worker", workers]].flatMap(([role, count]) => Array.from({length:count}, (_, i) => {
      const key = `${role}-${i}`; return nodeFields(saved.get(key) || {}, key, hosts);
    })).join("");
    networkSettings(); totals();
  }
  function totals() {
    const form = new FormData($("#dialog-form", dialog));
    const nodes = $$("[data-node]", dialog).map(e => readNode(form, e.dataset.node));
    $("#devops-totals", dialog).textContent = `Request: ${nodes.length} VMs · ${nodes.reduce((a,n) => a+n.cpu,0)} vCPUs · ${nodes.reduce((a,n) => a+n.memory_gib,0)} GiB memory · ${nodes.reduce((a,n) => a+n.disk_gib,0)} GiB disk`;
  }
  dialog.addEventListener("change", event => {
    const name = event.target.name || "";
    if (["master_count", "worker_count"].includes(name)) rebuild();
    else if (name.endsWith("_host")) {
      const element = event.target.closest("[data-node]");
      const key = element.dataset.node;
      const node = readNode(new FormData($("#dialog-form", dialog)), key);
      node.pool = ""; node.network = "";
      element.outerHTML = nodeFields(node, key, hosts);
      networkSettings();
    } else if (["network_mode", "network_kind"].includes(name)) networkSettings();
    totals();
  }, {signal:dialogSignal(dialog)});
  rebuild();
}
function dialogSignal(dialog) {
  const controller = new AbortController();
  dialog.addEventListener("close", () => controller.abort(), {once:true});
  dialog.addEventListener("modal-replace", () => controller.abort(), {once:true});
  return controller.signal;
}

export async function devopsAction(action, id, refresh) {
  if (action === "devops-manage") {
    const cluster = clusters().find(c => c.id === id);
    if (!cluster) throw new Error("Cluster not found");
    location.hash = "#devops/" + encodeURIComponent(cluster.id);
    return;
  }
  if (!can()) throw new Error("Your role does not permit this action");
  if (action === "devops-create") return clusterForm(refresh);
  const cluster = clusters().find(c => c.id === id);
  if (!cluster) throw new Error("Cluster not found");
  if (action === "devops-retry") { await queue(`/devops/clusters/${id}/retry`, "POST", {}); await refresh(true); }
  else if (action === "devops-delete") confirmAction("Delete cluster VMs", cluster.name, "Permanently delete all VMs and disks owned by this cluster, including workloads. Snapshots are removed; independent backups and networks are retained.", async () => {
    await queue(`/devops/clusters/${id}`, "DELETE", {confirm_name:cluster.name}); await refresh(true);
  });
  else if (action === "devops-kubeconfig") {
    const data = await api(`/devops/clusters/${id}/kubeconfig`, "POST", {});
    const url = URL.createObjectURL(new Blob([data.kubeconfig], {type:"application/yaml"}));
    const link = document.createElement("a"); link.href = url; link.download = data.name + "-kubeconfig.yaml"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("Kubeconfig downloaded. It grants cluster administrator access.");
  } else if (action === "devops-terminal") {
    const first = cluster.nodes[0];
    const url = `/terminal?kube_node=${encodeURIComponent(first.id)}&node_host=${encodeURIComponent(first.host_id)}&cluster_name=${encodeURIComponent(cluster.name)}`;
    modal("kubectl · " + cluster.name, `<p>Cluster administrator access through this cluster's kubeconfig. Try <code>kubectl get nodes</code> or <code>kubectl get pods -A</code>.</p><iframe class="devops-terminal" title="Kubernetes terminal" src="${esc(url)}" allow="clipboard-read; clipboard-write; fullscreen"></iframe>`, async () => {}, {wide:true, submit:"Close terminal"});
  }
}
