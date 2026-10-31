import { test } from "node:test";
import assert from "node:assert/strict";
import { pasteKeys, consoleKeySender, typeConsoleText, MAX_PASTE_LENGTH } from "../app/static/console-paste.js";

test("installer commands preserve case, punctuation and whitespace for US/UK guests", () => {
  const command = "sudo sh -c 'mount -t 9p config /mnt && echo \"$HOME\"; printf %s /mnt/agent/install.sh'";
  for (const layout of ["us", "uk"]) {
    assert.equal(pasteKeys(command, layout).map(key => String.fromCodePoint(key.keysym)).join(""), command);
    const ascii = Array.from({ length: 95 }, (_, i) => String.fromCharCode(i + 32)).join("");
    assert.equal(pasteKeys(ascii, layout).length, 95);
    assert.deepEqual(pasteKeys("\r\n\r\n\t", layout).map(key => key.code), ["Enter", "Enter", "Tab"]);
    assert.deepEqual(pasteKeys("aAzZ09", layout).map(key => key.shift), [false, true, false, true, false, false]);
  }
  assert.deepEqual(pasteKeys('@"#\\|', "uk").map(key => [key.scan, key.shift]), [[40, true], [3, true], [43, false], [86, false], [86, true]]);
  assert.deepEqual(pasteKeys('@"#\\|', "us").map(key => [key.scan, key.shift]), [[3, true], [40, true], [4, true], [43, false], [43, true]]);
  assert.equal(pasteKeys("£¬", "uk").length, 2);
});

test("invalid text and oversized transfers fail before transmission", () => {
  for (const text of ["", "ok\x00", "ok\x1b", "oké", "ok🙂", "a".repeat(MAX_PASTE_LENGTH + 1)])
    assert.throws(() => pasteKeys(text));
  assert.throws(() => pasteKeys("a", "unknown"));
  assert.equal(pasteKeys("a".repeat(MAX_PASTE_LENGTH)).length, MAX_PASTE_LENGTH);
});

test("VNC typing releases shortcut modifiers, brackets Shift, and adds no Enter", async () => {
  const events = [], progress = [], delays = [];
  const send = consoleKeySender({ sendKey: (...args) => events.push(args) });
  await typeConsoleText(pasteKeys("aA!"), send, () => true,
    (...args) => progress.push(args), async ms => delays.push(ms));
  assert.equal(events.slice(0, 8).every(event => event[2] === false), true);
  assert.deepEqual(events.slice(8), [
    [97, "KeyA", true], [97, "KeyA", false],
    [0xffe1, "ShiftLeft", true], [65, "KeyA", true], [65, "KeyA", false], [0xffe1, "ShiftLeft", false],
    [0xffe1, "ShiftLeft", true], [33, "Digit1", true], [33, "Digit1", false], [0xffe1, "ShiftLeft", false],
  ]);
  assert.deepEqual(progress, [[3, 3]]);
  assert.deepEqual(delays, [10]);
});

test("SPICE sends make/break scan codes on the input channel including extended modifier releases", async () => {
  const events = [];
  const spice = {
    Messages: { SpiceMsgcKeyDown: class {}, SpiceMsgcKeyUp: class {},
      SpiceMiniData: class { build_msg(type, key) { this.type = type; this.code = key.code; } } },
    Constants: { SPICE_MSGC_INPUTS_KEY_DOWN: 101, SPICE_MSGC_INPUTS_KEY_UP: 102 },
  };
  const connection = { inputs: { state: "ready", ws: { readyState: 1 }, send_msg: message => events.push([message.type, message.code]) } };
  await typeConsoleText(pasteKeys("A"), consoleKeySender(connection, spice), () => true, undefined, async () => {});
  assert.deepEqual(events[0], [102, 0x9d]);
  assert.deepEqual(events[1], [102, 0x9de0]);
  assert.deepEqual(events.slice(8), [[101, 42], [101, 30], [102, 158], [102, 170]]);
  connection.inputs.ws.readyState = 3;
  assert.throws(() => consoleKeySender(connection, spice)(pasteKeys("a")[0], true), /disconnected/);
});

test("cancellation and reconnection stop without replaying text to another connection", async () => {
  let active = true;
  const sent = [];
  await assert.rejects(typeConsoleText(pasteKeys("a".repeat(32)), (key, down) => { if (down) sent.push(key.code); },
    () => active, undefined, async () => { active = false; }), /Sending stopped/);
  assert.deepEqual(sent, Array(16).fill("KeyA"));
  await assert.rejects(typeConsoleText(pasteKeys("a"), () => assert.fail("must not send"), () => false), /Sending stopped/);
});

const installerCommand = `sudo sh -c 'set -eu; d=$(mktemp -d /run/incus-agent.XXXXXX); trap "cd /; umount $d; rmdir $d" EXIT; mount -t 9p -o ro config "$d" || mount -o ro /dev/disk/by-id/scsi-0QEMU_QEMU_CD-ROM_incus_agent "$d"; cd "$d"; ./install.sh; cd /; umount "$d"; rmdir "$d"; trap - EXIT; systemctl start incus-agent'`;

test("Linux text console mode preserves the reported installer command without layout-sensitive keys", async () => {
  const events = [], delays = [];
  const sequence = pasteKeys(installerCommand, "linux");
  assert.equal(sequence.map(key => String.fromCodePoint(Number(key.numeric))).join(""), installerCommand);
  await typeConsoleText(sequence, (key, down) => events.push([key, down]), () => true,
    undefined, async ms => delays.push(ms));
  let digits = "", decoded = "", alt = false;
  for (const [key, down] of events.slice(8)) {
    if (key.code === "AltLeft") {
      if (down) { alt = true; digits = ""; }
      else { decoded += String.fromCodePoint(Number(digits)); alt = false; }
    } else {
      assert.equal(alt, true);
      assert.match(key.code, /^Numpad[0-9]$/);
      if (down) digits += key.code.slice(-1);
    }
  }
  assert.equal(decoded, installerCommand);
  assert.equal(delays.length, Math.ceil(installerCommand.length / 16));
  assert.deepEqual(pasteKeys("\r\n\t", "linux"), [{numeric:"10"}, {numeric:"9"}]);
  for (const text of ["oké", "ok\x1b", "ok\0", "ok🙂"])
    assert.throws(() => pasteKeys(text, "linux"), /ASCII/);
});

test("numeric entry releases Alt when a transmission fails", async () => {
  const events = [];
  await assert.rejects(typeConsoleText(pasteKeys("a", "linux"), (key, down) => {
    events.push([key.code, down]);
    if (key.code === "Numpad9" && down) throw new Error("Disconnected");
  }, () => true), /Disconnected/);
  assert.deepEqual(events.at(-1), ["AltLeft", false]);
});

test("a failed key transmission releases Shift", async () => {
  const events = [];
  await assert.rejects(typeConsoleText(pasteKeys("A"), (key, down) => {
    events.push([key.code, down]);
    if (key.code === "KeyA" && down) throw new Error("Disconnected");
  }, () => true), /Disconnected/);
  assert.deepEqual(events.at(-1), ["ShiftLeft", false]);
});
