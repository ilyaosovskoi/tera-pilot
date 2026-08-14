#!/usr/bin/env node
/**
 * tera-pilot-daemon — Launch the Tera Pilot REST/SSE daemon.
 *
 * Runs `python -m tera_pilot.daemon [args...]`. The Python interpreter is
 * resolved in this order: TERA_PILOT_PYTHON → the npm-managed venv created
 * by scripts/postinstall.js (~/.tera_pilot/venv) → system python3/python.
 */
'use strict';

const { spawn } = require('child_process');
const { resolvePython, checkModule } = require('./_resolve-python');

const MODULE = 'tera_pilot.daemon';

const python = resolvePython();
if (!python) {
  console.error('Error: Python 3 not found. Please install Python 3.11+ and try again.');
  process.exit(1);
}

if (!checkModule(python, 'tera_pilot')) {
  console.error('Tera Pilot is not installed for the Python interpreter at:');
  console.error(`  ${python}`);
  console.error('');
  console.error('Fix — reinstall with one command:');
  console.error('  npm install -g tera-pilot');
  console.error('or install the Python package directly:');
  console.error('  pip install tera-pilot');
  process.exit(1);
}

const args = ['-m', MODULE, ...process.argv.slice(2)];
const child = spawn(python, args, { stdio: 'inherit', env: { ...process.env } });

child.on('exit', (code) => process.exit(code || 0));
child.on('error', (err) => {
  console.error('Failed to start tera-pilot-daemon:', err.message);
  process.exit(1);
});
