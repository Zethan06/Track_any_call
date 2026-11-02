'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

async function writeLauncher(filename, content) {
  try {
    if (await fs.readFile(filename, 'utf8') === content) {
      await fs.chmod(filename, 0o755);
      return;
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const temporary = filename + '.' + crypto.randomUUID() + '.tmp';
  try {
    await fs.writeFile(temporary, content, { mode: 0o755 });
    await fs.rename(temporary, filename);
  } finally {
    await fs.rm(temporary, { force: true });
  }
}

async function installTerminalCommand(context, platform = process.platform) {
  const backend = context.asAbsolutePath('python');
  // Development hosts use the same canonical source as the VSIX builder.
  let source = backend;
  try { await fs.access(path.join(source, 'callnav', '__main__.py')); }
  catch {
    if (context.extensionMode !== 2) throw new Error('The bundled recorder is missing. Reinstall CallNav.');
    source = context.asAbsolutePath('../src');
    await fs.access(path.join(source, 'callnav', '__main__.py'));
  }
  const directory = path.join(context.globalStorageUri.fsPath, 'bin');
  await fs.mkdir(directory, { recursive: true });
  const script = '#!/usr/bin/env python3\n' +
    'import sys\n' +
    'if sys.version_info < (3, 11):\n' +
    '    raise SystemExit("CallNav requires Python 3.11+ in this terminal environment")\n' +
    'sys.path.insert(0, ' + JSON.stringify(source) + ')\n' +
    'from callnav.cli import main\n' +
    'raise SystemExit(main())\n';
  const pythonLauncher = path.join(directory, platform === 'win32' ? 'callnav.py' : 'callnav');
  await writeLauncher(pythonLauncher, script);
  let launcher = pythonLauncher;
  if (platform === 'win32') {
    launcher = path.join(directory, 'callnav.cmd');
    await writeLauncher(launcher, '@echo off\r\npython "' +
      pythonLauncher.replaceAll('%', '%%') + '" %*\r\n');
  }
  // VS Code applies this to new terminals; user shell profiles are untouched.
  context.environmentVariableCollection.prepend('PATH', directory + path.delimiter);
  return { directory, launcher };
}

module.exports = { installTerminalCommand };
