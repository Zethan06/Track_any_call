'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { sha256 } = require('../src/index');
const { scanHistory, readRecording, historyItem, chooseRecording, commandText } = require('../src/history');
const { fixture } = require('./fixture');

async function setup(t) {
  const f = await fixture();
  t.after(f.cleanup);
  const folder = { name: 'Project', uri: { fsPath: f.root } };
  const record = async (directory, id, overrides = {}) => {
    await fs.mkdir(directory, { recursive: true });
    const data = { ...f.data, run_id: id, ...overrides.index };
    const manifest = { schema_version: 1, run_id: id, root: f.root,
      command: ['bash', 'launch with spaces.sh', '--steps', '1'], started_at: 1700000000,
      finished_at: 1700000010, status: 'finished', exit_code: 0, ...overrides.manifest };
    await fs.writeFile(path.join(directory, 'index.json'), JSON.stringify(data));
    if (overrides.manifest !== null) await fs.writeFile(path.join(directory, 'run.json'), JSON.stringify(manifest));
    return path.join(directory, 'index.json');
  };
  const catalog = async (index, id, overrides = {}) => {
    const directory = path.join(f.root, '.callnav', 'recordings');
    await fs.mkdir(directory, { recursive: true });
    await fs.writeFile(path.join(directory, sha256(id) + '.json'), JSON.stringify({ schema_version: 1,
      run_id: id, root: f.root, index_path: index, run_dir: path.dirname(index), ...overrides }));
  };
  return { ...f, folder, record, catalog };
}

test('catalog finds an external recording and deduplicates direct and latest indexes by run ID', async t => {
  const f = await setup(t);
  const external = await fs.mkdtemp(path.join(os.tmpdir(), 'callnav-history-external-'));
  t.after(() => fs.rm(external, { recursive: true, force: true }));
  const old = await f.record(external, 'old');
  await f.catalog(old, 'old');
  const newer = await f.record(path.join(f.root, '.callnav', 'newer'), 'newer', { manifest: { started_at: 1800000000 } });
  await f.catalog(newer, 'newer');
  await fs.copyFile(newer, path.join(f.root, '.callnav', 'latest.json'));
  const before = await fs.readFile(old);
  const entries = await scanHistory(f.root);
  assert.deepEqual(entries.map(entry => entry.runId), ['newer', 'old']);
  assert.ok(entries.every(entry => entry.usable));
  assert.equal(entries[1].indexPath, old);
  assert.deepEqual(await fs.readFile(old), before, 'history discovery must never rewrite recordings');
  const item = historyItem(entries[1]);
  assert.match(item.description, /Finished.*5 edges/);
  assert.match(item.detail, /bash "launch with spaces.sh" --steps 1/);
  assert.match(item.label, /UTC/);
});

test('actual manifests block running, failed, unknown, empty and corrupt recordings', async t => {
  const f = await setup(t);
  const cases = [
    ['running', { manifest: { status: 'running', exit_code: undefined } }],
    ['failed', { manifest: { exit_code: 2 } }],
    ['unknown', { manifest: null }],
    ['empty', { index: { edges: [] } }],
    ['corrupt', { index: { sources: { [f.caller]: { sha256: 'invalid' } } } }],
  ];
  for (const [id, options] of cases) {
    const index = await f.record(path.join(f.root, '.callnav', id), id, options);
    // Stale or invented catalog metadata must never override the owner.
    await f.catalog(index, id, { status: 'finished', exit_code: 0 });
  }
  const entries = await scanHistory(f.root);
  assert.equal(entries.length, cases.length);
  for (const [id] of cases) {
    const entry = entries.find(item => item.runId === id);
    assert.equal(entry.status, id);
    assert.equal(entry.usable, false);
    assert.ok(entry.issue);
  }
});

test('catalog corruption and mismatched run IDs stay visible without becoming selectable', async t => {
  const f = await setup(t);
  const index = await f.record(path.join(f.root, 'outside'), 'actual');
  await f.catalog(index, 'different');
  const directory = path.join(f.root, '.callnav', 'recordings');
  await fs.writeFile(path.join(directory, 'broken.json'), '{');
  const entries = await scanHistory(f.root);
  assert.equal(entries.length, 2);
  assert.ok(entries.every(entry => !entry.usable && entry.status === 'corrupt'));
  assert.ok(entries.some(entry => /run ID/i.test(entry.issue)));
  assert.ok(entries.some(entry => /catalog/.test(entry.issue)));
});

test('zero edges and missing indexes preserve running or failed launch status', async t => {
  const f = await setup(t);
  for (const status of ['running', 'failed']) {
    const index = await f.record(path.join(f.root, '.callnav', status), status,
      { manifest: { status, exit_code: status === 'failed' ? 2 : undefined }, index: { edges: [] } });
    let recording = await readRecording(index, { root: f.root });
    assert.equal(recording.status, status);
    assert.equal(recording.usable, false);
    await fs.rm(index);
    recording = await readRecording(index, { root: f.root });
    assert.equal(recording.status, status);
    assert.match(recording.issue, /unavailable/);
  }
});

test('a successful manifest cannot lend its status to a different recording root', async t => {
  const f = await setup(t);
  const index = await f.record(path.join(f.root, '.callnav', 'root-mismatch'), 'root-mismatch',
    { manifest: { root: path.join(f.root, 'another-project') } });
  const mismatched = await readRecording(index, { root: f.root });
  assert.equal(mismatched.status, 'corrupt');
  assert.equal(mismatched.usable, false);
  assert.match(mismatched.issue, /root/);
  const legacy = await f.record(path.join(f.root, '.callnav', 'legacy'), 'legacy', { manifest: { root: undefined } });
  assert.equal((await readRecording(legacy, { root: f.root })).status, 'unknown');
});

test('one inaccessible index is invalid without hiding other usable history', async t => {
  const f = await setup(t);
  const denied = await f.record(path.join(f.root, '.callnav', 'denied'), 'denied');
  await f.record(path.join(f.root, '.callnav', 'good'), 'good');
  await f.catalog(denied, 'denied');
  const realpath = fs.realpath.bind(fs);
  t.mock.method(fs, 'realpath', async filename => {
    if (path.resolve(filename) === denied) {
      throw Object.assign(new Error('EACCES: permission denied for this index'), { code: 'EACCES' });
    }
    return realpath(filename);
  });
  const entries = await scanHistory(f.root);
  assert.equal(entries.length, 2);
  const blocked = entries.find(entry => entry.runId === 'denied');
  assert.equal(blocked.status, 'corrupt');
  assert.equal(blocked.usable, false);
  assert.match(blocked.issue, /EACCES/);
  assert.equal(blocked.indexPath, denied);
  assert.equal(entries.find(entry => entry.runId === 'good').usable, true);
});

test('finite timestamps outside the JavaScript Date range do not break the picker', async t => {
  const f = await setup(t);
  await f.record(path.join(f.root, '.callnav', 'extreme-date'), 'extreme-date',
    { manifest: { started_at: 1e20 } });
  const entry = (await scanHistory(f.root))[0];
  assert.equal(entry.usable, true);
  assert.match(historyItem(entry).label, /Date unavailable/);
  const selected = await chooseRecording({ folders: [f.folder], inform: () => {}, choose: async items => items[0] });
  assert.equal(selected.recording.runId, 'extreme-date');
});

test('fixed-depth discovery ignores nested training trees and supports a symlinked latest index', async t => {
  const f = await setup(t);
  await f.record(path.join(f.root, '.callnav', 'container', 'deep', 'training'), 'hidden');
  const index = await f.record(path.join(f.root, '.callnav', 'visible'), 'visible');
  try { await fs.symlink(index, path.join(f.root, '.callnav', 'latest.json')); }
  catch (error) { if (!['EPERM', 'ENOSYS'].includes(error.code)) throw error; }
  const entries = await scanHistory(f.root);
  assert.deepEqual(entries.map(entry => entry.runId), ['visible']);
});

test('a selected history index retains source fingerprint checks and is re-read before use', async t => {
  const f = await setup(t);
  const index = await f.record(path.join(f.root, '.callnav', 'run'), 'run');
  const browsed = (await scanHistory(f.root))[0];
  assert.equal(browsed.usable, true);
  await fs.writeFile(f.caller, f.callerText + '# changed\n');
  const recording = await readRecording(index, { root: f.root, runId: 'run' });
  const result = await recording.index.lookup({ uri: { fsPath: f.caller }, isDirty: false }, { line: 0, character: 1 });
  assert.deepEqual(result.edges, []);
  assert.match(result.issue, /Source changed/);
  const manifestPath = path.join(path.dirname(index), 'run.json');
  const manifest = JSON.parse(await fs.readFile(manifestPath));
  await fs.writeFile(manifestPath, JSON.stringify({ ...manifest, exit_code: 1 }));
  assert.equal((await readRecording(index)).usable, false);
});

test('workspace choice prefers the active folder and picker cancellation returns no selection', async t => {
  const f = await setup(t);
  await f.record(path.join(f.root, '.callnav', 'first'), 'first');
  const another = { name: 'Another', uri: { fsPath: path.join(f.root, 'another') } };
  let calls = 0;
  const selected = await chooseRecording({ folders: [another, f.folder], activeFolder: f.folder,
    inform: () => assert.fail('history exists'), choose: async items => {
      calls += 1;
      if (calls === 1) { assert.equal(items[0].folder, f.folder); return items[0]; }
      assert.equal(items[0].recording.runId, 'first');
      return undefined;
    } });
  assert.equal(selected, null);
  assert.equal(calls, 2);
  assert.equal(await chooseRecording({ folders: [f.folder], inform: () => {}, choose: async () => undefined }), null);
});

test('history selection can return an older recording and empty workspaces explain the next action', async t => {
  const f = await setup(t);
  await f.record(path.join(f.root, '.callnav', 'old'), 'old');
  await f.record(path.join(f.root, '.callnav', 'new'), 'new', { manifest: { started_at: 1800000000 } });
  const selected = await chooseRecording({ folders: [f.folder], choose: async items => items.find(item => item.recording.runId === 'old'), inform: () => {} });
  assert.equal(selected.recording.runId, 'old');
  const messages = [];
  const empty = { name: 'Empty', uri: { fsPath: path.join(f.root, 'empty') } };
  assert.equal(await chooseRecording({ folders: [empty], choose: () => assert.fail('empty picker'), inform: value => messages.push(value) }), null);
  assert.match(messages[0], /no saved recordings/);
  await assert.rejects(chooseRecording({ folders: [], choose: () => {}, inform: () => {} }), /workspace folder/);
  assert.equal(commandText(['bash', 'line\nbreak.sh']), 'bash "line break.sh"');
});
