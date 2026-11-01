import {$, esc, can, api, select} from "./core.js";

const control = (label, action, index = "", disabled = false) => `<button class="button secondary" type="button" data-kube-action="${action}" data-index="${index}" ${disabled ? "disabled" : ""}>${esc(label)}</button>`;
const badge = (text, good) => `<span class="badge ${good ? "running" : ""}">${esc(text)}</span>`;

export function resourceTables(data, namespace = "", search = "") {
  const matches = text => text.toLowerCase().includes(search.toLowerCase());
  const nodes = data.nodes.filter(n => matches(n.name));
  const pods = data.pods.map((p, index) => ({...p, index})).filter(p => (!namespace || p.namespace === namespace) && matches(`${p.name} ${p.namespace} ${p.node} ${p.status}`));
  return `<section aria-label="Kubernetes nodes"><h3>Nodes <span class="hint">${nodes.length}</span></h3><div class="table-wrap"><table><thead><tr><th>Node</th><th>Status</th><th>Roles</th><th>Address</th><th>Kubernetes</th><th>Actions</th></tr></thead><tbody>${nodes.map(n => `<tr><td>${esc(n.name)}</td><td>${badge(n.ready === "True" ? "Ready" : n.ready === "False" ? "Not ready" : "Unknown", n.ready === "True")}${n.unschedulable ? " · Scheduling disabled" : ""}</td><td>${esc(n.roles.join(", "))}</td><td>${esc((n.addresses.find(a => a.type === "InternalIP") || n.addresses[0])?.address || "—")}</td><td>${esc(n.version)}</td><td>${control("Describe", "node-describe", data.nodes.indexOf(n))}</td></tr>`).join("") || `<tr><td colspan="6">No matching nodes.</td></tr>`}</tbody></table></div></section>
    <section aria-label="Kubernetes pods"><h3>Pods <span class="hint">${pods.length}</span></h3><div class="table-wrap"><table><thead><tr><th>Pod / namespace</th><th>Status</th><th>Ready</th><th>Restarts</th><th>Node / IP</th><th>Controller</th><th>Actions</th></tr></thead><tbody>${pods.map(p => `<tr><td><strong>${esc(p.name)}</strong><br><small>${esc(p.namespace)}</small></td><td>${badge(p.status, p.status === "Running" && p.ready === p.containers.length)}</td><td>${p.ready}/${p.containers.length}</td><td>${p.restarts}</td><td>${esc(p.node || "Unscheduled")}<br><small>${esc(p.ip || "—")}</small></td><td>${esc(p.owner || "Standalone")}</td><td><div class="actions">${control("Describe", "pod-describe", p.index)}${can() ? control("Terminal", "terminal", p.index, !p.containers.some(c => c.running) || p.status === "Terminating") + control("Restart", "restart", p.index, !p.restartable) + control("Delete", "delete", p.index, !p.deletable) : ""}</div></td></tr>`).join("") || `<tr><td colspan="7">No matching pods.</td></tr>`}</tbody></table></div></section>`;
}

export function clusterScreen(cluster) {
  return `<div id="kubernetes-screen" data-cluster-id="${esc(cluster.id)}"><div class="page-title"><div><a href="#devops">← DevOps clusters</a><h1>${esc(cluster.name)}</h1><p>Kubernetes management · ${esc(cluster.version)}</p></div>${badge("Ready", true)}</div><section class="panel kube-panel" aria-label="Cluster management">
    <div class="kube-toolbar"><div id="kube-namespaces"></div><label class="field"><span>Search nodes and pods</span><input id="kube-search" type="search" placeholder="Name, namespace, node or status"></label>${control("Refresh", "refresh")}</div><p id="kube-status" role="status">Loading cluster resources…</p><p id="kube-error" class="warning" role="alert" hidden></p><div id="kube-resources"></div><div id="kube-detail" hidden></div></section></div>`;
}

export function mountClusterScreen(root, cluster) {
  let data = {nodes:[], pods:[]}, namespace = "", search = "", stage = "list", loading = false;
  const controller = new AbortController();
  const endpoint = `/devops/clusters/${cluster.id}`;
  const detail = $("#kube-detail", root);
  function stop() { controller.abort(); detail.innerHTML = ""; clearInterval(timer); }
  function error(message = "") {
    const element = $("#kube-error", root);
    element.textContent = message; element.hidden = !message;
  }
  function render() {
    $("#kube-resources", root).innerHTML = resourceTables(data, namespace, search);
    $("#kube-namespaces", root).innerHTML = select("Pod namespace", "kube-namespace", [["", "All namespaces"], ...[...new Set(data.pods.map(p => p.namespace))].sort().map(n => [n,n])], namespace);
  }
  function showList() {
    stage = "list"; detail.hidden = true; detail.innerHTML = "";
    $("#kube-resources", root).hidden = false;
    $(".kube-toolbar", root).hidden = false;
    error(); render();
  }
  function showDetail(html) {
    stage = "detail"; error();
    $("#kube-resources", root).hidden = true;
    $(".kube-toolbar", root).hidden = true;
    detail.hidden = false;
    detail.innerHTML = control("← Nodes and pods", "back") + html;
  }
  async function refresh() {
    if (loading || controller.signal.aborted) return;
    loading = true;
    $("#kube-status", root).textContent = "Refreshing cluster resources…";
    try {
      const updated = await api(endpoint + "/resources", "GET", null, {signal:controller.signal});
      if (controller.signal.aborted) return;
      data = updated;
      if (namespace && !data.pods.some(p => p.namespace === namespace)) namespace = "";
      render(); error();
      $("#kube-status", root).textContent = `Updated ${new Date().toLocaleTimeString()} · ${data.nodes.length} nodes · ${data.pods.length} pods · refreshes every 15 seconds`;
    } catch (e) {
      if (!controller.signal.aborted) {
        error(e.message);
        $("#kube-status", root).textContent = "Unable to refresh. Previously displayed resources may be out of date.";
      }
    } finally { loading = false; }
  }
  const operate = body => api(endpoint + "/manage", "POST", body, {signal:controller.signal});
  root.addEventListener("input", event => {
    if (event.target.id === "kube-search") { search = event.target.value; render(); }
  }, {signal:controller.signal});
  root.addEventListener("change", event => {
    if (event.target.name === "kube-namespace") { namespace = event.target.value; render(); }
  }, {signal:controller.signal});
  root.addEventListener("click", async event => {
    const target = event.target.closest("[data-kube-action]");
    if (!target || target.disabled) return;
    const action = target.dataset.kubeAction;
    target.disabled = true;
    try {
      if (action === "refresh") return await refresh();
      if (action === "back") { showList(); return await refresh(); }
      const node = action === "node-describe";
      const resource = node ? data.nodes[+target.dataset.index] : data.pods[+target.dataset.index];
      if (!resource && !["confirm", "connect"].includes(action)) throw new Error("Resource changed; refresh and select it again.");
      if (action.endsWith("-describe")) {
        const result = await operate({action:"describe", resource:node ? "node" : "pod", name:resource.name, namespace:resource.namespace || "default"});
        if (controller.signal.aborted) return;
        showDetail(`<h3>${esc(resource.name)}</h3><pre class="kube-description"></pre>`);
        $(".kube-description", detail).textContent = result.text;
      } else if (action === "restart" || action === "delete") {
        showDetail(`<h3>${action === "restart" ? "Restart" : "Delete"} ${esc(resource.namespace)}/${esc(resource.name)}</h3><p>${action === "restart" ? "This gracefully deletes the pod so its controller can create a replacement. Service may be interrupted; temporary pod data may be lost." : "This gracefully deletes the pod. Its controller may create a replacement. Standalone pods are not recreated; temporary pod data may be lost."}</p><label class="field"><span>Type the pod name to confirm</span><input id="kube-confirm-name" autocomplete="off"></label>${control("Confirm " + action, "confirm")}`);
        $("[data-kube-action=confirm]", detail).onclick = async e => {
          e.stopPropagation(); const b = e.currentTarget; b.disabled = true;
          try {
            const result = await operate({action, resource:"pod", name:resource.name, namespace:resource.namespace, uid:resource.uid, confirm_name:$("#kube-confirm-name", detail).value});
            if (controller.signal.aborted) return;
            showList(); await refresh();
            $("#kube-status", root).textContent = result.message;
          } catch (err) { if (!controller.signal.aborted) error(err.message); }
          finally { b.disabled = false; }
        };
      } else if (action === "terminal") {
        const containers = resource.containers.filter(c => c.running).map(c => [c.name,c.name]);
        showDetail(`<h3>Terminal · ${esc(resource.namespace)}/${esc(resource.name)}</h3><div class="form-grid">${select("Container", "kube-container", containers)}${select("Shell", "kube-shell", [["/bin/sh","/bin/sh"], ["/bin/bash","/bin/bash"]])}</div><p class="hint">The selected shell must exist in the container. Exit closes the shell; reconnect starts a new session.</p>${control("Connect", "connect")}<div id="kube-terminal"></div>`);
        $("[data-kube-action=connect]", detail).onclick = async e => {
          e.stopPropagation(); const b = e.currentTarget; b.disabled = true;
          try {
            const container = $("[name=kube-container]", detail).value, shell = $("[name=kube-shell]", detail).value;
            await operate({action:"exec-check", resource:"pod", name:resource.name, namespace:resource.namespace, uid:resource.uid, container, shell});
            if (controller.signal.aborted) return;
            const first = cluster.nodes[0];
            const query = new URLSearchParams({kube_node:first.id, node_host:first.host_id || "", cluster_name:cluster.name, pod:resource.name, namespace:resource.namespace, uid:resource.uid, container, shell});
            $("#kube-terminal", detail).innerHTML = `<iframe class="devops-terminal" title="Pod terminal" src="/terminal?${esc(query.toString())}" allow="clipboard-read; clipboard-write; fullscreen"></iframe>`;
          } catch (err) { if (!controller.signal.aborted) error(err.message); }
          finally { b.disabled = false; }
        };
      }
    } catch (e) { if (!controller.signal.aborted) error(e.message); }
    finally { target.disabled = false; }
  }, {signal:controller.signal});
  const timer = setInterval(() => { if (stage === "list" && !document.hidden) refresh(); }, 15000);
  render(); refresh();
  return stop;
}
