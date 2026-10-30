import { test } from "node:test";
import assert from "node:assert/strict";
import { copyValue, copyAddresses, copyText } from "../app/static/core.js";

test("copy controls escape untrusted hostnames in text and attributes", () => {
  const html = copyValue('host"><img src=x onerror=alert(1)>');
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes('data-copy="host&quot;&gt;&lt;img'));
  assert.ok(html.includes('type="button"'));
  assert.ok(html.includes('aria-label="Copy hostname'));
});

test("each IPv4/IPv6 address or subnet has its own exact copy value", () => {
  const html = copyAddresses(["192.0.2.10", "2001:db8::10", "10.0.0.1/24"]);
  assert.equal((html.match(/data-copy=/g) || []).length, 3);
  for (const value of ["192.0.2.10", "2001:db8::10", "10.0.0.1/24"])
    assert.ok(html.includes(`data-copy="${value}"`));
  assert.equal((copyAddresses("192.0.2.1, 192.0.2.2").match(/data-copy=/g) || []).length, 2);
  assert.equal(copyAddresses([], "Not reported"), "Not reported");
  for (const value of ["none", "auto", "disabled", "—"])
    assert.ok(!copyAddresses(value).includes("data-copy"));
});

test("clipboard uses the modern API and falls back inside a modal without losing selection", async (t) => {
  const descriptors = Object.fromEntries(["navigator", "document"].map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  t.after(() => {
    for (const [key, descriptor] of Object.entries(descriptors)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  let copied;
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async value => { copied = value; } } } });
  await copyText("2001:db8::1");
  assert.equal(copied, "2001:db8::1");

  for (const denied of [false, true]) {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: denied
      ? { clipboard: { writeText: async () => { throw new Error("Denied"); } } } : {} });
    const calls = [];
    const focused = { tagName: "INPUT", selectionStart: 1, selectionEnd: 4, selectionDirection: "forward",
      focus: () => calls.push("focus"), setSelectionRange: (...range) => calls.push(range) };
    const input = { style: {}, setAttribute() {}, select: () => calls.push("select"), remove: () => calls.push("remove") };
    const range = {};
    const selection = { rangeCount: 1, getRangeAt: () => range,
      removeAllRanges: () => calls.push("clear selection"), addRange: r => assert.equal(r, range) };
    let success = true;
    Object.defineProperty(globalThis, "document", { configurable: true, value: {
      activeElement: focused, getSelection: () => selection, createElement: () => input,
      querySelector: () => ({ append: node => { assert.equal(node, input); calls.push("modal"); } }),
      execCommand: command => { assert.equal(command, "copy"); copied = input.value; return success; },
    } });
    await copyText("192.0.2.5");
    assert.equal(copied, "192.0.2.5");
    assert.deepEqual(calls, ["modal", "select", "remove", "focus", [1, 4, "forward"], "clear selection"]);
    success = false;
    await assert.rejects(copyText("192.0.2.6"), /Copy failed/);
    assert.equal(calls.filter(call => call === "remove").length, 2);
  }
});
