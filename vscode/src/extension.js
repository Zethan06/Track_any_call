'use strict';

const vscode = require('vscode');
const path = require('node:path');
const { RuntimeIndex } = require('./index');
const { Capture } = require('./capture');
const { chooseRecording, readRecording } = require('./history');
const { installTerminalCommand } = require('./terminal-command');

const RUNTIME_LANGUAGE = 'runtime-python';
let activeController;

class Controller {
  constructor(context) {
    this.index = null;
    this.mode = false;
    this.switching = new Map();
    this.status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 10);
    this.capture = new Capture(context, this);
    const installTerminal = async () => {
      try { return await installTerminalCommand(context); }
      catch (error) { this.capture.output.appendLine(`Terminal command unavailable: ${error.message}`); return null; }
    };
    this.terminalReady = installTerminal();
    const command = (name, action) => context.subscriptions.push(vscode.commands.registerCommand(name, async (...args) => {
      try { return await action(...args); }
      catch (error) { vscode.window.showErrorMessage(`CallNav: ${error.message}`); return false; }
    }));
    command('callnav.loadIndex', file => this.load(file));
    command('callnav.loadLatestIndex', () => this.load(this.latestPath()));
    command('callnav.selectRecording', () => this.selectRecording());
    command('callnav.installTerminalCommand', async () => {
      this.terminalReady = installTerminal();
      const installed = await this.terminalReady;
      if (!installed) return false;
      vscode.window.showInformationMessage('CallNav: terminal command enabled. Open a new terminal to use callnav.');
      return true;
    });
    command('callnav.toggleRuntimeNavigation', () => this.mode ? this.disable() : this.enable());
    command('callnav.enableRuntimeNavigation', () => this.enable());
    command('callnav.disableRuntimeNavigation', () => this.disable());
    command('callnav.recordScript', (uri, options) => this.capture.recordScript(uri, options));
    command('callnav.recordCommand', options => this.capture.recordCommand(options));
    command('callnav.stopRecording', () => this.capture.stop());
    context.subscriptions.push(this.status,
      vscode.languages.registerDefinitionProvider('python', {
        provideDefinition: (document, position, token) => this.provideDefinition(document, position, token)
      }),
      vscode.workspace.onDidOpenTextDocument(doc => {
        this.manage(doc).catch(error => vscode.window.showWarningMessage(`CallNav: ${error.message}`));
      }),
      vscode.window.onDidChangeActiveTextEditor(editor => {
        if (editor) this.manage(editor.document).catch(error => vscode.window.showWarningMessage(`CallNav: ${error.message}`));
        this.updateStatus();
      }),
      vscode.workspace.onDidChangeTextDocument(() => this.updateStatus())
    );
    for (const doc of vscode.workspace.textDocuments) {
      this.manage(doc).catch(error => vscode.window.showWarningMessage(`CallNav: ${error.message}`));
    }
    this.updateStatus();
    this.status.show();
  }

  latestPath() {
    const active = vscode.window.activeTextEditor;
    const folder = (active && vscode.workspace.getWorkspaceFolder(active.document.uri)) || vscode.workspace.workspaceFolders?.[0];
    const configured = vscode.workspace.getConfiguration('callnav', folder?.uri).get('indexPath', '${workspaceFolder}/.callnav/latest.json');
    if (configured.includes('${workspaceFolder}') && !folder) throw new Error('Open a workspace folder before loading its latest recording.');
    const expanded = configured.replaceAll('${workspaceFolder}', folder?.uri.fsPath || '');
    return path.isAbsolute(expanded) ? expanded : path.resolve(folder?.uri.fsPath || process.cwd(), expanded);
  }

  async load(file, { quiet = false } = {}) {
    if (!file) {
      const selection = await vscode.window.showOpenDialog({ canSelectMany: false, filters: { 'CallNav index': ['json'] } });
      if (!selection?.length) return false;
      file = selection[0].fsPath;
    }
    const filename = typeof file === 'string' ? file : file.fsPath;
    const index = await RuntimeIndex.load(filename);
    return this.useIndex(index, { quiet });
  }

  async useIndex(index, { quiet = false } = {}) {
    const wasEnabled = this.mode;
    if (wasEnabled) await this.disable();
    this.index = index;
    this.issue = null;
    if (wasEnabled) await this.enable();
    this.updateStatus();
    if (!quiet) vscode.window.showInformationMessage(`CallNav: loaded ${index.runId}. ${this.mode ? 'Runtime navigation is enabled.' : 'Enable Runtime Navigation to use Ctrl/Cmd+click.'}`);
    return true;
  }

  async selectRecording() {
    const active = vscode.window.activeTextEditor;
    const selection = await chooseRecording({ folders: vscode.workspace.workspaceFolders,
      activeFolder: active && vscode.workspace.getWorkspaceFolder(active.document.uri),
      choose: (items, options) => vscode.window.showQuickPick(items, options),
      inform: message => { vscode.window.showInformationMessage(message); } });
    if (!selection) return false;
    const selected = selection.recording;
    const recording = selected.indexPath && await readRecording(selected.indexPath,
      { root: selection.root, runId: selected.runId });
    if (!recording?.usable) {
      vscode.window.showWarningMessage(`CallNav: ${recording?.issue || selected.issue} Use Load Runtime Index to inspect partial data manually.`);
      return false;
    }
    await this.useIndex(recording.index, { quiet: true });
    await this.enable();
    vscode.window.showInformationMessage(`CallNav: selected ${recording.runId}. Runtime navigation is enabled.`);
    return true;
  }

  async enable() {
    if (!this.index) await this.load(this.latestPath());
    this.mode = true;
    for (const doc of vscode.workspace.textDocuments) await this.manage(doc);
    this.updateStatus();
    return true;
  }

  async manage(document) {
    const uri = document.uri.toString();
    if (this.switching.has(uri)) return this.switching.get(uri);
    // Repair language modes left by earlier CallNav versions. Python providers
    // now coexist; enabling a recording never changes ordinary Python documents.
    if (document.languageId !== RUNTIME_LANGUAGE) return;
    const switching = vscode.languages.setTextDocumentLanguage(document, 'python');
    this.switching.set(uri, switching);
    try { await switching; }
    finally { this.switching.delete(uri); }
  }

  async disable() {
    this.mode = false;
    await Promise.allSettled([...this.switching.values()]);
    for (const doc of vscode.workspace.textDocuments) await this.manage(doc);
    this.issue = null;
    this.issuePath = null;
    this.updateStatus();
    return true;
  }

  async provideDefinition(document, position, token) {
    if (!this.mode || !this.index || !this.index.sources[document.uri.fsPath] || token?.isCancellationRequested) return [];
    const result = await this.index.lookup(document, position, vscode.workspace.textDocuments);
    if (token?.isCancellationRequested) return [];
    this.issue = result.issue;
    this.issuePath = document.uri.fsPath;
    this.updateStatus();
    return result.edges.map(({ caller, target }) => ({
      originSelectionRange: new vscode.Range(caller.line - 1, caller.column, caller.end_line - 1, caller.end_column),
      targetUri: vscode.Uri.file(target.path),
      targetRange: new vscode.Range(target.line - 1, target.column, target.line - 1, target.column),
      targetSelectionRange: new vscode.Range(target.line - 1, target.column, target.line - 1, target.column)
    }));
  }

  updateStatus() {
    if (this.capture.state) {
      this.status.text = '$(sync~spin) CallNav: recording';
      this.status.command = 'callnav.stopRecording';
      this.status.tooltip = 'Capture is running in a VS Code task. Click to stop; completed captures load automatically.';
      return;
    }
    if (!this.index) {
      this.status.text = '$(record) CallNav: record script';
      this.status.command = 'callnav.recordScript';
      this.status.tooltip = 'Select a shell or Python script to capture. Existing recordings can be loaded from the Command Palette.';
      return;
    }
    const active = vscode.window.activeTextEditor?.document;
    const indexed = active && this.index.sources[active.uri.fsPath];
    const dirty = this.mode && indexed && active.isDirty;
    const warning = (indexed && this.issuePath === active.uri.fsPath && this.issue) ||
      (dirty ? 'Unsaved source changes: observed navigation is unavailable until the source matches the recording. Python navigation remains available.' : null);
    this.status.text = `${warning ? '$(warning)' : '$(debug-step-into)'} CallNav: ${this.index.runId}${this.mode ? '' : ' (off)'}`;
    this.status.command = 'callnav.toggleRuntimeNavigation';
    this.status.tooltip = warning || (this.mode
      ? 'Observed navigation is on. Ctrl/Cmd+click combines recorded targets with Python definitions; multiple results use the native chooser. Click to disable recorded targets.'
      : 'Click to add recorded targets to Python navigation. Python language features remain available.');
  }
}

function activate(context) {
  activeController = new Controller(context);
  return activeController;
}

async function deactivate() {
  if (activeController) await activeController.disable();
}

module.exports = { activate, deactivate, Controller, RUNTIME_LANGUAGE };
