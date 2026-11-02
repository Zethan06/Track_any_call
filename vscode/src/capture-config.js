'use strict';

const path = require('node:path');

function expandPath(value, root) {
  const expanded = value.replaceAll('${workspaceFolder}', root);
  return path.isAbsolute(expanded) ? expanded : path.resolve(root, expanded);
}

function interpreterPath(value, root) {
  return /[/\\]/.test(value) || value.includes('${workspaceFolder}') ? expandPath(value, root) : value;
}

// ProcessExecution receives argument vectors. Script paths and arguments never
// pass through a shell parser; only an explicitly entered command uses bash -c.
function recordingProcess({ python, backend, root, output, command, includeRoots = [],
  ray = false, adapter = '', iterations = 1, cwd = root, env = {} }) {
  if (!Array.isArray(command) || !command.length || command.some(arg => typeof arg !== 'string')) {
    throw new Error('The capture command must be a nonempty array of strings.');
  }
  if (!Array.isArray(includeRoots) || includeRoots.some(item => typeof item !== 'string')) {
    throw new Error('Additional source roots must be an array of paths.');
  }
  if (adapter && adapter !== 'slime') throw new Error('Unsupported adapter: ' + adapter);
  if (adapter && (!Number.isInteger(iterations) || iterations < 1)) {
    throw new Error('The iteration budget must be a positive integer.');
  }
  // -P prevents a workspace callnav.py/package from shadowing the bundled
  // recorder; Python 3.11+ still honors its explicit PYTHONPATH.
  const args = ['-P', '-m', 'callnav', 'record', '--root', root, '--output', output];
  for (const included of includeRoots) args.push('--include-root', expandPath(included, root));
  if (ray) args.push('--ray');
  if (adapter) args.push('--adapter', adapter, '--iterations', String(iterations));
  args.push('--', ...command);
  const inherited = { ...process.env, ...env };
  // VS Code may outlive an activated terminal; an explicitly selected Python
  // environment also supplies python/ray executables to the supplied script.
  if (path.isAbsolute(python)) {
    inherited.PATH = path.dirname(python) + path.delimiter + (inherited.PATH || '');
  }
  inherited.PYTHONPATH = backend + (inherited.PYTHONPATH ? path.delimiter + inherited.PYTHONPATH : '');
  inherited.PYTHONUNBUFFERED = '1';
  return { python, args, options: { cwd: expandPath(cwd, root), env: inherited } };
}

module.exports = { expandPath, interpreterPath, recordingProcess };
