'use strict';

const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { sha256 } = require('../src/index');

async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'callnav-vscode-'));
  const caller = path.join(root, 'caller.py');
  const targets = path.join(root, 'targets.py');
  const collision = path.join(root, 'collision.py');
  const collisionText = 'def declared():\n    pass\n\ndef observed():\n    pass\n\nfn = declared\nfn()\n';
  const callerText = 'fn()\nchoose()\nnot_recorded()\nambiguous(); other()\n';
  const targetText = 'def first():\n    pass\n\ndef second():\n    pass\n';
  await fs.writeFile(caller, callerText);
  await fs.writeFile(targets, targetText);
  await fs.writeFile(collision, collisionText);
  const edge = (line, end, targetLine, ambiguity = false) => ({
    caller: { path: caller, line, column: 0, end_line: line, end_column: end, qualname: '<module>', offset: line * 10, precision: 'instruction', ambiguous: ambiguity },
    target: { path: targets, line: targetLine, column: 0, qualname: targetLine === 1 ? 'first' : 'second' },
    count: 1, process_ids: [1], kind: 'python'
  });
  const data = {
    schema_version: 1, run_id: 'fixture', root,
    sources: { [caller]: { sha256: sha256(callerText) }, [targets]: { sha256: sha256(targetText) }, [collision]: { sha256: sha256(collisionText) } },
    processes: [{ pid: 1, python: '3.12' }],
    edges: [edge(1, 2, 1), edge(2, 6, 1), edge(2, 6, 4), edge(4, 20, 1, true), {
      ...edge(8, 2, 4),
      caller: { ...edge(8, 2, 4).caller, path: collision },
      target: { path: collision, line: 4, column: 0, qualname: 'observed' }
    }]
  };
  const indexFile = path.join(root, 'index.json');
  await fs.writeFile(indexFile, JSON.stringify(data));
  return { root, caller, targets, collision, callerText, targetText, data, indexFile, cleanup: () => fs.rm(root, { recursive: true, force: true }) };
}

module.exports = { fixture };
