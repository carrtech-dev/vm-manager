import { Terminal } from "./vendor/xterm/xterm.mjs";
import { FitAddon } from "./vendor/addon-fit/addon-fit.mjs";
import { copyText } from "./core.js";
import { terminalText, terminalPaste, inputMessages } from "./terminal-buffer.js";
import { bindConsoleNavigation } from "./console-navigation.js";

const vm = new URLSearchParams(location.search).get("vm");
bindConsoleNavigation({ linkId: "graphical-console", mode: "graphical" });
const status = document.getElementById("console-status");
const feedback = document.getElementById("terminal-feedback");
const screen = document.getElementById("terminal-screen");
const terminal = new Terminal({ scrollback: 5000, fontSize: 14, cursorBlink: true,
  theme: { background: "#0b1019", foreground: "#dce4ee" } });
const fit = new FitAddon();
terminal.loadAddon(fit);
terminal.open(screen);
let socket, ready = false, generation = 0;

function message(text, error = false) {
  status.textContent = text;
  status.className = error ? "error" : "";
}
function resize() {
  if (!screen.clientWidth || !screen.clientHeight) return;
  fit.fit();
  if (terminal.cols > 500 || terminal.rows > 200)
    terminal.resize(Math.min(500, terminal.cols), Math.min(200, terminal.rows));
  if (ready && socket?.readyState === WebSocket.OPEN)
    socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
}
new ResizeObserver(resize).observe(screen);
terminal.onData(data => {
  if (!ready || socket?.readyState !== WebSocket.OPEN) return;
  for (const chunk of inputMessages(data)) socket.send(chunk);
});

async function connect(mode = "login") {
  const attempt = ++generation;
  ready = false;
  socket?.close();
  if (!vm || !/^incus:[a-zA-Z][\w-]{0,62}$/.test(vm)) {
    message("Text terminals currently require an Incus VM.", true);
    return;
  }
  document.getElementById("terminal-title").textContent = vm.slice(6) + " · Terminal";
  document.getElementById("graphical-console").href = "/console?vm=" + encodeURIComponent(vm);
  message("Connecting to guest terminal…");
  const response = await fetch("/api/me");
  if (!response.ok) throw new Error("Your session expired. Sign in to the manager, then reconnect.");
  const user = await response.json();
  if (attempt !== generation) return;
  document.getElementById("terminal-root").hidden = user.role !== "administrator";
  if (mode === "root" && user.role !== "administrator") throw new Error("Root terminal access requires an administrator.");
  const current = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/terminal/${encodeURIComponent(vm)}?csrf=${encodeURIComponent(user.csrf)}&mode=${mode}`);
  socket = current;
  current.binaryType = "arraybuffer";
  current.addEventListener("message", event => {
    if (attempt !== generation) return;
    if (typeof event.data !== "string") {
      terminal.write(new Uint8Array(event.data), () => {
        if (attempt === generation && current.readyState === WebSocket.OPEN)
          current.send(JSON.stringify({ type: "ack" }));
      });
      return;
    }
    const data = JSON.parse(event.data);
    if (data.type === "ready") {
      ready = true;
      message(data.mode === "root" ? "Connected as root · Administrator session" : "VM login · Sign in with your VM account");
      resize();
      terminal.focus();
    } else if (data.type === "error") message(data.message, true);
  });
  current.addEventListener("close", () => {
    if (attempt !== generation) return;
    ready = false;
    if (!status.classList.contains("error")) message("Disconnected. Reconnect opens a new shell; output stays here.", true);
  });
  current.addEventListener("error", () => { if (attempt === generation) message("Terminal connection failed. Check the VM, guest agent and your session.", true); });
}
document.getElementById("reconnect").addEventListener("click", () => {
  terminal.writeln("\r\n── New terminal session ──\r\n");
  connect().catch(error => message(error.message, true));
});
document.getElementById("terminal-root").addEventListener("click", () => {
  terminal.writeln("\r\n── New root session ──\r\n");
  connect("root").catch(error => message(error.message, true));
});
async function copyOutput() {
  try {
    await copyText(terminal.getSelection() || terminalText(terminal.buffer.active));
    feedback.textContent = "Copied terminal text.";
  } catch (error) { feedback.textContent = error.message; }
}
document.getElementById("terminal-copy").addEventListener("click", copyOutput);
terminal.attachCustomKeyEventHandler(event => {
  if (event.type === "keydown" && event.key.toLowerCase() === "c" && (event.metaKey || (event.ctrlKey && event.shiftKey))) {
    event.preventDefault();
    copyOutput();
    return false;
  }
  return true;
});
document.getElementById("terminal-download").addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([terminalText(terminal.buffer.active)], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = (vm?.replace(":", "-") || "vm") + "-terminal.txt";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

const dialog = document.getElementById("paste-dialog");
const text = document.getElementById("paste-text");
const note = document.getElementById("paste-status");
const confirm = document.getElementById("paste-confirm");
let pasteRevision = 0;
const multiline = () => /[\r\n]/.test(text.value);
function changed() {
  pasteRevision++;
  confirm.checked = false;
  document.getElementById("paste-lines").hidden = !multiline();
  note.textContent = "";
}
function openPaste(value) {
  if (typeof value === "string") text.value = value;
  changed();
  if (!dialog.open) dialog.showModal();
  text.focus();
}
function closePaste() {
  text.value = "";
  changed();
  dialog.close();
  terminal.focus();
}
function paste(value) {
  if (!ready) throw new Error("Connect the terminal before pasting.");
  terminal.paste(terminalPaste(value));
}
screen.addEventListener("paste", event => {
  event.preventDefault();
  event.stopImmediatePropagation();
  const value = event.clipboardData?.getData("text/plain") || "";
  if (/[\r\n]/.test(value)) openPaste(value);
  else {
    try { paste(value); } catch (error) { feedback.textContent = error.message; }
  }
}, true);
document.getElementById("terminal-paste").addEventListener("click", () => openPaste());
text.addEventListener("input", changed);
document.getElementById("paste-close").addEventListener("click", closePaste);
dialog.addEventListener("cancel", event => { event.preventDefault(); closePaste(); });
document.getElementById("paste-read").addEventListener("click", async () => {
  const revision = pasteRevision;
  try {
    if (!navigator.clipboard?.readText) throw new Error();
    const value = await navigator.clipboard.readText();
    if (!dialog.open || revision !== pasteRevision) return;
    text.value = value;
    changed();
  } catch { note.textContent = "Paste into the text box with Cmd+V or Ctrl+V; clipboard access is unavailable."; }
  text.focus();
});
document.getElementById("paste-send").addEventListener("click", () => {
  try {
    if (multiline() && !confirm.checked) throw new Error("Confirm that line breaks may run commands before pasting.");
    paste(text.value);
    closePaste();
  } catch (error) { note.textContent = error.message; }
});
window.addEventListener("beforeunload", () => { generation++; ready = false; socket?.close(); });
connect().catch(error => message(error.message, true));
