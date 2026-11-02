"""Collect observed Python call edges without serializing argument or return values."""

import ast
import atexit
import bisect
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
import weakref
from pathlib import Path

from .cache import CodeIdentityMap
from .scheduled import ScheduledCalls
from .callbacks import RegisteredCallbacks

_collector = None
_SELF_ROOT = os.path.dirname(__file__) + os.sep
_SUSPENDED_CODE = inspect.CO_COROUTINE | inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR
_UNKNOWN = object()
_UNSET = object()
_BUILTIN_GETATTR = getattr
_BUILTIN_SUPER = super
_TYPE_MRO = vars(type)["__mro__"]
_TYPE_NAMESPACE = vars(type)["__dict__"]
_MODULE_TYPE_NAMES = frozenset(
    name for owner in types.ModuleType.__mro__ for name in vars(owner)
)


class _SuperValue:
    __slots__ = ("owner", "receiver")

    def __init__(self, owner, receiver):
        self.owner, self.receiver = owner, receiver


def _ordinary_instance_attribute(base, name):
    """Read fresh ordinary instance bindings using only builtin descriptors.

    Unusual metaclasses, shadowed dictionaries and data descriptors retain the
    conservative inspect path. No class or instance binding is cached: either
    can change between two calls at the same source site.
    """
    kind = type(base)
    if type(kind) is not type or kind is type:
        return _UNSET
    missing = object()
    getter = value = dictionary = missing
    for owner in _TYPE_MRO.__get__(kind):
        namespace = _TYPE_NAMESPACE.__get__(owner)
        if getter is missing:
            getter = namespace.get("__getattribute__", missing)
        if value is missing:
            value = namespace.get(name, missing)
        entry = namespace.get("__dict__", missing)
        if entry is not missing:
            if (type(entry) is not types.GetSetDescriptorType
                    or entry.__name__ != "__dict__" or entry.__objclass__ is not owner):
                return _UNSET
            if dictionary is missing:
                dictionary = entry
    if getter is not object.__getattribute__:
        return _UNSET
    if dictionary is not missing:
        bindings = dictionary.__get__(base, kind)
        if type(bindings) is not dict:
            return _UNSET
        stored = bindings.get(name, missing)
        if stored is not missing:
            # Only these exact class values cannot take precedence as a data
            # descriptor. Other shapes retain inspect's precedence checks.
            if (value is not missing and type(value) is not types.FunctionType
                    and type(value) is not staticmethod and type(value) is not classmethod):
                return _UNSET
            value = stored
    # inspect uses a private object sentinel for a missing attribute. Preserve
    # its treatment if an application explicitly stores that exact sentinel.
    if value is missing:
        return _UNKNOWN
    if type(value) is object:
        return _UNSET
    return value


def _attribute_value(base, name):
    """Inspect ordinary bindings, without invoking application attribute hooks."""
    if type(name) is not str:
        return _UNKNOWN
    if type(base) is types.ModuleType and name not in _MODULE_TYPE_NAMES:
        # Ordinary module globals are read afresh, without __getattr__. Keep
        # builtin descriptors on the original path even if globals shadow them.
        value = vars(base).get(name, _UNKNOWN)
        if value is _UNKNOWN or type(value) is types.FunctionType or type(value) is types.ModuleType:
            return value
        if type(value) is object:
            # Keep getattr_static's conservative treatment of its own missing
            # sentinel, even when an ordinary module exports an alias of it.
            try:
                value = inspect.getattr_static(base, name)
            except AttributeError:
                return _UNKNOWN
    elif type(base) is _SuperValue:
        receiver_type = base.receiver if issubclass(type(base.receiver), type) else type(base.receiver)
        try:
            mro = _TYPE_MRO.__get__(receiver_type)
            index = next(i for i, owner in enumerate(mro) if owner is base.owner)
        except (TypeError, StopIteration):
            return _UNKNOWN
        value = _UNKNOWN
        for owner in mro[index + 1:]:
            namespace = _TYPE_NAMESPACE.__get__(owner)
            if name in namespace:
                value = namespace[name]
                break
    else:
        value = _ordinary_instance_attribute(base, name)
        if value is not _UNSET:
            if type(value) is types.FunctionType:
                return value
            if type(value) is staticmethod or type(value) is classmethod:
                return value.__func__
        else:
            value = _UNKNOWN
        try:
            if value is _UNKNOWN:
                getter = inspect.getattr_static(type(base), "__getattribute__")
                if not any(getter is allowed for allowed in (
                        object.__getattribute__, type.__getattribute__, types.ModuleType.__getattribute__)):
                    return _UNKNOWN
            if value is _UNKNOWN and issubclass(type(base), type):
                # inspect.getattr_static(Class, name) prefers the class dict,
                # but type.__getattribute__ gives a metaclass data descriptor
                # precedence over that dictionary. Such a descriptor can
                # produce an unrelated factory; inspecting the class's stored
                # function must never prove that factory's native result.
                for owner in _TYPE_MRO.__get__(type(base)):
                    namespace = _TYPE_NAMESPACE.__get__(owner)
                    if name not in namespace:
                        continue
                    descriptor_type = type(namespace[name])
                    if (inspect.getattr_static(descriptor_type, "__get__", _UNKNOWN) is not _UNKNOWN
                            and (inspect.getattr_static(descriptor_type, "__set__", _UNKNOWN) is not _UNKNOWN
                                 or inspect.getattr_static(descriptor_type, "__delete__", _UNKNOWN) is not _UNKNOWN)):
                        return _UNKNOWN
                    break
            if value is _UNKNOWN:
                value = inspect.getattr_static(base, name)
        except AttributeError:
            return _UNKNOWN
    if type(value) is staticmethod or type(value) is classmethod:
        return value.__func__
    if type(value) is types.FunctionType:
        return value
    # These exact builtin types cannot acquire a descriptor protocol. Keep
    # ordinary stored classes/constants cheap without caching mutable bindings
    # or comparing a user metaclass by equality.
    value_type = type(value)
    if (value is None or value_type is type or value_type is types.BuiltinFunctionType
            or value_type is types.ModuleType or value_type is bool or value_type is int
            or value_type is float or value_type is complex or value_type is str
            or value_type is bytes or value_type is tuple or value_type is list
            or value_type is dict or value_type is set or value_type is frozenset):
        return value
    # Properties and user descriptors may produce a completely different
    # callable. Static inspection is evidence only for ordinary stored values.
    if inspect.getattr_static(value_type, "__get__", _UNKNOWN) is not _UNKNOWN:
        return _UNKNOWN
    return value


def _static_value(expression, frame):
    """Resolve common callees without evaluating user expressions/descriptors."""
    if isinstance(expression, ast.Name):
        name = expression.id
        bindings = frame.f_locals
        if name in bindings:
            return bindings[name]
        if name in frame.f_globals:
            return frame.f_globals[name]
        return frame.f_builtins.get(name, _UNKNOWN)
    if isinstance(expression, ast.Constant):
        return expression.value
    if isinstance(expression, ast.Attribute):
        base = _static_value(expression.value, frame)
        if base is _UNKNOWN:
            return _UNKNOWN
        return _attribute_value(base, expression.attr)
    if isinstance(expression, ast.Subscript):
        base = _static_value(expression.value, frame)
        key = _static_value(expression.slice, frame)
        # Exact built-in containers avoid invoking an overridden __getitem__.
        safe_dict = (type(base) is dict
                     and all(type(item) is str or type(item) is int for item in base))
        if ((safe_dict or type(base) is tuple or type(base) is list)
                and (type(key) is str or type(key) is int)):
            try:
                return base[key]
            except (KeyError, IndexError, TypeError):
                return _UNKNOWN
    if isinstance(expression, ast.Call) and not expression.keywords:
        function = _static_value(expression.func, frame)
        if function is _BUILTIN_GETATTR and len(expression.args) == 2:
            base, name = (_static_value(argument, frame) for argument in expression.args)
            if base is not _UNKNOWN:
                return _attribute_value(base, name)
        if function is _BUILTIN_SUPER:
            if len(expression.args) == 2:
                owner, receiver = (_static_value(argument, frame) for argument in expression.args)
            elif not expression.args and frame.f_code.co_argcount:
                owner = frame.f_locals.get("__class__", _UNKNOWN)
                receiver = frame.f_locals.get(frame.f_code.co_varnames[0], _UNKNOWN)
            else:
                return _UNKNOWN
            if issubclass(type(owner), type) and receiver is not _UNKNOWN:
                return _SuperValue(owner, receiver)
    return _UNKNOWN


def _function(value):
    """Inspect exact built-in partials and Python methods without user hooks."""
    if type(value) is types.FunctionType:
        return value
    if type(value) is types.MethodType:
        function = value.__func__
        return function if type(function) is types.FunctionType else None
    if type(value) is not functools.partial:
        return None
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


def _native_callee(expression, frame, target_code, *, value=_UNSET):
    """True: native/indirect; False: matching Python code; None: unproven."""
    if value is _UNSET:
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
        self.nodes_by_type = {}
        for node in ast.walk(self.tree):
            self.nodes_by_type.setdefault(type(node), []).append(node)
        self.calls = self.nodes_by_type.get(ast.Call, [])
        self.calls_by_span = {}
        self.calls_by_function = {}
        for call in self.calls:
            span = (call.lineno, call.col_offset, call.end_lineno, call.end_col_offset)
            self.calls_by_span.setdefault(span, []).append(call)
            self.calls_by_function.setdefault(call.func, []).append(call)
        self.callsite_cache = {}
        self.callsite_codes = {}
        self.calls_by_line = {}
        self.callee_effects = {}
        self.transparent_calls = CodeIdentityMap()
        self.returned_callables = {
            call.func: call for call in self.calls if isinstance(call.func, ast.Call)
        }
        self.returned_methods = {
            call.func.value: call.func for call in self.calls
            if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Call)
        }
        self.returned_producers_by_function = {}
        for node in (*self.returned_callables, *self.returned_methods):
            self.returned_producers_by_function.setdefault(node.func, []).append(node)
        self.awaits = self.nodes_by_type.get(ast.Await, [])
        self.comprehensions = [node for kind in (
            ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp
        ) for node in self.nodes_by_type.get(kind, [])]
        self.comprehensions_by_span = {}
        for node in self.comprehensions:
            span = (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)
            self.comprehensions_by_span.setdefault(span, []).append(node)
        self.definitions = [node for kind in (
            ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef
        ) for node in self.nodes_by_type.get(kind, [])]
        self.definitions_by_name = {}
        for node in self.definitions:
            self.definitions_by_name.setdefault(node.name, []).append(node)

    def transparent_forwarding(self, code, call):
        """Recognize a whole forwarding body, never an argument-consuming API."""
        plans = self.transparent_calls.get(code)
        if plans is None:
            plans = self.transparent_calls[code] = {}
        if call in plans:
            return plans[call]
        plans[call] = False
        if code.co_argcount > 1 or code.co_kwonlyargcount:
            return False
        definitions = [node for node in self.definitions_by_name.get(code.co_name, ())
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and node.lineno == code.co_firstlineno]
        if len(definitions) != 1:
            return False
        body = definitions[0].body
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and type(body[0].value.value) is str):
            body = body[1:]
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            return False
        returned = body[0].value
        if isinstance(returned, ast.Await):
            returned = returned.value
        if returned is not call:
            return False
        index = code.co_argcount
        varargs = code.co_varnames[index] if code.co_flags & inspect.CO_VARARGS else None
        index += bool(code.co_flags & inspect.CO_VARARGS)
        kwargs = code.co_varnames[index] if code.co_flags & inspect.CO_VARKEYWORDS else None
        if len(call.args) != bool(varargs) or len(call.keywords) != bool(kwargs):
            return False
        if varargs and not (isinstance(call.args[0], ast.Starred)
                            and isinstance(call.args[0].value, ast.Name)
                            and call.args[0].value.id == varargs):
            return False
        if kwargs and not (call.keywords[0].arg is None
                           and isinstance(call.keywords[0].value, ast.Name)
                           and call.keywords[0].value.id == kwargs):
            return False
        if any(isinstance(node, ast.Name) and node.id in (varargs, kwargs)
               for node in ast.walk(call.func)):
            return False
        plans[call] = True
        return True

    def callee_has_effects(self, expression):
        """Cache source structure only; runtime bindings are always read again."""
        if expression not in self.callee_effects:
            contains_call = any(isinstance(node, ast.Call) for node in ast.walk(expression))
            effectful = (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr,
                         ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            self.callee_effects[expression] = contains_call and any(
                isinstance(node, effectful)
                for call in self.calls_by_function.get(expression, ())
                for argument in (*call.args, *(item.value for item in call.keywords))
                for node in ast.walk(argument)
            )
        return self.callee_effects[expression]

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
        for node in self.definitions_by_name.get(code.co_name, ()):
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
        key = id(code), offset, line
        if key not in self.callsite_cache:
            self.callsite_codes[id(code)] = code
            self.callsite_cache[key] = self._callsite(code, offset, line, instructions)
        site, expression = self.callsite_cache[key]
        # Callers may annotate the observation, never the cached source range.
        return dict(site), expression

    def _callsite(self, code, offset, line, instructions):
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
        if operation is not None and not operation.opname.startswith("CALL"):
            # Specialized implicit calls can expose an inline CACHE position.
            # Identify their owning instruction, keeping CALL's raw convention.
            result["offset"] = operation.offset
        # A Python entry reached through a C callback has no Python CALL at this
        # offset. Never claim instruction precision for such inferred edges.
        positions = getattr(operation, "positions", None)
        if operation and operation.opname.startswith("CALL") and positions:
            span = positions.lineno, positions.col_offset, positions.end_lineno, positions.end_col_offset
            exact = self.calls_by_span.get(span, ())
            if len(exact) == 1:
                result.update(self.callee_range(exact[0]))
                result["precision"] = "instruction"
                return result, exact[0].func
            implicit = self.comprehensions_by_span.get(span, ())
            if len(implicit) == 1:
                # CPython 3.11 calls a generated function to evaluate a
                # comprehension. It has no explicit callee token. Keep that
                # observation on its own expression, never an enclosing call's
                # method name merely because f_lineno points at that call.
                result.update(self.range(implicit[0]), ambiguous=True)
                return result, None
        if line not in self.calls_by_line:
            self.calls_by_line[line] = [call for call in self.calls if call.lineno <= line <= call.end_lineno]
        candidates = self.calls_by_line[line]
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
        self.targets = CodeIdentityMap()
        self.site_codes = {}
        self.edge_codes = {}
        self.sites = {}
        self.site_expressions = {}
        self.native_calls = {}
        self.instructions = CodeIdentityMap()
        self.coroutine_starts = CodeIdentityMap()
        self.edges = {}
        self.entry_observer = None
        self.entry_observers = []
        self.returned_calls = {}
        self.scheduled = ScheduledCalls(self)
        self.callbacks = RegisteredCallbacks(self)
        # Only IDs and weak Python function bindings are kept; frames, closures,
        # and application arguments are never kept alive by relay bookkeeping.
        self.frame_functions = {}
        self.relays = {}
        self.call_bindings = {}
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
        self.profile_monitoring = None

    def source(self, path):
        if path not in self.sources:
            try:
                self.sources[path] = Source(path)
            except (OSError, SyntaxError, UnicodeError):
                self.sources[path] = None
        return self.sources[path]

    def path(self, filename):
        path = self.paths.get(filename, _UNSET)
        if path is not _UNSET:
            return path
        if not filename or filename.startswith("<"):
            self.paths[filename] = None
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
        return bool(frame.f_code.co_flags & inspect.CO_COROUTINE) and self._initial_entry(frame)

    def _initial_entry(self, frame):
        code = frame.f_code
        if not code.co_flags & _SUSPENDED_CODE:
            return True
        if code not in self.coroutine_starts:
            self.coroutine_starts[code] = next(
                (op.offset for op in dis.get_instructions(code)
                 if op.opname == "RESUME" and op.arg == 0), -1
            )
        return frame.f_lasti == self.coroutine_starts[code]

    def _suspended(self, frame):
        offsets, operations = self._instructions(frame.f_code)
        index = bisect.bisect_right(offsets, frame.f_lasti) - 1
        operation = operations[index] if index >= 0 else None
        if operation is None:
            return False
        if operation.opname in ("YIELD_VALUE", "YIELD_FROM"):
            return True
        # Python 3.13 can report the next RESUME instruction for a suspension
        # profile return. Final returns still point at RETURN_VALUE/RETURN_CONST.
        return (operation.opname == "RESUME" and operation.arg != 0 and index > 0
                and operations[index - 1].opname == "YIELD_VALUE")

    def _site(self, caller, target_code, immediate_await=False):
        path = self.path(caller.f_code.co_filename)
        if path is None:
            return None
        source = self.source(path)
        if source is None:
            return None
        code, offset = caller.f_code, caller.f_lasti
        key = (id(code), offset, "await" if immediate_await else "call")
        if key not in self.sites:
            self.site_codes[id(code)] = code
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
        function = self._bound_function(frame)
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
            key = id(frame)

            def discard(reference):
                if self.frame_functions.get(key) is reference:
                    self.frame_functions.pop(key, None)
                    self.relays.pop(key, None)

            self.frame_functions[key] = weakref.ref(function, discard)
            profile = self.profile_monitoring
            if (profile is not None and profile.active
                    and getattr(profile, "requires_native_masks", True)):
                if not profile.monitoring.native_codes.get(frame.f_code):
                    # An adapter can bind a paused ancestor, not just the new
                    # frame. The monitor arms it before this event resumes.
                    profile.bound_codes[key] = frame.f_code
            return True
        return False

    def _bound_function(self, frame):
        reference = self.frame_functions.get(id(frame))
        function = reference() if reference is not None else None
        return function if function is not None and function.__code__ is frame.f_code else None

    def add_entry_observer(self, callback):
        if callback not in self.entry_observers:
            self.entry_observers.append(callback)

    def _seed_proven_entry(self, frame, caller_code, offset, site, function, edge, via=()):
        if not self.bind_function(frame, function) or not self._wrapper_candidates(frame):
            self.frame_functions.pop(id(frame), None)
            return False
        self.relays[id(frame)] = [{
            "site": dict(site), "code": caller_code, "offset": offset,
            "via": list(via),
            "pending": [{"edge": edge, "forwarded": False}] if edge else [],
        }]
        return True

    def confirm_deferred(self, frame, caller_code, offset, site, function, kind="scheduled"):
        """Accept a scheduler's exact creation and actual initial-entry proof."""
        with self.lock:
            if (not frame.f_code.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_GENERATOR)
                    or not self._initial_entry(frame) or _function_code(function) is not frame.f_code):
                return False
            target = self._target(frame.f_code)
            if site.get("ambiguous") or site.get("precision") != "instruction":
                return False
            if target is None:
                # A framework can wrap an application method outside the
                # selected roots. Its proven entry may start a relay, but only
                # actual forwarding to an in-root body can add a public edge.
                return self._seed_proven_entry(frame, caller_code, offset, site, function, None)
            edge = self._add_edge(caller_code, offset, frame.f_code, kind, site, target)
            self._seed_proven_entry(frame, caller_code, offset, site, function, edge)
            return True

    def confirm_callback(self, frame, caller_code, offset, site, function, *, dispatch=None):
        """A trusted stdlib registration and this exact callback both executed."""
        with self.lock:
            if not self._initial_entry(frame) or _function_code(function) is not frame.f_code:
                return False
            target = self._target(frame.f_code)
            if target is None or site.get("ambiguous") or site.get("precision") != "instruction":
                return False
            edge = self._add_edge(caller_code, offset, frame.f_code, "registered", site, target)
            if dispatch is not None:
                edge["callback_dispatch"] = dispatch
            self._seed_proven_entry(frame, caller_code, offset, site, function, edge)
            return True

    def confirm_factory(self, frame, caller_frame, callable_value, via=()):
        """Accept an adapter-confirmed callable only after its real body enters."""
        with self.lock:
            if _function_code(callable_value) is not frame.f_code:
                return False
            target = self._target(frame.f_code)
            site_info = self._site(caller_frame, frame.f_code)
            if target is None or site_info is None:
                return False
            site, _ = site_info
            if site.get("ambiguous") or site.get("precision") != "instruction":
                return False
            boundaries = []
            for boundary in via:
                observed = self._target(boundary.f_code, external=True)
                if observed is None or observed["path"].startswith(_SELF_ROOT):
                    return False
                boundaries.append(dict(observed, sha256=self.sources[observed["path"]].sha256))
            edge = self._add_edge(caller_frame.f_code, caller_frame.f_lasti,
                                  frame.f_code, "factory", site, target, boundaries)
            self._seed_proven_entry(frame, caller_frame.f_code, caller_frame.f_lasti,
                                    site, callable_value, edge, boundaries)
            return True

    def _remember_returned_callable(self, frame, value):
        """Retain a weak binding only for the very next profile event.

        The producer must be the exact AST callee expression of an immediate
        second call. Any intervening call/return/C event invalidates the proof.
        This never evaluates a factory again or serializes its returned object.
        """
        # These exact builtin values cannot expose a Python call target, even
        # through factory().method(). Arbitrary non-callables must still reach
        # the returned-method proof below. Identity avoids metaclass hooks.
        value_type = type(value)
        if (value is None or value_type is bool or value_type is int or value_type is float
                or value_type is complex or value_type is str or value_type is bytes
                or value_type is tuple or value_type is list or value_type is dict
                or value_type is set or value_type is frozenset):
            return
        caller = frame.f_back
        if caller is None or frame.f_code.co_flags & _SUSPENDED_CODE:
            return
        path = self.path(caller.f_code.co_filename)
        source = self.source(path) if path else None
        if source is None or not (source.returned_callables or source.returned_methods):
            return
        site, expression = source.callsite(caller.f_code, caller.f_lasti,
                                           caller.f_lineno, self._instructions(caller.f_code))
        if site.get("ambiguous") or site.get("precision") != "instruction":
            return
        calls = [node for node in source.returned_producers_by_function.get(expression, ())
                 if source.callee_range(node) == {
                     key: site[key] for key in ("line", "column", "end_line", "end_column")
                 }]
        if len(calls) != 1:
            return
        returned_method = source.returned_methods.get(calls[0])
        if returned_method is not None:
            value = _attribute_value(value, returned_method.attr)
            if value is _UNKNOWN:
                return
        function = _function(value)
        binding = function if function is not None else value
        if function is None and not issubclass(type(value), type):
            try:
                binding = _function(inspect.getattr_static(type(value), "__call__"))
            except AttributeError:
                return
            if binding is None:
                return
        try:
            reference = weakref.ref(binding)
        except TypeError:
            return
        self.returned_calls[threading.get_ident()] = {
            "caller_id": id(caller), "code": caller.f_code,
            "expression": returned_method if returned_method is not None else calls[0],
            "binding": reference,
        }

    def _remember_builtin_callable(self, frame, event, callee, returned):
        """Prove an exact builtin getattr result before outer arguments run.

        The profile API omits C return values. For ordinary stored attributes,
        its c_call event is the last point before builtin getattr reads them.
        Any Python descriptor, hook, or intervening argument call invalidates
        the proof, as does a failing getter.
        """
        if event == "c_return":
            if (returned and returned.get("pending_builtin") is callee
                    and returned["caller_id"] == id(frame)
                    and returned["code"] is frame.f_code
                    and returned["offset"] == frame.f_lasti):
                returned.pop("pending_builtin")
                self.returned_calls[threading.get_ident()] = returned
            return
        if event != "c_call" or callee is not _BUILTIN_GETATTR:
            return
        path = self.path(frame.f_code.co_filename)
        source = self.source(path) if path else None
        if source is None:
            return
        site, expression = source.callsite(frame.f_code, frame.f_lasti,
                                           frame.f_lineno, self._instructions(frame.f_code))
        if site.get("ambiguous") or site.get("precision") != "instruction":
            return
        calls = [call for call in source.calls_by_function.get(expression, ())
                 if call in source.returned_callables]
        if len(calls) != 1:
            return
        outer_expression = calls[0]
        if (_static_value(expression, frame) is not _BUILTIN_GETATTR
                or outer_expression.keywords or len(outer_expression.args) != 2):
            return
        value = _static_value(outer_expression, frame)
        binding = _function(value)
        if binding is None:
            if issubclass(type(value), type):
                binding = value
            else:
                binding = _function(inspect.getattr_static(type(value), "__call__", None))
                if binding is None:
                    return
        try:
            reference = weakref.ref(binding)
        except TypeError:
            return
        self.returned_calls[threading.get_ident()] = {
            "caller_id": id(frame), "code": frame.f_code,
            "offset": frame.f_lasti, "expression": outer_expression,
            "binding": reference, "pending_builtin": callee,
        }

    def _callee_value(self, expression, caller, returned):
        if (returned and not returned.get("pending_builtin") and returned["caller_id"] == id(caller)
                and returned["code"] is caller.f_code and returned["expression"] is expression):
            value = returned["binding"]()
            if value is not None:
                return value
        if (isinstance(expression, ast.Call)
                and _static_value(expression.func, caller) is _BUILTIN_GETATTR):
            return _UNKNOWN
        # A resolver such as getattr/super is evaluated before call arguments.
        # If those arguments execute Python, a later lookup could inspect a
        # replacement binding with identical code but a different closure.
        if expression is not None:
            path = self.path(caller.f_code.co_filename)
            source = self.source(path) if path else None
            if source is not None and source.callee_has_effects(expression):
                return _UNKNOWN
        return _static_value(expression, caller) if expression is not None else _UNKNOWN

    @staticmethod
    def _has_bound_receiver(frame, function):
        """Verify a sole fixed parameter is an implicit receiver, without hooks."""
        code = frame.f_code
        if type(function) is not types.FunctionType or function.__code__ is not code:
            return False
        if code.co_argcount == 0:
            return True
        if code.co_argcount != 1:
            return False
        receiver = frame.f_locals.get(code.co_varnames[0], _UNKNOWN)
        class_receiver = issubclass(type(receiver), type)
        owner = receiver if class_receiver else type(receiver)
        for base in _TYPE_MRO.__get__(owner):
            # Snapshot only the native namespace values; do not evaluate any
            # descriptor, instance dictionary, property or user attribute hook.
            for candidate in tuple(_TYPE_NAMESPACE.__get__(base).values()):
                if class_receiver:
                    if type(candidate) is not classmethod:
                        continue
                    candidate = candidate.__func__
                elif type(candidate) is not types.FunctionType:
                    continue
                for _ in range(8):
                    if candidate is function:
                        return True
                    if type(candidate) is not types.FunctionType:
                        break
                    candidate = vars(candidate).get("__wrapped__")
        return False

    def _remember_forwarding_entry(self, frame, caller, site_info, actual):
        """Keep an activation-local receipt for an already resolved dispatch.

        A transparent Ray-bound caller can forward through a generated
        getattr/registry expression without closing over the function. Its
        whole body must return the call with all variadics unchanged and must
        not select the callee from those arguments. A worker API consuming a
        method/selector parameter remains the destination. The normal entry
        resolver proves the exact function without reevaluating it or keeping
        its receiver or arguments alive.
        """
        if (self.entry_observer is None or caller is None or site_info is None
                or not caller.f_code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS)
                or self._bound_function(caller) is None):
            return
        function = _function(actual)
        site, expression = site_info
        if (function is None or function.__code__ is not frame.f_code
                or site.get("ambiguous") or site.get("precision") != "instruction"
                or caller.f_lasti in self.native_calls.get((threading.get_ident(), id(caller)), ())):
            return
        source = self.sources.get(site["path"])
        calls = source.calls_by_function.get(expression, ()) if source is not None else ()
        if (len(calls) != 1 or not source.transparent_forwarding(caller.f_code, calls[0])
                or not self._has_bound_receiver(caller, self._bound_function(caller))):
            return
        key = id(frame)

        def discard(reference):
            current = self.call_bindings.get(key)
            if current is not None and current[3] is reference:
                self.call_bindings.pop(key, None)

        self.call_bindings[key] = (id(caller), caller.f_code, caller.f_lasti, weakref.ref(function, discard))

    def wrapper_link(self, caller, child):
        """Prove one active wrapper forwarding step to an actually entered body.

        __wrapped__ only identifies a possible binding. The child frame must
        actually enter that code directly. Without __wrapped__, a closed-over
        Python callable or an exact active-entry binding must additionally
        receive the wrapper's *args/**kwargs.
        """
        child_code = child.f_code
        function = self._bound_function(caller)
        declared = _function(vars(function).get("__wrapped__")) if function is not None else None
        if declared is not None:
            if declared.__code__ is child_code:
                self.bind_function(child, declared)
                return True
            return False
        code = caller.f_code
        if not code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS):
            return False
        binding = self.call_bindings.get(id(child))
        if (binding is not None and binding[0] == id(caller)
                and binding[1] is code and binding[2] == caller.f_lasti):
            function = binding[3]()
            if function is not None and function.__code__ is child_code:
                return self.bind_function(child, function)
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
        calls = source.calls_by_function.get(expression, ())
        if len(calls) != 1 or site.get("ambiguous"):
            # Immediate awaits are positioned at SEND rather than CALL.
            resolved = source.awaitsite(
                code, caller.f_lasti, caller.f_lineno, self._instructions(code), caller, child_code
            )
            if resolved is None:
                return False
            expression = resolved[1]
            calls = source.calls_by_function.get(expression, ())
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
            self.bind_function(child, bindings[expression.id])
        return forwarded

    def _add_edge(self, caller_code, offset, code, kind, site, target, via=None):
        via_key = tuple((item["path"], item["line"], item["qualname"]) for item in (via or []))
        key = (id(caller_code), site.get("offset", offset), id(code), kind, via_key)
        edge = self.edges.get(key)
        if edge is None:
            # Retain the code objects for as long as their identity keys live.
            self.edge_codes[id(caller_code)] = caller_code
            self.edge_codes[id(code)] = code
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

    def _observe_relays(self, frame, caller, site_info, edge, actual=_UNSET):
        """Project only observed contiguous wrapper chains, capped at 8 hops."""
        self._remember_forwarding_entry(frame, caller, site_info, actual)
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
            if actual is _UNSET:
                actual = _static_value(expression, caller) if expression is not None else _UNKNOWN
            function = _function(actual)
            if function is not None and function.__code__ is frame.f_code:
                self.bind_function(frame, function)
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
        if (id(frame) not in self.frame_functions and id(frame) not in self.relays
                and id(frame) not in self.call_bindings):
            return
        # A coroutine profile "return" may mean suspension. Preserve its small
        # binding record until final return/close, without retaining the frame.
        if self._suspended(frame):
            return
        self.frame_functions.pop(id(frame), None)
        self.relays.pop(id(frame), None)
        self.call_bindings.pop(id(frame), None)

    def profile(self, frame, event, arg):
        returned = self.returned_calls.pop(threading.get_ident(), None) if self.returned_calls else None
        code = frame.f_code
        # Every event reuses the filename decision, including excluded paths.
        # Keep normalization and root checks in path() on the first encounter.
        try:
            path = self.paths[code.co_filename]
        except KeyError:
            path = self.path(code.co_filename)
        if path is None:
            try:
                scheduled = self.scheduled
                if scheduled.status == "recording" and sys.gettrace() is not scheduled.expected_trace:
                    scheduled._conflict()
                deferred = scheduled.deferred
                frame_id = id(frame)
                if event in ("c_call", "c_return", "c_exception"):
                    # Only this frame's live construction proof can be
                    # invalidated by a C event. External frames have no source
                    # call site or native-call stack to record.
                    if deferred.active and (threading.get_ident(), frame_id) in deferred.active:
                        scheduled.observe(frame, event, arg)
                    self.callbacks.observe(frame, event, arg)
                    return
                caller = frame.f_back
                caller_id = id(caller) if caller is not None else None
                if caller is None:
                    caller_path = None
                else:
                    try:
                        caller_path = self.paths[caller.f_code.co_filename]
                    except KeyError:
                        caller_path = self.path(caller.f_code.co_filename)
                if event == "return":
                    # Returning into a source frame can prove factory().method().
                    # Wrapper cleanup and trusted callback receipts remain live
                    # even when the returning function is outside all roots.
                    self.callbacks.observe(frame, event, arg)
                    pending = (deferred.active and
                               (threading.get_ident(), frame_id) in deferred.active)
                    pending = pending or frame_id in deferred.wrappers
                    release = (frame_id in self.frame_functions or frame_id in self.relays
                               or frame_id in self.call_bindings)
                    if pending or caller_path or release:
                        with self.lock:
                            if pending:
                                scheduled.observe(frame, event, arg)
                            if caller_path:
                                self._remember_returned_callable(frame, arg)
                            if release:
                                self._released(frame)
                    return
                if event == "call" and not caller_path:
                    key = threading.get_ident(), frame_id
                    parent_key = key[0], caller_id
                    tracked = (key in deferred.active or parent_key in deferred.active
                               or key in scheduled.active or parent_key in scheduled.active
                               or frame_id in deferred.pending or frame_id in scheduled.pending
                               or frame_id in deferred.wrappers
                               or frame_id in self.frame_functions or frame_id in self.relays
                               or frame_id in self.call_bindings
                               or caller_id in self.relays)
                    if not tracked:
                        self.callbacks.observe(frame, event, arg)
                        if os.getpid() != self.pid:
                            self.after_fork()
                        # Adapters must still see every real external entry:
                        # a Ray/Hydra boundary can start an in-root wrapper chain.
                        if self.entry_observer is not None or self.entry_observers:
                            with self.lock:
                                if self.entry_observer is not None:
                                    self.entry_observer(frame)
                                for observer in self.entry_observers:
                                    observer(frame)
                                if frame_id in self.frame_functions or frame_id in self.relays:
                                    self._observe_relays(frame, caller, None, None, _UNKNOWN)
                        return
            except Exception:
                self.errors += 1
                return
        if event in ("c_call", "c_return", "c_exception"):
            try:
                self.scheduled.observe(frame, event, arg)
            except Exception:
                self.errors += 1
            try:
                self._remember_builtin_callable(frame, event, arg, returned)
            except Exception:
                self.errors += 1
            try:
                if self.callbacks.observe(frame, event, arg) is True:
                    profile = self.profile_monitoring
                    if profile is not None and profile.active:
                        try:
                            # Also cover direct legacy Collector.profile
                            # delivery in a thread using the shared registry.
                            profile._global_native_lease()
                        except Exception:
                            profile._fail("native registration lease failed")
                            return
            except Exception:
                self.errors += 1
            if path:
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
                    self.scheduled.observe(frame, event, arg)
                    self.callbacks.observe(frame, event, arg)
                    self._remember_returned_callable(frame, arg)
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
                # Close/throw can report a return at the old suspension opcode.
                # A new entry must never inherit state from a reused frame ID.
                # Do this before adapters confirm and seed the current entry.
                if self._initial_entry(frame):
                    self.frame_functions.pop(id(frame), None)
                    self.relays.pop(id(frame), None)
                    self.call_bindings.pop(id(frame), None)
                self.scheduled.observe(frame, event, arg)
                self.callbacks.observe(frame, event, arg)
                for observer in self.entry_observers:
                    observer(frame)
                if path is not None:
                    caller = frame.f_back
                    if caller is None:
                        caller_path = None
                    else:
                        try:
                            caller_path = self.paths[caller.f_code.co_filename]
                        except KeyError:
                            caller_path = self.path(caller.f_code.co_filename)
                immediate_await = False
                if code.co_flags & _SUSPENDED_CODE:
                    immediate_await = self._initial_coroutine(frame)
                    if not immediate_await:
                        if self.entry_observer is not None:
                            self.entry_observer(frame)
                        if path and caller_path:
                            self.excluded_resume_events += 1
                        return
                site_info = self._site(caller, code, immediate_await) if caller_path else None
                edge = None
                actual = self._callee_value(site_info[1], caller, returned) if site_info else _UNKNOWN
                if path and caller_path:
                    target = self._target(code)
                    if target is None or site_info is None:
                        if self.entry_observer is not None:
                            self.entry_observer(frame)
                        if immediate_await:
                            self.excluded_resume_events += 1
                        return
                    site, expression = site_info
                    native_bridge = caller.f_lasti in self.native_calls.get((threading.get_ident(), id(caller)), ())
                    relation = _native_callee(expression, caller, code, value=actual) if expression is not None else None
                    callback = native_bridge or relation is True
                    kind = "await" if immediate_await else "callback" if callback else "python"
                    observed = dict(site)
                    if callback:
                        observed.update(ambiguous=True, reason="native_callback")
                    elif relation is None:
                        observed.update(ambiguous=True, reason="unresolved_callee")
                    edge = self._add_edge(caller.f_code, caller.f_lasti, code, kind, observed, target)
                self._observe_relays(frame, caller, site_info, edge, actual)
                # Ray follows the current, actually entered callee proof.
                # Ordinary factory observers above keep their earlier phase.
                if self.entry_observer is not None:
                    self.entry_observer(frame)
                if self.events_since_flush and (
                    self.events_since_flush >= 250 or time.monotonic() - self.last_flush >= 2
                ):
                    self.flush()
        except Exception:
            # A navigation recorder must never break application code.
            self.errors += 1

    def after_fork(self):
        self.lock = threading.RLock()
        self.callbacks.task_lock = threading.RLock()
        self.pid = os.getpid()
        self.started_at = time.time()
        self.edges = {}
        self.edge_codes.clear()
        self.native_calls = {}
        self.frame_functions = {}
        self.relays = {}
        self.call_bindings = {}
        self.returned_calls = {}
        self.errors = 0
        if self.profile_monitoring is not None and self.profile_monitoring.started:
            self.profile_monitoring.after_fork()
        self.scheduled.after_fork()
        self.callbacks.after_fork()
        self.events_since_flush = 0
        self.excluded_resume_events = 0
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
                    "scheduled_coroutines_status": self.scheduled.status,
                    "profile_backend": (getattr(self.profile_monitoring, "backend_name", "monitoring")
                                        if self.profile_monitoring is not None
                                        and self.profile_monitoring.started else "legacy"),
                    "profile_backend_failure": (self.profile_monitoring.failure
                                                if self.profile_monitoring is not None else None),
                    "direct_proof_monitoring": self.scheduled.direct_monitoring,
                    "native_leases_acquired": (self.profile_monitoring.leases_acquired
                                               if self.profile_monitoring is not None else 0),
                    "native_leases_released": (self.profile_monitoring.leases_released
                                               if self.profile_monitoring is not None else 0),
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
            if self.profile_monitoring is not None and self.profile_monitoring.started:
                self.profile_monitoring.stop()
            else:
                sys.setprofile(self.previous_profile)
                threading.setprofile(self.previous_thread_profile)
            self.scheduled.stop()
            self.callbacks.stop()
            self.status = "complete"
        self.frame_functions.clear()
        self.relays.clear()
        self.call_bindings.clear()
        self.returned_calls.clear()
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
    instance.scheduled.install()
    if (os.environ.get("CALLNAV_PROFILE_BACKEND", "auto") in ("auto", "local")
            and sys.version_info[:2] in ((3, 12), (3, 13))):
        from .profile_monitoring import LocalProfileMonitoring

        instance.profile_monitoring = LocalProfileMonitoring(instance)
        if instance.profile_monitoring.install():
            return instance
    if (os.environ.get("CALLNAV_PROFILE_BACKEND", "auto") in ("auto", "hybrid")
            and sys.version_info[:2] in ((3, 12), (3, 13))):
        from .hybrid_profile import HybridProfile

        instance.profile_monitoring = HybridProfile(instance)
        if instance.profile_monitoring.install():
            return instance
    if os.environ.get("CALLNAV_PROFILE_BACKEND") == "monitoring":
        from .profile_monitoring import ProfileMonitoring

        instance.profile_monitoring = ProfileMonitoring(instance)
        if instance.profile_monitoring.install():
            return instance
    threading.setprofile(instance.profile)
    sys.setprofile(instance.profile)
    return instance
