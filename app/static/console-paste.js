// Physical keys work in installers and text consoles without a guest agent.
export const MAX_PASTE_LENGTH = 4096;
const keys = new Map();
function row(plain, shifted, scans, codes) {
  [...plain].forEach((character, i) => {
    keys.set(character, { scan: scans[i], code: codes[i], shift: false });
    if (shifted[i] !== character)
      keys.set(shifted[i], { scan: scans[i], code: codes[i], shift: true });
  });
}
row("1234567890-=", "!@#$%^&*()_+", Array.from({ length: 12 }, (_, i) => i + 2),
  [..."1234567890"].map(c => `Digit${c}`).concat("Minus", "Equal"));
row("qwertyuiop[]", "QWERTYUIOP{}", Array.from({ length: 12 }, (_, i) => i + 16),
  [..."QWERTYUIOP"].map(c => `Key${c}`).concat("BracketLeft", "BracketRight"));
row("asdfghjkl;'`", 'ASDFGHJKL:"~', [...Array.from({ length: 11 }, (_, i) => i + 30), 41],
  [..."ASDFGHJKL"].map(c => `Key${c}`).concat("Semicolon", "Quote", "Backquote"));
row("zxcvbnm,./", "ZXCVBNM<>?", Array.from({ length: 10 }, (_, i) => i + 44),
  [..."ZXCVBNM"].map(c => `Key${c}`).concat("Comma", "Period", "Slash"));
row("\\ ", "| ", [43, 57], ["Backslash", "Space"]);
keys.set("\n", { scan: 28, code: "Enter", shift: false });
keys.set("\t", { scan: 15, code: "Tab", shift: false });

export function pasteKeys(text, layout = "us") {
  if (!["linux", "us", "uk"].includes(layout)) throw new Error("Choose a paste destination.");
  if (!text.length) throw new Error("Paste some text first.");
  if (text.length > MAX_PASTE_LENGTH) throw new Error(`Send at most ${MAX_PASTE_LENGTH.toLocaleString("en-GB")} characters at a time.`);
  const characters = [...text.replace(/\r\n?/g, "\n")];
  if (layout === "linux") {
    // Linux virtual consoles accept decimal Alt+numpad input independently of
    // letter/punctuation mappings, Caps Lock and Num Lock. This is not a
    // desktop-terminal input method; retain physical keys for those guests.
    return characters.map(character => {
      const value = character.codePointAt(0);
      if ((value < 32 || value > 126) && character !== "\n" && character !== "\t")
        throw new Error(`Linux text console paste supports ASCII text. Remove ${JSON.stringify(character)} before sending.`);
      return { numeric: String(value) };
    });
  }
  const mapping = new Map(keys);
  if (layout === "uk") {
    for (const [character, scan, code, shift] of [
      ['"', 3, "Digit2", true], ["@", 40, "Quote", true],
      ["£", 4, "Digit3", true], ["#", 43, "Backslash", false],
      ["~", 43, "Backslash", true], ["\\", 86, "IntlBackslash", false],
      ["|", 86, "IntlBackslash", true], ["¬", 41, "Backquote", true],
    ]) mapping.set(character, { scan, code, shift });
  }
  return characters.map(character => {
    const key = mapping.get(character);
    if (!key) throw new Error(`This keyboard layout cannot type ${JSON.stringify(character)}. Remove it before sending.`);
    return { ...key, keysym: character === "\n" ? 0xff0d : character === "\t" ? 0xff09 : character.codePointAt(0) };
  });
}

const modifiers = [
  { scan: 29, code: "ControlLeft", keysym: 0xffe3 },
  { scan: 0xe01d, code: "ControlRight", keysym: 0xffe4 },
  { scan: 56, code: "AltLeft", keysym: 0xffe9 },
  { scan: 0xe038, code: "AltRight", keysym: 0xffea },
  { scan: 42, code: "ShiftLeft", keysym: 0xffe1 },
  { scan: 54, code: "ShiftRight", keysym: 0xffe2 },
  { scan: 0xe05b, code: "MetaLeft", keysym: 0xffeb },
  { scan: 0xe05c, code: "MetaRight", keysym: 0xffec },
];
const shift = modifiers[4];
const alt = modifiers[2];
const numpad = [82, 79, 80, 81, 75, 76, 77, 71, 72, 73].map((scan, digit) =>
  ({ scan, code: `Numpad${digit}`, keysym: 0xffb0 + digit }));

export function consoleKeySender(connection, spice) {
  return (key, down) => {
    if (!spice) {
      connection.sendKey(key.keysym, key.code, down);
      return;
    }
    if (connection.inputs?.state !== "ready" || connection.inputs.ws?.readyState !== 1)
      throw new Error("Console input is disconnected. Reconnect before sending text.");
    const payload = new (down ? spice.Messages.SpiceMsgcKeyDown : spice.Messages.SpiceMsgcKeyUp)();
    const scan = key.scan | (down ? 0 : 0x80);
    payload.code = key.scan > 255 ? ((scan & 255) << 8) | 0xe0 : scan;
    const message = new spice.Messages.SpiceMiniData();
    message.build_msg(down ? spice.Constants.SPICE_MSGC_INPUTS_KEY_DOWN : spice.Constants.SPICE_MSGC_INPUTS_KEY_UP, payload);
    connection.inputs.send_msg(message);
  };
}

export async function typeConsoleText(sequence, sendKey, active, progress = () => {}, pause = ms => new Promise(resolve => setTimeout(resolve, ms))) {
  const check = () => {
    if (!active()) throw new Error("Sending stopped. Text already sent remains in the VM; review it before retrying.");
  };
  check();
  for (const modifier of modifiers) sendKey(modifier, false);
  for (let i = 0; i < sequence.length; i++) {
    check();
    const key = sequence[i];
    if (key.numeric) {
      try {
        sendKey(alt, true);
        for (const digit of key.numeric) {
          sendKey(numpad[Number(digit)], true);
          sendKey(numpad[Number(digit)], false);
        }
      } finally {
        sendKey(alt, false);
      }
    } else {
      try {
        if (key.shift) sendKey(shift, true);
        sendKey(key, true);
        sendKey(key, false);
      } finally {
        if (key.shift) sendKey(shift, false);
      }
    }
    // Small bursts avoid slow character-by-character animation while bounding
    // guest keyboard queues and allowing cancellation/reconnect checks.
    if ((i + 1) % 16 === 0 || i === sequence.length - 1) {
      progress(i + 1, sequence.length);
      await pause(10);
    }
  }
}

export function bindConsolePaste(getSession) {
  const dialog = document.getElementById("paste-dialog");
  const text = document.getElementById("paste-text");
  const layout = document.getElementById("paste-layout");
  const feedback = document.getElementById("paste-status");
  const confirm = document.getElementById("paste-confirm");
  const send = document.getElementById("paste-send");
  const read = document.getElementById("paste-read");
  const close = document.getElementById("paste-close");
  const destinationKey = `console-paste-destination:${new URLSearchParams(location.search).get("vm")}`;
  try {
    const saved = localStorage.getItem(destinationKey);
    if (["linux", "us", "uk"].includes(saved)) layout.value = saved;
  } catch { /* Storage may be disabled by the browser. */ }
  const destinationChanged = () => {
    document.getElementById("paste-destination-help").textContent = layout.value === "linux"
      ? "For the full-screen Linux login or shell console. Preserves quotes and punctuation regardless of US/UK keyboard layout, Caps Lock or Num Lock. For a terminal app on a desktop, choose its keyboard layout instead."
      : "For desktops, terminal apps and installers. Match the VM's keyboard layout and turn Caps Lock off. A wrong layout changes quotes and punctuation.";
  };
  destinationChanged();
  layout.addEventListener("change", () => {
    destinationChanged();
    try { localStorage.setItem(destinationKey, layout.value); } catch { /* Optional preference only. */ }
  });
  let sending = false, cancelled = false, readGeneration = 0;
  const multiline = () => /[\r\n]/.test(text.value);
  const changed = () => {
    readGeneration++;
    confirm.checked = false;
    document.getElementById("paste-lines").hidden = !multiline();
    feedback.textContent = "";
  };
  const open = () => {
    if (!dialog.open) dialog.showModal();
    text.focus();
  };
  const finish = () => {
    if (sending) {
      cancelled = true;
      return;
    }
    readGeneration++;
    text.value = "";
    changed();
    dialog.close();
    document.querySelector("#spice-screen canvas")?.focus();
  };
  document.getElementById("paste-open").addEventListener("click", open);
  text.addEventListener("input", changed);
  close.addEventListener("click", finish);
  dialog.addEventListener("cancel", event => { event.preventDefault(); finish(); });
  read.addEventListener("click", async () => {
    const attempt = ++readGeneration;
    try {
      if (!navigator.clipboard?.readText) throw new Error();
      const value = await navigator.clipboard.readText();
      if (attempt !== readGeneration || !dialog.open || sending) return;
      text.value = value;
      changed();
    } catch {
      if (attempt === readGeneration && dialog.open)
        feedback.textContent = "Clipboard access is unavailable. Paste into the text box with Cmd+V or Ctrl+V.";
    }
    text.focus();
  });
  // Capture before the console libraries swallow the browser paste shortcut.
  document.addEventListener("keydown", event => {
    if (!event.target.closest?.("#spice-screen") || !(event.metaKey || event.ctrlKey) || event.altKey || event.key.toLowerCase() !== "v") return;
    event.stopImmediatePropagation();
    // Keep the browser's default paste action, now targeting an editable field.
    open();
  }, true);
  document.addEventListener("paste", event => {
    if (!event.target.closest?.("#spice-screen")) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    open();
    text.value = event.clipboardData?.getData("text/plain") || "";
    changed();
  }, true);
  send.addEventListener("click", async () => {
    if (sending) return;
    const session = getSession();
    try {
      const sequence = pasteKeys(text.value, layout.value);
      if (multiline() && !confirm.checked) throw new Error("Confirm that line breaks may run commands before sending.");
      if (!session?.ready) throw new Error("Wait for the console to connect before sending text.");
      sending = true;
      cancelled = false;
      readGeneration++;
      for (const control of [text, layout, read, send, confirm]) control.disabled = true;
      close.textContent = "Stop sending";
      await typeConsoleText(sequence, consoleKeySender(session.connection, session.spice),
        () => !cancelled && dialog.open && getSession()?.ready && getSession()?.connection === session.connection,
        (count, total) => { feedback.textContent = `Sending ${count} of ${total} characters…`; });
      text.value = "";
      changed();
      feedback.textContent = "Text sent. No extra Enter key was added.";
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      sending = false;
      for (const control of [text, layout, read, send, confirm]) control.disabled = false;
      close.textContent = "Close";
    }
  });
}
