const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const http = require("http");
const { spawn, spawnSync } = require("child_process");

const WATCHER = "watch_agents.py";

let server = null;    // watcher process, when this window is the one that started it
let output = null;    // "Agent Max" output channel
let tab = null;       // the single editor-tab webview, reused

function log(line) {
  if (output) output.appendLine(`[${new Date().toISOString().slice(11, 19)}] ${line}`);
}

function cfg() {
  const c = vscode.workspace.getConfiguration("agentMax");
  return {
    python: c.get("pythonPath", "").trim(),
    port: c.get("port", 8780),
    limit: c.get("limit", 16),
    recent: c.get("recentMinutes", 90),
    autoStart: c.get("autoStart", true),
    watcherDir: c.get("watcherDir", "").trim(),
  };
}

/**
 * Where the watcher lives. The bundled copy ships inside the extension, so a
 * marketplace install works with nothing else on disk. The setting exists for
 * anyone running their own fork and it wins when set.
 */
function watcherDir() {
  const configured = cfg().watcherDir;
  if (configured) return configured;
  return path.join(__dirname, "watcher");
}

/**
 * Find a Python that can actually run the watcher.
 *
 * The candidates differ per platform and the Windows launcher takes `-3` as its
 * own argument rather than the interpreter's, so each candidate carries its own
 * argv prefix. Getting this wrong is silent: `python3 -3 script.py` exits
 * non-zero and the panel just says it could not reach the watcher.
 *
 * Returns {cmd, prefix} or null.
 */
function findPython() {
  const configured = cfg().python;
  const candidates = configured
    ? [{ cmd: configured, prefix: [] }]
    : process.platform === "win32"
      ? [{ cmd: "py", prefix: ["-3"] }, { cmd: "python", prefix: [] }, { cmd: "python3", prefix: [] }]
      : [{ cmd: "python3", prefix: [] }, { cmd: "python", prefix: [] }];

  for (const c of candidates) {
    try {
      const probe = spawnSync(c.cmd, [...c.prefix, "-c", "import sys; print(sys.version_info[:2])"],
        { encoding: "utf8", timeout: 5000, windowsHide: true });
      if (probe.status === 0) {
        log(`python: ${c.cmd} ${c.prefix.join(" ")} -> ${String(probe.stdout).trim()}`);
        return c;
      }
    } catch { /* try the next candidate */ }
  }
  return null;
}

/** Resolve once the port answers on /state.json, reject after `tries`. */
function waitForPort(port, tries = 20) {
  return new Promise((resolve, reject) => {
    let n = 0;
    const probe = () => {
      const req = http.get(
        { host: "127.0.0.1", port, path: "/state.json", timeout: 1000 },
        (res) => { res.resume(); resolve(true); }
      );
      req.on("error", () => (++n >= tries ? reject(new Error("no response on port " + port)) : setTimeout(probe, 300)));
      req.on("timeout", () => req.destroy());
    };
    probe();
  });
}

const isUp = (port) => waitForPort(port, 1).then(() => true).catch(() => false);

async function startWatcher() {
  const { port, recent, limit } = cfg();

  // Someone is already serving this port: another window, or a run started
  // from a terminal. Reuse it. Two watchers on one port means the second binds
  // nothing and dies silently, which looks exactly like the extension failing.
  if (await isUp(port)) {
    log(`port ${port} already serving, reusing it`);
    return;
  }
  if (server) return;

  const dir = watcherDir();
  const script = path.join(dir, WATCHER);
  if (!fs.existsSync(script)) {
    throw new Error(`${WATCHER} not found in ${dir}`);
  }

  const py = findPython();
  if (!py) {
    throw new Error("no Python 3 interpreter found. Install Python 3.9 or later, or set agentMax.pythonPath.");
  }

  const args = [...py.prefix, WATCHER, "--port", String(port),
                "--limit", String(limit), "--recent", String(recent)];
  log(`starting: ${py.cmd} ${args.join(" ")} (cwd ${dir})`);

  server = spawn(py.cmd, args, { cwd: dir, windowsHide: true });
  server.stdout.on("data", (d) => output.append(String(d)));
  server.stderr.on("data", (d) => output.append(String(d)));
  server.on("error", (e) => log(`spawn failed: ${e.message}`));
  server.on("exit", (code) => { log(`watcher exited (${code})`); server = null; });

  await waitForPort(port);
  log(`watcher up on ${port}`);
}

function stopWatcher() {
  if (!server) return;
  log("stopping watcher");
  server.kill();
  server = null;
}

// ── webview HTML ──────────────────────────────────────────────────────────

function shell(body) {
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
  html,body{margin:0;padding:0;height:100%;background:var(--vscode-editor-background);}
  iframe{border:0;width:100%;height:100%;display:block;}
  .msg{font:13px/1.6 var(--vscode-font-family);color:var(--vscode-foreground);padding:18px 20px;}
  .msg h3{margin:0 0 8px;font-size:13px;}
  code{font-family:var(--vscode-editor-font-family);opacity:.85;}
</style></head><body>${body}</body></html>`;
}

const loadingHtml = () => shell(`<div class="msg">Starting the agent watcher&hellip;</div>`);

// The watcher serves the dashboard itself, so the iframe keeps a single copy
// of the UI rather than duplicating it into the webview.
const frameHtml = (port) => shell(`<iframe src="http://localhost:${port}/"></iframe>`);

function failedHtml(reason) {
  return shell(`<div class="msg">
    <h3>Could not start the agent watcher.</h3>
    <p>${escapeHtml(reason || "")}</p>
    <p>It needs Python 3.9 or later on your PATH. If Python is installed somewhere
       unusual, set <code>agentMax.pythonPath</code> in Settings.</p>
    <p>Run <em>Agent Max: Show Logs</em> for the full output, then
       <em>Agent Max: Restart Watcher</em>.</p>
  </div>`);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ── views ─────────────────────────────────────────────────────────────────

class AgentMaxViewProvider {
  async resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    await this.render(view.webview);
    view.onDidDispose(() => { this.view = null; });
  }

  async render(webview) {
    const { port, autoStart } = cfg();
    webview.html = loadingHtml();
    try {
      if (autoStart) await startWatcher();
      else await waitForPort(port);
      webview.html = frameHtml(port);
    } catch (err) {
      log(`view failed: ${err.message}`);
      webview.html = failedHtml(err.message);
    }
  }

  reload() {
    return this.view ? this.render(this.view.webview) : Promise.resolve();
  }
}

async function openAsTab() {
  const { port, autoStart } = cfg();

  if (tab) {                       // bring the existing one forward
    tab.reveal(vscode.ViewColumn.Beside, true);
    return tab;
  }

  tab = vscode.window.createWebviewPanel(
    "agentMax.tab", "Agents",
    { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true },
    { enableScripts: true, retainContextWhenHidden: true }
  );
  tab.onDidDispose(() => { tab = null; });
  tab.webview.html = loadingHtml();

  try {
    if (autoStart) await startWatcher();
    else await waitForPort(port);
    tab.webview.html = frameHtml(port);
  } catch (err) {
    log(`tab failed: ${err.message}`);
    tab.webview.html = failedHtml(err.message);
  }
  return tab;
}

// ── lifecycle ─────────────────────────────────────────────────────────────

function activate(context) {
  output = vscode.window.createOutputChannel("Agent Max");
  const provider = new AgentMaxViewProvider();

  context.subscriptions.push(
    output,
    vscode.window.registerWebviewViewProvider("agentMax.dashboard", provider),

    vscode.commands.registerCommand("agentMax.open", () => openAsTab()),

    vscode.commands.registerCommand("agentMax.focusPanel", () =>
      vscode.commands.executeCommand("agentMax.dashboard.focus")),

    vscode.commands.registerCommand("agentMax.restart", async () => {
      stopWatcher();
      await provider.reload();
      if (tab) {
        tab.webview.html = loadingHtml();
        const { port } = cfg();
        try { await startWatcher(); tab.webview.html = frameHtml(port); }
        catch (err) { tab.webview.html = failedHtml(err.message); }
      }
      vscode.window.showInformationMessage("Agent Max: watcher restarted.");
    }),

    vscode.commands.registerCommand("agentMax.openExternal", () =>
      vscode.env.openExternal(vscode.Uri.parse(`http://localhost:${cfg().port}/`))),

    vscode.commands.registerCommand("agentMax.showLogs", () => output.show(true)),

    { dispose: stopWatcher }
  );
}

function deactivate() {
  stopWatcher();
}

module.exports = { activate, deactivate };
