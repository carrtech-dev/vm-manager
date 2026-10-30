export const state = {
  user: null,
  inventory: null,
  tasks: [],
  users: [],
  audit: [],
  page: "overview",
  vm: null,
  tab: "summary",
  search: "",
  status: "all",
  provider: "all",
  project: "all",
  tag: "all",
  samples: [],
  taskFilter: "all",
};
export const matchesOrganization = (resource) =>
  (state.project === "all" ||
    (state.project === "unassigned"
      ? !resource.project_id
      : resource.project_id === state.project)) &&
  (state.tag === "all" ||
    (state.tag === "untagged"
      ? !resource.tag_ids?.length
      : resource.tag_ids?.includes(state.tag)));
export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [
  ...root.querySelectorAll(selector),
];
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
// Keep the value and its control together, including when an address list wraps.
export const copyButton = (value, label = "value") =>
  `<button type="button" class="copy-button" data-copy="${esc(value)}" aria-label="${esc(`Copy ${label} ${value}`)}" title="${esc(`Copy ${label}`)}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v11a1 1 0 0 0 1 1h4"/></svg></button>`;
export const copyValue = (value, label = "hostname", content = esc(value)) =>
  `<span class="copy-value">${content}${copyButton(value, label)}</span>`;
const missingAddresses = new Set(["none", "auto", "disabled", "—"]);
export const copyAddresses = (value, fallback = "None") => {
  const values = (Array.isArray(value) ? value : String(value || "").split(","))
    .map(v => String(v).trim()).filter(Boolean);
  return values.length ? values.map(v => missingAddresses.has(v.toLowerCase())
    ? esc(v) : copyValue(v, "IP address")).join(", ") : esc(fallback);
};
export async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    return;
  } catch {
    // Also support older browsers and LAN HTTP sessions without Clipboard API.
  }
  const focused = document.activeElement;
  const selection = document.getSelection();
  const ranges = Array.from({ length: selection?.rangeCount || 0 }, (_, i) => selection.getRangeAt(i));
  const inputSelection = focused && ["INPUT", "TEXTAREA"].includes(focused.tagName)
    ? [focused.selectionStart, focused.selectionEnd, focused.selectionDirection] : null;
  const input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("aria-label", "Copy to clipboard");
  input.style.cssText = "position:fixed;opacity:0;pointer-events:none";
  // A modal makes elements outside it inert, so the fallback must live inside it.
  (document.querySelector("dialog[open]") || document.body).append(input);
  try {
    input.select();
    if (!document.execCommand("copy")) throw new Error("Copy failed. Select the value and copy it manually.");
  } finally {
    input.remove();
    focused?.focus({ preventScroll: true });
    if (inputSelection?.[0] != null) focused.setSelectionRange(...inputSelection);
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach(range => selection.addRange(range));
    }
  }
}
function copyFeedback(message, type = "success") {
  $$(".copy-feedback").forEach(element => element.remove());
  toast(message, type, type === "success" ? 2500 : 7000).classList.add("copy-feedback");
}
export function bindCopyButtons() {
  document.addEventListener("click", async event => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (button.disabled) return;
    if (button.dataset.copying) return;
    button.dataset.copying = "true";
    button.setAttribute("aria-busy", "true");
    try {
      await copyText(button.dataset.copy);
      copyFeedback("Copied to clipboard");
    } catch {
      copyFeedback("Copy failed. Select the value and copy it manually.", "error");
    } finally {
      delete button.dataset.copying;
      button.removeAttribute("aria-busy");
    }
  }, true);
}
export const size = (bytes, decimals = 1) => {
  if (!bytes) return "—";
  const k = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** k).toFixed(k ? decimals : 0)} ${["B", "KiB", "MiB", "GiB", "TiB"][k]}`;
};
export const when = (value) =>
  new Date(typeof value === "number" ? value * 1000 : value).toLocaleString(
    "en-GB",
    {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    },
  );
export const duration = (seconds) =>
  seconds < 60
    ? `${Math.floor(seconds)}s`
    : seconds < 3600
      ? `${Math.floor(seconds / 60)}m`
      : seconds < 86400
        ? `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
        : `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
export const can = (permission) =>
  permission === "admin"
    ? state.user?.role === "administrator"
    : ["administrator", "operator"].includes(state.user?.role);
export async function api(path, method = "GET", body = null, options = {}) {
  const headers = {};
  if (state.user?.csrf) headers["X-CSRF-Token"] = state.user.csrf;
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch("/api" + path, { ...options, method, headers, body });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error("The server returned an unreadable response");
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "/login")
      window.dispatchEvent(new CustomEvent("session-expired"));
    const message = Array.isArray(data.detail)
      ? data.detail
          .map((x) => `${x.loc.slice(1).join(".")}: ${x.msg}`)
          .join("; ")
      : data.detail;
    throw new Error(message || "Request failed");
  }
  return data;
}
export function toast(message, type = "success", timeout = 7000) {
  const element = document.createElement("div");
  element.className = "toast " + type;
  element.textContent = message;
  ($("dialog[open] .dialog-toasts") || $("#toasts")).append(element);
  setTimeout(() => element.remove(), timeout);
  return element;
}
export function modal(
  title,
  content,
  onSubmit,
  { wide = false, submit = "Save changes", danger = false, copyTitle = "" } = {},
) {
  const dialog = $("#modal");
  dialog.dispatchEvent(new Event("modal-replace"));
  dialog.className = wide ? "wide" : "";
  dialog.innerHTML = `<form id="dialog-form"><header><div><span class="eyebrow">CARRTECH VM MANAGER</span><h2>${esc(title)}${copyTitle ? copyButton(copyTitle, "hostname") : ""}</h2></div><button type="button" class="icon-button close-modal" aria-label="Close dialog">×</button></header><div class="dialog-content">${content}</div><div class="form-error" role="alert" hidden></div><div class="dialog-toasts" aria-live="polite"></div><footer><button type="button" class="button secondary close-modal">Cancel</button><button class="button ${danger ? "danger" : "primary"}" type="submit">${esc(submit)}</button></footer></form>`;
  $$(".close-modal", dialog).forEach((b) => (b.onclick = () => dialog.close()));
  $("#dialog-form").onsubmit = async (event) => {
    event.preventDefault();
    const button = $("button[type=submit]", dialog);
    const error = $(".form-error", dialog);
    button.disabled = true;
    error.hidden = true;
    try {
      await onSubmit(new FormData(event.currentTarget));
      dialog.close();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  };
  dialog.showModal();
  return dialog;
}
export const field = (
  label,
  name,
  value = "",
  {
    type = "text",
    hint = "",
    required = true,
    min,
    max,
    step,
    placeholder,
    readonly = false,
  } = {},
) =>
  `<label class="field"><span>${esc(label)}</span><input name="${esc(name)}" type="${type}" ${type === "password" ? `autocomplete="${name === "current" ? "current-password" : "new-password"}"` : ""} value="${esc(value)}" ${required ? "required" : ""} ${min !== undefined ? `min="${min}"` : ""} ${max !== undefined ? `max="${max}"` : ""} ${step ? `step="${step}"` : ""} ${placeholder ? `placeholder="${esc(placeholder)}"` : ""} ${readonly ? "readonly" : ""}>${hint ? `<small>${esc(hint)}</small>` : ""}</label>`;
export const select = (label, name, options, value = "", hint = "") =>
  `<label class="field"><span>${esc(label)}</span><select name="${name}">${options
    .map((o) => {
      let [v, l] = Array.isArray(o) ? o : [o, o];
      return `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(l)}</option>`;
    })
    .join("")}</select>${hint ? `<small>${esc(hint)}</small>` : ""}</label>`;
export const check = (label, name, checked = false, hint = "") =>
  `<label class="check"><input type="checkbox" name="${name}" ${checked ? "checked" : ""}><span>${esc(label)}${hint ? `<small>${esc(hint)}</small>` : ""}</span></label>`;
export const textarea = (label, name, value = "") =>
  `<label class="field full"><span>${esc(label)}</span><textarea name="${name}" rows="3" maxlength="1000">${esc(value)}</textarea></label>`;
export function confirmAction(title, resource, message, fn) {
  const dialog = modal(
    title,
    `<div class="warning">${esc(message)}</div>${field(`Type “${resource}” to confirm`, "confirm_name", "", { placeholder: resource })}`,
    async (f) => {
      if (f.get("confirm_name") !== resource)
        throw new Error("The name does not match");
      await fn();
    },
    { submit: title, danger: true },
  );
  $("input[name=confirm_name]", dialog).autocomplete = "off";
}
export async function queue(path, method, body) {
  const result = await api(path, method, body);
  toast("Task queued. Follow its progress in the task log.");
  window.dispatchEvent(new CustomEvent("refresh-tasks"));
  return result;
}
