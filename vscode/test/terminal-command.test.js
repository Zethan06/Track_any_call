'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { installTerminalCommand } = require('../src/terminal-command');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "callnav terminal 'spaces' "));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const extension = path.join(root, 'extension');
  await fs.mkdir(path.join(extension, 'python', 'callnav'), { recursive: true });
  await fs.writeFile(path.join(extension, 'python', 'callnav', '__main__.py'), '');
  const changes = new Map();
  const context = {
    extensionMode: 1,
    globalStorageUri: { fsPath: path.join(root, 'storage') },
    asAbsolutePath: name => path.resolve(extension, name),
    environmentVariableCollection: { prepend: (key, value) => changes.set(key, value) }
  };
  return { root, extension, context, changes };
}

test('terminal command uses the bundled backend and changes only new-terminal PATH', async t => {
  const { context, extension, changes } = await fixture(t);
  const result = await installTerminalCommand(context, 'linux');
  const content = await fs.readFile(result.launcher, 'utf8');
  assert.ok(content.startsWith('#!/usr/bin/env python3\n'));
  assert.ok(content.includes(JSON.stringify(path.join(extension, 'python'))));
  assert.ok(content.includes('raise SystemExit(main())'));
  assert.deepEqual([...changes], [['PATH', result.directory + path.delimiter]]);
  assert.equal((await fs.stat(result.launcher)).mode & 0o777, 0o755);
});

test('repeat activation is idempotent and leaves no partial launchers', async t => {
  const { context } = await fixture(t);
  const first = await installTerminalCommand(context, 'linux');
  const before = await fs.stat(first.launcher);
  assert.deepEqual(await installTerminalCommand(context, 'linux'), first);
  assert.equal((await fs.stat(first.launcher)).mtimeMs, before.mtimeMs);
  assert.deepEqual(await fs.readdir(first.directory), ['callnav']);
});

test('missing production backend fails before exposing a broken command', async t => {
  const { context, extension, changes } = await fixture(t);
  await fs.rm(path.join(extension, 'python'), { recursive: true });
  await assert.rejects(installTerminalCommand(context, 'linux'), /bundled recorder is missing/);
  assert.equal(changes.size, 0);
});

test('Windows launcher quotes a path containing spaces and escapes percent signs', async t => {
  const { context, root } = await fixture(t);
  context.globalStorageUri.fsPath = path.join(root, 'storage %literal%');
  const result = await installTerminalCommand(context, 'win32');
  assert.ok(result.launcher.endsWith('callnav.cmd'));
  const content = await fs.readFile(result.launcher, 'utf8');
  assert.ok(content.includes('storage %%literal%%'));
  assert.ok(content.startsWith('@echo off\r\npython "'));
  assert.ok(content.endsWith('" %*\r\n'));
  assert.deepEqual((await fs.readdir(result.directory)).sort(), ['callnav.cmd', 'callnav.py']);
});
