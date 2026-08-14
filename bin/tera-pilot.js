#!/usr/bin/env node
/**
 * tera-pilot — Launch the Tera Pilot Web UI (browser-based GUI).
 *
 * Runs `python -m tera_pilot [args...]`. The Python interpreter is resolved
 * in this order: TERA_PILOT_PYTHON → the npm-managed venv created by
 * scripts/postinstall.js (~/.tera_pilot/venv) → system python3/python.
 *
 * If the Python package is not installed for the resolved interpreter, a
 * friendly recovery message is printed instead of a raw ModuleNotFoundError.
 */
'use strict';

const { spawn } = require('child_process');
const { resolvePython, checkModule } = require('./_resolve-python');

const MODULE = 'tera_pilot';

const python = resolvePython();
if (!python) {
  console.error('Error: Python 3 not found. Please install Python 3.11+ and try again.');
  process.exit(1);
}

if (!checkModule(python, MODULE)) {
  console.error('Tera Pilot is not installed for the Python interpreter at:');
  console.error(`  ${python}`);
  console.error('');
  console.error('Fix — reinstall with one command:');
  console.error('  npm install -g tera-pilot');
  console.error('or install the Python package directly:');
  console.error('  pip install tera-pilot');
  process.exit(1);
}

// Forward all CLI arguments
const args = ['-m', MODULE, ...process.argv.slice(2)];

const child = spawn(python, args, {
  stdio: 'inherit',
  env: { ...process.env },
});

child.on('exit', (code) => {
  process.exit(code || 0);
});

child.on('error', (err) => {
  console.error('Failed to start tera-pilot:', err.message);
  process.exit(1);
});
