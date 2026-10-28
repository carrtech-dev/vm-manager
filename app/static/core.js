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
export async function api(path, method = "GET", body = null) {
  const headers = {};
  if (state.user?.csrf) headers["X-CSRF-Token"] = state.user.csrf;
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch("/api" + path, { method, headers, body });
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
export function toast(message, type = "success") {
  const element = document.createElement("div");
  element.className = "toast " + type;
  element.textContent = message;
  $("#toasts").append(element);
  setTimeout(() => element.remove(), 7000);
}
export function modal(
  title,
  content,
  onSubmit,
  { wide = false, submit = "Save changes", danger = false } = {},
) {
  const dialog = $("#modal");
  dialog.className = wide ? "wide" : "";
  dialog.innerHTML = `<form id="dialog-form"><header><div><span class="eyebrow">CARRTECH VM MANAGER</span><h2>${esc(title)}</h2></div><button type="button" class="icon-button close-modal" aria-label="Close dialog">×</button></header><div class="dialog-content">${content}</div><div class="form-error" role="alert" hidden></div><footer><button type="button" class="button secondary close-modal">Cancel</button><button class="button ${danger ? "danger" : "primary"}" type="submit">${esc(submit)}</button></footer></form>`;
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
