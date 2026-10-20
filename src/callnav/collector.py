"""Collect observed Python call edges without serializing argument or return values."""

import ast
import atexit
import bisect
import builtins
import dis
import functools
import hashlib
import inspect
import json
import os
import socket
import sys
import threading
import time
import tokenize
import types
from pathlib import Path

_collector = None
_SELF_ROOT = os.path.dirname(__file__) + os.sep
_SUSPENDED_CODE = inspect.CO_COROUTINE | inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR
_UNKNOWN = object()


def _static_value(expression, frame):
    """Resolve common callees without evaluating user expressions/descriptors."""
    if isinstance(expression, ast.Name):
        name = expression.id
        if name in frame.f_locals:
            return frame.f_locals[name]
        if name in frame.f_globals:
            return frame.f_globals[name]
        return vars(builtins).get(name, _UNKNOWN)
    if isinstance(expression, ast.Constant):
        return expression.value
    if isinstance(expression, ast.Attribute):
        base = _static_value(expression.value, frame)
        if base is _UNKNOWN:
            return _UNKNOWN
        try:
            value = inspect.getattr_static(base, expression.attr)
            if (type(value) is staticmethod or type(value) is classmethod):
                return value.__func__
            return value
        except AttributeError:
            return _UNKNOWN
    if isinstance(expression, ast.Subscript):
        base = _static_value(expression.value, frame)
        key = _static_value(expression.slice, frame)
        # Exact built-in containers avoid invoking an overridden __getitem__.
        if type(base) in (dict, tuple, list) and type(key) in (str, int):
            try:
                return base[key]
            except (KeyError, IndexError, TypeError):
                return _UNKNOWN
    return _UNKNOWN


def _function(value):
    """Inspect exact built-in partials and Python methods without user hooks."""
    seen = set()
    while type(value) is functools.partial and id(value) not in seen:
        seen.add(id(value))
        value = value.func
    if type(value) is types.MethodType:
        value = value.__func__
    return value if type(value) is types.FunctionType else None


def _function_code(value):
    function = _function(value)
    return function.__code__ if function is not None else None


def _native_callee(expression, frame, target_code):
    """True: native/indirect; False: matching Python code; None: unproven."""
    value = _static_value(expression, frame)
    if value is _UNKNOWN:
        return None
    function = _function(value)
    if function is not None:
        return function.__code__ is not target_code
    if issubclass(type(value), type):
        # A Python class call may enter __init__, __new__, or metaclass __call__.
        for owner, name in ((value, "__init__"), (value, "__new__"), (type(value), "__call__")):
            try:
                method = inspect.getattr_static(owner, name)
                if (type(method) is staticmethod or type(method) is classmethod):
                    method = method.__func__
                if type(method) is types.FunctionType and method.__code__ is target_code:
                    return False
            except AttributeError:
                pass
        return True
    if any(type(value) is kind for kind in (
        types.BuiltinFunctionType, types.BuiltinMethodType,
        types.MethodDescriptorType, types.WrapperDescriptorType,
        types.ClassMethodDescriptorType, types.MethodWrapperType,
    )):
        return True
    # A user callable object can enter its Python __call__ directly.
    try:
        method = inspect.getattr_static(type(value), "__call__")
        if type(method) is types.FunctionType:
            return method.__code__ is not target_code
    except AttributeError:
        pass
    return None


def _inside(path, root):
    return path == root or path.startswith(root + os.sep)


def _utf16_column(lines, line, byte_column):
    if not 0 < line <= len(lines):
        return 0
    prefix = lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8", errors="ignore")
    return len(prefix.encode("utf-16-le")) // 2


class Source:
    def __init__(self, path):
        self.path = path
        raw = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        # tokenize honors Python's declared source encoding.
        with tokenize.open(path) as stream:
            self.text = stream.read()
        self.lines = self.text.splitlines()
        self.tree = ast.parse(self.text, filename=path)
        self.calls = [node for node in ast.walk(self.tree) if isinstance(node, ast.Call)]
        self.awaits = [node for node in ast.walk(self.tree) if isinstance(node, ast.Await)]
        self.definitions = [
            node for node in ast.walk(self.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]

    def range(self, node):
        return {
            "line": node.lineno,
            "column": _utf16_column(self.lines, node.lineno, node.col_offset),
            "end_line": node.end_lineno,
            "end_column": _utf16_column(self.lines, node.end_lineno, node.end_col_offset),
        }

    def callee_range(self, node):
        function = node.func
        result = self.range(function)
        if isinstance(function, ast.Attribute):
            # Bind Ctrl+click to the method name, not the receiver expression.
            result["line"] = function.end_lineno
            result["column"] = result["end_column"] - len(function.attr.encode("utf-16-le")) // 2
        return result

    def target(self, code):
        line = code.co_firstlineno
        candidates = []
        for node in self.definitions:
            if node.name != code.co_name:
                continue
            start = min([node.lineno] + [item.lineno for item in node.decorator_list])
            if start <= line <= node.lineno:
                candidates.append(node)
        if len(candidates) == 1:
            line = candidates[0].lineno
        return {
            "path": self.path, "line": line, "column": 0,
            "qualname": getattr(code, "co_qualname", code.co_name),
        }

    def callsite(self, code, offset, line, instructions):
        result = {
            "path": self.path, "line": line, "column": 0,
            "end_line": line,
            "end_column": len(self.lines[line - 1].encode("utf-16-le")) // 2
            if 0 < line <= len(self.lines) else 0,
            "qualname": getattr(code, "co_qualname", code.co_name),
            "offset": offset, "precision": "line",
        }
        offsets, operations = instructions
        index = bisect.bisect_right(offsets, offset) - 1
        operation = operations[index] if index >= 0 else None
        # A Python entry reached through a C callback has no Python CALL at this
        # offset. Never claim instruction precision for such inferred edges.
        positions = getattr(operation, "positions", None)
        if operation and operation.opname.startswith("CALL") and positions:
            exact = [
                call for call in self.calls
                if (call.lineno, call.col_offset, call.end_lineno, call.end_col_offset)
                == (positions.lineno, positions.col_offset, positions.end_lineno, positions.end_col_offset)
            ]
            if len(exact) == 1:
                result.update(self.callee_range(exact[0]))
                result["precision"] = "instruction"
                return result, exact[0].func
        candidates = [call for call in self.calls if call.lineno <= line <= call.end_lineno]
        if len(candidates) == 1 and operation and operation.opname.startswith("CALL"):
            result.update(self.callee_range(candidates[0]))
        else:
            result["ambiguous"] = True
        return result, candidates[0].func if len(candidates) == 1 else None

    def awaitsite(self, code, offset, line, instructions, caller_frame, target_code):
        """Only immediate `await fn(...)`, with a proven matching callable."""
        offsets, operations = instructions
        index = bisect.bisect_right(offsets, offset) - 1
        operation = operations[index] if index >= 0 else None
        if not operation or operation.opname not in ("SEND", "YIELD_FROM"):
            return None
        positions = getattr(operation, "positions", None)
        if positions and positions.col_offset is not None:
            candidates = [
                node for node in self.awaits
                if (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
                == (positions.lineno, positions.col_offset, positions.end_lineno, positions.end_col_offset)
            ]
            precision = "instruction"
        else:
            candidates = [node for node in self.awaits if node.lineno <= line <= node.end_lineno]
            precision = "line"
        if len(candidates) != 1 or not isinstance(candidates[0].value, ast.Call):
            return None
        call = candidates[0].value
        actual = _static_value(call.func, caller_frame)
        if _function_code(actual) is not target_code:
            return None
        return dict(
            self.callee_range(call), path=self.path,
            qualname=getattr(code, "co_qualname", code.co_name), offset=offset, precision=precision,
        ), call.func


class Collector:
    def __init__(self):
        self.root = os.path.realpath(os.environ["CALLNAV_ROOT"])
        included = json.loads(os.environ.get("CALLNAV_INCLUDE_ROOTS", "[]"))
        if not isinstance(included, list) or not all(isinstance(path, str) and os.path.isabs(path) for path in included):
            raise ValueError("CALLNAV_INCLUDE_ROOTS must be a JSON list of absolute paths")
        self.roots = tuple(dict.fromkeys([self.root] + [os.path.realpath(path) for path in included]))
        self.run_dir = os.path.realpath(os.environ["CALLNAV_RUN_DIR"])
        self.run_id = os.environ["CALLNAV_RUN_ID"]
        self.lock = threading.RLock()
        self.sources = {}
        self.paths = {}
        self.targets = {}
        self.sites = {}
        self.site_expressions = {}
        self.native_calls = {}
        self.instructions = {}
        self.coroutine_starts = {}
        self.edges = {}
        self.entry_observer = None
        # Only IDs and Python function bindings are kept; frame objects (and
        # application arguments) are never retained by relay bookkeeping.
        self.frame_functions = {}
        self.relays = {}
        self.pid = os.getpid()
        self.started_at = time.time()
        self.last_flush = 0.0
        self.events_since_flush = 0
        self.excluded_resume_events = 0
        self.errors = 0
        self.status = "recording"
        self.finalizer = None
        self.previous_profile = sys.getprofile()
        self.previous_thread_profile = threading.getprofile()

    def source(self, path):
        if path not in self.sources:
            try:
                self.sources[path] = Source(path)
            except (OSError, SyntaxError, UnicodeError):
                self.sources[path] = None
        return self.sources[path]

    def path(self, filename):
        if filename in self.paths:
            return self.paths[filename]
        if not filename or filename.startswith("<"):
            return None
        path = os.path.realpath(filename)
        if path.startswith(_SELF_ROOT) or not any(_inside(path, root) for root in self.roots):
            path = None
        self.paths[filename] = path
        return path

    def _instructions(self, code):
        if code not in self.instructions:
            operations = list(dis.get_instructions(code))
            self.instructions[code] = ([op.offset for op in operations], operations)
        return self.instructions[code]

    def _initial_coroutine(self, frame):
        code = frame.f_code
        if not code.co_flags & inspect.CO_COROUTINE:
            return False
        if code not in self.coroutine_starts:
            self.coroutine_starts[code] = next(
                (op.offset for op in dis.get_instructions(code)
                 if op.opname == "RESUME" and op.arg == 0), -1
            )
        return frame.f_lasti == self.coroutine_starts[code]

    def _site(self, caller, target_code, immediate_await=False):
        path = self.path(caller.f_code.co_filename)
        if path is None:
            return None
        source = self.source(path)
        if source is None:
            return None
        code, offset = caller.f_code, caller.f_lasti
        key = (code, offset, "await" if immediate_await else "call")
        if key not in self.sites:
            if immediate_await:
                resolved = source.awaitsite(
                    code, offset, caller.f_lineno, self._instructions(code), caller, target_code
                )
                if resolved is None:
                    return None
                site, expression = resolved
            else:
                site, expression = source.callsite(
                    code, offset, caller.f_lineno, self._instructions(code)
                )
            self.sites[key], self.site_expressions[key] = site, expression
        expression = self.site_expressions[key]
        if immediate_await and _function_code(_static_value(expression, caller)) is not target_code:
            return None
        return self.sites[key], expression

    def _target(self, code, external=False):
        path = self.path(code.co_filename)
        if path is None and external and not code.co_filename.startswith("<"):
            path = os.path.realpath(code.co_filename)
        if path is None:
            return None
        source = self.source(path)
        if source is None:
            return None
        if code not in self.targets:
            self.targets[code] = source.target(code)
        return self.targets[code]

    def _wrapper_candidates(self, frame):
        """Only inspect wrapper bindings, never arbitrary application arguments."""
        function = self.frame_functions.get(id(frame))
        declared = _function(vars(function).get("__wrapped__")) if function is not None else None
        if declared is not None:
            return {declared.__code__}
        codes = set()
        if frame.f_code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS):
            for name in frame.f_code.co_freevars:
                candidate = _function(frame.f_locals.get(name))
                if candidate is not None:
                    codes.add(candidate.__code__)
        return codes if len(codes) == 1 else set()

    def bind_function(self, frame, function):
        """Bind an adapter-known callable only after its actual frame entered."""
        function = _function(function)
        if function is not None and function.__code__ is frame.f_code:
            self.frame_functions[id(frame)] = function
            return True
        return False

    def wrapper_link(self, caller, child):
        """Prove one active wrapper forwarding step to an actually entered body.

        __wrapped__ only identifies a possible binding. The child frame must
        actually enter that code directly. Without __wrapped__, a closed-over
        Python callable must additionally receive the wrapper's *args/**kwargs.
        """
        child_code = child.f_code
        function = self.frame_functions.get(id(caller))
        declared = _function(vars(function).get("__wrapped__")) if function is not None else None
        if declared is not None:
            if declared.__code__ is child_code:
                self.frame_functions[id(child)] = declared
                return True
            return False
        code = caller.f_code
        if not code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS):
            return False
        bindings = {
            name: _function(caller.f_locals.get(name)) for name in code.co_freevars
            if _function(caller.f_locals.get(name)) is not None
        }
        if len({id(value) for value in bindings.values()}) != 1:
            return False
        closed = {name for name, value in bindings.items() if value.__code__ is child_code}
        if not closed:
            return False
        path = os.path.realpath(code.co_filename)
        source = self.source(path)
        if source is None:
            return False
        site, expression = source.callsite(
            code, caller.f_lasti, caller.f_lineno, self._instructions(code)
        )
        if not isinstance(expression, ast.Name) or expression.id not in closed:
            return False
        calls = [call for call in source.calls if call.func is expression]
        if len(calls) != 1 or site.get("ambiguous"):
            # Immediate awaits are positioned at SEND rather than CALL.
            resolved = source.awaitsite(
                code, caller.f_lasti, caller.f_lineno, self._instructions(code), caller, child_code
            )
            if resolved is None:
                return False
            expression = resolved[1]
            calls = [call for call in source.calls if call.func is expression]
        if len(calls) != 1:
            return False
        index = code.co_argcount + code.co_kwonlyargcount
        varargs = code.co_varnames[index] if code.co_flags & inspect.CO_VARARGS else None
        index += bool(code.co_flags & inspect.CO_VARARGS)
        kwargs = code.co_varnames[index] if code.co_flags & inspect.CO_VARKEYWORDS else None
        forwarded = (
            any(isinstance(item, ast.Starred) and isinstance(item.value, ast.Name)
                and item.value.id == varargs for item in calls[0].args)
            or any(item.arg is None and isinstance(item.value, ast.Name)
                   and item.value.id == kwargs for item in calls[0].keywords)
        )
        if forwarded:
            self.frame_functions[id(child)] = bindings[expression.id]
        return forwarded

    def _add_edge(self, caller_code, offset, code, kind, site, target, via=None):
        via_key = tuple((item["path"], item["line"], item["qualname"]) for item in (via or []))
        key = (caller_code, offset, code, kind, via_key)
        edge = self.edges.get(key)
        if edge is None:
            edge = {
                "caller": dict(site), "target": target, "count": 0,
                "process_ids": [self.pid], "kind": kind,
            }
            if via:
                edge["via"] = via
            self.edges[key] = edge
        edge["count"] += 1
        self.events_since_flush += 1
        return edge

    def _observe_relays(self, frame, caller, site_info, edge):
        """Project only observed contiguous wrapper chains, capped at 8 hops."""
        states = []
        if caller is not None and self.relays.get(id(caller)) and self.wrapper_link(caller, frame):
            parent_target = self._target(caller.f_code, external=True)
            if parent_target is not None:
                parent_target = dict(parent_target, sha256=self.sources[parent_target["path"]].sha256)
            target = self._target(frame.f_code)
            if parent_target is not None:
                for origin in self.relays[id(caller)]:
                    if len(origin["via"]) >= 8:
                        continue
                    via = origin["via"] + [parent_target]
                    pending = origin["pending"]
                    if target is not None:
                        projected = self._add_edge(
                            origin["code"], origin["offset"], frame.f_code, "wrapped",
                            origin["site"], target, via,
                        )
                        for token in pending:
                            if not token["forwarded"]:
                                raw = token["edge"]
                                raw["wrapper_forwarded_count"] = raw.get("wrapper_forwarded_count", 0) + 1
                                token["forwarded"] = True
                        pending = [{"edge": projected, "forwarded": False}]
                    states.append(dict(origin, via=via, pending=pending))
        if caller is not None and site_info is not None:
            site, expression = site_info
            actual = _static_value(expression, caller) if expression is not None else _UNKNOWN
            function = _function(actual)
            if function is not None and function.__code__ is frame.f_code:
                self.frame_functions[id(frame)] = function
                native = caller.f_lasti in self.native_calls.get((threading.get_ident(), id(caller)), ())
                if not site.get("ambiguous") and not native:
                    states.append({
                        "site": dict(site), "code": caller.f_code, "offset": caller.f_lasti,
                        "via": [], "pending": [{"edge": edge, "forwarded": False}] if edge else [],
                    })
        if states or id(frame) in self.frame_functions:
            if self._wrapper_candidates(frame):
                if states:
                    self.relays[id(frame)] = states[-8:]
            else:
                self.frame_functions.pop(id(frame), None)

    def _released(self, frame):
        if id(frame) not in self.frame_functions and id(frame) not in self.relays:
            return
        # A coroutine profile "return" may mean suspension. Preserve its small
        # binding record until final return/close, without retaining the frame.
        offsets, operations = self._instructions(frame.f_code)
        index = bisect.bisect_right(offsets, frame.f_lasti) - 1
        operation = operations[index] if index >= 0 else None
        if operation and operation.opname in ("YIELD_VALUE", "YIELD_FROM"):
            return
        self.frame_functions.pop(id(frame), None)
        self.relays.pop(id(frame), None)

    def profile(self, frame, event, arg):
        if event in ("c_call", "c_return", "c_exception"):
            if self.path(frame.f_code.co_filename):
                key = (threading.get_ident(), id(frame))
                if event == "c_call":
                    self.native_calls.setdefault(key, []).append(frame.f_lasti)
                else:
                    pending = self.native_calls.get(key)
                    if pending:
                        pending.pop()
                        if not pending:
                            self.native_calls.pop(key, None)
            return
        if event == "return":
            try:
                with self.lock:
                    self._released(frame)
            except Exception:
                self.errors += 1
            return
        if event != "call":
            return
        try:
            with self.lock:
                if os.getpid() != self.pid:
                    self.after_fork()
                code = frame.f_code
                path = self.path(code.co_filename)
                # Ray must also see real external wrapper entries. It decides
                # which active method chain can prove a worker implementation.
                if self.entry_observer is not None:
                    self.entry_observer(frame)
                caller = frame.f_back
                caller_path = self.path(caller.f_code.co_filename) if caller else None
                immediate_await = False
                if code.co_flags & _SUSPENDED_CODE:
                    immediate_await = self._initial_coroutine(frame)
                    if not immediate_await:
                        if path and caller_path:
                            self.excluded_resume_events += 1
                        return
                site_info = self._site(caller, code, immediate_await) if caller_path else None
                edge = None
                if path and caller_path:
                    target = self._target(code)
                    if target is None or site_info is None:
                        if immediate_await:
                            self.excluded_resume_events += 1
                        return
                    site, expression = site_info
                    native_bridge = caller.f_lasti in self.native_calls.get((threading.get_ident(), id(caller)), ())
                    relation = _native_callee(expression, caller, code) if expression is not None else None
                    callback = native_bridge or relation is True
                    kind = "await" if immediate_await else "callback" if callback else "python"
                    observed = dict(site)
                    if callback:
                        observed.update(ambiguous=True, reason="native_callback")
                    elif relation is None:
                        observed.update(ambiguous=True, reason="unresolved_callee")
                    edge = self._add_edge(caller.f_code, caller.f_lasti, code, kind, observed, target)
                self._observe_relays(frame, caller, site_info, edge)
                if self.events_since_flush and (
                    self.events_since_flush >= 250 or time.monotonic() - self.last_flush >= 2
                ):
                    self.flush()
        except Exception:
            # A navigation recorder must never break application code.
            self.errors += 1

    def after_fork(self):
        self.lock = threading.RLock()
        self.pid = os.getpid()
        self.started_at = time.time()
        self.edges = {}
        self.native_calls = {}
        self.frame_functions = {}
        self.relays = {}
        self.events_since_flush = 0
        self.excluded_resume_events = 0
        self.errors = 0
        self.last_flush = 0.0
        self.flush()

    def flush(self):
        with self.lock:
            directory = Path(self.run_dir) / "shards"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (str(self.pid) + ".json")
            payload = {
                "schema_version": 1, "run_id": self.run_id, "root": self.root, "roots": list(self.roots),
                "sources": {p: {"sha256": src.sha256} for p, src in self.sources.items() if src},
                "process": {
                    "pid": self.pid, "ppid": os.getppid(), "host": socket.gethostname(),
                    "python": sys.version.split()[0], "executable": sys.executable,
                    "argv": sys.argv, "orig_argv": getattr(sys, "orig_argv", None),
                    "status": self.status,
                    "started_at": self.started_at, "flushed_at": time.time(),
                    "excluded_resume_events": self.excluded_resume_events, "errors": self.errors,
                },
                "edges": list(self.edges.values()),
            }
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
            os.replace(temporary, path)
            self.events_since_flush = 0
            self.last_flush = time.monotonic()

    def stop(self):
        if self.status == "complete":
            return
        if self.status == "recording":
            sys.setprofile(self.previous_profile)
            threading.setprofile(self.previous_thread_profile)
            self.status = "complete"
        self.frame_functions.clear()
        self.relays.clear()
        self.flush()


def start():
    global _collector
    if _collector is not None:
        return _collector
    instance = _collector = Collector()
    if instance.previous_profile or instance.previous_thread_profile:
        instance.status = "profile_conflict"
        print("callnav: existing profile hook detected; this process was not instrumented", file=sys.stderr)
        instance.flush()
        return instance
    atexit.register(instance.stop)
    # multiprocessing.spawn invokes os._exit after its own finalizers rather
    # than ordinary atexit callbacks.
    from multiprocessing.util import Finalize

    instance.finalizer = Finalize(None, instance.stop, exitpriority=-100)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=instance.after_fork)
    instance.flush()
    threading.setprofile(instance.profile)
    sys.setprofile(instance.profile)
    return instance
