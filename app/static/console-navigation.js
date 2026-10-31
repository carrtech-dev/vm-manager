// Embedded navigation switches the existing frames, retaining the text shell
// and scrollback. Standalone viewers retain their normal links and fullscreen.
export function bindConsoleNavigation({ linkId, mode, fullscreenId = "fullscreen" }) {
  const embedded = new URLSearchParams(location.search).get("embedded") === "1" && window.parent !== window;
  const link = document.getElementById(linkId);
  const button = document.getElementById(fullscreenId);
  if (embedded) {
    if (button) button.hidden = true; // The parent keeps the view switch visible in fullscreen.
    if (link) link.textContent = mode === "text" ? "Switch to text terminal" : "Switch to SPICE";
    link?.addEventListener("click", event => {
      event.preventDefault();
      window.parent.postMessage({ type: "vm-console-switch", mode }, location.origin);
    });
    return;
  }
  button?.addEventListener("click", async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await document.documentElement.requestFullscreen();
    } catch {
      const status = document.getElementById("console-status");
      status.textContent = "Full screen is unavailable in this browser.";
    }
  });
  document.addEventListener("fullscreenchange", () => {
    if (button) button.textContent = document.fullscreenElement ? "Exit full screen" : "Full screen";
  });
}
