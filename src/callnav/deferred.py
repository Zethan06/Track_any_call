"""Track native suspended objects from proven creation to their first entry.

The opcode hook only snapshots callables. Objects come from actual Python
argument/return events, never from an evaluated source expression or a search
through application objects.
"""

import ast
import bisect
import inspect
import os
import threading
import types
import weakref

from .cache import CodeIdentityMap, CodeIdentitySet


def _span(node):
    return node.lineno, node.col_offset, node.end_lineno, node.end_col_offset


def _position(operation):
    point = operation.positions
    return point.lineno, point.col_offset, point.end_lineno, point.end_col_offset


class DeferredCalls:
    def __init__(self, scheduled):
        # Resolve the recorder module once after Collector finishes importing.
        # Its helper attributes stay live; application bindings are read anew.
        from . import collector

        self._helpers = collector
        self.scheduled = scheduled
        self.collector = scheduled.collector
        self.plans = CodeIdentityMap()
        self.active = {}
        self.pending = {}
        self.wrappers = {}
        self.wrapper_codes = CodeIdentitySet()
        self.namespaces = CodeIdentityMap()
        self.yield_offsets = CodeIdentityMap()
        self.load_offsets = CodeIdentityMap()
        self.opcode_offsets = CodeIdentityMap()
        self.prefix_offsets = CodeIdentityMap()
        self.tuple_loops = CodeIdentityMap()
        self.list_comps = CodeIdentityMap()
        self.fast_names = CodeIdentityMap()
        self.iterations = CodeIdentityMap()
        self.assignments = CodeIdentityMap()
        self.return_spans = {}
        self.source_metadata = {}

    def _plans(self, code):
        if code in self.plans:
            return self.plans[code]
        # Cache unsupported/excluded code too. A newly recognized wrapper
        # invalidates this entry before its body is entered.
        self.plans[code] = ()
        path = self.collector.path(code.co_filename)
        if path is None and code in self.wrapper_codes:
            path = os.path.realpath(code.co_filename)
        if path is None:
            return ()
        source = self.collector.source(path)
        if source is None:
            return ()
        operations = self.collector._instructions(code)[1]
        operation_indices = {op.offset: index for index, op in enumerate(operations)}
        metadata = self._metadata(source)
        calls = {_position(op): op for op in operations if op.opname in ("CALL", "CALL_KW", "CALL_FUNCTION_EX")}
        loads = {_position(op): op for op in operations if op.opname.startswith("LOAD_")}
        self.namespaces[code] = loads
        self.yield_offsets[code] = frozenset(op.offset for op in operations
                                             if op.opname in ("YIELD_VALUE", "YIELD_FROM"))
        plans = {}
        for node in (node for position in calls
                     for node in source.calls_by_span.get(position, ())):
            if not isinstance(node.func, (ast.Name, ast.Attribute)):
                continue
            call, load = calls.get(_span(node)), loads.get(_span(node.func))
            if call is None or load is None or load.offset >= call.offset:
                continue
            site = dict(source.callee_range(node), path=path,
                        qualname=getattr(code, "co_qualname", code.co_name),
                        offset=call.offset, precision="instruction")
            plans[call.offset] = (load, call, node, site)
        loops = {}
        tuple_positions = dict.fromkeys(_position(op) for op in operations if op.opname == "BUILD_TUPLE")
        for node in (node for position in tuple_positions
                     for node in metadata[ast.For].get(position, ())):
            if (not isinstance(node.target, ast.Name)
                    or not isinstance(node.iter, ast.Tuple)
                    or any(isinstance(item, ast.Starred) for item in node.iter.elts)):
                continue
            builds = [op for op in operations if op.opname == "BUILD_TUPLE"
                      and _position(op) == _span(node.iter) and op.arg == len(node.iter.elts)]
            stores = [op for op in operations if op.opname == "STORE_FAST"
                      and _position(op) == _span(node.target) and op.argval == node.target.id]
            iterations = [op for op in operations if op.opname == "FOR_ITER"
                          and _position(op) in (_span(node), _span(node.iter))]
            if (len(builds) == len(stores) == len(iterations) == 1
                    and builds[0].offset < iterations[0].offset < stores[0].offset):
                loops[builds[0].offset] = (stores[0].offset, node.target.id, node.iter.elts)
        self.tuple_loops[code] = loops
        builds, appends = {}, {}
        fast_names = {}
        list_positions = dict.fromkeys(_position(op) for op in operations if op.opname == "BUILD_LIST")
        for node in (node for position in list_positions
                     for node in metadata[ast.ListComp].get(position, ())):
            if (len(node.generators) != 1
                    or node.generators[0].ifs or node.generators[0].is_async
                    or not isinstance(node.generators[0].target, ast.Name)
                    or not isinstance(node.elt, ast.Call)):
                continue
            created = [offset for offset, plan in plans.items() if plan[2] is node.elt]
            build = [op for op in operations if op.opname == "BUILD_LIST" and op.arg == 0
                     and _position(op) == _span(node)]
            append = [op for op in operations if op.opname == "LIST_APPEND"
                      and _position(op) in (_span(node), _span(node.elt))]
            if len(created) != 1 or len(build) != 1 or len(append) != 1:
                continue
            comprehension = (_span(node), node, created[0])
            builds[build[0].offset] = comprehension
            appends[append[0].offset] = comprehension
            # Python 3.13 may fuse the comprehension target's STORE_FAST and
            # the receiver LOAD_FAST. Its source span describes the target,
            # rather than the receiver Name inside the element expression.
            function = node.elt.func
            if (isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
                    and function.value.id == node.generators[0].target.id):
                load = plans[created[0]][0]
                index = operation_indices[load.offset]
                previous = operations[index - 1] if index else None
                name = function.value.id
                if (previous is not None and previous.opname == "STORE_FAST_LOAD_FAST"
                        and previous.argval == (name, name)):
                    fast_names[_span(function.value)] = name
        self.list_comps[code] = builds, appends
        self.fast_names[code] = fast_names
        iterations, assignments = {}, {}
        for node in (node for position in calls
                     for node in metadata[ast.AsyncFor].get(position, ())):
            if isinstance(node.iter, ast.Call):
                candidates = [offset for offset, plan in plans.items() if plan[2] is node.iter]
                for offset in candidates:
                    index = operation_indices[offset]
                    protocol = operations[index + 1:index + 5]
                    if ([op.opname for op in protocol] == ["GET_AITER", "GET_ANEXT", "LOAD_CONST", "SEND"]
                            and protocol[2].argval is None
                            and _position(protocol[0]) in (_span(node), _span(node.iter))
                            and _position(protocol[1]) == _position(protocol[3]) == _span(node)):
                        iterations[offset] = tuple(op.offset for op in protocol)
        for node in (node for position in calls
                     for node in metadata[ast.Assign].get(position, ())):
            if (len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Call)):
                for offset, plan in plans.items():
                    if plan[2] is not node.value:
                        continue
                    index = operation_indices[offset]
                    store = operations[index + 1] if index + 1 < len(operations) else None
                    if (store is not None and store.opname == "STORE_FAST"
                            and store.argval == node.targets[0].id and _position(store) == _span(node.targets[0])):
                        assignments[store.offset] = offset, store.argval
        self.iterations[code] = iterations
        self.assignments[code] = assignments
        load_offsets = {plan[0].offset: (offset, plan) for offset, plan in plans.items()}
        self.load_offsets[code] = load_offsets
        self.prefix_offsets[code] = frozenset((
            *builds, *appends, *loops, *assignments,
            *(store for store, _, _ in loops.values()),
        ))
        self.opcode_offsets[code] = frozenset((*plans, *load_offsets, *self.prefix_offsets[code]))
        self.plans[code] = plans
        return plans

    def _metadata(self, source):
        if source.path in self.source_metadata:
            return self.source_metadata[source.path]
        metadata = {kind: {} for kind in (ast.For, ast.ListComp, ast.AsyncFor, ast.Assign)}
        for kind, attribute in ((ast.For, "iter"), (ast.ListComp, None),
                                (ast.AsyncFor, "iter"), (ast.Assign, "value")):
            for node in source.nodes_by_type.get(kind, ()):
                expression = getattr(node, attribute) if attribute else node
                metadata[kind].setdefault(_span(expression), []).append(node)
        returned = {}
        for node in source.nodes_by_type.get(ast.Return, ()):
            if isinstance(node.value, ast.Call):
                returned.setdefault(node.value, set()).add(_span(node))
        for node in source.nodes_by_type.get(ast.With, ()):
            if (len(node.items) == 1 and len(node.body) == 1
                    and isinstance(node.body[0], ast.Return)
                    and isinstance(node.body[0].value, ast.Call)):
                returned.setdefault(node.body[0].value, set()).update(
                    (_span(node), _span(node.items[0].context_expr)))
        self.return_spans[source.path] = returned
        self.source_metadata[source.path] = metadata
        return metadata

    def begin(self, frame):
        plans = self._plans(frame.f_code)
        if not plans:
            return False
        key = (threading.get_ident(), id(frame))
        if key not in self.active or self.collector._initial_entry(frame):
            self.active[key] = {"code": frame.f_code, "plans": plans,
                                "token": object(),
                                "yield_offsets": self.yield_offsets[frame.f_code],
                                "load_offsets": self.load_offsets[frame.f_code],
                                "opcode_offsets": self.opcode_offsets[frame.f_code],
                                "prefix_offsets": self.prefix_offsets[frame.f_code],
                                "list_comps": self.list_comps[frame.f_code],
                                "tuple_builds": self.tuple_loops[frame.f_code],
                                "assignments": self.assignments[frame.f_code],
                                "iterations": self.iterations[frame.f_code],
                                "namespaces": self.namespaces[frame.f_code],
                                "fast_names": self.fast_names[frame.f_code],
                                "loads": {}, "creations": {}, "calls": {},
                                "tuple_loops": {}, "binding": None, "comprehensions": {},
                                "iteration": None}
        return True

    def opcode(self, frame, state=None):
        if state is None:
            state = self.active.get((threading.get_ident(), id(frame)))
            if state is None:
                return
        offset = frame.f_lasti
        # A STORE receipt consumes the very next executed opcode, and an
        # iteration receipt must reject any unexpected opcode. Neither can be
        # skipped merely because this offset has no static call plan.
        if (offset not in state["opcode_offsets"]
                and state["binding"] is None and state["iteration"] is None):
            return
        # LOAD/CALL cannot also be a BUILD/APPEND/STORE instruction. Avoid
        # structural bookkeeping at those sites unless a live STORE or native
        # iteration receipt must consume the very next executed instruction.
        if (state["binding"] is not None or state["iteration"] is not None
                or offset in state["prefix_offsets"]):
            iteration = state["iteration"]
            if iteration is not None:
                steps, index, call = iteration
                if index < len(steps) and offset == steps[index]:
                    iteration[1] += 1
                else:
                    state["iteration"] = None
            builds, appends = state["list_comps"]
            comprehension = builds.get(offset)
            if comprehension is not None:
                state["comprehensions"][comprehension[0]] = []
            comprehension = appends.get(offset)
            if comprehension is not None:
                entries = state["comprehensions"].get(comprehension[0])
                if entries is not None:
                    entries.append(state["creations"].pop(comprehension[2], None))
            binding = state["binding"]
            if binding is not None:
                state["binding"] = None
                name, creation = binding
                self._remember(frame.f_locals.get(name), frame, creation, state["token"])
            loop = state["tuple_builds"].get(offset)
            if loop is not None:
                store, name, elements = loop
                creations = {id(creation[2][2]): creation for creation in state["creations"].values()}
                state["tuple_loops"][store] = [name, [creations.get(id(item)) for item in elements], 0]
            loop = state["tuple_loops"].get(offset)
            if loop is not None:
                name, elements, index = loop
                if index < len(elements) and elements[index] is not None:
                    state["binding"] = name, elements[index]
                loop[2] += 1
            assignment = state["assignments"].get(offset)
            if assignment is not None:
                creation = state["creations"].get(assignment[0])
                if creation is not None and creation[1].co_flags & (inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR):
                    state["binding"] = assignment[1], creation
        loaded = state["load_offsets"].get(offset)
        if loaded:
            call_offset, plan = loaded
            expression = plan[2].func
            value = self._loaded_expression(expression, frame, state)
            function = self.scheduled.function(value)
            receiver = (type(value) is types.FunctionType and (
                isinstance(expression, ast.Name) or (isinstance(expression, ast.Attribute)
                and type(self._loaded_expression(expression.value, frame, state)) is types.ModuleType)))
            state["loads"][call_offset] = function, receiver
        plan = state["plans"].get(offset)
        if plan:
            call_offset = offset
            function, receiver = state["loads"].pop(call_offset, (None, False))
            if function is None:
                state["calls"].pop(call_offset, None)
                state["creations"].pop(call_offset, None)
                return
            code = function.__code__
            state["calls"][call_offset] = (function, code, plan, receiver)
            state["creations"].pop(call_offset, None)
            if code.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_GENERATOR):
                state["creations"][call_offset] = (function, code, plan)
                protocol = state["iterations"].get(call_offset)
                if protocol is not None and code.co_flags & inspect.CO_ASYNC_GENERATOR:
                    state["iteration"] = [protocol, 0, state["calls"][call_offset]]
            elif (type(vars(function).get("__wrapped__")) is types.FunctionType
                  and code not in self.wrapper_codes):
                self.wrapper_codes.add(code)
                self.plans.pop(code, None)

    def _loaded_expression(self, expression, frame, state=None):
        _UNKNOWN = self._helpers._UNKNOWN

        if isinstance(expression, ast.Name):
            namespaces = state["namespaces"] if state is not None else self.namespaces[frame.f_code]
            operation = namespaces.get(_span(expression))
            if type(frame.f_globals) is not dict or type(frame.f_builtins) is not dict:
                return _UNKNOWN
            name = expression.id
            if operation is None:
                fast_names = state["fast_names"] if state is not None else self.fast_names[frame.f_code]
                if fast_names.get(_span(expression)) == name:
                    return frame.f_locals.get(name, _UNKNOWN)
                return _UNKNOWN
            if operation.opname.startswith("LOAD_GLOBAL"):
                return frame.f_globals.get(name, frame.f_builtins.get(name, _UNKNOWN))
            if operation.opname.startswith(("LOAD_FAST", "LOAD_DEREF")):
                return frame.f_locals.get(name, _UNKNOWN)
            if operation.opname == "LOAD_NAME" and type(frame.f_locals) is dict:
                return frame.f_locals.get(name, frame.f_globals.get(name, frame.f_builtins.get(name, _UNKNOWN)))
            return _UNKNOWN
        if isinstance(expression, ast.Attribute):
            base = self._loaded_expression(expression.value, frame, state)
            return self._helpers._attribute_value(base, expression.attr) if base is not _UNKNOWN else _UNKNOWN
        return _UNKNOWN

    def _call(self, caller, state):
        if state is None:
            return None
        offsets = self.collector._instructions(caller.f_code)[0]
        index = bisect.bisect_right(offsets, caller.f_lasti) - 1
        offset = offsets[index] if index >= 0 else -1
        return state["calls"].get(offset)

    def _iteration_call(self, frame, caller, state):
        """A native async-generator CALL flowed directly through VM iteration.

        The exact native object produced by CALL is preserved by GET_AITER and
        GET_ANEXT. With no intervening application opcode, an initial frame
        entry at this same activation's SEND proves the constructor's object.
        """
        iteration = state["iteration"] if state else None
        if iteration is None or not self.collector._initial_entry(frame):
            return None
        steps, index, call = iteration
        if (index != len(steps) or caller.f_lasti != steps[-1]
                or call[1] is not frame.f_code or call[0].__code__ is not frame.f_code
                or not frame.f_code.co_flags & inspect.CO_ASYNC_GENERATOR):
            return None
        state["iteration"] = None
        self.collector.confirm_deferred(frame, caller.f_code, call[2][1].offset,
                                        call[2][3], call[0], kind="deferred")
        return call

    @staticmethod
    def _object(value):
        if type(value) is types.CoroutineType:
            if value.cr_frame is None or value.cr_running:
                return None
            return value.cr_frame, value.cr_code
        if type(value) is types.AsyncGeneratorType:
            if value.ag_frame is None or value.ag_running:
                return None
            return value.ag_frame, value.ag_code
        if type(value) is types.GeneratorType:
            if value.gi_frame is None or value.gi_running:
                return None
            return value.gi_frame, value.gi_code
        return None

    def _remember(self, value, creator, creation, token):
        observed = self._object(value)
        if observed is None:
            return False
        frame, code = observed
        function, expected, plan = creation
        start = next((op.offset for op in self.collector._instructions(code)[1]
                      if op.opname == "RESUME" and op.arg == 0), -1)
        if type(value) is types.CoroutineType:
            created = inspect.getcoroutinestate(value) == inspect.CORO_CREATED
        elif type(value) is types.GeneratorType:
            created = inspect.getgeneratorstate(value) == inspect.GEN_CREATED
        else:
            created = frame.f_lasti < start
        if code is not expected or function.__code__ is not code or not created:
            return False
        key = id(frame)
        pending = self.pending.get(key)
        if pending is None or pending[0]() is not value:
            reference = weakref.ref(value, lambda ref: self._discard(key, ref))
            pending = (reference, [])
            self.pending[key] = pending
        origin = (creator.f_code, plan[1].offset, plan[3], weakref.ref(function), token)
        if not any(item[:2] == origin[:2] for item in pending[1]):
            pending[1].append(origin)
        return True

    def _discard(self, key, reference):
        pending = self.pending.get(key)
        if pending is not None and pending[0] is reference:
            self.pending.pop(key, None)

    def observe(self, frame, event, value):
        # STORE_FAST can decref a replaced value and execute its destructor
        # before the following opcode. Such a callback can replace the local
        # again (including through Python 3.13's writable f_locals proxy), so
        # the later local is no longer a receipt for our native construction.
        if event == "call" or event in ("c_call", "c_return", "c_exception"):
            caller = frame.f_back if event == "call" else frame
            state = self.active.get((threading.get_ident(), id(caller))) if caller else None
            call = self._call(caller, state) if event == "call" else None
            if state is not None:
                state["binding"] = None
                receipt = False
                if event == "call":
                    if call is not None and call[1] is frame.f_code and call[3]:
                        arguments = call[2][2].args
                        receipt = (len(arguments) == 1 and isinstance(arguments[0], ast.Starred)
                                   and isinstance(arguments[0].value, ast.ListComp)
                                   and frame.f_code.co_flags & inspect.CO_VARARGS
                                   and frame.f_code.co_argcount == 0)
                if not receipt:
                    # An iterator, destructor, argument callback or C call can
                    # expose and replace a partially built list's elements.
                    # Its earlier entries no longer prove construction even
                    # if the substituted objects happen to have equal codes.
                    for entries in state["comprehensions"].values():
                        entries[:] = [None] * len(entries)
                    for _, _, offset in state["list_comps"][1].values():
                        state["creations"].pop(offset, None)
        if event == "return":
            key = (threading.get_ident(), id(frame))
            if key not in self.active and id(frame) not in self.wrappers:
                return
            if self.collector._suspended(frame):
                return
            state = self.active.pop(key, None)
            wrapper = self.wrappers.pop(id(frame), None)
            if state and type(value) is list and frame.f_code.co_name == "<listcomp>":
                offsets, operations = self.collector._instructions(frame.f_code)
                index = bisect.bisect_right(offsets, frame.f_lasti) - 1
                operation = operations[index] if index >= 0 else None
                entries = state["comprehensions"].get(_position(operation)) if operation else None
                if (operation is not None and operation.opname == "RETURN_VALUE"
                        and entries is not None and len(entries) == len(value)):
                    for item, creation in zip(value, entries):
                        if creation is not None:
                            self._remember(item, frame, creation, state["token"])
            if state and self._object(value):
                # Only a literal return of this exact native creation proves
                # its object. A factory returning an earlier object is absent.
                offsets, operations = self.collector._instructions(frame.f_code)
                index = bisect.bisect_right(offsets, frame.f_lasti) - 1
                operation = operations[index] if index >= 0 else None
                for creation in state["creations"].values():
                    node = creation[2][2]
                    # CPython can assign a simple with block's return to
                    # its cleanup span; both literal forms were indexed once.
                    spans = self.return_spans[creation[2][3]["path"]].get(node, ())
                    if (operation is not None and operation.opname == "RETURN_VALUE"
                            and _position(operation) in spans):
                        self._remember(value, frame, creation, state["token"])
            if wrapper and self._object(value):
                pending = self.pending.get(id(self._object(value)[0]))
                if pending and pending[0]() is value:
                    function, caller_code, call_plan, token = wrapper
                    declared = vars(function).get("__wrapped__")
                    for origin in tuple(pending[1]):
                        if token is not None and origin[4] is token and declared is origin[3]():
                            pending[1].append((caller_code, call_plan[1].offset, call_plan[3], origin[3], None))
            return
        if event != "call":
            return
        pending = self.pending.pop(id(frame), None)
        if pending:
            obj = pending[0]()
            # At entry the object's running flag is true; inspect ownership
            # directly instead of allowing resumptions to consume this proof.
            owner = (obj.cr_frame if type(obj) is types.CoroutineType else
                     obj.ag_frame if type(obj) is types.AsyncGeneratorType else
                     obj.gi_frame if type(obj) is types.GeneratorType else None)
            if owner is frame and self.collector._initial_entry(frame):
                for code, offset, site, reference, _ in pending[1]:
                    function = reference()
                    if function is not None and self.collector.path(site["path"]):
                        primary = self.scheduled.pending.get(id(frame))
                        # The specialized scheduler will confirm this exact
                        # proof immediately after this observer returns. A
                        # duplicate seed would overwrite its wrapper relay and
                        # leave one unforwarded raw wrapper visible in lookup.
                        if (primary is not None and primary[0]() is obj
                                and primary[4]() is function and primary[1] is code
                                and primary[2] == offset and primary[3] == site
                                and self.collector._initial_coroutine(frame)):
                            continue
                        self.collector.confirm_deferred(frame, code, offset, site, function, kind="deferred")
        iteration_call = self._iteration_call(frame, caller, state)
        if iteration_call is not None:
            call = iteration_call
        if call is None or call[1] is not frame.f_code:
            return
        function, _, outer, receiver = call
        if type(vars(function).get("__wrapped__")) is types.FunctionType:
            current = self.active.get((threading.get_ident(), id(frame)))
            token = current["token"] if current else None
            self.wrappers[id(frame)] = function, caller.f_code, outer, token
        if not receiver:
            return
        # Match a positional/keyword argument to its real Python parameter.
        # Reject starred and variadic binding shapes rather than approximating.
        node = outer[2]
        if (len(node.args) == 1 and isinstance(node.args[0], ast.Starred)
                and isinstance(node.args[0].value, ast.ListComp)
                and frame.f_code.co_flags & inspect.CO_VARARGS and frame.f_code.co_argcount == 0):
            entries = state["comprehensions"].get(_span(node.args[0].value))
            name = frame.f_code.co_varnames[frame.f_code.co_kwonlyargcount]
            arguments = frame.f_locals.get(name)
            if entries is not None and type(arguments) is tuple and len(entries) == len(arguments):
                for item, creation in zip(arguments, entries):
                    if creation is not None:
                        self._remember(item, caller, creation, state["token"])
            return
        names = frame.f_code.co_varnames[:frame.f_code.co_argcount]
        if len(node.args) > len(names) or any(isinstance(item, ast.Starred) for item in node.args):
            return
        for offset, creation in tuple(state["creations"].items()):
            inner = creation[2][2]
            name = None
            for index, argument in enumerate(node.args):
                if argument is inner:
                    name = names[index] if index < len(names) else None
            for keyword in node.keywords:
                if keyword.value is inner and keyword.arg is not None:
                    name = keyword.arg
            if name is not None and name in frame.f_locals:
                self._remember(frame.f_locals[name], caller, creation, state["token"])
                state["creations"].pop(offset, None)

    def clear(self):
        self.active.clear()
        self.pending.clear()
        self.wrappers.clear()
