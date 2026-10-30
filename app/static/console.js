import RFB from "/static/vendor/novnc/core/rfb.js";
import { copyValue, bindCopyButtons } from "./core.js";
bindCopyButtons();
const vm = new URLSearchParams(location.search).get("vm");
const status = document.getElementById("console-status");
let connection, retryTimer, healthTimer;
let generation = 0, leaving = false, retryDelay = 2000;
function closeConnection() {
  connection?.stop?.();
  connection?.disconnect?.();
  connection = null;
}
function reconnectSoon(text) {
  if (leaving || retryTimer) return;
  message(`${text} Reconnecting…`, true);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect().catch(e => reconnectSoon(e.message));
  }, retryDelay);
  retryDelay = Math.min(30000, retryDelay * 2);
}
function connected(protocol) {
  retryDelay = 2000;
  message(`Connected · ${protocol} · Keep-alive active`);
}
function message(text, error = false) {
  status.textContent = text;
  status.className = error ? "error" : "";
}
async function connect() {
  const attempt = ++generation;
  closeConnection();
  clearInterval(healthTimer);
  clearTimeout(retryTimer);
  retryTimer = null;
  if (!vm || !/^(incus|libvirt):[a-zA-Z][\w-]{0,62}$/.test(vm)) {
    message("Invalid virtual machine", true);
    return;
  }
  document.getElementById("console-title").innerHTML = copyValue(vm.split(":")[1]);
  const response = await fetch("/api/me");
  if (!response.ok) {
    message(
      "Your session expired. Sign in to the manager and reconnect.",
      true,
    );
    return;
  }
  const user = await response.json();
  if (attempt !== generation || leaving) return;
  const uri = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/console/${encodeURIComponent(vm)}?csrf=${encodeURIComponent(user.csrf)}`;
  document.getElementById("spice-screen").replaceChildren();
  message("Connecting to display…");
  if (vm.startsWith("incus:")) {
    const Spice = await import("/vendor/spice/main.js");
    if (attempt !== generation || leaving) return;
    connection = new Spice.SpiceMainConn({
      uri,
      screen_id: "spice-screen",
      message_id: "spice-message",
      onerror: (e) => { if (attempt === generation) reconnectSoon(e?.message || "Console disconnected."); },
      onsuccess: () => { if (attempt === generation) connected("SPICE"); },
    });
  } else {
    connection = new RFB(document.getElementById("spice-screen"), uri, {
      wsProtocols: ["binary"],
    });
    connection.scaleViewport = true;
    connection.resizeSession = true;
    connection.addEventListener("connect", () => { if (attempt === generation) connected("VNC"); });
    connection.addEventListener("disconnect", () =>
      attempt === generation && reconnectSoon("Console disconnected."),
    );
    connection.addEventListener("credentialsrequired", () =>
      message(
        "This existing VM has a VNC password; use its native viewer or update its display settings.",
        true,
      ),
    );
  }
  // The server sends protocol-level ping/pong on every channel. Watch SPICE's
  // separate input channel too: a healthy display does not prove input is alive.
  healthTimer = setInterval(() => {
    if (attempt !== generation || leaving || !connection) return;
    const channels = [connection, connection.inputs, connection.display, connection.cursor].filter(Boolean);
    if (channels.some(c => c.ws && c.ws.readyState >= WebSocket.CLOSING))
      reconnectSoon("A console channel disconnected.");
  }, 5000);
}
document.getElementById("reconnect").addEventListener("click", () => {
  retryDelay = 2000;
  connect().catch((e) => reconnectSoon(e.message));
});
document
  .getElementById("fullscreen")
  .addEventListener("click", () =>
    document.getElementById("console-screen").requestFullscreen(),
  );
document.getElementById("send-keys").addEventListener("click", async () => {
  if (connection?.sendCtrlAltDel) connection.sendCtrlAltDel();
  else {
    const Spice = await import("/vendor/spice/main.js");
    Spice.sendCtrlAltDel(connection);
  }
});
window.addEventListener("beforeunload", () => {
  leaving = true;
  generation++;
  clearTimeout(retryTimer);
  clearInterval(healthTimer);
  closeConnection();
});
connect().catch((e) => reconnectSoon(e.message));
