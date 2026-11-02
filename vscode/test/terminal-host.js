'use strict';

// Genuine installed-VSIX test: no pip, no project settings, no mocked picker.
const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function until(action, message, milliseconds = 30000) {
  const deadline = Date.now() + milliseconds;
  while (Date.now() < deadline) {
    const result = await action();
    if (result) return result;
    await wait(100);
  }
  throw new Error(message);
}

async function pick(row, cancel = false) {
  const selecting = vscode.commands.executeCommand('callnav.selectRecording');
  // Await the actual QuickPick, then send native QuickInput commands.
  await wait(500);
  if (cancel) await vscode.commands.executeCommand('workbench.action.closeQuickOpen');
  else {
    for (let i = 0; i < row; i++) await vscode.commands.executeCommand('workbench.action.quickOpenSelectNext');
    await vscode.commands.executeCommand('workbench.action.acceptSelectedQuickOpenItem');
  }
  return Promise.race([selecting, wait(5000).then(() => { throw new Error('History picker did not complete'); })]);
}

async function run() {
  const root = process.env.CALLNAV_TERMINAL_WORKSPACE;
  const python = process.env.CALLNAV_TERMINAL_PYTHON;
  assert.ok(root && python, 'Set a disposable workspace and a clean Python executable.');
  assert.equal(vscode.workspace.workspaceFolders[0].uri.fsPath, root);
  const extension = vscode.extensions.getExtension('callnav.callnav');
  const controller = await extension.activate();
  assert.equal(controller.capture.context.extensionMode, vscode.ExtensionMode.Production);
  const installed = await controller.terminalReady;
  assert.ok(installed?.launcher);
  const { scanHistory } = require(path.join(extension.extensionPath, 'src/history'));
  const report = { extension: extension.extensionPath, version: extension.packageJSON.version,
    terminal_launcher: installed.launcher, production: true, checks: [] };
  let terminal;
  try {
    const source = path.join(root, 'dynamic.py');
    await fs.writeFile(source,
      'import json, pathlib, sys, callnav\n' +
      'def first(): return 1\n' +
      'def second(): return 2\n' +
      'selected = {"first": first, "second": second}[sys.argv[1]]\n' +
      'selected()\n' +
      'pathlib.Path("proof-" + sys.argv[1] + ".json").write_text(json.dumps({"args": sys.argv[1:], "backend": callnav.__file__}))\n');
    await fs.writeFile(path.join(root, 'launch.sh'), 'set -eu\npython dynamic.py "$@"\n');
    await fs.writeFile(path.join(root, 'fail.sh'), 'python -c "raise SystemExit(7)"\n');
    await fs.writeFile(path.join(root, 'callnav.py'), 'raise RuntimeError("workspace shadow must not execute")\n');
    terminal = vscode.window.createTerminal({ name: 'CallNav prefix test', cwd: root,
      shellPath: '/bin/bash', shellArgs: ['--noprofile', '--norc'],
      env: { PATH: path.dirname(python) + path.delimiter + process.env.PATH,
        PYTHONPATH: '', PYTHONUNBUFFERED: '1' } });
    terminal.show();
    const completed = async count => until(async () => {
      const entries = await scanHistory(root);
      return entries.length === count && entries.every(entry => entry.status !== 'running' &&
        entry.status !== 'unknown' && entry.status !== 'corrupt') && entries;
    }, `Expected ${count} completed terminal recordings`, 45000);

    terminal.sendText('callnav bash launch.sh first "a space" \'$(literal)\' ""');
    const initial = (await completed(1))[0];
    assert.equal(initial.usable, true);
    const initialIndex = await fs.readFile(initial.indexPath);
    const initialManifest = await fs.readFile(path.join(path.dirname(initial.indexPath), 'run.json'));
    const firstProof = JSON.parse(await fs.readFile(path.join(root, 'proof-first.json'), 'utf8'));
    assert.deepEqual(firstProof.args, ['first', 'a space', '$(literal)', '']);
    assert.equal(firstProof.backend, path.join(extension.extensionPath, 'python/callnav/__init__.py'));
    terminal.sendText('callnav bash launch.sh second');
    const second = (await completed(2)).find(entry => entry.runId !== initial.runId);
    assert.equal(second.usable, true);
    assert.ok(initial.runId !== second.runId);
    assert.deepEqual(await fs.readFile(initial.indexPath), initialIndex);
    assert.deepEqual(await fs.readFile(path.join(path.dirname(initial.indexPath), 'run.json')), initialManifest);
    report.run_ids = [initial.runId, second.runId];
    report.checks.push('bare callnav in a real fresh terminal uses the bundled backend and preserves literal arguments');
    report.checks.push('two captures have independent UUID directories and leave the first index/manifest unchanged');

    terminal.sendText('callnav --output ../external-output -- bash launch.sh second');
    const external = (await completed(3)).find(entry => !report.run_ids.includes(entry.runId));
    assert.equal(external.usable, true);
    assert.equal(external.indexPath, path.resolve(root, '../external-output/index.json'));
    report.run_ids.push(external.runId);
    assert.deepEqual(await fs.readFile(initial.indexPath), initialIndex);

    terminal.sendText('callnav bash fail.sh');
    const entries = await completed(4);
    const failed = entries.find(entry => !report.run_ids.includes(entry.runId));
    const manifest = JSON.parse(await fs.readFile(path.join(path.dirname(failed.indexPath), 'run.json'), 'utf8'));
    assert.equal(manifest.exit_code, 7);
    assert.equal(failed.usable, false);
    assert.deepEqual(await fs.readFile(initial.indexPath), initialIndex);
    const failingRow = entries.findIndex(entry => entry.runId === failed.runId);
    assert.equal(await pick(failingRow), false);
    assert.equal(controller.index, null);
    report.checks.push('failed capture keeps exit code and history; selecting it does not enable a partial index');

    async function selectAndNavigate(entry, definitionLine) {
      const ordered = await scanHistory(root);
      assert.equal(await pick(ordered.findIndex(item => item.runId === entry.runId)), true);
      assert.equal(controller.index.runId, entry.runId);
      assert.equal(controller.mode, true);
      const document = await vscode.workspace.openTextDocument(vscode.Uri.file(source));
      const editor = await vscode.window.showTextDocument(document);
      const position = new vscode.Position(4, 1);
      const targets = await vscode.commands.executeCommand('vscode.executeDefinitionProvider', document.uri, position);
      assert.equal(targets.length, 1);
      assert.equal((targets[0].targetSelectionRange || targets[0].range).start.line, definitionLine);
      editor.selection = new vscode.Selection(position, position);
      await vscode.commands.executeCommand('editor.action.revealDefinition');
      await until(() => vscode.window.activeTextEditor?.document.uri.fsPath === source &&
        vscode.window.activeTextEditor.selection.active.line === definitionLine, 'F12 landed at the wrong historical target');
    }
    await selectAndNavigate(initial, 1);
    await selectAndNavigate(second, 2);
    await selectAndNavigate(external, 2);
    report.checks.push('an external output directory is discoverable and selectable through its catalog pointer');
    assert.equal(await pick(0, true), false);
    assert.equal(controller.index.runId, external.runId);
    assert.equal(controller.mode, true);
    report.checks.push('real history QuickPick switches old/new targets and native F12 lands at each recorded implementation');
    report.checks.push('cancelling the history picker preserves the selected recording and navigation');
    await assert.rejects(fs.access(path.join(root, '.vscode/settings.json')));
    report.checks.push('the complete flow uses no project settings JSON');
    report.ok = true;
    console.log('CallNav genuine terminal-prefix and history checks passed.');
  } catch (error) {
    report.ok = false;
    report.error = error.stack;
    throw error;
  } finally {
    terminal?.dispose();
    await fs.writeFile(path.join(root, 'terminal-host-result.json'), JSON.stringify(report, null, 2) + '\n');
  }
}

module.exports = { run };
