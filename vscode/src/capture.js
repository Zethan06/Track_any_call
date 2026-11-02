'use strict';

const vscode = require('vscode');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { expandPath, interpreterPath, recordingProcess } = require('./capture-config');

const executeFile = promisify(execFile);

class Capture {
  constructor(context, controller) {
    this.context = context;
    this.controller = controller;
    this.state = null;
    this.lastResult = null;
    this.output = vscode.window.createOutputChannel('CallNav');
    context.subscriptions.push(this.output, { dispose: () => this.state?.execution?.terminate() });
  }

  folder(uri) {
    return (uri && vscode.workspace.getWorkspaceFolder(uri)) ||
      (vscode.window.activeTextEditor && vscode.workspace.getWorkspaceFolder(vscode.window.activeTextEditor.document.uri)) ||
      vscode.workspace.workspaceFolders?.[0];
  }

  async python(folder, configured) {
    if (configured) return interpreterPath(configured, folder.uri.fsPath);
    const extension = vscode.extensions.getExtension('ms-python.python');
    if (extension) {
      const api = await extension.activate();
      const selected = api.environments?.getActiveEnvironmentPath(folder.uri);
      const environment = selected && await api.environments.resolveEnvironment(selected);
      if (environment?.executable?.uri) return environment.executable.uri.fsPath;
    }
    return process.platform === 'win32' ? 'python' : 'python3';
  }

  async recordScript(uri, options = {}) {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before running a capture script.');
    if (this.state) throw new Error('A capture is already running. Stop it before starting another.');
    if (!uri) {
      const selection = await vscode.window.showOpenDialog({
        canSelectMany: false, openLabel: 'Record script',
        defaultUri: this.folder()?.uri, filters: { 'Shell or Python scripts': ['sh', 'bash', 'py'] }
      });
      if (!selection?.length) return false;
      uri = selection[0];
    }
    const scriptUri = typeof uri === 'string' ? vscode.Uri.file(path.resolve(uri)) : uri;
    const filename = scriptUri.fsPath;
    if (!filename) throw new Error('Select a script on the workspace host.');
    const folder = this.folder(scriptUri);
    if (!folder) throw new Error('Open the script\'s project folder before recording.');
    if (!(await fs.stat(filename)).isFile()) throw new Error('Select a script file.');
    if (vscode.workspace.textDocuments.some(doc => doc.uri.fsPath === filename && doc.isDirty)) {
      throw new Error('Save the script before recording it.');
    }
    const config = vscode.workspace.getConfiguration('callnav', folder.uri);
    const python = await this.python(folder, options.pythonPath ?? config.get('pythonPath', ''));
    const shell = options.shellPath ?? config.get('shellPath', 'bash');
    const arguments_ = options.args ?? config.get('scriptArguments', []);
    if (!Array.isArray(arguments_) || arguments_.some(arg => typeof arg !== 'string')) {
      throw new Error('Script arguments must be an array of strings.');
    }
    const launcher = filename.endsWith('.py') ? python : shell;
    return this.start(folder, [launcher, filename, ...arguments_], { ...options, pythonPath: python });
  }

  async recordCommand(options = {}) {
    if (!vscode.workspace.isTrusted) throw new Error('Trust this workspace before running a capture command.');
    if (this.state) throw new Error('A capture is already running. Stop it before starting another.');
    const folder = this.folder();
    if (!folder) throw new Error('Open a project folder before recording.');
    let command = options.command;
    if (!command) {
      const input = await vscode.window.showInputBox({
        title: 'CallNav: Record Command', prompt: 'Launch command, using your configured shell',
        placeHolder: 'bash ./launch.sh --steps 1', ignoreFocusOut: true
      });
      if (!input?.trim()) return false;
      const shell = vscode.workspace.getConfiguration('callnav', folder.uri).get('shellPath', 'bash');
      command = [shell, '-c', input];
    }
    return this.start(folder, command, options);
  }

  async start(folder, command, options) {
    if (this.state) throw new Error('A capture is already running.');
    const id = crypto.randomUUID();
    const root = folder.uri.fsPath;
    const output = options.output ? expandPath(options.output, root) : path.join(root, '.callnav', `vscode-${id}`);
    const state = { id, output, cancelled: false, finishing: false, execution: null };
    state.completion = new Promise(resolve => { state.resolve = resolve; });
    this.state = state;
    this.controller.updateStatus();
    try {
      const config = vscode.workspace.getConfiguration('callnav', folder.uri);
      const python = await this.python(folder, options.pythonPath ?? config.get('pythonPath', ''));
      const packaged = this.context.asAbsolutePath('python');
      // Development hosts run the canonical source; installed VSIXes use their
      // bundled copy, generated from that same source by the package builder.
      let backend = packaged;
      try { await fs.access(path.join(packaged, 'callnav', '__main__.py')); }
      catch {
        if (this.context.extensionMode !== vscode.ExtensionMode.Development) {
          throw new Error('The bundled recorder is missing. Reinstall the CallNav VSIX.');
        }
        backend = this.context.asAbsolutePath('../src');
      }
      const process_ = recordingProcess({
        python, backend, root, output, command,
        cwd: options.cwd ?? config.get('workingDirectory', '${workspaceFolder}'),
        includeRoots: options.includeRoots ?? config.get('includeRoots', []),
        ray: options.ray ?? config.get('ray', false),
        adapter: options.adapter ?? config.get('adapter', ''),
        iterations: options.iterations ?? config.get('iterations', 1),
        env: { ...config.get('environment', {}), ...options.env }
      });
      state.process = process_;
      await executeFile(python, ['-P', '-c',
        'import sys; assert sys.version_info >= (3, 11), "CallNav requires Python 3.11+"; import callnav'],
      { ...process_.options, timeout: 15000 });
      if (state.cancelled) throw new Error('Capture cancelled before launch.');
      const task = new vscode.Task({ type: 'callnav', captureId: id }, folder,
        'Record Python calls', 'CallNav', new vscode.ProcessExecution(python, process_.args, process_.options), []);
      task.presentationOptions = { reveal: vscode.TaskRevealKind.Always, panel: vscode.TaskPanelKind.Dedicated, clear: true };
      state.listeners = [
        vscode.tasks.onDidEndTaskProcess(event => {
          if (event.execution.task.definition.captureId === id) void this.finish(state, event.exitCode);
        }),
        vscode.tasks.onDidEndTask(event => {
          if (event.execution.task.definition.captureId === id) void this.finish(state, undefined);
        })
      ];
      this.output.appendLine(`Recording ${command.join(' ')}\nWorking directory: ${process_.options.cwd}\nOutput: ${output}`);
      state.execution = await vscode.tasks.executeTask(task);
      if (state.cancelled) state.execution.terminate();
      return { captureId: id, outputDirectory: output };
    } catch (error) {
      state.listeners?.forEach(listener => listener.dispose());
      if (this.state === state) this.state = null;
      this.lastResult = { ok: false, outputDirectory: output, error: error.message };
      state.resolve(this.lastResult);
      this.controller.updateStatus();
      throw error;
    }
  }

  async finish(state, exitCode) {
    if (state.finishing) return;
    state.finishing = true;
    state.listeners.forEach(listener => listener.dispose());
    const indexPath = path.join(state.output, 'index.json');
    const result = { ok: false, exitCode, cancelled: state.cancelled, outputDirectory: state.output, indexPath };
    try {
      if (exitCode === 0 && !state.cancelled) {
        await this.controller.load(indexPath, { quiet: true });
        await this.controller.enable();
        result.ok = true;
        const index = JSON.parse(await fs.readFile(indexPath, 'utf8'));
        result.runId = index.run_id;
        if (index.warnings?.length) {
          this.output.appendLine(index.warnings.join('\n'));
          vscode.window.showWarningMessage(`CallNav: capture finished with coverage warnings. See the CallNav output channel.`);
        } else {
          vscode.window.showInformationMessage('CallNav: capture finished. Runtime navigation is enabled; Ctrl/Cmd+click a recorded call.');
        }
      } else {
        // Task termination can stop the recorder before its finally block. Merge
        // only this run's flushed shards; never substitute a previous latest.json.
        await fs.access(path.join(state.output, 'run.json'));
        await executeFile(state.process.python, ['-P', '-m', 'callnav', 'merge', state.output],
          { ...state.process.options, timeout: 30000 });
        const message = state.cancelled ? 'Capture stopped.' : `Capture exited with code ${exitCode ?? 'unknown'}.`;
        vscode.window.showWarningMessage(`CallNav: ${message} Partial data is available at ${indexPath}; load it explicitly to inspect coverage.`);
      }
    } catch (error) {
      result.error = error.message;
      vscode.window.showErrorMessage(`CallNav: capture could not be loaded: ${error.message}`);
    } finally {
      this.output.appendLine(JSON.stringify(result));
      this.lastResult = result;
      if (this.state === state) this.state = null;
      this.controller.updateStatus();
      state.resolve(result);
    }
  }

  stop() {
    if (!this.state || this.state.finishing) return false;
    this.state.cancelled = true;
    this.state.execution?.terminate();
    return true;
  }

  wait() {
    return this.state?.completion || Promise.resolve(this.lastResult);
  }
}

module.exports = { Capture };
