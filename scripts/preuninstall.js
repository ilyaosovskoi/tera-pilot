#!/usr/bin/env node
'use strict';

/**
 * preuninstall.js — remove the npm-managed Python venv on uninstall.
 *
 * Only removes ~/.tera_pilot/venv (or TERA_PILOT_VENV) when the marker
 * written by scripts/postinstall.js says this exact package version
 * created it. A venv that predates the npm package (or belongs to a
 * different installed version) is left untouched — the user's data is
 * never deleted speculatively.
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const PKG_DIR = path.join(__dirname, '..');
const VERSION = require(path.join(PKG_DIR, 'package.json')).version;
const MARKER_NAME = '.npm-managed.json';

function venvDir() {
  return process.env.TERA_PILOT_VENV
    || path.join(os.homedir(), '.tera_pilot', 'venv');
}

function main() {
  const vdir = venvDir();
  const markerPath = path.join(vdir, MARKER_NAME);
  let marker = null;
  try {
    marker = JSON.parse(fs.readFileSync(markerPath, 'utf8'));
  } catch (_) {
    marker = null;
  }

  if (!marker || marker.package !== 'tera-pilot') {
    console.log(`[tera-pilot] no npm-managed venv at ${vdir} — nothing to remove`);
    return 0;
  }
  if (marker.version !== VERSION) {
    console.log(`[tera-pilot] venv at ${vdir} belongs to v${marker.version}, not v${VERSION} — leaving it in place`);
    return 0;
  }
  if (!fs.existsSync(vdir)) {
    return 0;
  }

  console.log(`[tera-pilot] removing npm-managed Python venv at ${vdir} …`);
  const res = spawnSync(process.platform === 'win32' ? 'rmdir' : 'rm',
    process.platform === 'win32' ? ['/s', '/q', vdir] : ['-rf', vdir],
    { stdio: 'inherit' });
  if (res.status !== 0) {
    console.warn('[tera-pilot] warning: could not remove the venv — remove it manually if you want:');
    console.warn(`  ${vdir}`);
  }
  return 0;
}

if (require.main === module) {
  process.exit(main());
}
