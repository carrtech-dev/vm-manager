import { test } from "node:test";
import assert from "node:assert/strict";
import { renderEmbeddedConsole, switchEmbeddedConsole, toggleConsoleFullscreen, bindEmbeddedConsoleEvents } from "../app/static/console-view.js";

function consoleRoot(provider = "incus") {
  const frames = ["text", "graphical"].filter(mode => provider === "incus" || mode === "graphical").map(mode => ({
    dataset: { consoleFrame: mode, src: `/${mode === "text" ? "terminal" : "console"}?vm=${provider}%3Aview-test&embedded=1` },
    hidden: mode !== "text", src: mode === "text" ? "/terminal?existing-session" : null, loads: 0,
    hasAttribute: function () { return this.src !== null; },
    setAttribute: function (key, value) { assert.equal(key, "src"); this.src = value; this.loads++; },
    contentWindow: { focus() {} },
  }));
  const buttons = ["text", "graphical"].map(mode => ({ dataset: { consoleMode: mode },
    setAttribute(key, value) { this[key] = value; } }));
  const open = {}, description = {}, fullscreen = { setAttribute(key, value) { this[key] = value; } };
  return { dataset: { vm: `${provider}:view-test`, provider }, frames, buttons, open, fullscreen,
    querySelectorAll: selector => selector === "[data-console-frame]" ? frames : buttons,
    querySelector: selector => selector === "[data-console-open]" ? open : selector === "[data-console-description]" ? description : fullscreen,
  };
}

test("Incus starts with embedded text and defers the SPICE connection until selected", () => {
  const html = renderEmbeddedConsole({id:"incus:default-test",name:'name"><bad>',provider:"incus"});
  assert.match(html, /data-mode="text"/);
  const graphical = html.match(/<iframe[^>]*data-console-frame="graphical"[^>]*>/)[0];
  assert.match(graphical, / hidden /);
  assert.doesNotMatch(graphical, /\ssrc=/);
  assert.match(html, /data-action="console-fullscreen"/);
  assert.doesNotMatch(html, /<bad>/);
});

test("switches retain both iframe instances and load SPICE only once", () => {
  const root = consoleRoot();
  const original = [...root.frames];
  switchEmbeddedConsole("graphical", root);
  assert.equal(root.frames[0].hidden, true);
  assert.equal(root.frames[1].hidden, false);
  assert.equal(root.frames[1].loads, 1);
  assert.equal(root.open.href, "/console?vm=incus%3Aview-test");
  switchEmbeddedConsole("text", root);
  assert.equal(root.frames[0].hidden, false);
  assert.equal(root.frames[0].loads, 0, "text shell must never be reloaded by a mode switch");
  assert.equal(root.open.href, "/terminal?vm=incus%3Aview-test");
  assert.equal(root.buttons[0]["aria-pressed"], "true");
  switchEmbeddedConsole("graphical", root);
  assert.equal(root.frames[1].loads, 1);
  assert.deepEqual(root.frames, original);
  assert.match(renderEmbeddedConsole({id:root.dataset.vm,name:"view-test",provider:"incus"}), /data-mode="graphical"/);
  const vnc = consoleRoot("libvirt");
  switchEmbeddedConsole("text", vnc);
  assert.equal(vnc.open.href, undefined);
});

test("fullscreen expands the console container and messages can switch only from its own frames", async (t) => {
  const saved = Object.fromEntries(["document", "window", "location"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  t.after(() => { for (const [key, descriptor] of Object.entries(saved)) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor); else delete globalThis[key];
  } });
  const root = consoleRoot(), listeners = {};
  const document = { fullscreenElement: null, getElementById: () => root,
    addEventListener: (kind, fn) => listeners[kind] = fn,
    exitFullscreen: async () => { document.fullscreenElement = null; } };
  root.requestFullscreen = async () => { document.fullscreenElement = root; };
  for (const [key, value] of Object.entries({document, window: {addEventListener: (kind, fn) => listeners[kind] = fn}, location: {origin:"https://manager.test"}}))
    Object.defineProperty(globalThis, key, { configurable:true, value });
  bindEmbeddedConsoleEvents();
  await toggleConsoleFullscreen(root);
  listeners.fullscreenchange();
  assert.equal(document.fullscreenElement, root);
  assert.equal(root.fullscreen.textContent, "Exit full screen");
  await toggleConsoleFullscreen(root);
  assert.equal(document.fullscreenElement, null);
  for (const [origin, source] of [["https://elsewhere.test",root.frames[0].contentWindow], ["https://manager.test",{}]])
    listeners.message({origin,source,data:{type:"vm-console-switch",mode:"graphical"}});
  assert.equal(root.frames[1].loads, 0);
  listeners.message({origin:"https://manager.test",source:root.frames[0].contentWindow,data:{type:"vm-console-switch",mode:"graphical"}});
  assert.equal(root.frames[1].loads, 1);
});
