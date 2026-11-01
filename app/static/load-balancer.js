import {state, $, $$, esc, can, api, modal, field, select, confirmAction, toast} from "./core.js";

const button = (label, action, id = "") => `<button class="button secondary" type="button" data-edge-action="${action}" data-id="${esc(id)}">${esc(label)}</button>`;
export function edgeScreen() {
  return `<div id="edge-screen"><div class="page-title"><div><a href="#devops">← DevOps</a><h1>Load balancer</h1><p>One routing layer for VM Manager, applications and Kubernetes Services.</p></div></div><p id="edge-error" class="warning" role="alert" hidden></p><div id="edge-content"><p role="status">Loading load balancer…</p></div></div>`;
}
export function edgeContent(data) {
  const {settings:s, status} = data;
  const admin = can("admin");
  const clusters = state.devops?.clusters?.filter(c => c.status === "ready") || [];
  return `<section class="panel"><div class="panel-header"><h3>Managed edge</h3><span class="badge ${status.state === "ready" ? "running" : ""}">${esc(status.state)}</span></div><p role="status">${esc(status.message)}</p><p>${s.address ? `HTTPS address: <strong>${esc(s.address)}:443</strong> · ` : ""}Caddy handles HTTPS and certificates; HAProxy handles TCP listeners.</p><div class="actions">${admin ? (!s.enabled ? button("Launch load balancer", "launch") : button("Apply / retry configuration", "reconcile") + button("Add hostname route", "route") + button("Cloudflare connection", "cloudflare")) : ""}${button("Refresh", "refresh")}</div><p class="hint">Internet → Cloudflare Tunnel or direct HTTPS → load balancer → application. The existing VM Manager tunnel is switched only after proxy health checks pass. Direct port 8443 remains available. Let’s Encrypt uses TLS-ALPN on port 443; public DNS and firewall/NAT forwarding must reach this host.</p></section>
    <section class="panel"><h3>Hostname routes</h3><div class="table-wrap"><table><thead><tr><th>Hostname</th><th>HTTPS</th><th>Upstream targets</th><th>Actions</th></tr></thead><tbody>${s.routes.map(r => `<tr><td>${esc(r.hostname)}${r.id.startsWith("manager") ? " · VM Manager" : ""}</td><td>${esc({manual:"Supplied certificate",letsencrypt:"Let’s Encrypt",cloudflare:"Cloudflare Tunnel",manager:"VM Manager certificate"}[r.mode])}</td><td>${esc(r.backend === "vm-manager" ? "VM Manager (verified HTTPS origin)" : r.cluster_id ? `${clusters.find(c => c.id === r.cluster_id)?.name || r.cluster_id} · ${r.namespace}/${r.service}:${r.service_port}` : r.upstreams.join(", "))}</td><td>${admin && !r.id.startsWith("manager") ? button("Edit", "route", r.id) + button("Delete", "remove", r.id) : "Protected route"}</td></tr>`).join("") || '<tr><td colspan="4">Launch the load balancer to add routes.</td></tr>'}</tbody></table></div><p class="hint">Certificate issuance and renewal run in Caddy. Manual certificates must match the hostname and be renewed by their owner. Cloudflare terminates public TLS and carries all hostnames through one tunnel to this routing layer.</p></section>
    <section class="panel"><h3>Kubernetes integration</h3><p>Connect a cluster, then create a Service with <code>type: LoadBalancer</code> and <code>loadBalancerClass: ${esc(data.load_balancer_class)}</code>. Services created in VM Manager use this class automatically.</p><p class="hint">The controller reconciles every 15 seconds and publishes the edge host’s IP after listener configuration succeeds. TCP uses ports 1024–65535. Ports 443, 8443 and control ports are reserved; share HTTPS using hostname routes to a NodePort Service or the cluster’s Traefik Service. Nodes must be reachable from this host. This is a single host load balancer.</p>${clusters.map(c => `<div class="panel-header"><span><a href="#devops/${esc(c.id)}">${esc(c.name)}</a> · ${(s.clusters || []).includes(c.id) ? "Connected" : "Not connected"}</span>${admin && s.enabled ? button((s.clusters || []).includes(c.id) ? "Disconnect" : "Connect cluster", "bind", c.id) : ""}</div>`).join("") || '<p>No ready clusters.</p>'}<div class="table-wrap"><table><thead><tr><th>Service</th><th>Ports</th><th>Address / state</th></tr></thead><tbody>${(status.services || []).map(r => `<tr><td>${esc(r.namespace)}/${esc(r.name)}</td><td>${esc(r.ports.join(", "))}</td><td>${esc(r.address || r.state)}${r.message ? " · " + esc(r.message) : ""}</td></tr>`).join("") || '<tr><td colspan="3">No Services assigned to this controller.</td></tr>'}</tbody></table></div></section>
    <section class="panel"><h3>Cloudflare transport</h3><p>${s.cloudflare.tunnel_id ? `Tunnel: ${esc(s.cloudflare.tunnel_id)} · ${s.cloudflare.local ? "existing VM Manager connector" : "managed connector"}` : "No tunnel configured. Direct HTTPS works without Cloudflare."}</p><p>${s.cloudflare.configured ? "API token stored · " + esc(s.cloudflare.zone_name || s.cloudflare.zone_id) : "Provide a scoped API token to publish additional hostnames automatically."}${status.tunnel_status ? " · " + esc(status.tunnel_status) : ""}</p></section>`;
}

export function mountEdgeScreen(root) {
  const controller = new AbortController();
  let data, loading = false;
  const request = (path, method = "GET", body = null) => api("/devops/load-balancer" + path, method, body, {signal:controller.signal});
  function error(message = "") { const e = $("#edge-error", root); e.textContent = message; e.hidden = !message; }
  async function refresh() {
    if (loading || controller.signal.aborted) return;
    loading = true;
    try {
      data = await request("");
      if (controller.signal.aborted) return;
      $("#edge-content", root).innerHTML = edgeContent(data); error();
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
    const dialog = modal(route ? "Edit hostname route" : "Add hostname route", `<div class="form-grid">${field("Hostname", "hostname", route?.hostname || "")}${select("HTTPS method", "mode", [["letsencrypt","Let’s Encrypt · direct HTTPS"],["manual","Supplied certificate · direct HTTPS"],["cloudflare","Cloudflare Tunnel"]], route?.mode || "letsencrypt")}${select("Backend", "cluster_id", [["", "IPv4 targets"], ["vm-manager", "VM Manager"], ...clusters.map(c => [c.id,c.name])], route?.backend === "vm-manager" ? "vm-manager" : route?.cluster_id || "")}</div><div id="edge-upstream-fields"></div><div id="edge-certificate-fields"></div><p class="hint">All routes share port 443 by hostname. Cloudflare is optional. Cloudflare hostnames share one tunnel whose only origin is this load balancer.</p>`, async form => {
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
      if (!id) { target.innerHTML = field("Upstreams (IPv4:port, separated by commas)", "upstreams", route?.upstreams?.join(", ") || ""); return; }
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
  root.addEventListener("click", async event => {
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
        const enabled = !(data.settings.clusters || []).includes(id);
        if (enabled) await mutate("/clusters/" + id, "PUT", {enabled});
        else confirmAction("Disconnect cluster", state.devops.clusters.find(c => c.id === id).name, "Remove the cluster’s TCP listeners and external Service addresses. Hostname routes remain configured.", () => mutate("/clusters/" + id, "PUT", {enabled}));
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
