'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');

const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const compare = (a, b) => a.line - b.line || a.character - b.character;
const keyFor = range => `${range.line}:${range.column}:${range.end_line}:${range.end_column}`;
const startOf = range => ({ line: range.line - 1, character: range.column });
const endOf = range => ({ line: range.end_line - 1, character: range.end_column });
const contains = (range, position) => compare(startOf(range), position) <= 0 && compare(position, endOf(range)) < 0;
const integer = (value, minimum) => Number.isInteger(value) && value >= minimum;
const isAmbiguous = caller => caller.ambiguous === true || caller.ambiguity === true;
const targetKey = target => [target.path, target.line, target.column, target.qualname].join(':');
const sameCaller = (first, second) => first.path === second.path &&
  keyFor(first) === keyFor(second) && first.qualname === second.qualname &&
  first.offset === second.offset;

// Comprehensions execute implicit compiler-generated frames. Their fallback
// ranges do not compete with a precise call proved inside those same frames.
const isComprehensionEntry = edge => {
  const caller = edge.caller;
  const target = edge.target;
  const helper = (target.qualname || '').match(/(?:^|\.)<(?:listcomp|setcomp|dictcomp|genexpr)>$/);
  if (!helper || caller.precision !== 'line' || !isAmbiguous(caller) ||
      target.path !== caller.path || target.line < caller.line) return false;
  const implicit = (edge.kind === 'python' && caller.reason === 'unresolved_callee') ||
    (edge.kind === 'callback' && caller.reason === 'native_callback');
  const name = helper[0].replace(/^\./, '');
  const expected = caller.qualname === '<module>' ? [name] :
    [caller.qualname + '.<locals>.' + name, caller.qualname + '.' + name];
  return implicit && expected.includes(target.qualname);
};

const isComprehensionAncestor = (edge, precise, entries) => {
  if (!isComprehensionEntry(edge) || precise.caller.precision !== 'instruction' ||
      isAmbiguous(precise.caller) || edge.target.path !== precise.caller.path) return false;
  const containsInstruction = entry => entry.target.line <= precise.caller.line &&
    compare(startOf(entry.caller), startOf(precise.caller)) <= 0 &&
    compare(endOf(precise.caller), endOf(entry.caller)) <= 0;
  if (!containsInstruction(edge)) return false;
  const pending = [edge.target.qualname];
  const seen = new Set();
  while (pending.length) {
    const qualname = pending.pop();
    if (qualname === precise.caller.qualname) return true;
    if (seen.has(qualname)) continue;
    seen.add(qualname);
    // Every intermediate helper must also have been observed. Qualified-name
    // similarity alone never proves a missing link in a nested comprehension.
    for (const entry of entries) {
      if (entry.caller.path === edge.target.path && entry.caller.qualname === qualname &&
          containsInstruction(entry)) pending.push(entry.target.qualname);
    }
  }
  return false;
};

class RuntimeIndex {
  constructor(data) {
    if (data.schema_version !== 1 || typeof data.run_id !== 'string' || !path.isAbsolute(data.root || '') ||
        !data.sources || typeof data.sources !== 'object' || !Array.isArray(data.edges)) {
      throw new Error('Unsupported or malformed CallNav index (expected schema_version 1).');
    }
    for (const [file, source] of Object.entries(data.sources)) {
      if (!path.isAbsolute(file) || !source || !/^[a-f0-9]{64}$/.test(source.sha256)) {
        throw new Error(`Invalid source fingerprint: ${file}`);
      }
    }
    this.runId = data.run_id;
    this.root = data.root;
    this.sources = data.sources;
    this.byFile = new Map();
    for (const edge of data.edges) {
      const c = edge.caller;
      const t = edge.target;
      if (!c || !t || !path.isAbsolute(c.path || '') || !path.isAbsolute(t.path || '') ||
          !integer(c.line, 1) || !integer(c.end_line, c.line) || !integer(c.column, 0) ||
          !integer(c.end_column, 0) || compare(startOf(c), endOf(c)) >= 0 ||
          !integer(t.line, 1) || !integer(t.column, 0)) {
        throw new Error('Invalid call-site or target coordinates in CallNav index.');
      }
      if (!this.sources[c.path] || !this.sources[t.path]) {
        throw new Error('CallNav index is missing a caller or target source fingerprint.');
      }
      if (edge.kind === 'wrapped' && (!Array.isArray(edge.via) || !edge.via.length)) {
        throw new Error('CallNav wrapped edge is missing its observed wrapper chain.');
      }
      if (edge.via !== undefined) {
        if (!Array.isArray(edge.via) || edge.via.some(wrapper =>
          !wrapper || !path.isAbsolute(wrapper.path || '') || !integer(wrapper.line, 1) ||
          !integer(wrapper.column, 0) || typeof wrapper.qualname !== 'string' ||
          !this.sources[wrapper.path] || wrapper.sha256 !== this.sources[wrapper.path].sha256)) {
          throw new Error('CallNav wrapper chain is missing valid source fingerprints or coordinates.');
        }
      }
      if (edge.wrapper_forwarded_count !== undefined &&
          (!integer(edge.wrapper_forwarded_count, 0) || !integer(edge.count, 1) ||
            edge.wrapper_forwarded_count > edge.count)) {
        throw new Error('Invalid CallNav wrapper forwarding count.');
      }
      const edges = this.byFile.get(c.path) || [];
      edges.push(edge);
      this.byFile.set(c.path, edges);
    }
  }

  static async load(file) {
    return new RuntimeIndex(JSON.parse(await fs.readFile(file, 'utf8')));
  }

  async sourceState(file, openDocuments = []) {
    if (openDocuments.some(doc => doc.uri.fsPath === file && doc.isDirty)) {
      return `Unsaved changes: ${file}. Save and record a new run.`;
    }
    try {
      const bytes = await fs.readFile(file);
      if (!this.sources[file] || sha256(bytes) !== this.sources[file].sha256) {
        return `Source changed since recording: ${file}. Record a new run.`;
      }
    } catch {
      return `Recorded source is unavailable: ${file}.`;
    }
    return null;
  }

  async lookup(document, position, openDocuments = []) {
    const file = document.uri.fsPath;
    const staleCaller = await this.sourceState(file, [...openDocuments, document]);
    if (staleCaller) return { edges: [], issue: staleCaller };
    let matches = (this.byFile.get(file) || []).filter(edge => contains(edge.caller, position));
    if (!matches.length) return { edges: [], issue: null };
    const comprehensions = matches.filter(isComprehensionEntry);
    // Native callbacks have no exact source call. They must not suppress a
    // separately observed instruction at the cursor, or become its destination.
    if (matches.some(edge => edge.caller.precision === 'instruction' && !isAmbiguous(edge.caller))) {
      matches = matches.filter(edge => !(edge.kind === 'callback' &&
        edge.caller.reason === 'native_callback' && isAmbiguous(edge.caller)));
    }
    const precise = matches.filter(edge =>
      edge.caller.precision === 'instruction' && !isAmbiguous(edge.caller));
    matches = matches.filter(edge =>
      !precise.some(instruction => isComprehensionAncestor(edge, instruction, comprehensions)));
    // Other ambiguous evidence still cannot identify a safe target.
    if (matches.some(edge => isAmbiguous(edge.caller))) {
      return { edges: [], issue: 'Ambiguous call site in this recording; no safe runtime target can be selected.' };
    }
    // Nested expressions select the smallest containing site; never guess a nearby call.
    const ranges = [...new Map(matches.map(edge => [keyFor(edge.caller), edge.caller])).values()];
    const narrowest = ranges.filter(range => ranges.every(other =>
      compare(startOf(other), startOf(range)) <= 0 && compare(endOf(range), endOf(other)) <= 0));
    if (narrowest.length !== 1) return { edges: [], issue: 'Overlapping call sites cannot be resolved safely.' };
    const selected = matches.filter(edge => keyFor(edge.caller) === keyFor(narrowest[0]));
    const targetStates = new Map();
    const eligible = [];
    let issue = null;
    for (const edge of selected) {
      let stale = null;
      for (const target of [edge.target, ...(edge.via || [])]) {
        if (!targetStates.has(target.path)) {
          targetStates.set(target.path, await this.sourceState(target.path, openDocuments));
        }
        stale = stale || targetStates.get(target.path);
      }
      if (stale) { issue = stale; continue; }
      eligible.push(edge);
    }
    // A wrapper can disappear only when every observed invocation forwarded,
    // and a source-verified observation proves the inner target at this site.
    // Conditional wrappers retain both their own body and forwarded candidates.
    const visible = eligible.filter(edge => !(integer(edge.count, 1) &&
      edge.wrapper_forwarded_count === edge.count && eligible.some(forwarded =>
        forwarded !== edge && sameCaller(forwarded.caller, edge.caller) &&
        (forwarded.via || []).some(wrapper => targetKey(wrapper) === targetKey(edge.target)))));
    const results = [];
    const seen = new Set();
    for (const edge of visible) {
      const key = [edge.target.path, edge.target.line, edge.target.column].join(':');
      if (!seen.has(key)) { results.push(edge); seen.add(key); }
    }
    return { edges: results, issue };
  }
}

module.exports = { RuntimeIndex, sha256 };
