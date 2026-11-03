import {state, $, $$, esc, can, api, modal, field, select, confirmAction, toast} from "./core.js";

const button = (label, action, id = "") => `<button class="button secondary" type="button" data-edge-action="${action}" data-id="${esc(id)}">${esc(label)}</button>`;
const edgeTabs = [["overview", "Overview"], ["routes", "Hostname routes"], ["kubernetes", "Kubernetes"], ["cloudflare", "Cloudflare"]];
const httpsMethods = {manual:"Supplied certificate", letsencrypt:"Let’s Encrypt", cloudflare:"Cloudflare Tunnel", manager:"VM Manager certificate"};
const clusterName = id => state.devops?.clusters?.find(c => c.id === id)?.name || id;
function routeTargets(route) {
  if (route.backend === "vm-manager") return ["VM Manager (verified HTTPS origin)"];
  if (route.cluster_id) return [clusterName(route.cluster_id), `${route.namespace}/${route.service}:${route.service_port}`, "Via cluster node / NodePort"];
  return route.upstreams || [];
}
const flowNode = (label, lines, kind = "") => `<div class="edge-flow-node ${kind}"><span class="edge-node-label">${esc(label)}</span>${lines.map(line => `<span>${esc(line)}</span>`).join("")}</div>`;
const arrow = '<span class="edge-flow-arrow" aria-hidden="true">→</span>';
export function edgeDiagram(data) {
  const {settings:s, status} = data;
  const paths = s.routes.map(route => `<li class="edge-flow-path"><div class="edge-flow-heading"><strong>${esc(route.hostname)}</strong><span class="badge">Hostname route</span></div><div class="edge-flow">${[
    flowNode("1 · Client", [route.hostname]),
    flowNode("2 · Entry", route.mode === "cloudflare" ? ["Cloudflare", "Public HTTPS → shared tunnel"] : ["Direct HTTPS", "TCP port 443"]),
    flowNode("3 · Load balancer", [route.mode === "cloudflare" ? "127.0.0.1:18080 · local HTTP" : s.address ? `${s.address}:443` : "Address not assigned", "Caddy · matches hostname"], "edge-flow-balancer"),
    flowNode("4 · Destination", [...routeTargets(route), route.backend === "vm-manager" || route.id.startsWith("manager") ? "HTTPS to VM Manager" : "HTTP to application"])
  ].join(arrow)}</div></li>`);
  for (const service of status.services || []) {
    const configured = service.state === "configured" && Boolean(service.address);
    paths.push(`<li class="edge-flow-path ${configured ? "" : "edge-flow-pending"}"><div class="edge-flow-heading"><strong>${esc(clusterName(service.cluster_id))} · ${esc(service.namespace)}/${esc(service.name)}</strong><span class="badge">${configured ? "TCP listener configured" : "TCP listener pending"}</span></div><div class="edge-flow">${[
      flowNode("1 · Client", ["TCP application"]),
      flowNode("2 · Entry", ["Direct TCP · no Cloudflare Tunnel", `Ports ${service.ports.join(", ")}`]),
      flowNode("3 · Load balancer", [service.address || s.address || "Address not assigned", "HAProxy · matches port"], "edge-flow-balancer"),
      flowNode("4 · Destination", [clusterName(service.cluster_id), `${service.namespace}/${service.name}`, "Via cluster node / NodePort"])
    ].join(arrow)}</div>${service.message ? `<p class="edge-flow-note">${esc(service.message)}</p>` : ""}</li>`);
  }
  return `<section class="panel edge-panel" aria-labelledby="edge-diagram-title"><h2 id="edge-diagram-title">Network &amp; traffic flow</h2><p>Read each path from client to destination. The load balancer forwards requests to the application or Kubernetes Service that should receive them.</p><p class="hint">These paths come from the current configuration, not a live connectivity test. ${s.enabled ? `Overall state: ${esc(status.state)}. Check the status above for configuration errors.` : "The load balancer has not started yet."} Arrows show request direction; responses return along the same path. Multiple destination addresses share traffic.</p>${paths.length ? `<ul class="edge-flow-list">${paths.join("")}</ul>` : '<div class="edge-empty"><strong>No traffic paths yet</strong><p>Add a hostname route for a web application, or create a Kubernetes LoadBalancer Service. Your connections will appear here.</p></div>'}</section>`;
}
export function edgeScreen() {
  return `<div id="edge-screen"><div class="page-title"><div><a href="#devops">← DevOps</a><h1>Load balancer</h1><p>Send incoming traffic to the right application, VM or Kubernetes Service.</p></div></div><p id="edge-error" class="warning" role="alert" hidden></p><div class="tabs" role="tablist" aria-label="Load balancer sections">${edgeTabs.map(([id,label], i) => `<button type="button" class="tab${i ? "" : " active"}" id="edge-tab-${id}" role="tab" aria-selected="${!i}" aria-controls="edge-panel-${id}" tabindex="${i ? -1 : 0}" data-edge-tab="${id}">${label}</button>`).join("")}</div><div id="edge-content"><p role="status">Loading load balancer…</p></div></div>`;
}
export function edgeContent(data, activeTab = "overview") {
  const {settings:s, status} = data;
  const admin = can("admin");
  const clusters = state.devops?.clusters?.filter(c => c.status === "ready") || [];
  const panel = (id, content) => `<div id="edge-panel-${id}" role="tabpanel" aria-labelledby="edge-tab-${id}" tabindex="0"${id === activeTab ? "" : " hidden"}>${content}</div>`;
  return panel("overview", `<section class="panel edge-panel"><div class="edge-section-heading"><div><h2>Load balancer status</h2><p>A shared entry point for your applications. Web requests are routed by hostname; other TCP traffic is routed by port.</p></div><span class="badge ${status.state === "ready" ? "running" : ""}">${esc(status.state)}</span></div><p role="status">${esc(status.message)}</p><dl class="edge-summary"><div><dt>HTTPS entry point</dt><dd>${s.address ? esc(s.address) + ":443" : "Not started"}</dd></div><div><dt>Hostname routes</dt><dd>${s.routes.length}</dd></div><div><dt>Kubernetes TCP Services</dt><dd>${(status.services || []).length}</dd></div></dl><div class="actions">${admin ? (!s.enabled ? button("Launch load balancer", "launch") : button("Apply / retry configuration", "reconcile")) : ""}${button("Refresh", "refresh")}</div><p class="edge-help">${s.enabled ? "Publish web applications using" : "Kubernetes starts the load balancer automatically when a Service needs it. For web applications, launch it here, then use"} <button type="button" class="edge-tab-link" data-edge-tab="routes">Hostname routes</button>. VM Manager’s direct port 8443 remains available for recovery.</p></section>${edgeDiagram(data)}`)
    + panel("routes", `<section class="panel edge-panel"><div class="edge-section-heading"><div><h2>Hostname routes</h2><p>A hostname route connects a web address, such as <code>app.example.com</code>, to an application. Many applications can share HTTPS port 443 because each has its own hostname.</p></div>${admin && s.enabled ? button("Add hostname route", "route") : ""}</div><ol class="edge-steps"><li><strong>Choose a hostname.</strong> Use a DNS name you control.</li><li><strong>Choose HTTPS.</strong> Let’s Encrypt renews certificates automatically; a supplied certificate needs manual renewal. Cloudflare uses the shared tunnel.</li><li><strong>Choose a destination.</strong> Enter application IP addresses and ports, select VM Manager, or choose a Kubernetes Service.</li></ol>${!s.enabled ? '<p class="hint">Launch the load balancer in Overview before adding a hostname route.</p>' : ""}<div class="table-wrap"><table><thead><tr><th>Hostname</th><th>HTTPS method</th><th>Destination</th><th>Actions</th></tr></thead><tbody>${s.routes.map(r => `<tr><td>${esc(r.hostname)}${r.id.startsWith("manager") ? " · VM Manager" : ""}</td><td>${esc(httpsMethods[r.mode])}</td><td>${routeTargets(r).map(esc).join("<br>")}</td><td>${r.id.startsWith("manager") ? "Protected route" : admin ? button("Edit", "route", r.id) + button("Delete", "remove", r.id) : "Read only"}</td></tr>`).join("") || '<tr><td colspan="4">No hostname routes yet. Add one to publish a web application.</td></tr>'}</tbody></table></div><details id="edge-help-routes" class="edge-details"><summary>DNS, certificates and Kubernetes destinations</summary><p>For direct HTTPS, point public DNS at this host and forward TCP 443 through your firewall or router. Let’s Encrypt uses TLS-ALPN on port 443. Caddy handles certificate issuance and renewal; supplied certificates must match the hostname and be renewed by their owner.</p><p>For Cloudflare hostnames, configure the connection in the Cloudflare tab first. Cloudflare terminates public HTTPS and carries these requests through one tunnel to this load balancer.</p><p>A Kubernetes destination needs a reachable NodePort or LoadBalancer Service. For Ingress rules, select the cluster’s Traefik Service on port 80 and match the hostname in your Ingress. Built-in VM Manager routes are protected to preserve access.</p></details></section>`)
    + panel("kubernetes", `<section class="panel edge-panel"><h2>Kubernetes integration</h2><p>A Kubernetes Service gives an application a stable network endpoint. A Service with <code>type: LoadBalancer</code> asks this host to accept TCP traffic and forward it to the cluster.</p><ol class="edge-steps"><li><strong>Use a ready cluster.</strong> Clusters connect automatically; no class setting or connection step is required.</li><li><strong>Create a Service.</strong> Use VM Manager, kubectl or Helm and choose <code>type: LoadBalancer</code> with a TCP port.</li><li><strong>Wait for an address.</strong> The controller checks every 15 seconds and publishes this host’s IP after listener configuration succeeds. The first setup can take longer.</li></ol><p class="hint">Use <button type="button" class="edge-tab-link" data-edge-tab="routes">Hostname routes</button> to share HTTPS port 443 between web applications. The automatic Service integration below assigns dedicated TCP ports.</p>${clusters.map(c => `<div class="edge-cluster"><div><a href="#devops/${esc(c.id)}">${esc(c.name)}</a><p>${(s.excluded_clusters || []).includes(c.id) ? "Automatic integration paused" : "Automatic integration enabled"}</p></div>${admin ? button((s.excluded_clusters || []).includes(c.id) ? "Resume automatic integration" : "Pause automatic integration", "bind", c.id) : ""}</div>`).join("") || '<p class="edge-empty">No ready clusters. Create a cluster in DevOps first.</p>'}<div class="table-wrap"><table><thead><tr><th>Cluster / Service</th><th>TCP ports</th><th>Address / state</th></tr></thead><tbody>${(status.services || []).map(r => `<tr><td>${esc(clusterName(r.cluster_id))}<br>${esc(r.namespace)}/${esc(r.name)}</td><td>${esc(r.ports.join(", "))}</td><td>${esc(r.address || r.state)}${r.message ? " · " + esc(r.message) : ""}</td></tr>`).join("") || '<tr><td colspan="3">No LoadBalancer Services discovered yet. Create one in a ready cluster to get started.</td></tr>'}</tbody></table></div><details id="edge-help-kubernetes" class="edge-details"><summary>Supported ports and troubleshooting</summary><p>TCP ports 1024–65535 are supported. Ports 80, 443, 8443, 18080 and 20191 are reserved. Each TCP port can belong to only one Service on this host. UDP is not supported.</p><p>Cluster nodes must be reachable from this host. Services need allocated NodePorts and <code>externalTrafficPolicy: Cluster</code>. A pending Service shows its reason in the table. Existing Services owned by another provider must be recreated to move them here; they are never deleted automatically.</p><p>Pausing integration removes the cluster’s TCP listeners and external Service addresses. Hostname routes remain configured. This is a single host load balancer: applications depend on this host being available.</p></details></section>`)
    + panel("cloudflare", `<section class="panel edge-panel"><div class="edge-section-heading"><div><h2>Cloudflare connection</h2><p>Cloudflare Tunnel is an optional way to publish web applications without opening an inbound HTTPS port on your router. A connector on this host carries requests from Cloudflare to the load balancer.</p></div>${admin && s.enabled ? button("Cloudflare connection", "cloudflare") : ""}</div><dl class="edge-summary"><div><dt>Shared tunnel</dt><dd>${s.cloudflare.tunnel_id ? esc(s.cloudflare.tunnel_id) : "Not configured"}</dd></div><div><dt>Connector</dt><dd>${s.cloudflare.tunnel_id ? s.cloudflare.local ? "Existing VM Manager connector" : "Managed connector" : "None"}</dd></div><div><dt>DNS management</dt><dd>${s.cloudflare.configured ? "API token stored · " + esc(s.cloudflare.zone_name || s.cloudflare.zone_id) : "API token not configured"}</dd></div>${status.tunnel_status ? `<div><dt>Reported tunnel state</dt><dd>${esc(status.tunnel_status)}</dd></div>` : ""}</dl><ol class="edge-steps"><li><strong>Connect your Cloudflare account.</strong> Supply the account ID, zone ID and a scoped API token using the connection button.</li><li><strong>Add a hostname route.</strong> In Hostname routes, choose Cloudflare Tunnel as the HTTPS method and select an application destination.</li><li><strong>Check the route.</strong> VM Manager creates its DNS record and reuses the shared tunnel. Open the hostname to check the application responds.</li></ol><p class="hint">${!s.enabled ? "Launch the load balancer in Overview to configure this connection. " : ""}Direct HTTPS works without Cloudflare. An existing VM Manager tunnel can carry its protected route even before a DNS-management API token is supplied.</p><details id="edge-help-cloudflare" class="edge-details"><summary>API token permissions and DNS records</summary><p>Scope the token to this account and zone with Cloudflare Tunnel Edit, Zone Read and DNS Edit. Tokens are stored privately. All Cloudflare hostname routes share one tunnel; existing conflicting DNS records are retained.</p><p>The tunnel hands requests to Caddy at <code>127.0.0.1:18080</code> over HTTP on this host only; visitors connect to Cloudflare using HTTPS.</p><p>Cloudflare must have certificate coverage for each hostname. Deeper subdomains may need additional coverage. This connection publishes web hostname routes; Kubernetes TCP listeners use direct network access.</p></details></section>`);
}

export function mountEdgeScreen(root) {
  const controller = new AbortController();
  let data, loading = false, activeTab = "overview", renderedContent = "";
  function selectTab(id, focus = false) {
    if (!edgeTabs.some(([key]) => key === id)) return;
    activeTab = id;
    $$("[role=tab]", root).forEach(tab => {
      const active = tab.dataset.edgeTab === id;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      if (active && focus) tab.focus();
    });
    $$("[role=tabpanel]", root).forEach(panel => { panel.hidden = panel.id !== "edge-panel-" + id; });
  }
  const request = (path, method = "GET", body = null) => api("/devops/load-balancer" + path, method, body, {signal:controller.signal});
  function error(message = "") { const e = $("#edge-error", root); e.textContent = message; e.hidden = !message; }
  async function refresh() {
    if (loading || controller.signal.aborted) return;
    loading = true;
    try {
      data = await request("");
      if (controller.signal.aborted) return;
      const html = edgeContent(data);
      if (html !== renderedContent) {
        const openHelp = $$("details[open]", root).map(details => details.id);
        const focused = document.activeElement;
        const focusId = focused?.id || focused?.closest("details")?.id;
        const focusSummary = focused?.tagName === "SUMMARY";
        const action = focused?.dataset.edgeAction, identity = focused?.dataset.id;
        $("#edge-content", root).innerHTML = html;
        renderedContent = html;
        selectTab(activeTab);
        openHelp.forEach(id => { const help = $("#" + id, root); if (help) help.open = true; });
        if (focusId) {
          const replacement = $("#" + focusId, root);
          (focusSummary ? replacement?.querySelector("summary") : replacement)?.focus({preventScroll:true});
        } else if (action) {
          $$("[data-edge-action]", root).find(control => control.dataset.edgeAction === action && control.dataset.id === identity)?.focus({preventScroll:true});
        }
      }
      error();
    } catch (e) { if (!controller.signal.aborted) error(e.message); }
    finally { loading = false; }
  }
  async function mutate(path, method = "POST", body = {}) {
    const result = await request(path, method, body);
    toast("Configuration queued. Follow its result in the task dock.");
    await refresh(); return result;
  }
  async function routeForm(identity) {
    const route = data.settings.routes.find(r => r.id === identity);
    const clusters = state.devops?.clusters?.filter(c => c.status === "ready") || [];
    const inventories = new Map();
    const dialog = modal(route ? "Edit hostname route" : "Add hostname route", `<div class="form-grid">${field("Hostname", "hostname", route?.hostname || "")}${select("HTTPS method", "mode", [["letsencrypt","Let’s Encrypt · direct HTTPS"],["manual","Supplied certificate · direct HTTPS"],["cloudflare","Cloudflare Tunnel"]], route?.mode || "letsencrypt")}${select("Destination", "cluster_id", [["", "Application IPv4 addresses"], ["vm-manager", "VM Manager"], ...clusters.map(c => [c.id,c.name])], route?.backend === "vm-manager" ? "vm-manager" : route?.cluster_id || "")}</div><div id="edge-upstream-fields"></div><div id="edge-certificate-fields"></div><p class="hint">All routes share port 443 by hostname. Cloudflare is optional. Cloudflare hostnames share one tunnel whose only origin is this load balancer.</p>`, async form => {
      const choice = form.get("cluster_id"), backend = choice === "vm-manager" ? "vm-manager" : "http";
      const cluster_id = backend === "vm-manager" ? "" : choice;
      const selected = String(form.get("service") || "").split("/");
      const body = {id:route?.id || "", hostname:form.get("hostname"), mode:form.get("mode"), backend, cluster_id,
        namespace:cluster_id ? selected[0] : "default", service:cluster_id ? selected[1] : "", service_port:+(form.get("service_port") || 80),
        upstreams:cluster_id || backend === "vm-manager" ? [] : String(form.get("upstreams") || "").split(/[\s,]+/).filter(Boolean),
        email:form.get("email") || "", certificate:form.get("certificate") || "", private_key:form.get("private_key") || ""};
      await mutate("/routes", "POST", body);
    }, {wide:true, submit:"Save route"});
    function certificates() {
      const mode = $("[name=mode]", dialog).value;
      $("#edge-certificate-fields", dialog).innerHTML = mode === "manual" ? `<p>Paste a PEM certificate chain and matching private key.${route?.has_certificate ? " Leave both blank to keep the stored pair." : ""}</p><div class="form-grid"><label class="field"><span>Certificate chain</span><textarea name="certificate" rows="6" autocomplete="off" spellcheck="false"></textarea></label><label class="field"><span>Private key</span><textarea name="private_key" rows="6" autocomplete="off" spellcheck="false"></textarea></label></div>` : mode === "letsencrypt" ? field("Certificate renewal contact email", "email", route?.email || "") + '<p class="hint">Point public DNS at this host and forward TCP 443. Certificate issuance begins after saving; Caddy renews it automatically.</p>' : '<p class="hint">Configure Cloudflare with a scoped API token first. The app creates the hostname’s DNS record and uses one shared tunnel.</p>';
    }
    async function upstreams() {
      const id = $("[name=cluster_id]", dialog).value;
      const target = $("#edge-upstream-fields", dialog);
      if (id === "vm-manager") { target.innerHTML = '<p class="hint">Forward to VM Manager’s existing HTTPS origin with certificate verification. Direct HTTPS works with supplied certificates or Let’s Encrypt.</p>'; return; }
      if (!id) { target.innerHTML = field("Application addresses (IPv4:port, separated by commas)", "upstreams", route?.upstreams?.join(", ") || "") + '<p class="hint">Use the application’s IP address and listening port, for example <code>10.0.0.10:8080</code>. Multiple addresses share requests between copies of the application.</p>'; return; }
      target.innerHTML = '<p>Loading Services…</p>';
      try {
        if (!inventories.has(id)) inventories.set(id, await api(`/devops/clusters/${id}/resources`));
        if (!dialog.open || $("[name=cluster_id]", dialog).value !== id) return;
        const services = inventories.get(id).services.filter(s => s.ports.some(p => p.nodePort && (p.protocol || "TCP") === "TCP"));
        target.innerHTML = select("Service", "service", services.map(s => [`${s.namespace}/${s.name}`,`${s.namespace}/${s.name}`]), route ? `${route.namespace}/${route.service}` : "kube-system/traefik") + field("Service port", "service_port", route?.service_port || 80, {type:"number",min:1,max:65535}) + '<p class="hint">Select an HTTP NodePort or LoadBalancer backend. For Kubernetes Ingress rules, use kube-system/traefik port 80; the hostname must match the Ingress.</p>';
      } catch (e) { target.textContent = e.message; }
    }
    dialog.addEventListener("change", event => { if (event.target.name === "mode") certificates(); if (event.target.name === "cluster_id") upstreams(); });
    certificates(); await upstreams();
  }
  root.addEventListener("keydown", event => {
    const tab = event.target.closest("[role=tab]");
    if (!tab || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = edgeTabs.findIndex(([id]) => id === tab.dataset.edgeTab);
    const next = event.key === "Home" ? 0 : event.key === "End" ? edgeTabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + edgeTabs.length) % edgeTabs.length;
    selectTab(edgeTabs[next][0], true);
  }, {signal:controller.signal});
  root.addEventListener("click", async event => {
    const tab = event.target.closest("[data-edge-tab]");
    if (tab) { selectTab(tab.dataset.edgeTab, true); return; }
    const target = event.target.closest("[data-edge-action]");
    if (!target || target.disabled) return;
    target.disabled = true;
    try {
      const {edgeAction:action,id} = target.dataset;
      if (action === "refresh") return await refresh();
      if (!can("admin")) throw new Error("Load balancer configuration requires an administrator");
      if (action === "launch" || action === "reconcile") await mutate("/" + action);
      else if (action === "route") await routeForm(id);
      else if (action === "bind") {
        const enabled = (data.settings.excluded_clusters || []).includes(id);
        if (enabled) await mutate("/clusters/" + id, "PUT", {enabled});
        else confirmAction("Pause automatic integration", state.devops.clusters.find(c => c.id === id).name, "Pause automatic load-balancer integration for this cluster and remove its TCP listeners and external Service addresses. Existing claimed Services retain their load-balancer class. Hostname routes remain configured.", () => mutate("/clusters/" + id, "PUT", {enabled}));
      } else if (action === "remove") {
        const route = data.settings.routes.find(r => r.id === id);
        confirmAction("Delete hostname route", route.hostname, "Withdraw this route and any DNS record created by VM Manager. Existing DNS records and the shared tunnel are retained.", () => mutate("/routes/" + id, "DELETE", {confirm_name:route.hostname}));
      } else if (action === "cloudflare") {
        const cf = data.settings.cloudflare;
        modal("Cloudflare connection", `<p>Use an API token scoped to this account and zone with Cloudflare Tunnel Edit, Zone Read and DNS Edit. The app reuses the existing VM Manager tunnel when present; otherwise it creates one shared tunnel. Conflicting DNS records are retained.</p><div class="form-grid">${field("Account ID", "account_id", cf.account_id || "")}${field("Zone ID", "zone_id", cf.zone_id || "")}${field("API token (stored privately)", "token", "", {type:"password"})}</div>`, async form => mutate("/cloudflare", "PUT", Object.fromEntries(form)), {submit:"Save connection"});
      }
    } catch (e) { if (!controller.signal.aborted) error(e.message); }
    finally { target.disabled = false; }
  }, {signal:controller.signal});
  const timer = setInterval(() => { if (!document.hidden) refresh(); }, 15000);
  refresh();
  return () => { controller.abort(); clearInterval(timer); };
}
