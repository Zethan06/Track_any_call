'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const { RuntimeIndex } = require('./index');

const absolute = value => typeof value === 'string' && path.isAbsolute(value);
const singleLine = value => String(value).replace(/[\r\n\t\x00-\x1f]/g, ' ');
const timestamp = value => typeof value === 'number' && Number.isFinite(value)
  ? value * (value < 1e12 ? 1000 : 1) : typeof value === 'string' ? Date.parse(value) : NaN;
const canonical = async file => fs.realpath(file).catch(error => {
  if (error.code === 'ENOENT') return path.resolve(file);
  throw error;
});

async function optionalJSON(file) {
  try { return JSON.parse(await fs.readFile(file, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

async function children(directory) {
  try { return await fs.readdir(directory, { withFileTypes: true }); }
  catch (error) { if (error.code === 'ENOENT') return []; throw error; }
}

function commandText(command) {
  if (!Array.isArray(command) || !command.length || command.some(part => typeof part !== 'string')) return 'Launch command unavailable';
  return command.map(part => /^[\w./:@%+=,-]+$/.test(part) ? part : JSON.stringify(singleLine(part))).join(' ');
}

/** Read the owning manifest and index afresh; catalog entries never supply status. */
async function readRecording(filename, { root, runId } = {}) {
  let indexPath = path.resolve(filename);
  const entry = { indexPath, runId: runId || path.basename(path.dirname(indexPath)),
    status: 'unknown', edgeCount: null, usable: false, issue: null, command: null, warningCount: 0 };
  try {
    indexPath = await canonical(filename);
    entry.indexPath = indexPath;
    const manifest = await optionalJSON(path.join(path.dirname(indexPath), 'run.json'));
    const data = await optionalJSON(indexPath);
    if (manifest && (manifest.schema_version !== 1 || typeof manifest.run_id !== 'string' || !manifest.run_id)) {
      throw new Error('Malformed recording manifest.');
    }
    if (data) {
      entry.index = new RuntimeIndex(data);
      if (!data.run_id) throw new Error('Recording run ID is empty.');
      entry.runId = data.run_id;
      entry.edgeCount = data.edges.length;
      entry.warningCount = Array.isArray(data.warnings) ? data.warnings.length : 0;
      if (root && await canonical(data.root) !== await canonical(root)) throw new Error('Recording belongs to a different workspace folder.');
      if (runId && data.run_id !== runId) throw new Error('Catalog run ID does not match its index.');
    }
    if (manifest) {
      if (data && manifest.run_id !== data.run_id) throw new Error('Manifest and index run IDs differ.');
      if (runId && manifest.run_id !== runId) throw new Error('Catalog run ID does not match its manifest.');
      if (manifest.root !== undefined && (!absolute(manifest.root) ||
          (root && await canonical(manifest.root) !== await canonical(root)) ||
          (data && await canonical(manifest.root) !== await canonical(data.root)))) {
        throw new Error('Manifest belongs to a different recording root.');
      }
      entry.runId = manifest.run_id;
      entry.command = manifest.command;
      entry.startedAt = manifest.started_at;
      entry.finishedAt = manifest.finished_at;
      if (['running', 'recording'].includes(manifest.status)) entry.status = 'running';
      else if (manifest.status === 'finished' && manifest.exit_code === 0 && manifest.root !== undefined) entry.status = 'finished';
      else if (['failed', 'cancelled', 'interrupted'].includes(manifest.status) ||
        (Number.isInteger(manifest.exit_code) && manifest.exit_code !== 0)) entry.status = 'failed';
    }
    if (!data) entry.issue = 'The recording index is unavailable.';
    else if (entry.status === 'running') entry.issue = 'This recording is still running.';
    else if (entry.status === 'failed') entry.issue = 'The launch failed or was interrupted; its index may be partial.';
    else if (entry.status !== 'finished') entry.issue = 'The launch status is unknown because a successful run manifest is unavailable.';
    else if (entry.edgeCount === 0) { entry.status = 'empty'; entry.issue = 'This recording contains no call edges.'; }
    else entry.usable = true;
  } catch (error) {
    entry.status = 'corrupt';
    entry.issue = `Invalid recording: ${error.message}`;
    delete entry.index;
  }
  entry.sortTime = timestamp(entry.startedAt);
  if (!Number.isFinite(entry.sortTime)) entry.sortTime = timestamp(entry.finishedAt);
  if (!Number.isFinite(entry.sortTime)) entry.sortTime = (await fs.stat(indexPath).catch(() => null))?.mtimeMs || 0;
  return entry;
}

/** Fixed-depth discovery only: never walk a training output tree. */
async function scanHistory(root) {
  const directory = path.join(root, '.callnav');
  const candidates = [];
  const diagnostics = [];
  for (const file of await children(path.join(directory, 'recordings'))) {
    if (!file.isFile() || !file.name.endsWith('.json')) continue;
    const catalogPath = path.join(directory, 'recordings', file.name);
    try {
      const catalog = await optionalJSON(catalogPath);
      if (!catalog || catalog.schema_version !== 1 || typeof catalog.run_id !== 'string' || !catalog.run_id ||
          !absolute(catalog.root) || !absolute(catalog.index_path) || !absolute(catalog.run_dir) ||
          await canonical(catalog.root) !== await canonical(root) ||
          path.dirname(path.resolve(catalog.index_path)) !== path.resolve(catalog.run_dir)) {
        throw new Error('Malformed catalog pointer or workspace mismatch.');
      }
      candidates.push({ filename: catalog.index_path, runId: catalog.run_id, priority: 3 });
    } catch (error) {
      diagnostics.push({ indexPath: null, runId: file.name, status: 'corrupt', edgeCount: null,
        command: null, usable: false, issue: `Invalid history catalog: ${error.message}`, sortTime: 0 });
    }
  }
  for (const file of await children(directory)) {
    if (file.isDirectory() && file.name !== 'recordings') {
      const runDirectory = path.join(directory, file.name);
      const present = await Promise.all(['index.json', 'run.json'].map(name => fs.access(path.join(runDirectory, name)).then(() => true, () => false)));
      if (present.some(Boolean)) candidates.push({ filename: path.join(runDirectory, 'index.json'), priority: 2 });
    }
  }
  for (const name of ['index.json', 'latest.json']) {
    const filename = path.join(directory, name);
    if (await fs.access(filename).then(() => true, () => false)) candidates.push({ filename, priority: 1 });
  }
  const entries = [];
  for (const candidate of candidates) {
    const entry = await readRecording(candidate.filename, { root, runId: candidate.runId });
    // Keep metadata only while browsing; loading re-reads and validates the
    // selected index, preserving RuntimeIndex's source-fingerprint checks.
    delete entry.index;
    entry.priority = candidate.priority;
    const duplicate = entries.findIndex(other => other.indexPath === entry.indexPath || other.runId === entry.runId);
    if (duplicate < 0) entries.push(entry);
    else if (Number(entry.usable) * 10 + entry.priority > Number(entries[duplicate].usable) * 10 + entries[duplicate].priority) entries[duplicate] = entry;
  }
  return [...entries, ...diagnostics].sort((first, second) => second.sortTime - first.sortTime || first.runId.localeCompare(second.runId));
}

function historyItem(entry) {
  const time = timestamp(entry.startedAt);
  const value = new Date(time);
  const date = Number.isFinite(value.getTime()) ? value.toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : 'Date unavailable';
  const status = { finished: 'Finished', running: 'Running', failed: 'Failed / interrupted',
    empty: 'Empty', corrupt: 'Invalid', unknown: 'Status unknown' }[entry.status];
  return { label: `${entry.usable ? '$(history)' : '$(warning)'} ${date}`,
    description: `${status} · ${entry.edgeCount ?? '?'} edges${entry.warningCount ? ` · ${entry.warningCount} coverage warnings` : ''} · ${singleLine(entry.runId)}`,
    detail: `${commandText(entry.command)}${entry.issue ? ` · ${entry.issue}` : ''}`,
    recording: entry };
}

async function chooseRecording({ folders, activeFolder, choose, inform }) {
  if (!folders?.length) throw new Error('Open a workspace folder before selecting a recording.');
  let folder = folders[0];
  if (folders.length > 1) {
    const ordered = [...folders].sort((first, second) => Number(second === activeFolder) - Number(first === activeFolder));
    const selection = await choose(ordered.map(item => ({ label: item.name, description: item.uri.fsPath, folder: item })),
      { title: 'CallNav: Choose Recording Folder', placeHolder: 'Select a workspace folder', ignoreFocusOut: true });
    if (!selection) return null;
    folder = selection.folder;
  }
  const recordings = await scanHistory(folder.uri.fsPath);
  if (!recordings.length) {
    await inform(`CallNav: no saved recordings in ${folder.name}. Record a script or launch a command with callnav first.`);
    return null;
  }
  const selection = await choose(recordings.map(historyItem), { title: 'CallNav: Select Recording',
    placeHolder: 'Choose a finished recording to enable its observed navigation', matchOnDescription: true,
    matchOnDetail: true, ignoreFocusOut: true });
  return selection ? { recording: selection.recording, root: folder.uri.fsPath } : null;
}

module.exports = { scanHistory, readRecording, historyItem, chooseRecording, commandText };
