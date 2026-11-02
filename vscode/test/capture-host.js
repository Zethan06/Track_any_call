'use strict';

// Run in a genuine VS Code extension host against an installed, extracted VSIX.
// CALLNAV_CAPTURE_WORKSPACE must be an empty, disposable opened workspace.
const vscode = require('vscode');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function run() {
  const root = process.env.CALLNAV_CAPTURE_WORKSPACE;
  const python = process.env.CALLNAV_CAPTURE_PYTHON || 'python3';
  assert.ok(root, 'Use a disposable workspace and set CALLNAV_CAPTURE_WORKSPACE');
  assert.equal(vscode.workspace.workspaceFolders?.[0].uri.fsPath, root);
  assert.ok(vscode.workspace.isTrusted);
  const extension = vscode.extensions.getExtension('callnav.callnav');
  await extension.activate();
  const controller = extension.exports;
  const report = { packaged: extension.extensionPath,
    extension_mode: controller.capture.context.extensionMode, checks: [] };
  if (process.env.CALLNAV_EXPECT_INSTALLED === '1') {
    assert.equal(report.extension_mode, vscode.ExtensionMode.Production, 'The test must exercise the installed VSIX');
  }
  try {
    await fs.writeFile(path.join(root, 'callnav.py'), 'raise RuntimeError("Workspace must not shadow bundled recorder")\n');
    const working = path.join(root, 'work');
    await fs.mkdir(working, { recursive: true });
    await fs.writeFile(path.join(working, 'callnav.py'), 'raise RuntimeError("Working directory must not shadow bundled recorder")\n');
    const caller = path.join(root, 'dynamic.py');
    const script = path.join(root, "launch 'with spaces'.sh");
    await fs.writeFile(caller,
      'import json, os, pathlib, sys, callnav\n' +
      'def actual(value):\n    return value + 1\n' +
      'selected = {"test": actual}[os.environ["CAPTURE_CASE"]]\n' +
      'assert selected(41) == 42\n' +
      'pathlib.Path("backend.json").write_text(json.dumps({"backend": callnav.__file__, "args": sys.argv[1:]}))\n');
    await fs.writeFile(script, 'set -eu\npython "$(dirname "$0")/dynamic.py" "$@"\n');
    const options = { pythonPath: path.isAbsolute(python) ? path.relative(root, python) : python,
      cwd: 'work', args: ['a space', '$(literal)', ''], env: { CAPTURE_CASE: 'test' } };
    const launched = await vscode.commands.executeCommand('callnav.recordScript', vscode.Uri.file(script), options);
    assert.ok(launched?.captureId, 'Capture must start asynchronously');
    const success = await controller.capture.wait();
    assert.equal(success.ok, true, JSON.stringify(success));
    assert.equal(controller.mode, true);
    const manifest = JSON.parse(await fs.readFile(path.join(launched.outputDirectory, 'run.json'), 'utf8'));
    assert.deepEqual(manifest.command, ['bash', script, ...options.args]);
    assert.equal(manifest.exit_code, 0);
    const metadata = JSON.parse(await fs.readFile(path.join(working, 'backend.json'), 'utf8'));
    assert.deepEqual(metadata.args, options.args);
    assert.equal(metadata.backend, path.join(extension.extensionPath, 'python', 'callnav', '__init__.py'),
      'Recording must use the VSIX backend without a pip installation');
    const document = await vscode.workspace.openTextDocument(vscode.Uri.file(caller));
    const editor = await vscode.window.showTextDocument(document);
    const position = new vscode.Position(4, 9);
    const definitions = await vscode.commands.executeCommand('vscode.executeDefinitionProvider', document.uri, position);
    assert.ok(definitions.some(item => (item.targetUri || item.uri).fsPath === caller &&
      (item.targetSelectionRange || item.range).start.line === 1));
    editor.selection = new vscode.Selection(position, position);
    await vscode.commands.executeCommand('editor.action.revealDefinition');
    assert.equal(vscode.window.activeTextEditor.selection.active.line, 1);
    report.checks.push('bundled backend, literal shell path/args, env inheritance, auto-load, native definition');
    const previousRun = controller.index.runId;

    await vscode.commands.executeCommand('callnav.recordCommand', { pythonPath: python,
      command: [python, '-c', 'raise SystemExit(7)'] });
    const failed = await controller.capture.wait();
    assert.equal(failed.ok, false);
    assert.equal(failed.exitCode, 7);
    assert.equal(controller.index.runId, previousRun, 'Failed tasks must not replace the loaded recording');
    report.checks.push('nonzero exit preserves prior recording');

    const sleeper = path.join(root, 'sleeper.py');
    await fs.writeFile(sleeper,
      'import os, pathlib, time\npathlib.Path("sleep.pid").write_text(str(os.getpid()))\n' +
      'def tick():\n    time.sleep(0.1)\nwhile True:\n    tick()\n');
    await vscode.commands.executeCommand('callnav.recordScript', vscode.Uri.file(sleeper), { pythonPath: python, args: [] });
    const deadline = Date.now() + 30000;
    while (true) {
      try { await fs.access(path.join(root, 'sleep.pid')); break; }
      catch { assert.ok(Date.now() < deadline, 'Sleep task did not start'); await sleep(100); }
    }
    const duplicate = await vscode.commands.executeCommand('callnav.recordScript', vscode.Uri.file(sleeper), { pythonPath: python });
    assert.equal(duplicate, false, 'Concurrent recording must be rejected');
    assert.equal(await vscode.commands.executeCommand('callnav.stopRecording'), true);
    const stopped = await controller.capture.wait();
    assert.equal(stopped.ok, false);
    assert.equal(stopped.cancelled, true);
    assert.equal(controller.index.runId, previousRun);
    const sleeperPid = Number(await fs.readFile(path.join(root, 'sleep.pid'), 'utf8'));
    let alive = true;
    const stopDeadline = Date.now() + 5000;
    while (alive && Date.now() < stopDeadline) {
      try {
        process.kill(sleeperPid, 0);
        if (process.platform === 'linux') {
          const status = await fs.readFile(`/proc/${sleeperPid}/stat`, 'utf8');
          alive = !status.slice(status.lastIndexOf(')') + 2).startsWith('Z');
        }
      } catch (error) {
        if (error.code !== 'ESRCH' && error.code !== 'ENOENT') throw error;
        alive = false;
      }
      if (alive) await sleep(100);
    }
    if (alive) process.kill(sleeperPid, 'SIGKILL');
    assert.equal(alive, false, 'Stopping the task must stop its Python workload');
    assert.equal(await vscode.commands.executeCommand('callnav.stopRecording'), false);
    report.checks.push('duplicate capture rejection and task cancellation preserve prior recording');

    const missing = await vscode.commands.executeCommand('callnav.recordScript', vscode.Uri.file(script),
      { pythonPath: path.join(root, 'missing-python') });
    assert.equal(missing, false);
    assert.equal(controller.capture.state, null);
    assert.equal(controller.index.runId, previousRun);
    report.checks.push('interpreter failure clears capture state');
    report.ok = true;
    console.log('CallNav genuine extension-host capture checks passed.');
  } catch (error) {
    report.ok = false;
    report.error = error.stack;
    throw error;
  } finally {
    controller.capture.stop();
    await fs.writeFile(path.join(root, 'capture-host-result.json'), JSON.stringify(report, null, 2) + '\n');
  }
}

module.exports = { run };
