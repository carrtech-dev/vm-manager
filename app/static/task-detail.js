import { $, api } from "./core.js";

// One connection per expanded output panel. A cursor avoids replay on reopening.
export class TaskOutputStream {
  constructor(taskId, { append, status, Source = EventSource }) {
    Object.assign(this, { taskId, append, status, Source });
    this.cursor = 0;
    this.finished = false;
    this.source = null;
  }
  open() {
    if (this.source || this.finished) return;
    const source = new this.Source(`/api/tasks/${encodeURIComponent(this.taskId)}/output?after=${this.cursor}`);
    this.source = source;
    this.status("Connecting…");
    source.onopen = () => { if (this.source === source) this.status("Live host output"); };
    source.addEventListener("output", event => {
      if (this.source !== source) return;
      const data = JSON.parse(event.data);
      if (data.truncated) this.append("\n[Earlier output is no longer retained.]\n");
      for (const entry of data.entries) {
        if (entry.seq <= this.cursor) continue;
        this.append(entry.text);
        this.cursor = entry.seq;
      }
    });
    source.addEventListener("done", () => {
      if (this.source !== source) return;
      this.finished = true;
      this.close();
      this.status(this.cursor ? "Task finished · saved output" : "No host output was recorded for this task.");
    });
    source.addEventListener("denied", () => {
      if (this.source !== source) return;
      this.close();
      this.status("Access ended. Sign in again to view task output.");
    });
    source.onerror = () => {
      if (this.source === source) this.status("Connection interrupted · reconnecting…");
    };
  }
  close() {
    this.source?.close();
    this.source = null;
  }
}

export function watchTaskDetail(dialog, task, render) {
  const details = $(".task-output", dialog);
  const pre = $(".task-output-text", dialog);
  const hint = $(".task-output-status", dialog);
  const status = $(".task-status-note", dialog);
  const summary = $(".task-detail-properties", dialog);
  const stream = new TaskOutputStream(task.id, {
    append(text) {
      const follow = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 32;
      pre.textContent = (pre.textContent + text).slice(-262144);
      if (follow) pre.scrollTop = pre.scrollHeight;
    },
    status(text) { hint.textContent = text; },
  });
  let disposed = false, timer, request;
  const active = () => !disposed && dialog.open && details.isConnected;
  function syncOutput() {
    if (active() && details.open && !document.hidden) stream.open();
    else {
      stream.close();
      if (!stream.finished) hint.textContent = "Output paused. Expand to resume.";
    }
  }
  async function update() {
    clearTimeout(timer);
    if (!active() || document.hidden) return;
    request = new AbortController();
    try {
      task = await api(`/tasks/${encodeURIComponent(task.id)}`, "GET", null, { signal: request.signal });
      if (!active()) return;
      summary.innerHTML = render(task);
      status.textContent = ["running", "queued"].includes(task.status) ? "Live status · updates every second" : "Task finished";
    } catch (error) {
      if (!active() || error.name === "AbortError") return;
      status.textContent = `Status unavailable: ${error.message}. Retrying…`;
    } finally {
      request = null;
      if (active() && !document.hidden && ["running", "queued"].includes(task.status)) timer = setTimeout(update, 1000);
    }
  }
  function visibility() {
    syncOutput();
    if (document.hidden) {
      clearTimeout(timer);
      request?.abort();
    } else if (!request) update();
  }
  function dispose() {
    disposed = true;
    clearTimeout(timer);
    request?.abort();
    stream.close();
    details.removeEventListener("toggle", syncOutput);
    document.removeEventListener("visibilitychange", visibility);
    dialog.removeEventListener("close", dispose);
    dialog.removeEventListener("modal-replace", dispose);
    window.removeEventListener("hashchange", navigate);
  }
  function navigate() { dialog.close(); dispose(); }
  details.addEventListener("toggle", syncOutput);
  document.addEventListener("visibilitychange", visibility);
  dialog.addEventListener("close", dispose);
  dialog.addEventListener("modal-replace", dispose);
  window.addEventListener("hashchange", navigate);
  update();
  return dispose;
}
