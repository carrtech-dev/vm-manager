export function terminalText(buffer) {
  const lines = [];
  for (let i = 0; i < buffer.length; i++) {
    const line = buffer.getLine(i);
    if (!line) continue;
    // Padding at a soft wrap may contain real separating spaces. Trim only
    // the final physical row of each logical output line.
    const value = line.translateToString(!buffer.getLine(i + 1)?.isWrapped);
    if (line.isWrapped && lines.length) lines[lines.length - 1] += value;
    else lines.push(value);
  }
  return lines.join("\n").replace(/\n+$/, "");
}

export function terminalPaste(text) {
  const value = text.replace(/\r\n?/g, "\n");
  if (new TextEncoder().encode(value).length > 32768) throw new Error("Paste up to 32 KiB at a time.");
  if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value)) throw new Error("Remove hidden control characters before pasting.");
  return value;
}

export function inputMessages(data) {
  const characters = Array.from(data), messages = [];
  for (let i = 0; i < characters.length; i += 8192)
    messages.push(JSON.stringify({ type: "input", data: characters.slice(i, i + 8192).join("") }));
  return messages;
}
