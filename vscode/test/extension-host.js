'use strict';

// Run using VS Code's --extensionDevelopmentPath and --extensionTestsPath.
// This file requires a genuine extension host; node:test does not exercise it.
const vscode = require('vscode');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { fixture } = require('./fixture');

async function run() {
  const extension = vscode.extensions.getExtension('callnav.callnav');
  assert.ok(extension, 'CallNav must be installed or passed as extensionDevelopmentPath');
  await extension.activate();
  const f = await fixture();
  let caller;
  let target;
  let staticProvider;
  try {
    caller = await vscode.workspace.openTextDocument(vscode.Uri.file(f.caller));
    await vscode.languages.setTextDocumentLanguage(caller, 'python');
    await vscode.window.showTextDocument(caller);
    let collision = await vscode.workspace.openTextDocument(vscode.Uri.file(f.collision));
    await vscode.languages.setTextDocumentLanguage(collision, 'python');
    // A bounded Python provider proves coexistence independently of Pylance.
    const unindexedPath = path.join(path.dirname(f.caller), 'unindexed.py');
    await fs.writeFile(unindexedPath, 'def ordinary():\n    pass\nordinary()\n');
    const unindexed = await vscode.workspace.openTextDocument(vscode.Uri.file(unindexedPath));
    await vscode.languages.setTextDocumentLanguage(unindexed, 'python');
    staticProvider = vscode.languages.registerDefinitionProvider('python', {
      provideDefinition: (document, position) => {
        if (document.uri.fsPath === f.collision && position.line === 7) {
          return new vscode.Location(collision.uri, new vscode.Range(0, 0, 0, 1));
        }
        if (document.uri.fsPath === f.caller && position.line === 2) {
          return new vscode.Location(vscode.Uri.file(f.targets), new vscode.Range(3, 0, 3, 1));
        }
        if (document.uri.fsPath === unindexedPath) {
          return new vscode.Location(unindexed.uri, new vscode.Range(0, 0, 0, 1));
        }
        return [];
      }
    });
    const staticDefinitions = await vscode.commands.executeCommand(
      'vscode.executeDefinitionProvider', collision.uri, new vscode.Position(7, 1));
    assert.ok(staticDefinitions.some(location => (location.range || location.targetSelectionRange).start.line === 0),
      'Sentinel must be active in ordinary Python mode');
    assert.equal(await vscode.commands.executeCommand('callnav.loadIndex', f.indexFile), true);
    assert.equal(await vscode.commands.executeCommand('callnav.enableRuntimeNavigation'), true);
    caller = vscode.workspace.textDocuments.find(doc => doc.uri.fsPath === f.caller);
    assert.equal(caller.languageId, 'python');
    collision = vscode.workspace.textDocuments.find(doc => doc.uri.fsPath === f.collision);
    assert.equal(collision.languageId, 'python');
    const combined = await vscode.commands.executeCommand(
      'vscode.executeDefinitionProvider', collision.uri, new vscode.Position(7, 1));
    assert.equal(combined.length, 2, 'Observed and ordinary Python definitions must coexist in the native chooser');
    assert.deepEqual(combined.map(location => (location.range || location.targetSelectionRange).start.line).sort(), [0, 3]);
    const definitions = (line, character = 1) => vscode.commands.executeCommand(
      'vscode.executeDefinitionProvider', caller.uri, new vscode.Position(line, character));
    let found = await definitions(0);
    assert.equal(found.length, 1, 'The recorded unique call must resolve when static Python has no answer');
    assert.equal(found[0].targetUri.fsPath, f.targets);
    assert.equal(found[0].targetSelectionRange.start.line, 0);
    found = await definitions(1);
    assert.equal(found.length, 2);
    assert.equal((await definitions(2)).length, 1, 'Unobserved calls must retain ordinary Python definitions');
    assert.equal((await extension.exports.provideDefinition(caller, new vscode.Position(2, 1))).length, 0, 'Static definitions must never be claimed as recorded');
    extension.exports.issue = null;
    assert.equal((await extension.exports.provideDefinition(unindexed, new vscode.Position(2, 1))).length, 0);
    assert.equal(extension.exports.issue, null, 'Unindexed files must not trigger stale-recording warnings');
    assert.equal((await vscode.commands.executeCommand('vscode.executeDefinitionProvider', unindexed.uri, new vscode.Position(2, 1))).length, 1, 'Unindexed files retain static navigation');
    assert.equal((await definitions(3)).length, 0);

    // An unobserved call still uses the ordinary Python definition action.
    const staticEditor = await vscode.window.showTextDocument(caller);
    staticEditor.selection = new vscode.Selection(2, 1, 2, 1);
    await vscode.commands.executeCommand('editor.action.revealDefinition');
    assert.equal(vscode.window.activeTextEditor.document.uri.fsPath, f.targets);
    assert.equal(vscode.window.activeTextEditor.selection.active.line, 3);

    // Exercise VS Code's native Go to Definition editor action, not just providers.
    const sourceEditor = await vscode.window.showTextDocument(caller);
    sourceEditor.selection = new vscode.Selection(0, 1, 0, 1);
    await vscode.commands.executeCommand('editor.action.revealDefinition');
    const deadline = Date.now() + 3000;
    while (vscode.window.activeTextEditor?.document.uri.fsPath !== f.targets && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    const destinationEditor = vscode.window.activeTextEditor;
    assert.equal(destinationEditor.document.uri.fsPath, f.targets, 'Native Go to Definition must open the recorded target');
    await extension.exports.manage(destinationEditor.document);
    assert.equal(vscode.window.activeTextEditor.document.languageId, 'python');
    assert.equal(vscode.window.activeTextEditor.selection.active.line, 0, 'Native action must select the recorded definition line');

    if (process.env.CALLNAV_TEST_MOUSE_DIR) {
      const directory = process.env.CALLNAV_TEST_MOUSE_DIR;
      await fs.mkdir(directory, { recursive: true });
      const ready = path.join(directory, 'ready.json');
      const clicked = path.join(directory, 'clicked.json');
      const result = path.join(directory, 'result.json');
      await Promise.all([fs.rm(clicked, { force: true }), fs.rm(result, { force: true })]);
      const mouseEditor = await vscode.window.showTextDocument(caller);
      mouseEditor.selection = new vscode.Selection(0, 0, 0, 0);
      mouseEditor.revealRange(new vscode.Range(0, 0, 0, 4), vscode.TextEditorRevealType.AtTop);
      await extension.exports.manage(mouseEditor.document);
      await fs.writeFile(ready, JSON.stringify({ caller_path: f.caller, target_path: f.targets, expected_line: 0 }));
      console.log(`CallNav physical mouse phase ready: ${ready}`);
      try {
        const mouseDeadline = Date.now() + 120000;
        while (true) {
          try { await fs.access(clicked); break; }
          catch {
            if (Date.now() >= mouseDeadline) throw new Error('Timed out waiting for physical Ctrl+click signal');
            await new Promise(resolve => setTimeout(resolve, 100));
          }
        }
        const openDeadline = Date.now() + 5000;
        while (vscode.window.activeTextEditor?.document.uri.fsPath !== f.targets && Date.now() < openDeadline) {
          await new Promise(resolve => setTimeout(resolve, 25));
        }
        const mouseDestination = vscode.window.activeTextEditor;
        assert.equal(mouseDestination.document.uri.fsPath, f.targets, 'Physical Ctrl+click must open recorded target');
        assert.equal(mouseDestination.selection.active.line, 0, 'Physical Ctrl+click must select recorded definition line');
        await fs.writeFile(result, JSON.stringify({ ok: true, path: mouseDestination.document.uri.fsPath, line: mouseDestination.selection.active.line }));
        console.log('CallNav physical Ctrl+click navigation check passed.');
      } catch (error) {
        await fs.writeFile(result, JSON.stringify({ ok: false, error: error.message }));
        throw error;
      }
    }

    // Destinations retain ordinary Python language features.
    target = await vscode.workspace.openTextDocument(vscode.Uri.file(f.targets));
    await vscode.window.showTextDocument(target);
    await extension.exports.manage(target);
    target = vscode.workspace.textDocuments.find(doc => doc.uri.fsPath === f.targets);
    assert.equal(target.languageId, 'python');

    const edit = new vscode.WorkspaceEdit();
    edit.insert(target.uri, new vscode.Position(0, 0), '# unsaved\n');
    await vscode.workspace.applyEdit(edit);
    assert.equal((await definitions(0)).length, 0, 'Unsaved target must not contribute recorded navigation');
    assert.equal((await definitions(2)).length, 1, 'Unsaved recorded targets must not disable ordinary static navigation');
    await vscode.window.showTextDocument(target);
    await vscode.commands.executeCommand('workbench.action.files.revert');
    await fs.writeFile(f.targets, `${f.targetText}# saved change\n`);
    assert.equal((await definitions(0)).length, 0, 'Changed target must not navigate');
    await fs.writeFile(f.targets, f.targetText);

    const callerEdit = new vscode.WorkspaceEdit();
    callerEdit.insert(caller.uri, new vscode.Position(0, 0), '# unsaved\n');
    await vscode.workspace.applyEdit(callerEdit);
    assert.equal((await definitions(0)).length, 0, 'Unsaved caller must not navigate');
    await vscode.window.showTextDocument(caller);
    await vscode.commands.executeCommand('workbench.action.files.revert');
    await fs.writeFile(f.caller, `${f.callerText}# saved change\n`);
    assert.equal((await definitions(0)).length, 0, 'Changed caller must not contribute recorded navigation');
    assert.equal((await definitions(2)).length, 1, 'Stale recorded callers must not disable ordinary static navigation');
    await fs.writeFile(f.caller, f.callerText);
    await vscode.commands.executeCommand('callnav.disableRuntimeNavigation');
    caller = vscode.workspace.textDocuments.find(doc => doc.uri.fsPath === f.caller);
    assert.equal(caller.languageId, 'python');
    // Repair language modes left by earlier extension/window versions on enable and disable.
    await vscode.languages.setTextDocumentLanguage(caller, 'runtime-python');
    await vscode.commands.executeCommand('callnav.enableRuntimeNavigation');
    caller = vscode.workspace.textDocuments.find(doc => doc.uri.fsPath === f.caller);
    assert.equal(caller.languageId, 'python', 'Enable must restore legacy runtime documents');
    await vscode.languages.setTextDocumentLanguage(caller, 'runtime-python');
    await vscode.commands.executeCommand('callnav.disableRuntimeNavigation');
    caller = vscode.workspace.textDocuments.find(doc => doc.uri.fsPath === f.caller);
    assert.equal(caller.languageId, 'python', 'Disable must restore orphan runtime documents');
    console.log('CallNav genuine extension-host navigation checks passed.');
  } finally {
    staticProvider?.dispose();
    await vscode.commands.executeCommand('callnav.disableRuntimeNavigation');
    await f.cleanup();
  }
}

module.exports = { run };
