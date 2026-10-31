import { esc } from "./core.js";

const modes = new Map();
const graphicalLabel = provider => provider === "incus" ? "SPICE" : "VNC";
const viewURL = (vm, mode) => `${mode === "text" ? "/terminal" : "/console"}?vm=${encodeURIComponent(vm)}`;

export function renderEmbeddedConsole(vm) {
  const mode = vm.provider === "incus" ? modes.get(vm.id) || "text" : "graphical";
  const graphic = graphicalLabel(vm.provider);
  const button = (kind, label) => `<button type="button" class="button small secondary" data-action="console-mode" data-id="${kind}" data-console-mode="${kind}" aria-pressed="${mode === kind}">${label}</button>`;
  const frame = kind => `<iframe class="console-frame" title="${esc(vm.name)} ${kind === "text" ? "text terminal" : graphic + " console"}" data-console-frame="${kind}" data-src="${esc(viewURL(vm.id, kind))}&amp;embedded=1" ${mode === kind ? `src="${esc(viewURL(vm.id, kind))}&amp;embedded=1"` : "hidden"} allow="fullscreen; clipboard-read; clipboard-write"></iframe>`;
  return `<section id="vm-console" class="vm-console" aria-label="VM console" data-vm="${esc(vm.id)}" data-provider="${esc(vm.provider)}" data-mode="${mode}">
    <div class="console-intro"><div class="console-view-switch" role="group" aria-label="Console view">${vm.provider === "incus" ? button("text", "Text terminal") : ""}${button("graphical", graphic)}</div>
    <span data-console-description>${mode === "text" ? "Text terminal · Requires a connected guest agent" : graphic + " · Click inside to capture keyboard input"}</span>
    <div class="actions"><button type="button" class="button small secondary" data-action="console-fullscreen" aria-pressed="false">Full screen</button><a class="button small secondary" data-console-open href="${esc(viewURL(vm.id, mode))}" target="_blank" rel="noopener">Open in new tab ↗</a></div></div>
    ${vm.provider === "incus" ? frame("text") : ""}${frame("graphical")}</section>`;
}

export function switchEmbeddedConsole(mode, root = document.getElementById("vm-console")) {
  if (!root || !["text", "graphical"].includes(mode) || (mode === "text" && root.dataset.provider !== "incus")) return;
  modes.set(root.dataset.vm, mode);
  root.dataset.mode = mode;
  for (const frame of root.querySelectorAll("[data-console-frame]")) {
    frame.hidden = frame.dataset.consoleFrame !== mode;
    if (!frame.hidden && !frame.hasAttribute("src")) frame.setAttribute("src", frame.dataset.src);
    if (!frame.hidden) frame.contentWindow?.focus();
  }
  for (const button of root.querySelectorAll("[data-console-mode]"))
    button.setAttribute("aria-pressed", String(button.dataset.consoleMode === mode));
  root.querySelector("[data-console-open]").href = viewURL(root.dataset.vm, mode);
  root.querySelector("[data-console-description]").textContent = mode === "text"
    ? "Text terminal · Requires a connected guest agent"
    : graphicalLabel(root.dataset.provider) + " · Click inside to capture keyboard input";
}

export async function toggleConsoleFullscreen(root = document.getElementById("vm-console")) {
  if (!root) return;
  if (document.fullscreenElement === root) await document.exitFullscreen();
  else await root.requestFullscreen();
}

export function bindEmbeddedConsoleEvents() {
  document.addEventListener("fullscreenchange", () => {
    const root = document.getElementById("vm-console");
    const button = root?.querySelector('[data-action="console-fullscreen"]');
    if (!button) return;
    const active = document.fullscreenElement === root;
    button.textContent = active ? "Exit full screen" : "Full screen";
    button.setAttribute("aria-pressed", String(active));
  });
  window.addEventListener("message", event => {
    if (event.origin !== location.origin || event.data?.type !== "vm-console-switch") return;
    const root = document.getElementById("vm-console");
    if (!root || ![...root.querySelectorAll("[data-console-frame]")].some(frame => frame.contentWindow === event.source)) return;
    switchEmbeddedConsole(event.data.mode, root);
  });
}
