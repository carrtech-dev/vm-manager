import { test } from "node:test";
import assert from "node:assert/strict";
import { TaskOutputStream } from "../app/static/task-detail.js";

class Source {
  static opened = [];
  constructor(url) { this.url = url; this.handlers = {}; Source.opened.push(this); }
  addEventListener(type, fn) { this.handlers[type] = fn; }
  close() { this.closed = true; }
  send(type, data = {}) { this.handlers[type]({ data: JSON.stringify(data) }); }
}

test("output connects on expansion only, closes on collapse and resumes without duplication", () => {
  Source.opened = [];
  let text = "", status = "";
  const stream = new TaskOutputStream("task", { Source, append: value => text += value, status: value => status = value });
  assert.equal(Source.opened.length, 0);
  stream.open();
  const first = Source.opened[0];
  stream.open();
  assert.equal(Source.opened.length, 1);
  first.send("output", { entries: [{ seq: 1, text: "first" }] });
  stream.close();
  assert.ok(first.closed);
  first.send("output", { entries: [{ seq: 2, text: "stale" }] });
  assert.equal(text, "first");
  stream.open();
  const second = Source.opened[1];
  assert.match(second.url, /after=1$/);
  second.send("output", { entries: [{ seq: 1, text: "duplicate" }, { seq: 2, text: "second" }] });
  assert.equal(text, "firstsecond");
  second.send("done");
  assert.ok(second.closed);
  stream.open();
  assert.equal(Source.opened.length, 2);
  assert.match(status, /finished/);
});

test("truncated catchup is labelled and revoked access closes the connection", () => {
  let text = "", status = "";
  const stream = new TaskOutputStream("task", { Source, append: value => text += value, status: value => status = value });
  stream.open();
  const source = Source.opened.at(-1);
  source.send("output", { truncated: true, entries: [{ seq: 100, text: "latest" }] });
  assert.match(text, /Earlier output is no longer retained/);
  assert.equal(stream.cursor, 100);
  source.onerror();
  assert.match(status, /reconnecting/);
  source.send("denied");
  assert.ok(source.closed);
  assert.match(status, /Access ended/);
});
