import { App } from "@modelcontextprotocol/ext-apps";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { SearchAddon } from "@xterm/addon-search";
import { LiveInput } from "./live-input.js";
const app = new App({ name: "tmux fleet terminal", version: "0.3.1" }),
  $ = (id) => document.getElementById(id),
  requestId = () =>
    Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) =>
      value.toString(16).padStart(2, "0"),
    ).join("");
let connected = false,
  ended = false,
  state,
  panes = [],
  targetId = null,
  attachment = null,
  cursor = "start",
  generation = 0,
  outputTimer,
  stateTimer,
  draftTimer,
  dirty = false,
  busy = false,
  pending = null,
  inputAuthority = null,
  typingStopped = false,
  refreshing = false;
const terminal = new Terminal({
    fontFamily: "ui-monospace,SFMono-Regular,Menlo,monospace",
    fontSize: 13,
    scrollback: 2000,
    convertEol: false,
    allowProposedApi: false,
    linkHandler: {
      activate: () =>
        notice(
          "Select and copy a link explicitly; terminal output cannot navigate this view.",
        ),
    },
  }),
  fit = new FitAddon(),
  search = new SearchAddon();
terminal.loadAddon(fit);
terminal.loadAddon(search);
terminal.open($("terminal"));
terminal.parser.registerOscHandler(52, () => true);
terminal.parser.registerOscHandler(8, () => true);
const notice = (message, error = false) => {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
};
function decode(result) {
  const value =
    result.structuredContent ||
    JSON.parse(
      result.content?.find((row) => row.type === "text")?.text || "{}",
    );
  if (result.isError || value.error) {
    const error = Error(
      value.error?.message ||
        value.result?.error?.message ||
        "The operation did not complete.",
    );
    error.receipt = value.result;
    error.definitive = true;
    throw error;
  }
  return value.result ?? value;
}
async function call(name, args = {}) {
  if (!connected) throw Error("The host is not connected.");
  return decode(
    await app.callServerTool({ name: "tmux_fleet_" + name, arguments: args }),
  );
}
async function resource(uri) {
  const result = await app.readServerResource({ uri });
  const text = result.contents?.find((row) => row.text)?.text;
  if (!text) throw Error("The host did not return the requested resource.");
  return JSON.parse(text);
}
function theme(context) {
  if (context?.theme)
    document.documentElement.style.colorScheme = context.theme;
  for (const [key, value] of Object.entries(context?.styles?.variables || {}))
    if (key.startsWith("--") && typeof value === "string")
      document.documentElement.style.setProperty(key, value);
  requestAnimationFrame(() => {
    const style = getComputedStyle(document.documentElement);
    terminal.options.theme = {
      background:
        style.getPropertyValue("--color-background-primary").trim() ||
        (context?.theme === "dark" ? "#17191e" : "#ffffff"),
      foreground:
        style.getPropertyValue("--color-text-primary").trim() ||
        (context?.theme === "dark" ? "#eceef3" : "#242933"),
      cursor: context?.theme === "dark" ? "#98b1ff" : "#365bd9",
      selectionBackground: "#6c87db66",
    };
    syncWidth();
  });
}
function syncWidth() {
  const screen = terminal.element?.querySelector(".xterm-screen");
  if (screen)
    $("terminal").style.width =
      Math.ceil(screen.getBoundingClientRect().width) + "px";
}
function grant() {
  return state?.grants.find(
    (item) =>
      item.target_id === targetId &&
      item.active &&
      (item.scope !== "attachment" ||
        item.attachment_id === attachment?.attachment_id) &&
      (item.expires_at === null || item.expires_at * 1000 > Date.now()) &&
      (item.remaining_bytes === null || item.remaining_bytes > 0),
  );
}
function context() {
  if (connected)
    app
      .updateModelContext({
        structuredContent: {
          target_id: targetId,
          shared_view_version: state?.view.version,
          draft: $("draft").value.slice(0, 2000),
          draft_is_authority: false,
          input_grant: grant()?.id || null,
          pending_request: pending?.args.request_id || null,
          management_draft: {
            action: $("manage-action").value,
            name: $("name").value,
            cwd: $("cwd").value,
          },
          terminal_output_in_context: false,
        },
      })
      .catch(() => {});
}
function controls() {
  const active = grant();
  $("writable").textContent =
    active && typingStopped
      ? "Typing paused"
      : active
        ? active.scope === "attachment"
          ? "Typing enabled · until this view closes"
          : `Typing enabled · ${Math.ceil((active.expires_at * 1000 - Date.now()) / 1000)}s · ${active.remaining_bytes} bytes left`
        : "Read only";
  $("terminal").classList.toggle("readonly", !active || typingStopped);
  $("enable").disabled =
    !attachment ||
    !state?.allow_input ||
    busy ||
    !!pending ||
    (active?.scope === "attachment" && !typingStopped);
  $("readonly").disabled = !active || busy;
  $("detach").disabled = !targetId || busy;
  $("detach").textContent = attachment ? "Detach view" : "Open view";
  $("type").disabled = $("paste").disabled =
    !targetId || !state?.allow_input || busy || !!pending;
  $("fit").disabled = !targetId || !state?.allow_management || busy;
  $("manage").disabled = !state?.allow_management || busy || !!pending;
  for (const button of document.querySelectorAll("[data-key]"))
    button.disabled = !targetId || !state?.allow_input || busy || !!pending;
  $("inspect").hidden = !pending;
  $("reviewed").hidden = !pending?.unknown;
}
function renderReceipts() {
  const list = $("receipts");
  list.replaceChildren();
  for (const row of state.operations) {
    const item = document.createElement("li");
    item.textContent = `${row.action} · ${row.status}${row.error ? " · " + row.error.message : ""} `;
    const code = document.createElement("code");
    code.textContent = row.request_id;
    item.append(code);
    list.append(item);
  }
}
function renderPanes() {
  const query = $("filter").value.toLowerCase();
  $("pane").replaceChildren(new Option("Choose a pane", ""));
  for (const pane of panes) {
    const label = `${pane.session} / ${pane.window} · ${pane.pane_id}`;
    if (!query || label.toLowerCase().includes(query) || pane.id === targetId)
      $("pane").append(new Option(label, pane.id));
  }
  $("pane").value = targetId || "";
}
async function saveDraft() {
  clearTimeout(draftTimer);
  if (!dirty || !state) return;
  const version = state.view.version,
    text = $("draft").value,
    selected = targetId;
  const saved = await call("save_view", {
    target_id: selected,
    draft: text,
    follow: state.view.follow,
    expected_version: version,
  });
  state.view = saved;
  if ($("draft").value === text && targetId === selected) dirty = false;
  context();
}
async function selectPane(id, { external = false } = {}) {
  if (id === targetId && attachment) return;
  liveInput.clear();
  inputAuthority = null;
  typingStopped = false;
  if (dirty) await saveDraft();
  const previous = attachment;
  generation++;
  clearTimeout(outputTimer);
  attachment = null;
  cursor = "start";
  targetId = id || null;
  terminal.reset();
  if (previous)
    await call("detach", { attachment_id: previous.attachment_id }).catch(
      () => {},
    );
  if (!external) {
    state.view = await call("save_view", {
      target_id: targetId,
      draft: "",
      follow: true,
      expected_version: state.view.version,
    });
    $("draft").value = "";
    dirty = false;
  }
  renderPanes();
  const pane = panes.find((row) => row.id === targetId);
  $("identity").textContent = pane
    ? `${pane.session_id} / ${pane.window_id} / ${pane.pane_id} · ${pane.cols} × ${pane.rows}`
    : targetId || "No pane selected";
  if (targetId) {
    attachment = await call("attach", { target_id: targetId });
    pollOutput(generation);
  }
  controls();
  context();
}
async function refresh({ fleet = false } = {}) {
  if (refreshing) return;
  refreshing = true;
  try {
    if (fleet) {
      const roster = await call("fleet");
      panes = roster.panes;
      $("scope").textContent =
        roster.socket +
        " · " +
        (roster.available ? "configured server" : "no server running");
      renderPanes();
    }
    const next = await resource("tmux-fleet://state");
    if (!state) {
      state = next;
      targetId = null;
      $("draft").value = next.view.draft || "";
      await selectPane(next.view.target_id, { external: true });
    } else if (!dirty && !busy) {
      const changed = next.view.target_id !== targetId;
      state = next;
      $("draft").value = next.view.draft || "";
      if (changed) await selectPane(next.view.target_id, { external: true });
    } else {
      state = { ...next, view: state.view };
    }
    if (!pending) {
      const unknown = (state.unresolved_inputs || state.operations).find(
        (row) =>
          row.action === "input" &&
          row.target_id === targetId &&
          row.status === "unknown" &&
          !row.reviewed_at,
      );
      if (unknown) {
        liveInput.clear();
        pending = {
          name: "input",
          args: { request_id: unknown.request_id },
          unknown: true,
        };
      }
    }
    renderReceipts();
    controls();
    context();
  } finally {
    refreshing = false;
  }
}
async function pollOutput(epoch) {
  if (ended || epoch !== generation || !attachment) return;
  try {
    if (document.hidden) {
      outputTimer = setTimeout(() => pollOutput(epoch), 1200);
      return;
    }
    const frame = await resource(
      `tmux-fleet://terminal/${attachment.attachment_id}/${cursor}`,
    );
    if (epoch !== generation) return;
    const bytes = Uint8Array.from(atob(frame.data), (char) =>
      char.charCodeAt(0),
    );
    if (frame.reset) {
      terminal.reset();
      terminal.resize(frame.cols, frame.rows);
      $("transport").textContent = frame.lost_history
        ? "Reconnected with a visible-screen reset; older output and private emulator modes may be incomplete."
        : "Live read-only stream. Screen resets reconstruct visible ANSI text/cursor; Fit tmux window explicitly changes shared geometry.";
      if (frame.truncated)
        $("transport").textContent += " Snapshot truncated at 256 KiB.";
    } else if (terminal.cols !== frame.cols || terminal.rows !== frame.rows)
      terminal.resize(frame.cols, frame.rows);
    await new Promise((resolve) => terminal.write(bytes, resolve));
    if (epoch !== generation) return;
    cursor = frame.next_cursor;
    syncWidth();
    outputTimer = setTimeout(() => pollOutput(epoch), frame.poll_after_ms);
  } catch (error) {
    if (epoch !== generation) return;
    liveInput.clear();
    typingStopped = true;
    controls();
    notice(
      "Terminal disconnected: " + error.message + " Input is not replayed.",
      true,
    );
    outputTimer = setTimeout(() => pollOutput(epoch), 1500);
  }
}
async function effect(name, args) {
  await liveInput.flush();
  if (pending)
    throw Error(
      "Inspect the previous uncertain receipt before starting another action.",
    );
  const intent = {
    name,
    args: { ...args, request_id: requestId(), actor: "app-reported" },
  };
  pending = intent;
  controls();
  context();
  try {
    const result = await call(name, intent.args);
    pending = null;
    return result;
  } catch (error) {
    if (error.definitive && error.receipt?.status !== "unknown") pending = null;
    throw error;
  } finally {
    controls();
    context();
  }
}
async function action(fn, message) {
  if (busy) return;
  busy = true;
  controls();
  try {
    await fn();
    await refresh();
    notice(message || "Action completed.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    controls();
    context();
  }
}
$("refresh").onclick = () =>
  action(() => refresh({ fleet: true }), "Fleet refreshed.");
$("filter").oninput = () => {
  renderPanes();
  context();
};
$("pane").onchange = () =>
  action(
    () => selectPane($("pane").value),
    "Viewing this exact pane; no input authority was added.",
  );
$("draft").oninput = () => {
  dirty = true;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(
    () =>
      saveDraft().catch((error) =>
        notice(error.message + " Your local draft is retained.", true),
      ),
    600,
  );
  context();
};
$("enable").onclick = () =>
  action(async () => {
    const previous = grant();
    if (previous) await call("revoke_input", { grant_id: previous.id });
    await effect("authorize_input", {
      target_id: targetId,
      confirmed: true,
      attachment_id: attachment.attachment_id,
    });
    typingStopped = false;
    await refresh();
    terminal.focus();
  }, "Typing enabled until you choose Read only or close this view.");
$("readonly").onclick = () =>
  action(() => {
    liveInput.clear();
    typingStopped = true;
    return call("revoke_input", { grant_id: grant().id });
  }, "Input grant revoked.");
$("detach").onclick = () => {
  const opening = !attachment;
  return action(
    async () => {
      if (opening) {
        await selectPane(targetId, { external: true });
        return;
      }
      liveInput.clear();
      typingStopped = true;
      generation++;
      clearTimeout(outputTimer);
      await call("detach", { attachment_id: attachment.attachment_id });
      attachment = null;
      terminal.reset();
    },
    opening
      ? "Read-only view opened."
      : "View detached. tmux work is still running.",
  );
};
async function sendDraft(kind) {
  await saveDraft();
  await effect("input", {
    target_id: targetId,
    kind,
    value: $("draft").value,
    submit: kind === "paste" && $("submit").checked,
    confirmed: true,
  });
  state.view = await call("save_view", {
    target_id: targetId,
    draft: "",
    follow: true,
    expected_version: state.view.version,
  });
  $("draft").value = "";
  dirty = false;
}
$("type").onclick = () =>
  action(
    () => sendDraft("text"),
    "Draft typed without Enter. Delivery does not prove command success.",
  );
$("paste").onclick = () =>
  action(
    () => sendDraft("paste"),
    "Paste delivered. Inspect the terminal for its outcome.",
  );
for (const button of document.querySelectorAll("[data-key]"))
  button.onclick = () =>
    action(
      () =>
        effect("input", {
          target_id: targetId,
          kind: "keys",
          value: button.dataset.key,
          confirmed: true,
        }),
      "Key delivered.",
    );
$("inspect").onclick = () =>
  action(async () => {
    const receipt = await call("operation", {
      request_id: pending.args.request_id,
    });
    if (receipt.status === "running")
      throw Error("The receipt is still running; wait and inspect again.");
    if (receipt.status === "unknown") {
      pending.unknown = true;
      controls();
      throw Error(
        "Outcome unknown. Inspect the terminal; this input will not be replayed. Use Outcome reviewed only after deciding on new work.",
      );
    }
    pending = null;
    typingStopped = false;
  }, "Receipt inspected. Nothing was replayed.");
$("reviewed").onclick = () =>
  action(async () => {
    await call("acknowledge_operation", {
      request_id: pending.args.request_id,
      confirmed: true,
    });
    pending = null;
    typingStopped = false;
  }, "Outcome marked reviewed; nothing was replayed.");
$("manage").onclick = () =>
  action(async () => {
    if (!$("confirm-manage").checked)
      throw Error("Confirm this management action first.");
    const operation = $("manage-action").value;
    await effect("manage", {
      action: operation,
      target_id: operation === "create_session" ? null : targetId,
      name: $("name").value || null,
      cwd: $("cwd").value || null,
      confirmed: true,
    });
    $("confirm-manage").checked = false;
    await refresh({ fleet: true });
  }, "Management receipt recorded; fleet refreshed.");
$("fit").onclick = () =>
  action(async () => {
    const container = $("terminal"),
      width = container.style.width;
    container.style.width = $("terminal-shell").clientWidth - 18 + "px";
    const proposed = fit.proposeDimensions();
    container.style.width = width;
    if (!proposed) throw Error("Terminal dimensions are not ready yet.");
    await effect("manage", {
      action: "resize_window",
      target_id: targetId,
      cols: Math.max(20, Math.min(300, proposed.cols)),
      rows: Math.max(5, Math.min(150, Math.floor((innerHeight * 0.5) / 17))),
      confirmed: true,
    });
  }, "tmux window resized; other clients share this geometry.");
$("find").onclick = () => search.findNext($("search").value);
$("search").onkeydown = (event) => {
  if (event.key === "Enter") search.findNext($("search").value);
};
// Native clipboard events work in an opaque sandbox without clipboard-read
// permission. Keep them synchronous; xterm.paste preserves bracketed-paste mode.
terminal.element.addEventListener(
  "copy",
  (event) => {
    const text = terminal.getSelection();
    if (text && event.clipboardData) {
      event.clipboardData.setData("text/plain", text);
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  },
  true,
);
terminal.element.addEventListener(
  "paste",
  (event) => {
    if (!event.clipboardData) return;
    const text = event.clipboardData.getData("text/plain");
    event.preventDefault();
    event.stopImmediatePropagation();
    terminal.paste(text);
  },
  true,
);
async function copySelection() {
  const text = terminal.getSelection();
  if (!text) {
    notice("Select terminal text first.", true);
    return;
  }
  const focused = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.cssText = "position:fixed;left:-9999px;top:0";
  document.body.append(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {}
  textarea.remove();
  focused?.focus({ preventScroll: true });
  if (!copied) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch {}
  }
  notice(
    copied
      ? "Selection copied."
      : "Use your browser's Copy command for the selected terminal text.",
    !copied,
  );
}
$("copy").onclick = copySelection;
terminal.attachCustomKeyEventHandler((event) => {
  if (event.type !== "keydown") return true;
  const copyOrPaste =
    (event.metaKey && !event.ctrlKey && !event.altKey) ||
    (event.ctrlKey && event.shiftKey && !event.altKey && !event.metaKey);
  if (!copyOrPaste) return true;
  if (event.code === "KeyC" && terminal.hasSelection()) {
    event.preventDefault();
    copySelection();
    return false;
  }
  // The browser delivers paste; never read clipboard contents programmatically.
  if (event.code === "KeyV") return false;
  return true;
});
for (const id of ["manage-action", "name", "cwd", "submit"])
  $(id).addEventListener("input", context);
const liveInput = new LiveInput({
  async send(bytes, authority, current) {
    if (
      !current() ||
      authority.generation !== generation ||
      authority.target !== targetId ||
      authority.attachment !== attachment?.attachment_id
    )
      return;
    if (pending || grant()?.id !== authority.id)
      throw Error(
        "Typing permission changed. Enable typing again to continue.",
      );
    const intent = {
      name: "input",
      args: {
        target_id: authority.target,
        kind: "bytes",
        value: btoa(String.fromCharCode(...bytes)),
        grant_id: authority.id,
        request_id: requestId(),
        actor: "app-reported",
      },
    };
    try {
      await call("input", intent.args);
      if (!current()) return;
      const active = state?.grants.find((item) => item.id === authority.id);
      if (active?.remaining_bytes != null)
        active.remaining_bytes -= bytes.length;
    } catch (error) {
      if (!error.definitive || error.receipt?.status === "unknown")
        error.intent = {
          ...intent,
          unknown: error.receipt?.status === "unknown",
        };
      throw error;
    }
  },
  failed(error) {
    typingStopped = true;
    if (error.intent) pending = error.intent;
    notice(
      error.message +
        " Unsent keystrokes were discarded; input will not be replayed.",
      true,
    );
    controls();
    context();
  },
});
terminal.onData((value) => {
  const active = grant();
  if (!active || pending || busy || typingStopped || !attachment) {
    notice(
      pending
        ? "Inspect the uncertain receipt before typing again."
        : "Enable typing for this pane before using the live keyboard.",
      true,
    );
    return;
  }
  if (
    inputAuthority?.id !== active.id ||
    inputAuthority.generation !== generation
  )
    inputAuthority = {
      id: active.id,
      target: targetId,
      attachment: attachment.attachment_id,
      generation,
    };
  liveInput.push(value, inputAuthority);
});
app.onhostcontextchanged = theme;
app.ontoolresult = (result) => {
  // Receipts still appear in the periodic state view, but a keyboard response
  // must not trigger another fleet/state/context round trip.
  if (result?.structuredContent?.operation === "input") return;
  if (connected && !busy && !liveInput.active)
    refresh({ fleet: true }).catch((error) => notice(error.message, true));
};
app.onteardown = async () => {
  ended = true;
  generation++;
  clearTimeout(outputTimer);
  clearTimeout(stateTimer);
  clearTimeout(draftTimer);
  liveInput.clear();
  if (attachment)
    await call("detach", { attachment_id: attachment.attachment_id }).catch(
      () => {},
    );
  terminal.dispose();
  return {};
};
async function pollState() {
  if (ended) return;
  try {
    if (!document.hidden && !liveInput.active) await refresh();
  } catch (error) {
    notice(error.message, true);
  }
  stateTimer = setTimeout(pollState, 2000);
}
try {
  await app.connect();
  connected = true;
  theme(app.getHostContext());
  if (!app.getHostCapabilities()?.serverResources)
    throw Error(
      "This terminal App needs standard serverResources support. The typed fleet/capture/input tools remain available.",
    );
  await refresh({ fleet: true });
  notice("Choose a pane to open its read-only terminal.");
  pollState();
} catch (error) {
  notice(error.message, true);
}
