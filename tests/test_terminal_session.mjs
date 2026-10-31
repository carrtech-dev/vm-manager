import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("../app/static/terminal.js", import.meta.url), "utf8").replace(/^import .*;\n/gm, "");
const tick = () => new Promise(resolve => setImmediate(resolve));

async function browser() {
  const elements = new Map(), sockets = [], terminals = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, { listeners: {}, clientWidth: 0, clientHeight: 0,
      classList: { contains(value) { return element(id).className === value; } },
      addEventListener(kind, fn) { this.listeners[kind] = fn; } });
    return elements.get(id);
  };
  class Terminal {
    output = "";
    constructor() { terminals.push(this); }
    loadAddon() {} open() {} focus() {} attachCustomKeyEventHandler() {}
    onData(fn) { this.input = fn; }
    write(data, done) { this.output += new TextDecoder().decode(data); done(); }
    writeln(data) { this.output += data; }
  }
  class WebSocket {
    static OPEN = 1;
    readyState = 1;
    listeners = {};
    sent = [];
    constructor(url) { this.url = url; sockets.push(this); }
    addEventListener(kind, fn) { this.listeners[kind] = fn; }
    send(value) { this.sent.push(value); }
    message(data) { this.listeners.message({data: typeof data === "object" && !(data instanceof ArrayBuffer) ? JSON.stringify(data) : data}); }
    close(code = 1000) { this.readyState = 3; this.listeners.close?.({code}); }
  }
  let authRequests = 0;
  runInNewContext(source, {
    Terminal, FitAddon: class {}, WebSocket, URLSearchParams, Uint8Array, ArrayBuffer,
    location: {search: "?vm=incus:logout-test", protocol: "https:", host: "manager.test"},
    document: {getElementById: element}, window: {addEventListener() {}},
    ResizeObserver: class { observe() {} }, bindConsoleNavigation() {},
    fetch: async () => { authRequests++; return {ok: true, json: async () => ({role: "administrator", csrf: "token"})}; },
    inputMessages: value => [JSON.stringify({type: "input", data: value})],
  });
  await tick();
  return {sockets, terminals, element, authRequests: () => authRequests};
}

test("logout reconnects to guest login with authentication, retained output and no replay", async () => {
  const b = await browser(), first = b.sockets[0], terminal = b.terminals[0];
  first.message({type: "ready", mode: "login"});
  first.message(new TextEncoder().encode("previous output\r\nlogout\r\n").buffer);
  terminal.input("exit\r");
  first.message({type: "exit"});
  const sent = first.sent.length;
  terminal.input("must not reach the next login");
  assert.equal(first.sent.length, sent);
  first.close();
  await tick();
  assert.equal(b.sockets.length, 2);
  assert.equal(b.authRequests(), 2);
  assert.match(b.sockets[1].url, /mode=login$/);
  assert.equal(b.terminals.length, 1);
  assert.match(terminal.output, /previous output\r\nlogout/);
  assert.deepEqual(b.sockets[1].sent, []);
  b.sockets[1].message({type: "ready", mode: "login"});
  terminal.input("new-user\r");
  assert.match(b.sockets[1].sent[0], /new-user/);
});

for (const failure of ["disconnect", "error", "abnormal logout close"]) {
  test(`${failure} stays disconnected without automatic retries`, async () => {
    const b = await browser(), socket = b.sockets[0];
    socket.message({type: "ready", mode: "login"});
    if (failure === "error") socket.message({type: "error", message: "Guest unavailable"});
    if (failure === "abnormal logout close") socket.message({type: "exit"});
    socket.close(failure === "error" ? 1000 : 1006);
    await tick();
    assert.equal(b.sockets.length, 1);
  });
}

test("a replaced connection cannot reconnect after a stale logout", async () => {
  const b = await browser(), stale = b.sockets[0];
  stale.message({type: "exit"});
  b.element("reconnect").listeners.click();
  await tick();
  stale.close();
  await tick();
  assert.equal(b.sockets.length, 2);
});
