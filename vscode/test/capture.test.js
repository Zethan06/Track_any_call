'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { recordingProcess, expandPath, interpreterPath } = require('../src/capture-config');

test('script capture preserves literal paths, arguments, and inherited environment', () => {
  const root = path.resolve('/tmp/a project');
  const python = path.join(root, 'env/bin/python');
  const backend = path.resolve('/tmp/extension/python');
  const command = ['bash', path.join(root, "launch 'special'.sh"), 'space arg', '$(touch /tmp/never)', ''];
  const result = recordingProcess({
    python, backend, root, output: path.join(root, '.callnav', 'one'), command,
    includeRoots: ['../library', '${workspaceFolder}/extra'], ray: true,
    env: { CUDA_VISIBLE_DEVICES: '2,3', PYTHONPATH: '/existing', PATH: '/tools' }
  });
  assert.deepEqual(result.args.slice(-command.length), command);
  assert.deepEqual(result.args.slice(0, 4), ['-P', '-m', 'callnav', 'record']);
  assert.equal(result.args.filter(arg => arg === '--').length, 1);
  assert.ok(result.args.includes('--ray'));
  assert.ok(result.args.includes(path.resolve(root, '../library')));
  assert.ok(result.args.includes(path.join(root, 'extra')));
  assert.equal(result.options.cwd, root);
  assert.equal(result.options.env.PYTHONPATH, backend + path.delimiter + '/existing');
  assert.equal(result.options.env.PATH, path.dirname(python) + path.delimiter + '/tools');
  assert.equal(result.options.env.CUDA_VISIBLE_DEVICES, '2,3');
});

test('iteration budgets are opt-in and validated at the process owner', () => {
  const base = { python: 'python3', backend: '/backend', root: '/source', output: '/trace', command: ['bash', 'launch.sh'] };
  assert.ok(!recordingProcess(base).args.includes('--iterations'));
  const args = recordingProcess({ ...base, adapter: 'slime', iterations: 2 }).args;
  assert.deepEqual(args.slice(args.indexOf('--adapter'), args.indexOf('--')), ['--adapter', 'slime', '--iterations', '2']);
  assert.throws(() => recordingProcess({ ...base, adapter: 'slime', iterations: 0 }), /positive integer/);
  assert.throws(() => recordingProcess({ ...base, adapter: 'verl' }), /Unsupported adapter/);
  assert.throws(() => recordingProcess({ ...base, command: 'bash launch.sh' }), /array of strings/);
  assert.throws(() => recordingProcess({ ...base, includeRoots: 'relative' }), /array of paths/);
  assert.equal(expandPath('work', '/source'), path.resolve('/source/work'));
});

test('relative interpreter paths resolve independently of the capture working directory', () => {
  const root = path.resolve('/tmp/project');
  const python = interpreterPath('.venv/bin/python', root);
  assert.equal(python, path.join(root, '.venv/bin/python'));
  assert.equal(interpreterPath('${workspaceFolder}/env/python', root), path.join(root, 'env/python'));
  assert.equal(interpreterPath('python3', root), 'python3');
  const process_ = recordingProcess({ python, root, cwd: 'work', backend: '/backend',
    output: '/trace', command: ['bash', 'launch.sh'], env: { PATH: '/tools' } });
  assert.equal(process_.options.cwd, path.join(root, 'work'));
  assert.equal(process_.options.env.PATH, path.dirname(python) + path.delimiter + '/tools');
});
