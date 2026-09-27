#!/usr/bin/env node
'use strict';

/**
 * postinstall.js — bootstrap the Python side of the npm distribution.
 *
 * `npm install -g tera-pilot` runs this right after unpacking, so the
 * user gets a working install with a single command — no git clone, no
 * manual `pip install`. It:
 *
 *   1. resolves a Python interpreter (TERA_PILOT_PYTHON → python3 → python);
 *   2. creates (or reuses) a virtualenv — default ~/.tera_pilot/venv,
 *      overridable with TERA_PILOT_VENV;
 *   3. installs the bundled Python package (this npm package's directory)
 *      plus its dependencies into that venv via pip;
 *   4. writes a marker file so scripts/preuninstall.js knows the venv is
 *      npm-managed and safe to remove on `npm uninstall -g tera-pilot`.
 *
 * All env knobs are optional:
 *   TERA_PILOT_PYTHON         python interpreter to use (default python3)
 *   TERA_PILOT_VENV           venv directory (default ~/.tera_pilot/venv)
 *   TERA_PILOT_SKIP_PIP=1     create/reuse the venv but skip pip install
 *                             (offline setups / custom installs)
 *   TERA_PILOT_PIP_EXTRA_ARGS extra flags for the pip install command
 *   TERA_PILOT_FORCE=1        recreate the venv even if the marker matches
 *
 * Exits non-zero with a clear message if the venv creation or pip install
 * fails — a half-installed package should not report success.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const PKG_DIR = path.join(__dirname, '..');
const VERSION = require(path.join(PKG_DIR, 'package.json')).version;
const MARKER_NAME = '.npm-managed.json';

function log(msg) {
  console.log(`[tera-pilot] ${msg}`);
}

function resolvePython() {
  if (process.env.TERA_PILOT_PYTHON) return process.env.TERA_PILOT_PYTHON;
  const candidates = process.platform === 'win32'
    ? ['python3.exe', 'python.exe']
    : ['python3', 'python'];
  for (const cmd of candidates) {
    try {
      const res = spawnSync(cmd, ['-c', 'import sys; sys.exit(0)'], {
        stdio: 'ignore',
        timeout: 15000,
      });
      if (res.status === 0) return cmd;
    } catch (_) { /* try next */ }
  }
  return null;
}

function venvDir() {
  return process.env.TERA_PILOT_VENV
    || path.join(os.homedir(), '.tera_pilot', 'venv');
}

function venvPython(dir) {
  // Preferred location; some systems only create `bin/python`.
  // Prefer python3, fall back to python (POSIX). Win32 uses Scripts\python.exe.
  if (process.platform === 'win32') {
    return path.join(dir, 'Scripts', 'python.exe');
  }
  const p3 = path.join(dir, 'bin', 'python3');
  if (fs.existsSync(p3)) return p3;
  return path.join(dir, 'bin', 'python');
}

function checkPythonVersion(python) {
  // Require 3.11+. Unparseable output (stubs, wrappers) → warn and continue,
  // never fail: the pip install below is the real gate.
  try {
    const res = spawnSync(python, ['-c', 'import sys; print(sys.version_info[0]*1000+sys.version_info[1])'], {
      encoding: 'utf8',
      timeout: 15000,
    });
    const v = parseInt(String(res.stdout || '').trim(), 10);
    if (Number.isNaN(v)) {
      log(`could not detect Python version from ${python} — continuing anyway.`);
      return true;
    }
    if (v < 3011) {
      console.error(`[tera-pilot] ERROR: Python 3.11+ is required, but ${python} reports ${Math.floor(v / 1000)}.${v % 1000}.`);
      console.error('  Fix: install Python 3.11+ and re-run with TERA_PILOT_PYTHON=/path/to/python3 npm install -g tera-pilot');
      return false;
    }
    return true;
  } catch (_) {
    log(`could not detect Python version from ${python} — continuing anyway.`);
    return true;
  }
}

function markerPath(dir) {
  return path.join(dir, MARKER_NAME);
}

function readMarker(dir) {
  try {
    const raw = fs.readFileSync(markerPath(dir), 'utf8');
    const data = JSON.parse(raw);
    return data && typeof data === 'object' ? data : null;
  } catch (_) {
    return null;
  }
}

function writeMarker(dir) {
  const data = {
    package: 'tera-pilot',
    version: VERSION,
    installed_at: new Date().toISOString(),
  };
  fs.writeFileSync(markerPath(dir), JSON.stringify(data, null, 2) + '\n', 'utf8');
}

function run(cmd, args) {
  const res = spawnSync(cmd, args, { stdio: 'inherit', timeout: 60 * 60 * 1000 });
  return res.status === 0;
}

function buildPipArgs(pkgDir, extra) {
  const flags = ['install', '--no-cache-dir', '--no-input'];
  if (extra) {
    flags.push(...extra.split(/\s+/).filter(Boolean));
  }
  flags.push(pkgDir);
  return flags;
}

function main() {
  const python = resolvePython();
  if (!python) {
    console.error('[tera-pilot] ERROR: Python 3 not found. Install Python 3.11+ and re-run: npm install -g tera-pilot');
    process.exit(1);
  }

  const vdir = venvDir();
  const marker = readMarker(vdir);

  // Fast path: same version, venv looks intact → nothing to do.
  // NOTE: the marker is written only AFTER a successful install (see below),
  // so a previous failed pip install can never masquerade as "already installed".
  if (!process.env.TERA_PILOT_FORCE && marker && marker.version === VERSION && fs.existsSync(venvPython(vdir))) {
    log(`already installed (v${VERSION}) — Python venv at ${vdir}`);
    return 0;
  }

  if (!checkPythonVersion(python)) {
    process.exit(1);
  }

  // Create the venv (reuse an existing one if present).
  if (!fs.existsSync(venvPython(vdir))) {
    log(`creating Python virtualenv at ${vdir} …`);
    fs.mkdirSync(path.dirname(vdir), { recursive: true });
    if (!run(python, ['-m', 'venv', vdir])) {
      console.error('[tera-pilot] ERROR: failed to create the Python virtualenv.');
      console.error(`  target: ${vdir}`);
      console.error('  Fix: create it manually with:  python3 -m venv ' + vdir);
      console.error('  Debian/Ubuntu note: this usually means the python3-venv package is missing —');
      console.error('    run:  sudo apt install python3-venv   (or python3.11-venv / python3.12-venv)');
      process.exit(1);
    }
  }

  if (!fs.existsSync(venvPython(vdir))) {
    console.error('[tera-pilot] ERROR: virtualenv created but no Python interpreter found inside it.');
    console.error(`  looked in: ${vdir} (bin/python3, bin/python)`);
    process.exit(1);
  }

  if (process.env.TERA_PILOT_SKIP_PIP === '1') {
    writeMarker(vdir);
    log(`venv ready at ${vdir} (pip install skipped — TERA_PILOT_SKIP_PIP=1).`);
    return 0;
  }

  log(`installing Tera Pilot v${VERSION} + dependencies into the venv (this downloads packages on first install) …`);
  const pipArgs = buildPipArgs(PKG_DIR, process.env.TERA_PILOT_PIP_EXTRA_ARGS);
  if (!run(venvPython(vdir), ['-m', 'pip', ...pipArgs])) {
    console.error('[tera-pilot] ERROR: pip install failed.');
    console.error('  The npm package is installed, but the Python runtime is not ready.');
    console.error('  (No success marker was written, so re-running the install will retry cleanly.)');
    console.error('  Fix options:');
    console.error('    1. Re-run:              npm install -g tera-pilot');
    console.error('    2. Install offline:     TERA_PILOT_SKIP_PIP=1 npm install -g tera-pilot');
    console.error('       then pip install manually:  ' + venvPython(vdir) + ' -m pip install -r ' + path.join(PKG_DIR, 'requirements.txt'));
    process.exit(1);
  }

  writeMarker(vdir);
  log(`done — Python venv at ${vdir} (v${VERSION}). Run: tera-pilot / tera-pilot-tui`);
  return 0;
}

module.exports = { buildPipArgs, venvDir, markerPath };

if (require.main === module) {
  process.exit(main());
}
