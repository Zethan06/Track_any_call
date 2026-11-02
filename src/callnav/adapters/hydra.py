"""Observe Hydra root factories without importing Hydra or inspecting configs.

The resolved callable is captured at Hydra's invocation boundary. An edge needs
that callable's real Python entry directly from the original boundary; target
names in a config, deferred partials, and recursive argument factories are not
execution evidence. Private hooks are optional and fail closed on API changes.
"""

import ast
from contextvars import ContextVar
import functools
import importlib.abc
import importlib.machinery
import inspect
import os
import sys
import types

_collector = None
_finder = None
_origin = ContextVar("callnav_hydra_origin", default=None)
_pending = ContextVar("callnav_hydra_target", default=None)
_main_pending = ContextVar("callnav_hydra_main", default=None)
_INTERNAL = "hydra._internal.instantiate._instantiate2"
_MODULES = {_INTERNAL, "hydra.utils", "hydra.main", "hydra"}


def _main_entry(frame):
    context = _main_pending.get()
    if context is None or not context["active"]:
        return
    function, caller, parent = context["function"], context["caller"], frame.f_back
    if frame.f_code is not function.__code__ or parent is None:
        return
    # Identical closure code is insufficient: the actual forwarding expression
    # must load Hydra's captured task_function object at this exact CALL.
    if parent.f_locals.get("task_function") is not function:
        return
    source = _collector.source(os.path.realpath(parent.f_code.co_filename))
    if source is None:
        return
    site, expression = source.callsite(parent.f_code, parent.f_lasti, parent.f_lineno,
                                       _collector._instructions(parent.f_code))
    if (site.get("precision") != "instruction" or site.get("ambiguous")
            or not isinstance(expression, ast.Name) or expression.id != "task_function"):
        return
    via, saw_decorated, current = [], False, parent
    while current is not None and current is not caller:
        if current.f_code is context["adapter_code"]:
            current = current.f_back
            continue
        name = current.f_globals.get("__name__", "")
        module = sys.modules.get(name)
        filename = os.path.realpath(current.f_code.co_filename)
        if (not (name == "hydra" or name.startswith("hydra.")) or module is None
                or vars(module) is not current.f_globals
                or not filename.startswith(context["root"] + os.sep)):
            return
        if current.f_code is context["decorated_code"]:
            if current.f_locals.get("task_function") is not function:
                return
            saw_decorated = True
        via.append(current)
        current = current.f_back
    if saw_decorated and current is caller:
        _collector.confirm_factory(frame, caller, function, via=tuple(reversed(via)))


def _patch_main(module):
    original = getattr(module, "main", None)
    if type(original) is not types.FunctionType:
        return
    _collector.add_entry_observer(observe_entry)
    if getattr(original, "__callnav_hydra_main__", False):
        return
    root = os.path.dirname(os.path.realpath(original.__code__.co_filename))

    @functools.wraps(original)
    def main(*args, **kwargs):
        decorator = original(*args, **kwargs)
        if type(decorator) is not types.FunctionType:
            return decorator

        @functools.wraps(decorator)
        def decorate(task_function):
            decorated = decorator(task_function)
            if type(task_function) is not types.FunctionType or type(decorated) is not types.FunctionType:
                return decorated

            @functools.wraps(decorated)
            def run_main(*args, **kwargs):
                caller = sys._getframe(1)
                context = None
                if _collector.path(caller.f_code.co_filename):
                    context = {"active": True, "caller": caller, "function": task_function,
                        "decorated_code": decorated.__code__, "adapter_code": run_main.__code__, "root": root}
                token = _main_pending.set(context)
                try:
                    return decorated(*args, **kwargs)
                finally:
                    if context is not None:
                        context["active"] = False
                        context["caller"] = context["function"] = None
                    _main_pending.reset(token)

            # Preserve Hydra's own unwrap contract. The adapter wrapper does
            # not claim the external decorated_main directly entered the body.
            run_main.__wrapped__ = vars(decorated).get("__wrapped__", task_function)
            return run_main
        return decorate

    main.__callnav_hydra_main__ = True
    main.__callnav_original__ = original
    module.main = main


def _entry_function(value):
    """Find a possible direct entry without invoking application descriptors."""
    seen = set()
    while type(value) is functools.partial and id(value) not in seen:
        seen.add(id(value))
        value = value.func
    if type(value) is types.MethodType:
        return value.__func__
    if type(value) is types.FunctionType:
        return value
    if issubclass(type(value), type):
        # Inherited initializers are real entries too. A metaclass that skips
        # initialization must never yield an edge to this possible initializer.
        names = ("__init__", "__new__")
        owner = value
    else:
        names = ("__call__",)
        owner = type(value)
    for name in names:
        try:
            function = inspect.getattr_static(owner, name)
        except AttributeError:
            continue
        if type(function) in (staticmethod, classmethod):
            function = function.__func__
        if type(function) is types.FunctionType:
            return function
    return None


def observe_entry(frame):
    """Confirm a resolved root target only when its actual body enters."""
    _main_entry(frame)
    pending = _pending.get()
    if pending is None or not pending["active"] or pending["seen"]:
        return
    origin = pending["origin"]
    caller = origin["caller"]
    function = pending["function"]
    parent = frame.f_back
    if (caller is None or frame.f_code is not function.__code__ or parent is None
            or parent.f_code is not pending["boundary_code"]
            or parent.f_locals.get("_target_") is not pending["target"]):
        return
    # Keep only the actually active Hydra frames as forwarding fingerprints.
    # A copied async context becomes inert as soon as its factory returns.
    via = []
    current = parent
    while current is not None and current is not caller:
        if current.f_globals.get("__name__") == _INTERNAL:
            via.append(current)
        elif current.f_code not in pending["adapter_codes"]:
            # A target or arbitrary helper that reenters Hydra's private hook
            # does not belong to the original source's root factory call.
            return
        current = current.f_back
    if current is not caller:
        return
    pending["seen"] = True
    _collector.confirm_factory(frame, caller, function, via=tuple(reversed(via)))


def _patch_internal(module):
    original = getattr(module, "instantiate", None)
    boundary = getattr(module, "_call_target", None)
    if not (type(original) is types.FunctionType and type(boundary) is types.FunctionType):
        return
    _collector.add_entry_observer(observe_entry)
    if getattr(original, "__callnav_hydra__", False):
        return
    try:
        parameters = inspect.signature(boundary)
    except (TypeError, ValueError):
        return
    if not {"_target_", "_partial_", "full_key"}.issubset(parameters.parameters):
        return

    @functools.wraps(original)
    def instantiate(*args, **kwargs):
        caller = sys._getframe(1)
        context = {"caller": caller if _collector.path(caller.f_code.co_filename) else None}
        token = _origin.set(context)
        try:
            return original(*args, **kwargs)
        finally:
            context["caller"] = None
            _origin.reset(token)

    @functools.wraps(boundary)
    def call_target(*args, **kwargs):
        context = _origin.get()
        pending = None
        if context is not None and context["caller"] is not None:
            try:
                bound = parameters.bind(*args, **kwargs).arguments
                # A recursive parameter target belongs to its parent factory,
                # not to the source's outer instantiate call. Lists/mappings
                # without a single root target remain conservatively uncovered.
                if bound.get("full_key") == "" and bound.get("_partial_") is False:
                    target = bound["_target_"]
                    function = _entry_function(target)
                    if function is not None:
                        pending = {
                            "origin": context, "target": target, "function": function,
                            "boundary_code": boundary.__code__, "active": True, "seen": False,
                            "adapter_codes": {instantiate.__code__, call_target.__code__},
                        }
            except Exception:
                # Adapter inspection must not change Hydra's own exceptions.
                pass
        token = _pending.set(pending)
        try:
            return boundary(*args, **kwargs)
        finally:
            if pending is not None:
                pending["active"] = False
                pending["origin"] = pending["target"] = pending["function"] = None
            _pending.reset(token)

    instantiate.__callnav_hydra__ = True
    instantiate.__callnav_original__ = original
    call_target.__callnav_hydra__ = True
    module.instantiate = instantiate
    module._call_target = call_target


def _patch(module):
    if module.__name__ == _INTERNAL:
        _patch_internal(module)
        return
    if module.__name__ == "hydra.main":
        _patch_main(module)
        return
    if module.__name__ == "hydra":
        main_module = sys.modules.get("hydra.main")
        main = getattr(main_module, "main", None)
        original = getattr(main, "__callnav_original__", None)
        if original is not None and getattr(module, "main", None) is original:
            module.main = main
        return
    internal = sys.modules.get(_INTERNAL)
    if internal is None:
        return
    instantiate = getattr(internal, "instantiate", None)
    original = getattr(instantiate, "__callnav_original__", None)
    if original is not None:
        for name in ("instantiate", "call"):
            if getattr(module, name, None) is original:
                setattr(module, name, instantiate)


class _Loader(importlib.abc.Loader):
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def create_module(self, spec):
        method = getattr(self.wrapped, "create_module", None)
        return method(spec) if method else None

    def exec_module(self, module):
        self.wrapped.exec_module(module)
        _patch(module)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _MODULES:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.loader and not isinstance(spec.loader, _Loader):
            spec.loader = _Loader(spec.loader)
        return spec


def install(collector):
    """Enable during recording; Hydra stays an optional application dependency."""
    global _collector, _finder
    _collector = collector
    # Optional frameworks must not add a per-entry observer to unrelated
    # imports. Their compatible boundaries register it before first use.
    if _finder is None:
        _finder = _Finder()
        sys.meta_path.insert(0, _finder)
    for name in (_INTERNAL, "hydra.utils", "hydra.main", "hydra"):
        module = sys.modules.get(name)
        if module is not None and not getattr(getattr(module, "__spec__", None), "_initializing", False):
            _patch(module)
