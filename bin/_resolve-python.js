'use strict';

/**
 * _resolve-python.js — shared Python resolution for the npm launchers.
 *
 * Resolution order:
 *   1. TERA_PILOT_PYTHON env override (CI / custom setups)
 *   2. the npm-managed virtualenv created by scripts/postinstall.js
 *      (default ~/.tera_pilot/venv, overridable with TERA_PILOT_VENV)
 *   3. system python3 / python
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync, spawnSync } = require('child_process');

function npmVenvPython() {
  const venvDir = process.env.TERA_PILOT_VENV
    || path.join(os.homedir(), '.tera_pilot', 'venv');
  const py = process.platform === 'win32'
    ? path.join(venvDir, 'Scripts', 'python.exe')
    : path.join(venvDir, 'bin', 'python3');
  return fs.existsSync(py) ? py : null;
}

function systemPython() {
  const candidates = process.platform === 'win32'
    ? ['python3.exe', 'python.exe']
    : ['python3', 'python'];
  const which = process.platform === 'win32' ? 'where' : 'which';
  for (const cmd of candidates) {
    try {
      const result = execSync(`${which} ${cmd}`, {
        encoding: 'utf8',
        stdio: ['pipe', 'pipe', 'ignore'],
      });
      if (result.trim()) return cmd;
    } catch (_) { /* not found — try next */ }
  }
  return null;
}

function resolvePython() {
  if (process.env.TERA_PILOT_PYTHON) return process.env.TERA_PILOT_PYTHON;
  const venvPy = npmVenvPython();
  if (venvPy) return venvPy;
  return systemPython();
}

function checkModule(python, moduleName) {
  try {
    const res = spawnSync(python, ['-c', `import ${moduleName}`], {
      stdio: 'ignore',
      timeout: 20000,
    });
    return res.status === 0;
  } catch (_) {
    return false;
  }
}

module.exports = { resolvePython, npmVenvPython, systemPython, checkModule };
