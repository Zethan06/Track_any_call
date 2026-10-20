'use strict';

// Genuine VS Code extension-host acceptance against an existing runtime index.
// No fixture records, source edits, or rewritten source paths are permitted here.
const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { RuntimeIndex } = require('../src/index');

const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const locationPath = result => (result.targetUri || result.uri).fsPath;
const locationLine = result => (result.targetSelectionRange || result.range).start.line + 1;
const summarize = results => results.map(result => ({ path: locationPath(result), line: locationLine(result) }));

async function definitions(document, position) {
  return vscode.commands.executeCommand('vscode.executeDefinitionProvider', document.uri, position);
}

async function waitForStatic(document, site) {
  const deadline = Date.now() + 90000;
  let results;
  do {
    results = await definitions(document, site.position);
    if (results.some(result => locationPath(result) === document.uri.fsPath && locationLine(result) === site.targetLine)) return results;
    await sleep(500);
  } while (Date.now() < deadline);
  throw new Error(`Pylance did not resolve ${site.name}: ${JSON.stringify(summarize(results || []))}`);
}

async function waitForDestination(expectedPath, expectedLine) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const editor = vscode.window.activeTextEditor;
    if (editor?.document.uri.fsPath === expectedPath && editor.selection.active.line === expectedLine - 1) return editor;
    await sleep(25);
  }
  const editor = vscode.window.activeTextEditor;
  throw new Error(`Expected ${expectedPath}:${expectedLine}; actual ${editor?.document.uri.fsPath}:${(editor?.selection.active.line ?? -1) + 1}`);
}

async function physicalNavigation(directory, document, position, targetPath, targetLine, phase) {
  await fs.mkdir(directory, { recursive: true });
  const clicked = path.join(directory, 'clicked.json');
  await fs.rm(clicked, { force: true });
  const editor = await vscode.window.showTextDocument(document);
  editor.selection = new vscode.Selection(position, position);
  editor.revealRange(new vscode.Range(position.line, 0, position.line, document.lineAt(position.line).text.length), vscode.TextEditorRevealType.AtTop);
  await fs.writeFile(path.join(directory, 'ready.json'), JSON.stringify({
    phase, caller_path: document.uri.fsPath, caller_line: position.line + 1, caller_column: position.character,
    caller_text: document.lineAt(position.line).text, target_path: targetPath, target_line: targetLine,
  }, null, 2));
  console.log(`CallNav physical ${phase} ready: ${directory}/ready.json`);
  const deadline = Date.now() + 180000;
  while (true) {
    try { await fs.access(clicked); break; }
    catch {
      if (Date.now() >= deadline) throw new Error(`Timed out waiting for actual ${phase} Ctrl+left-click`);
      await sleep(100);
    }
  }
  const destination = await waitForDestination(targetPath, targetLine);
  await fs.writeFile(path.join(directory, 'result.json'), JSON.stringify({ ok: true, phase, path: destination.document.uri.fsPath, line: destination.selection.active.line + 1 }, null, 2));
}

async function run() {
  const filename = process.env.CALLNAV_REAL_INDEX;
  assert.ok(filename, 'Set CALLNAV_REAL_INDEX to the index to validate');
  const bytes = await fs.readFile(filename);
  const data = JSON.parse(bytes);
  const callerSuffix = process.env.CALLNAV_REAL_CALLER_SUFFIX || '/verl/trainer/main_ppo.py';
  const targetName = process.env.CALLNAV_REAL_TARGET_NAME || 'FullyAsyncTaskRunner.run';
  const candidates = data.edges.filter(edge => edge.caller.path.endsWith(callerSuffix) && edge.target.qualname === targetName);
  assert.ok(candidates.length, `Real recording does not contain ${callerSuffix} -> ${targetName}`);
  const edge = candidates[0];
  const source = await fs.readFile(edge.caller.path);
  const targetSource = await fs.readFile(edge.target.path);
  assert.equal(digest(source), data.sources[edge.caller.path].sha256, 'Actual caller must match recorded source');
  assert.equal(digest(targetSource), data.sources[edge.target.path].sha256, 'Actual target must match recorded source');
  const sourceLine = source.toString('utf8').split(/\r?\n/)[edge.caller.line - 1];
  const methodName = targetName.split('.').pop();
  const token = `${methodName}.remote`;
  const tokenColumn = sourceLine.indexOf(token);
  assert.ok(tokenColumn >= 0, `Expected ${token} at actual recorded call site: ${sourceLine}`);
  // Click the method token, deliberately not ray.get() or the remote() wrapper.
  const column = tokenColumn + Math.min(1, methodName.length - 1);
  const position = new vscode.Position(edge.caller.line - 1, column);
  const index = await RuntimeIndex.load(filename);
  let document = await vscode.workspace.openTextDocument(vscode.Uri.file(edge.caller.path));
  const lookup = await index.lookup(document, position, vscode.workspace.textDocuments);
  assert.ok(lookup.edges.some(found => found.target.path === edge.target.path && found.target.line === edge.target.line),
    `Recorded method token must resolve to actual Ray implementation: ${JSON.stringify(lookup)}`);

  const extension = vscode.extensions.getExtension('callnav.callnav');
  assert.ok(extension, 'CallNav extension must be installed or in extensionDevelopmentPath');
  await extension.activate();
  const reportDirectory = process.env.CALLNAV_REAL_MOUSE_DIR || process.env.CALLNAV_REAL_REPORT_DIR;
  const report = {
    index: filename, index_sha256: digest(bytes), run_id: data.run_id,
    caller_path: edge.caller.path, caller_line: edge.caller.line, caller_column: column,
    caller_text: sourceLine, target_path: edge.target.path, target_line: edge.target.line,
    target_qualname: edge.target.qualname, edge_kind: edge.kind,
    caller_sha256: digest(source), target_sha256: digest(targetSource),
    exact_index_lookup: true, native_provider: false, native_editor_action: false, ctrl_left_click: false,
    python_language_preserved: false, pylance: null, static_methods: [],
  };
  try {
    let targetDocument;
    const localSites = [];
    if (process.env.CALLNAV_REAL_PYLANCE === '1') {
      const python = vscode.extensions.getExtension('ms-python.python');
      const pylance = vscode.extensions.getExtension('ms-python.vscode-pylance');
      assert.ok(python && pylance, 'Real Pylance acceptance requires installed official Python and Pylance extensions');
      await python.activate();
      await pylance.activate();
      report.pylance = { python: python.packageJSON.version, pylance: pylance.packageJSON.version };
      targetDocument = await vscode.workspace.openTextDocument(vscode.Uri.file(edge.target.path));
      await vscode.window.showTextDocument(targetDocument);
      const lines = targetSource.toString('utf8').split(/\r?\n/);
      for (const name of ['_initialize_components', '_run_training_loop']) {
        const callLine = lines.findIndex(line => line.includes(`self.${name}(`));
        const targetLine = lines.findIndex(line => line.trimStart().startsWith(`def ${name}(`)) + 1;
        assert.ok(callLine >= 0 && targetLine > 0, `Actual source must contain ${name}`);
        const site = { name, position: new vscode.Position(callLine, lines[callLine].indexOf(name) + 1), targetLine };
        const baseline = await waitForStatic(targetDocument, site);
        const recorded = await index.lookup(targetDocument, site.position, vscode.workspace.textDocuments);
        localSites.push(site);
        report.static_methods.push({ name, caller_line: callLine + 1, target_line: targetLine, baseline: summarize(baseline), recorded_edges: recorded.edges.length, native_editor_action: false });
      }
    }
    await vscode.window.showTextDocument(document);
    assert.equal(await vscode.commands.executeCommand('callnav.loadIndex', filename), true);
    assert.equal(await vscode.commands.executeCommand('callnav.enableRuntimeNavigation'), true);
    document = vscode.workspace.textDocuments.find(doc => doc.uri.fsPath === edge.caller.path);
    assert.equal(document.languageId, 'python');
    report.python_language_preserved = true;
    for (const [i, site] of localSites.entries()) {
      assert.equal(targetDocument.languageId, 'python');
      const combined = await waitForStatic(targetDocument, site);
      report.static_methods[i].enabled = summarize(combined);
      const staticEditor = await vscode.window.showTextDocument(targetDocument);
      staticEditor.selection = new vscode.Selection(site.position, site.position);
      await vscode.commands.executeCommand('editor.action.revealDefinition');
      await waitForDestination(edge.target.path, site.targetLine);
      report.static_methods[i].native_editor_action = true;
      if (process.env.CALLNAV_REAL_STATIC_MOUSE === '1') {
        assert.ok(process.env.CALLNAV_REAL_MOUSE_DIR, 'Static mouse acceptance requires CALLNAV_REAL_MOUSE_DIR');
        await physicalNavigation(path.join(process.env.CALLNAV_REAL_MOUSE_DIR, site.name), targetDocument, site.position, edge.target.path, site.targetLine, site.name);
        report.static_methods[i].ctrl_left_click = true;
      }
      console.log(`CallNav preserved real Pylance method: ${edge.target.path}:${site.position.line + 1} -> ${site.targetLine}`);
    }
    const links = await definitions(document, position);
    report.remote_definitions = summarize(links);
    assert.ok(links.some(link => locationPath(link) === edge.target.path && locationLine(link) === edge.target.line), 'The actual observed implementation must be contributed alongside Python definitions');
    assert.equal(links.length, 1, 'This real Ray submission must be unique for direct native-action acceptance; competing static results require the native chooser');
    report.native_provider = true;

    const editor = await vscode.window.showTextDocument(document);
    editor.selection = new vscode.Selection(position, position);
    await vscode.commands.executeCommand('editor.action.revealDefinition');
    const destination = await waitForDestination(edge.target.path, edge.target.line);
    await extension.exports.manage(destination.document);
    assert.equal(vscode.window.activeTextEditor.document.languageId, 'python');
    report.native_editor_action = true;
    console.log(`CallNav real-index native navigation passed: ${edge.caller.path}:${edge.caller.line} -> ${edge.target.path}:${edge.target.line}`);

    if (process.env.CALLNAV_REAL_MOUSE_DIR) {
      const directory = process.env.CALLNAV_REAL_MOUSE_DIR;
      await fs.mkdir(directory, { recursive: true });
      const clicked = path.join(directory, 'clicked.json');
      await fs.rm(clicked, { force: true });
      const sourceEditor = await vscode.window.showTextDocument(document);
      sourceEditor.selection = new vscode.Selection(position, position);
      sourceEditor.revealRange(new vscode.Range(position.line, 0, position.line, sourceLine.length), vscode.TextEditorRevealType.AtTop);
      await fs.writeFile(path.join(directory, 'ready.json'), JSON.stringify(report, null, 2));
      console.log(`CallNav real Ctrl+click ready: ${directory}/ready.json`);
      const deadline = Date.now() + 180000;
      while (true) {
        try { await fs.access(clicked); break; }
        catch {
          if (Date.now() >= deadline) throw new Error('Timed out waiting for actual Ctrl+left-click input');
          await sleep(100);
        }
      }
      await waitForDestination(edge.target.path, edge.target.line);
      report.ctrl_left_click = true;
      console.log('CallNav real-index actual Ctrl+left-click passed.');
    }
    // Verify UI navigation did not change either recorded source file.
    assert.equal(digest(await fs.readFile(edge.caller.path)), report.caller_sha256);
    assert.equal(digest(await fs.readFile(edge.target.path)), report.target_sha256);
    report.sources_unchanged = true;
    assert.equal(digest(await fs.readFile(filename)), report.index_sha256, 'Recording index must remain unchanged throughout acceptance');
    report.index_unchanged = true;
    report.ok = true;
  } catch (error) {
    report.ok = false;
    report.error = error.message;
    throw error;
  } finally {
    await vscode.commands.executeCommand('callnav.disableRuntimeNavigation');
    if (reportDirectory) {
      await fs.mkdir(reportDirectory, { recursive: true });
      await fs.writeFile(path.join(reportDirectory, 'result.json'), JSON.stringify(report, null, 2));
    }
  }
}

module.exports = { run };
