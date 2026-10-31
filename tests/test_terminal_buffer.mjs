import { test } from "node:test";
import assert from "node:assert/strict";
import { terminalText, terminalPaste, inputMessages } from "../app/static/terminal-buffer.js";

test("terminal output copy joins wrapped screen rows without inventing line breaks", () => {
  const rows = [
    { text: "old output", isWrapped: false },
    { text: "a long command ", isWrapped: false },
    { text: "continues here", isWrapped: true },
    { text: "next line", isWrapped: false },
    { text: "", isWrapped: false },
  ];
  assert.equal(terminalText({ length: rows.length, getLine: i => ({
    isWrapped: rows[i]?.isWrapped, translateToString: trim => trim ? rows[i].text.trimEnd() : rows[i].text,
  }) }), "old output\na long command continues here\nnext line");
});

test("terminal paste preserves spaces, punctuation and Unicode without extra Enter keys", () => {
  const command = `printf '%s' "quotes: ' \\ | $ ; @ £ é 🙂"`;
  assert.equal(terminalPaste(command), command);
  assert.equal(terminalPaste("first\r\nsecond\rthird"), "first\nsecond\nthird");
  assert.throws(() => terminalPaste("oops\x1b[31m"), /control characters/);
  assert.throws(() => terminalPaste("é".repeat(16385)), /32 KiB/);
});

test("input chunks preserve complete Unicode characters and exact multiline text", () => {
  const value = "a".repeat(8191) + "🙂\n" + "é".repeat(20000);
  const chunks = inputMessages(value).map(value => JSON.parse(value));
  assert.equal(chunks.map(chunk => chunk.data).join(""), value);
  for (const chunk of chunks) {
    assert.equal(chunk.type, "input");
    assert.ok(new TextEncoder().encode(chunk.data).length <= 65536);
    assert.doesNotMatch(chunk.data, /^[\udc00-\udfff]|[\ud800-\udbff]$/);
  }
});
