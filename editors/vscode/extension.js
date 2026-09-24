/* Tera Pilot Bridge — example VS Code extension (MIT).
 *
 * Minimal but real client for tera_pilot/ide_bridge.py:
 *  - connects over TCP (localhost) with the bearer token,
 *  - sends the current selection + open files as agent context,
 *  - long-polls agent events: shows approval prompts as VS Code dialogs,
 *    renders diffs with the built-in diff viewer, opens files on request.
 *
 * Protocol: newline-delimited JSON. Request:
 *   {"id": N, "token": "...", "method": "...", "params": {...}}
 * Response: {"id": N, "ok": true, "result": {...}}
 */
const net = require('net');
const vscode = require('vscode');

let sock = null;
let buffer = '';
let nextId = 0;
let polling = false;
const pending = new Map();

function config() {
  const c = vscode.workspace.getConfiguration('teraPilot');
  return { host: c.get('host', '127.0.0.1'), port: c.get('port', 0), token: c.get('token', '') };
}

function connect() {
  const { host, port, token } = config();
  if (!port) {
    vscode.window.showErrorMessage('Tera Pilot: set teraPilot.port (printed by tera-pilot-bridge).');
    return null;
  }
  if (sock) { sock.destroy(); sock = null; }
  buffer = '';
  sock = net.createConnection({ host, port }, () => {
    vscode.window.showInformationMessage(`Tera Pilot: connected to bridge :${port}`);
    startPolling();
  });
  sock.on('data', (chunk) => {
    buffer += chunk.toString('utf8');
    let idx;
    while ((idx = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line) continue;
      try {
        const msg = JSON.parse(line);
        const cb = pending.get(msg.id);
        if (cb) { pending.delete(msg.id); cb(msg); }
      } catch (e) { console.error('bridge parse error', e); }
    }
  });
  sock.on('error', (e) => vscode.window.showErrorMessage(`Tera Pilot bridge error: ${e.message}`));
  sock.on('close', () => { sock = null; polling = false; });
  void token;
  return sock;
}

function call(method, params) {
  return new Promise((resolve, reject) => {
    if (!sock) return reject(new Error('not connected — run “Tera Pilot: Connect to bridge”'));
    const id = ++nextId;
    pending.set(id, (msg) => (msg.ok ? resolve(msg.result) : reject(new Error(msg.error || 'bridge error'))));
    sock.write(JSON.stringify({ id, token: config().token, method, params: params || {} }) + '\n');
  });
}

async function startPolling() {
  if (polling) return;
  polling = true;
  while (sock && polling) {
    try {
      const { events } = await call('poll_events', { timeout: 25 });
      for (const ev of events || []) await handleEvent(ev);
    } catch (e) {
      polling = false; // connection dropped; user reconnects explicitly
    }
  }
}

async function handleEvent(ev) {
  const p = ev.payload || {};
  if (ev.event === 'request_approval') {
    const decision = await vscode.window.showWarningMessage(
      `Tera Pilot wants approval: ${p.action}: ${p.summary}`,
      { modal: true }, 'Allow', 'Deny');
    await call('resolve_approval', { id: p.id, decision: decision === 'Allow' }).catch(() => {});
  } else if (ev.event === 'show_diff') {
    const left = vscode.Uri.parse(`untitled:TeraPilot/${p.path || 'diff'}.orig`);
    const right = vscode.Uri.file(p.path || '');
    await vscode.commands.executeCommand('vscode.diff', left, right, 'Tera Pilot diff');
  } else if (ev.event === 'open_file') {
    const doc = await vscode.workspace.openTextDocument(p.path);
    await vscode.window.showTextDocument(doc, { selection: p.selection });
  } else if (ev.event === 'notify') {
    vscode.window.showInformationMessage(`Tera Pilot: ${p.text || ''}`);
  }
}

async function sendSelection() {
  const editor = vscode.window.activeTextEditor;
  if (!editor) { vscode.window.showWarningMessage('No active editor.'); return; }
  const sel = editor.selection;
  const openFiles = vscode.workspace.textDocuments.map((d) => d.fileName).slice(0, 20);
  await call('provide_context', {
    selection: {
      path: editor.document.fileName,
      text: editor.document.getText(sel),
      start_line: sel.start.line + 1,
      end_line: sel.end.line + 1,
    },
    open_files: openFiles,
  });
  vscode.window.showInformationMessage('Tera Pilot: selection sent as context.');
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand('teraPilot.connect', () => connect()),
    vscode.commands.registerCommand('teraPilot.sendSelection', sendSelection),
  );
}

function deactivate() { if (sock) sock.destroy(); }

module.exports = { activate, deactivate };
