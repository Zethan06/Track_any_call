'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const { RuntimeIndex } = require('../src/index');
const { fixture } = require('./fixture');
const doc = file => ({ uri: { fsPath: file }, isDirty: false });

async function setup(t) {
  const f = await fixture();
  t.after(f.cleanup);
  return { ...f, index: await RuntimeIndex.load(f.indexFile), document: doc(f.caller) };
}

test('exact call site returns its observed implementation', async t => {
  const f = await setup(t);
  const found = await f.index.lookup(f.document, { line: 0, character: 1 });
  assert.equal(found.edges.length, 1);
  assert.equal(found.edges[0].target.line, 1);
});

test('same site preserves multiple observed implementations', async t => {
  const f = await setup(t);
  const found = await f.index.lookup(f.document, { line: 1, character: 1 });
  assert.deepEqual(found.edges.map(edge => edge.target.line), [1, 4]);
});

test('unobserved and outside positions never use a nearby call', async t => {
  const f = await setup(t);
  for (const position of [{ line: 2, character: 1 }, { line: 0, character: 2 }]) {
    assert.deepEqual((await f.index.lookup(f.document, position)).edges, []);
  }
});

test('ambiguous Python 3.10 line is rejected', async t => {
  const f = await setup(t);
  const found = await f.index.lookup(f.document, { line: 3, character: 2 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Ambiguous/);
});

test('modified caller or target on disk is rejected', async t => {
  const f = await setup(t);
  await fs.writeFile(f.caller, `${f.callerText}# changed\n`);
  let found = await f.index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Source changed/);
  await fs.writeFile(f.caller, f.callerText);
  await fs.writeFile(f.targets, `${f.targetText}# changed\n`);
  found = await f.index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Source changed/);
});

test('unsaved caller and unsaved target are rejected', async t => {
  const f = await setup(t);
  let found = await f.index.lookup({ ...f.document, isDirty: true }, { line: 0, character: 1 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Unsaved/);
  found = await f.index.lookup(f.document, { line: 0, character: 1 }, [{ ...doc(f.targets), isDirty: true }]);
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Unsaved/);
});

test('missing target file is rejected', async t => {
  const f = await setup(t);
  await fs.unlink(f.targets);
  const found = await f.index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /unavailable/);
});

test('nested call expression uses the smallest containing range', async t => {
  const f = await setup(t);
  const outer = structuredClone(f.data.edges[0]);
  outer.caller.end_column = 12;
  outer.target.line = 4;
  const nested = structuredClone(f.data.edges[0]);
  nested.caller.column = 5;
  nested.caller.end_column = 7;
  const index = new RuntimeIndex({ ...f.data, edges: [outer, nested] });
  assert.equal((await index.lookup(f.document, { line: 0, character: 6 })).edges[0].target.line, 1);
  assert.equal((await index.lookup(f.document, { line: 0, character: 1 })).edges[0].target.line, 4);
});

test('crossing expression ranges fail closed', async t => {
  const f = await setup(t);
  const first = structuredClone(f.data.edges[0]);
  first.caller.end_column = 5;
  const second = structuredClone(first);
  second.caller.column = 3;
  second.caller.end_column = 7;
  const index = new RuntimeIndex({ ...f.data, edges: [first, second] });
  const found = await index.lookup(f.document, { line: 0, character: 4 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Overlapping/);
});

test('duplicate observations collapse to one destination', async t => {
  const f = await setup(t);
  const index = new RuntimeIndex({ ...f.data, edges: [f.data.edges[0], f.data.edges[0]] });
  assert.equal((await index.lookup(f.document, { line: 0, character: 0 })).edges.length, 1);
});

test('malformed indices are rejected before activation', async t => {
  const f = await setup(t);
  assert.throws(() => new RuntimeIndex({ ...f.data, schema_version: 99 }), /schema_version/);
  const malformed = structuredClone(f.data);
  malformed.edges[0].target.line = -1;
  assert.throws(() => new RuntimeIndex(malformed), /coordinates/);
  delete malformed.sources[f.targets];
  malformed.edges[0].target.line = 1;
  assert.throws(() => new RuntimeIndex(malformed), /fingerprint/);
});


test('ambiguous native callback cannot suppress a precise call at the same span', async t => {
  const f = await setup(t);
  const callback = structuredClone(f.data.edges[0]);
  callback.kind = 'callback';
  callback.caller.precision = 'line';
  callback.caller.ambiguous = true;
  callback.caller.reason = 'native_callback';
  callback.target.line = 4;
  const index = new RuntimeIndex({ ...f.data, edges: [callback, f.data.edges[0]] });
  const found = await index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges.map(edge => edge.target.line), [1]);
  assert.equal(found.issue, null);
});

test('native callback alone remains ambiguous instead of becoming a guessed target', async t => {
  const f = await setup(t);
  const callback = structuredClone(f.data.edges[0]);
  callback.kind = 'callback';
  callback.caller.precision = 'line';
  callback.caller.ambiguous = true;
  callback.caller.reason = 'native_callback';
  const index = new RuntimeIndex({ ...f.data, edges: [callback] });
  const found = await index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Ambiguous/);
});

test('ordinary ambiguous evidence still blocks a matching precise instruction', async t => {
  const f = await setup(t);
  const uncertain = structuredClone(f.data.edges[0]);
  uncertain.caller.ambiguous = true;
  uncertain.caller.precision = 'line';
  const index = new RuntimeIndex({ ...f.data, edges: [uncertain, f.data.edges[0]] });
  const found = await index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Ambiguous/);
});


function comprehensionEdges(f, helper = '<listcomp>') {
  const implicit = structuredClone(f.data.edges[0]);
  implicit.kind = 'python';
  implicit.caller.end_column = 20;
  implicit.caller.precision = 'line';
  implicit.caller.ambiguous = true;
  implicit.caller.reason = 'unresolved_callee';
  implicit.target = { path: f.caller, line: 1, column: 0, qualname: helper };
  const instruction = structuredClone(f.data.edges[0]);
  instruction.kind = 'ray_actor';
  instruction.caller.column = 5;
  instruction.caller.end_column = 10;
  instruction.caller.qualname = helper;
  instruction.ray = { worker_confirmed: true };
  return { implicit, instruction };
}

test('implicit comprehension entry cannot suppress its proven inner call', async t => {
  const f = await setup(t);
  for (const helper of ['<listcomp>', '<setcomp>', '<dictcomp>', '<genexpr>']) {
    const { implicit, instruction } = comprehensionEdges(f, 'owner.<locals>.' + helper);
    implicit.caller.qualname = 'owner';
    const index = new RuntimeIndex({ ...f.data, edges: [implicit, instruction] });
    const found = await index.lookup(f.document, { line: 0, character: 6 });
    assert.deepEqual(found.edges, [instruction], helper);
    assert.equal(found.issue, null);
    // Outside the precise inner instruction the implicit call is still unsafe.
    const outside = await index.lookup(f.document, { line: 0, character: 1 });
    assert.deepEqual(outside.edges, []);
    assert.match(outside.issue, /Ambiguous/);
  }
});

test('comprehension exemption requires an instruction from the same generated frame', async t => {
  const f = await setup(t);
  const variants = [
    ({ implicit }) => { implicit.target.path = f.targets; },
    ({ instruction }) => { instruction.caller.qualname = 'another.<locals>.<listcomp>'; },
    ({ implicit }) => { implicit.target.qualname = 'ordinary_callable'; },
    ({ implicit }) => { implicit.caller.qualname = 'another'; },
    ({ implicit }) => { implicit.target.line = 2; },
    ({ implicit }) => { implicit.caller.reason = 'multiple_calls'; },
    ({ implicit }) => { implicit.caller.precision = 'instruction'; },
    ({ implicit }) => { implicit.kind = 'callback'; },
    ({ instruction }) => { instruction.caller.precision = 'line'; },
    ({ instruction }) => { instruction.caller.ambiguous = true; }
  ];
  for (const mutate of variants) {
    const edges = comprehensionEdges(f);
    mutate(edges);
    const index = new RuntimeIndex({ ...f.data, edges: Object.values(edges) });
    const found = await index.lookup(f.document, { line: 0, character: 6 });
    assert.deepEqual(found.edges, []);
    assert.match(found.issue, /Ambiguous/);
  }
});

test('a genuine unresolved call inside a comprehension still blocks navigation', async t => {
  const f = await setup(t);
  const { implicit, instruction } = comprehensionEdges(f);
  const unresolved = structuredClone(instruction);
  unresolved.caller.ambiguous = true;
  unresolved.caller.reason = 'unresolved_callee';
  unresolved.target.line = 4;
  const index = new RuntimeIndex({ ...f.data, edges: [implicit, instruction, unresolved] });
  const found = await index.lookup(f.document, { line: 0, character: 6 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Ambiguous/);
});

test('multiple observed implementations inside a comprehension remain candidates', async t => {
  const f = await setup(t);
  const { implicit, instruction } = comprehensionEdges(f);
  const alternative = structuredClone(instruction);
  alternative.target.line = 4;
  const index = new RuntimeIndex({ ...f.data, edges: [implicit, instruction, alternative] });
  const found = await index.lookup(f.document, { line: 0, character: 6 });
  assert.deepEqual(found.edges.map(edge => edge.target.line), [1, 4]);
  assert.equal(found.issue, null);
});


function wrappedEdges(f) {
  const raw = structuredClone(f.data.edges[0]);
  raw.target.line = 4;
  raw.target.qualname = 'wrapper';
  raw.count = 2;
  raw.wrapper_forwarded_count = 2;
  const forwarded = structuredClone(f.data.edges[0]);
  forwarded.kind = 'wrapped';
  forwarded.count = 2;
  forwarded.via = [{ ...raw.target, sha256: f.data.sources[raw.target.path].sha256 }];
  return { raw, forwarded };
}

test('fully forwarded wrappers navigate to observed bodies and retain original evidence', async t => {
  const f = await setup(t);
  const { raw, forwarded } = wrappedEdges(f);
  const original = structuredClone([raw, forwarded]);
  const index = new RuntimeIndex({ ...f.data, edges: [raw, forwarded] });
  const found = await index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, [forwarded]);
  assert.equal(found.issue, null);
  assert.deepEqual([raw, forwarded], original);
});

test('conditional wrappers and multiple actual bodies retain their observed candidates', async t => {
  const f = await setup(t);
  const { raw, forwarded } = wrappedEdges(f);
  raw.wrapper_forwarded_count = 1;
  const alternative = structuredClone(forwarded);
  alternative.target = { path: f.collision, line: 1, column: 0, qualname: 'declared' };
  const index = new RuntimeIndex({ ...f.data, edges: [raw, forwarded, alternative] });
  const found = await index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, [raw, forwarded, alternative]);
  assert.equal(found.issue, null);
});

test('wrapper suppression requires matching caller and wrapper proof', async t => {
  const f = await setup(t);
  const variants = [
    ({ raw }) => { delete raw.wrapper_forwarded_count; },
    ({ raw }) => { raw.wrapper_forwarded_count = 0; },
    ({ forwarded }) => { forwarded.caller.qualname = 'another'; },
    ({ forwarded }) => { forwarded.caller.offset += 2; },
    ({ forwarded }) => { forwarded.via[0].line = 1; },
    ({ forwarded }) => { forwarded.via[0].qualname = 'another_wrapper'; }
  ];
  for (const mutate of variants) {
    const edges = wrappedEdges(f);
    mutate(edges);
    const index = new RuntimeIndex({ ...f.data, edges: Object.values(edges) });
    const found = await index.lookup(f.document, { line: 0, character: 1 });
    assert.deepEqual(found.edges, [edges.raw, edges.forwarded]);
  }
  const { raw } = wrappedEdges(f);
  const index = new RuntimeIndex({ ...f.data, edges: [raw] });
  assert.deepEqual((await index.lookup(f.document, { line: 0, character: 1 })).edges, [raw]);
});

test('every intermediate wrapper source is checked before using a forwarded body', async t => {
  const f = await setup(t);
  const { raw, forwarded } = wrappedEdges(f);
  forwarded.via.push({
    path: f.collision, line: 1, column: 0, qualname: 'intermediate',
    sha256: f.data.sources[f.collision].sha256
  });
  const index = new RuntimeIndex({ ...f.data, edges: [raw, forwarded] });
  await fs.appendFile(f.collision, '# wrapper changed\n');
  const found = await index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(found.edges, [raw]);
  assert.match(found.issue, /Source changed/);
});

test('Ray wrapper chains are source checked even without a separate raw wrapper edge', async t => {
  const f = await setup(t);
  const { forwarded } = wrappedEdges(f);
  forwarded.kind = 'ray_actor';
  forwarded.via = [{
    path: f.collision, line: 1, column: 0, qualname: 'ray_wrapper',
    sha256: f.data.sources[f.collision].sha256
  }];
  const index = new RuntimeIndex({ ...f.data, edges: [forwarded] });
  assert.deepEqual((await index.lookup(f.document, { line: 0, character: 1 })).edges, [forwarded]);
  const dirty = await index.lookup(f.document, { line: 0, character: 1 }, [{ ...doc(f.collision), isDirty: true }]);
  assert.deepEqual(dirty.edges, []);
  assert.match(dirty.issue, /Unsaved/);
  await fs.unlink(f.collision);
  const missing = await index.lookup(f.document, { line: 0, character: 1 });
  assert.deepEqual(missing.edges, []);
  assert.match(missing.issue, /unavailable/);
});

test('malformed wrapper chains and impossible forwarding counts are rejected', async t => {
  const f = await setup(t);
  for (const mutate of [
    edge => { delete edge.via; },
    edge => { edge.via = []; },
    edge => { delete edge.via[0].sha256; },
    edge => { edge.via[0].sha256 = '0'.repeat(64); },
    edge => { edge.via[0].path = 'relative.py'; },
    edge => { edge.via[0].line = 0; },
    edge => { edge.via[0].column = -1; }
  ]) {
    const { forwarded } = wrappedEdges(f);
    mutate(forwarded);
    assert.throws(() => new RuntimeIndex({ ...f.data, edges: [forwarded] }), /wrapper|fingerprint/i);
  }
  const { raw } = wrappedEdges(f);
  raw.wrapper_forwarded_count = raw.count + 1;
  assert.throws(() => new RuntimeIndex({ ...f.data, edges: [raw] }), /forwarding count/);
});


test('a proved implicit helper may share the precise body instruction range', async t => {
  const f = await setup(t);
  const { implicit, instruction } = comprehensionEdges(f);
  implicit.caller.column = instruction.caller.column;
  implicit.caller.end_column = instruction.caller.end_column;
  const index = new RuntimeIndex({ ...f.data, edges: [implicit, instruction] });
  const found = await index.lookup(f.document, { line: 0, character: 6 });
  assert.deepEqual(found.edges, [instruction]);
  assert.equal(found.issue, null);
});

function nestedComprehensionEdges(f) {
  const { implicit: outer, instruction } = comprehensionEdges(f, 'owner.<locals>.<listcomp>');
  outer.caller.qualname = 'owner';
  const inner = structuredClone(outer);
  inner.caller.qualname = outer.target.qualname;
  inner.target.qualname += '.<listcomp>';
  instruction.caller.qualname = inner.target.qualname;
  return { outer, inner, instruction };
}

test('nested implicit helpers require a fully observed chain to the precise body', async t => {
  const f = await setup(t);
  for (const sameRange of [false, true]) {
    const { outer, inner, instruction } = nestedComprehensionEdges(f);
    if (sameRange) {
      for (const entry of [outer, inner]) {
        entry.caller.column = instruction.caller.column;
        entry.caller.end_column = instruction.caller.end_column;
      }
    }
    const index = new RuntimeIndex({ ...f.data, edges: [outer, inner, instruction] });
    const found = await index.lookup(f.document, { line: 0, character: 6 });
    assert.deepEqual(found.edges, [instruction]);
    assert.equal(found.issue, null);
    const missing = new RuntimeIndex({ ...f.data, edges: [outer, instruction] });
    const unresolved = await missing.lookup(f.document, { line: 0, character: 6 });
    assert.deepEqual(unresolved.edges, []);
    assert.match(unresolved.issue, /Ambiguous/);
  }
});

test('class-scope helpers and mixed callback helper chains use their actual compiler lineage', async t => {
  const f = await setup(t);
  const classEdges = comprehensionEdges(f, 'C.<listcomp>');
  classEdges.implicit.caller.qualname = 'C';
  let index = new RuntimeIndex({ ...f.data, edges: Object.values(classEdges) });
  assert.deepEqual((await index.lookup(f.document, { line: 0, character: 6 })).edges, [classEdges.instruction]);
  const { outer, inner, instruction } = nestedComprehensionEdges(f);
  inner.kind = 'callback';
  inner.caller.reason = 'native_callback';
  index = new RuntimeIndex({ ...f.data, edges: [outer, inner, instruction] });
  assert.deepEqual((await index.lookup(f.document, { line: 0, character: 6 })).edges, [instruction]);
});

test('invalid intermediate helper evidence and real unresolved body calls stay blocked', async t => {
  const f = await setup(t);
  for (const mutate of [
    edges => { edges.inner.caller.qualname = 'another'; },
    edges => { edges.inner.target.qualname = 'ordinary'; },
    edges => { edges.inner.target.path = f.targets; },
    edges => { edges.inner.caller.column = 6; },
    edges => { edges.inner.target.line = 2; },
    edges => { edges.inner.caller.reason = 'unresolved_user_call'; }
  ]) {
    const edges = nestedComprehensionEdges(f);
    mutate(edges);
    const index = new RuntimeIndex({ ...f.data, edges: Object.values(edges) });
    const found = await index.lookup(f.document, { line: 0, character: 6 });
    assert.deepEqual(found.edges, []);
    assert.match(found.issue, /Ambiguous/);
  }
  const { outer, inner, instruction } = nestedComprehensionEdges(f);
  const uncertain = structuredClone(instruction);
  uncertain.caller.ambiguous = true;
  uncertain.caller.reason = 'unresolved_callee';
  const index = new RuntimeIndex({ ...f.data, edges: [outer, inner, instruction, uncertain] });
  const found = await index.lookup(f.document, { line: 0, character: 6 });
  assert.deepEqual(found.edges, []);
  assert.match(found.issue, /Ambiguous/);
});
