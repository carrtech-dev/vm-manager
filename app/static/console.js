import RFB from "/static/vendor/novnc/core/rfb.js";
const vm = new URLSearchParams(location.search).get("vm");
const status = document.getElementById("console-status");
let connection;
function message(text, error = false) {
  status.textContent = text;
  status.className = error ? "error" : "";
}
async function connect() {
  if (!vm || !/^(incus|libvirt):[a-zA-Z][\w-]{0,62}$/.test(vm)) {
    message("Invalid virtual machine", true);
    return;
  }
  document.getElementById("console-title").textContent = vm.split(":")[1];
  const response = await fetch("/api/me");
  if (!response.ok) {
    message(
      "Your session expired. Sign in to the manager and reconnect.",
      true,
    );
    return;
  }
  const user = await response.json();
  const uri = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/console/${encodeURIComponent(vm)}?csrf=${encodeURIComponent(user.csrf)}`;
  document.getElementById("spice-screen").replaceChildren();
  message("Connecting to display…");
  if (vm.startsWith("incus:")) {
    const Spice = await import("/vendor/spice/main.js");
    connection = new Spice.SpiceMainConn({
      uri,
      screen_id: "spice-screen",
      message_id: "spice-message",
      onerror: (e) =>
        message(
          e?.message || "Console disconnected. Check that the VM is running.",
          true,
        ),
      onsuccess: () => message("Connected · SPICE"),
    });
  } else {
    connection = new RFB(document.getElementById("spice-screen"), uri, {
      wsProtocols: ["binary"],
    });
    connection.scaleViewport = true;
    connection.resizeSession = true;
    connection.addEventListener("connect", () => message("Connected · VNC"));
    connection.addEventListener("disconnect", () =>
      message("Console disconnected. Check that the VM is running.", true),
    );
    connection.addEventListener("credentialsrequired", () =>
      message(
        "This existing VM has a VNC password; use its native viewer or update its display settings.",
        true,
      ),
    );
  }
}
document.getElementById("reconnect").addEventListener("click", () => {
  connection?.stop?.();
  connection?.disconnect?.();
  connect().catch((e) => message(e.message, true));
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
  connection?.stop?.();
  connection?.disconnect?.();
});
connect().catch((e) => message(e.message, true));
