"""Prove immediate native coroutine creation before asyncio schedules it.

Python's profile hook cannot observe coroutine construction. Opcode tracing is
limited to source frames containing a possible immediate ``create_task(fn())``.
Only a Name load proves the callable placed on the interpreter's value stack;
resolving that name later, in create_task, could attribute a factory's result to
an unrelated binding installed by the factory itself.
"""

import ast
import bisect
import inspect
import os
import sys
import threading
import types
import weakref

from .cache import CodeIdentityMap
from .deferred import DeferredCalls
from .monitoring import InstructionMonitoring


_UNKNOWN_PATH = object()


def _span(node):
    return node.lineno, node.col_offset, node.end_lineno, node.end_col_offset


def _position(operation):
    position = operation.positions
    return position.lineno, position.col_offset, position.end_lineno, position.end_col_offset


class ScheduledCalls:
    def __init__(self, collector):
        # Collector owns callable inspection; defer this import to avoid the
        # module import cycle when Collector imports ScheduledCalls.
        from .collector import _function, _static_value

        self.collector = collector
        self.function = _function
        self.static_value = _static_value
        self.status = "inactive"
        self.plans = CodeIdentityMap()
        self.opcode_lines = CodeIdentityMap()
        self.scheduler_names = {}
        self.active = {}
        self.pending = {}
        self.scheduler_code = None
        self.hook = self._trace
        self.expected_trace = self.hook
        self.direct_monitoring = False
        self.previous_trace = None
        self.previous_thread_trace = None
        self.trace_epoch = 0
        self.frame_traces = {}
        self.legacy_opcodes_ready = sys.version_info[:2] != (3, 12)
        self.deferred = DeferredCalls(self)
        self.monitoring = InstructionMonitoring(self)

    def install(self):
        self.previous_trace = sys.gettrace()
        self.previous_thread_trace = threading.gettrace()
        if self.previous_trace is not None or self.previous_thread_trace is not None:
            self._conflict()
            return
        self.status = "recording"
        self.monitoring.install()
        threading.settrace(self.hook)
        self._install_trace_hook(opcode_fallback=self.monitoring.tool_id is None)

    def _install_trace_hook(self, *, opcode_fallback):
        installer_frame = None
        if opcode_fallback and not self.legacy_opcodes_ready:
            # CPython 3.12 snapshots this interpreter-wide flag at settrace
            # installation; setting it later inside a call hook is too late.
            # Arm it without retaining or altering an application frame.
            installer_frame = sys._getframe()
            previous_opcodes = installer_frame.f_trace_opcodes
            installer_frame.f_trace_opcodes = True
        try:
            sys.settrace(self.hook)
            if opcode_fallback:
                self.legacy_opcodes_ready = True
        finally:
            if installer_frame is not None:
                installer_frame.f_trace_opcodes = previous_opcodes
                del installer_frame

    def activate_direct(self):
        """Use the owned monitor for proof events, before profile activation."""
        if (self.status != "recording" or sys.gettrace() is not self.hook
                or threading.gettrace() is not self.hook):
            return False
        try:
            threading.settrace(self.previous_thread_trace)
            sys.settrace(self.previous_trace)
        except Exception:
            self.direct_monitoring = True
            self.restore_legacy()
            return False
        self.direct_monitoring = True
        self.expected_trace = None
        self._clear_installation_proofs()
        return True

    def _clear_installation_proofs(self):
        # A user-supplied trace setter can itself execute included Python
        # frames during installation, before any profile entry was admitted.
        self.trace_epoch += 1
        self._release_traces()
        self.active.clear()
        self.pending.clear()
        self.deferred.clear()

    def restore_legacy(self):
        if not self.direct_monitoring:
            return
        self.direct_monitoring = False
        self.expected_trace = self.hook
        if sys.gettrace() is None:
            self._install_trace_hook(opcode_fallback=self.monitoring.tool_id is None)
        if threading.gettrace() is None:
            threading.settrace(self.hook)
        self._clear_installation_proofs()

    def monitor_event(self, frame, event, arg):
        """Deliver proof events without admitting application frames to tracing."""
        if self.status != "recording":
            return
        if sys.gettrace() is not self.expected_trace:
            self._conflict()
            return
        registered = self.frame_traces.get(id(frame))
        if registered is not None:
            trace = registered[0]()
            if trace is not None:
                if frame.f_trace is trace:
                    trace(frame, event, arg)
                else:
                    self._conflict()

    def _conflict(self):
        if self.status != "trace_conflict":
            print(
                "callnav: existing trace hook detected; scheduled coroutine capture disabled",
                file=sys.stderr,
            )
        self.status = "trace_conflict"
        self.trace_epoch += 1
        self.monitoring.stop()
        self._release_traces()
        self.active.clear()
        self.pending.clear()
        self.deferred.clear()

    def _scheduler(self):
        if self.scheduler_code is None:
            module = sys.modules.get("asyncio.tasks")
            candidate = vars(module).get("create_task") if module is not None else None
            filename = os.path.join(os.path.dirname(os.__file__), "asyncio", "tasks.py")
            if (type(candidate) is types.FunctionType
                    and candidate.__code__.co_name == "create_task"
                    and os.path.realpath(candidate.__code__.co_filename) == os.path.realpath(filename)):
                self.scheduler_code = candidate.__code__
        return self.scheduler_code

    def _names(self, source):
        if source.path not in self.scheduler_names:
            names = {"create_task"}
            for node in source.nodes_by_type.get(ast.ImportFrom, ()):
                if node.module in ("asyncio", "asyncio.tasks"):
                    names.update(alias.asname or alias.name for alias in node.names
                                 if alias.name == "create_task")
            assignments = source.nodes_by_type.get(ast.Assign, ())
            # Follow simple local/module aliases without evaluating expressions.
            for _ in range(len(assignments)):
                previous = len(names)
                for node in assignments:
                    if self._named_scheduler(node.value, names):
                        names.update(target.id for target in node.targets if isinstance(target, ast.Name))
                if len(names) == previous:
                    break
            self.scheduler_names[source.path] = names
        return self.scheduler_names[source.path]

    @staticmethod
    def _named_scheduler(expression, names):
        return ((isinstance(expression, ast.Name) and expression.id in names)
                or (isinstance(expression, ast.Attribute) and expression.attr == "create_task"))

    def _plans(self, code):
        if code in self.plans:
            return self.plans[code]
        self.plans[code] = ()
        path = self.collector.path(code.co_filename)
        source = self.collector.source(path) if path else None
        if source is None:
            return ()
        operations = self.collector._instructions(code)[1]
        calls = {_position(op): op for op in operations
                 if op.opname in ("CALL", "CALL_KW", "CALL_FUNCTION_EX")}
        loads = {_position(op): op for op in operations
                 if op.opname.startswith(("LOAD_GLOBAL", "LOAD_FAST", "LOAD_DEREF", "LOAD_NAME"))}
        plans = []
        for outer in (node for position in calls
                      for node in source.calls_by_span.get(position, ())):
            if not outer.args or not isinstance(outer.args[0], ast.Call):
                continue
            inner = outer.args[0]
            if not isinstance(inner.func, ast.Name):
                continue
            outer_op, inner_op, load = calls.get(_span(outer)), calls.get(_span(inner)), loads.get(_span(inner.func))
            if outer_op is None or inner_op is None or load is None:
                continue
            # A single expression must map to one real pre-call load. Reject
            # exotic compiler shapes rather than substituting a source guess.
            if not load.offset < inner_op.offset < outer_op.offset:
                continue
            site = dict(
                source.callee_range(inner), path=path,
                qualname=getattr(code, "co_qualname", code.co_name),
                offset=inner_op.offset, precision="instruction",
            )
            plans.append((load.offset, inner_op.offset, outer_op.offset, inner.func,
                          outer.func, site, self._named_scheduler(outer.func, self._names(source)),
                          load.opname))
        self.plans[code] = tuple(plans)
        return self.plans[code]

    def _select(self, frame):
        plans = self._plans(frame.f_code)
        if not plans:
            return ()
        scheduler = self._scheduler()
        safe_namespaces = (
            type(frame.f_globals) is dict and type(frame.f_builtins) is dict
            and (type(frame.f_locals) is dict or frame.f_code.co_flags & inspect.CO_OPTIMIZED)
        )
        return tuple(plan for plan in plans if plan[6] or (
            scheduler is not None and safe_namespaces
            and getattr(self.function(self.static_value(plan[4], frame)), "__code__", None) is scheduler
        ))

    def _trace(self, frame, event, arg):
        try:
            if self.status != "recording":
                return None
            if event in ("call", "prepare"):
                if frame.f_trace is not None and frame.f_trace is not self.hook:
                    registered = self.frame_traces.get(id(frame))
                    if registered is None or registered[0]() is not frame.f_trace:
                        self._conflict()
                        return None
                profile = getattr(self.collector, "profile_monitoring", None)
                if (event == "call" and profile is not None and profile.active
                        and profile.delegates_entry()):
                    # The monitoring profile callback prepares before observing
                    # this entry, independently of cross-tool dispatch order.
                    return frame.f_trace
                code = frame.f_code
                # Both planners reject excluded source unless a proven wrapper
                # has promoted this exact code. Check that shared boundary once.
                if (self.collector.path(code.co_filename) is None
                        and code not in self.deferred.wrapper_codes):
                    return None
                plans = self._select(frame)
                deferred = self.deferred.begin(frame)
                if not plans and not deferred:
                    return None
                key = (threading.get_ident(), id(frame))
                offsets, operations = self.collector._instructions(frame.f_code)
                index = bisect.bisect_right(offsets, frame.f_lasti) - 1
                operation = operations[index] if index >= 0 else None
                initial = operation is not None and operation.opname == "RESUME" and operation.arg == 0
                state = self.active.get(key)
                if plans and (state is None or state["code"] is not frame.f_code or initial):
                    self.active[key] = {
                        "code": frame.f_code, "plans": plans, "loads": {}, "created": {},
                        "opcode_offsets": frozenset(offset for plan in plans for offset in plan[:2]),
                        "exception_pending": False,
                        "yield_offsets": frozenset(op.offset for op in operations
                                                   if op.opname in ("YIELD_VALUE", "YIELD_FROM")),
                    }
                state = self.active.get(key)
                deferred_state = self.deferred.active.get(key)
                monitored = self.monitoring.watch(frame.f_code, operations, deferred_state)
                if self.direct_monitoring and not monitored:
                    monitored = self.monitoring.watch_global(frame.f_code, operations, deferred_state)
                    if not monitored:
                        self.collector.errors += 1
                        self.collector.stop()
                        return None
                if self.status != "recording":
                    self.active.pop(key, None)
                    self.deferred.active.pop(key, None)
                    return None
                if not monitored and not self.legacy_opcodes_ready:
                    # A code-specific monitoring failure can require the
                    # legacy backend after installation already succeeded.
                    if sys.gettrace() is not self.expected_trace:
                        self._conflict()
                        return None
                    self._install_trace_hook(opcode_fallback=True)
                line_filter = self._opcode_lines(code) if sys.version_info < (3, 12) else None
                # The full-tracing fallback on Python 3.12+ needs a first
                # line callback to apply opcode instrumentation immediately.
                frame.f_trace_lines = not self.direct_monitoring and not monitored and (
                    sys.version_info >= (3, 12) or line_filter is not None)
                frame.f_trace_opcodes = not self.direct_monitoring and not monitored
                return self._frame_trace(key, state, deferred_state, line_filter=line_filter)
            key = (threading.get_ident(), id(frame))
            state = self.active.get(key)
            if state is None:
                deferred_state = self.deferred.active.get(key)
                if deferred_state is None:
                    return None
                if event == "opcode":
                    offset = frame.f_lasti
                    if offset in deferred_state["yield_offsets"]:
                        deferred_state["exception_pending"] = False
                    if (offset in deferred_state["opcode_offsets"]
                            or deferred_state["binding"] is not None
                            or deferred_state["iteration"] is not None):
                        self.deferred.opcode(frame, deferred_state)
                elif event == "line":
                    frame.f_trace_lines = False
                    frame.f_trace_opcodes = True
                elif event == "exception":
                    offsets, operations = self.collector._instructions(frame.f_code)
                    index = bisect.bisect_right(offsets, frame.f_lasti) - 1
                    operation = operations[index] if index >= 0 else None
                    completed = (arg[0] is StopIteration and operation is not None
                                 and operation.opname in ("SEND", "YIELD_FROM", "END_SEND"))
                    if not completed:
                        deferred_state["creations"].clear()
                        deferred_state["loads"].clear()
                        deferred_state["exception_pending"] = True
                elif event == "return" and deferred_state.get("exception_pending"):
                    self.deferred.active.pop(key, None)
                return self.hook
            if event == "opcode":
                offset = frame.f_lasti
                if offset in state["yield_offsets"]:
                    state["exception_pending"] = False
                if offset in state["opcode_offsets"]:
                    self._opcode(frame, state)
                deferred_state = self.deferred.active.get(key)
                if deferred_state is not None and (
                        offset in deferred_state["opcode_offsets"]
                        or deferred_state["binding"] is not None
                        or deferred_state["iteration"] is not None):
                    self.deferred.opcode(frame, deferred_state)
            elif event == "line":
                frame.f_trace_lines = False
                frame.f_trace_opcodes = True
            elif event == "exception":
                offsets, operations = self.collector._instructions(frame.f_code)
                index = bisect.bisect_right(offsets, frame.f_lasti) - 1
                operation = operations[index] if index >= 0 else None
                completed_await = (arg[0] is StopIteration and operation is not None
                                   and operation.opname in ("SEND", "YIELD_FROM", "END_SEND"))
                if not completed_await:
                    state["exception_pending"] = True
                    state["loads"].clear()
                    state["created"].clear()
                    deferred_state = self.deferred.active.get(key)
                    if deferred_state:
                        deferred_state["creations"].clear()
                        deferred_state["loads"].clear()
            elif event == "return":
                # close()/throw() can report the old YIELD or RESUME offset
                # even though the frame terminated. Exception unwinding can
                # execute cleanup opcodes while restoring the old yield offset;
                # only an actually executed new YIELD proves suspension again.
                if state["exception_pending"] or not self.collector._suspended(frame):
                    self.active.pop(key, None)
                    if state["exception_pending"]:
                        self.deferred.active.pop(key, None)
            return self.hook
        except Exception:
            self.collector.errors += 1
            self.active.pop((threading.get_ident(), id(frame)), None)
            return None

    def prepare_profile_entry(self, frame):
        """Prepare once under the monitoring profile callback's ownership."""
        # A known excluded frame without a local hook cannot have source
        # plans. Still check promotion on every entry, and let _trace handle
        # unknown paths and every existing hook (including foreign hooks).
        if frame.f_trace is None:
            code = frame.f_code
            if (self.collector.paths.get(code.co_filename, _UNKNOWN_PATH) is None
                    and code not in self.deferred.wrapper_codes):
                return None
        return self._trace(frame, "prepare", None)

    def _opcode_lines(self, code):
        """Bound proof-bearing instructions by their actual VM source line.

        PEP 626 guarantees another line event on a new line or a backward
        jump, including jumps within the same line. Code without a complete
        line mapping keeps full opcode tracing.
        """
        if code not in self.opcode_lines:
            operations = self.collector._instructions(code)[1]
            if not any("BACKWARD" in operation.opname for operation in operations):
                # Straight-line short calls do not amortize an extra line
                # event. Use windows only where a loop can repeat the saving.
                self.opcode_lines[code] = None
                return None
            wanted = {offset for plan in self._plans(code) for offset in plan[:2]}
            wanted.update(self.deferred.opcode_offsets.get(code, ()))
            wanted.update(self.deferred.yield_offsets.get(code, ()))
            wanted = sorted(wanted)
            lines = {}
            index = 0
            for start, end, line in code.co_lines():
                while index < len(wanted) and wanted[index] < end:
                    offset = wanted[index]
                    if offset < start or line is None:
                        self.opcode_lines[code] = None
                        return None
                    lines[line] = offset
                    index += 1
            self.opcode_lines[code] = lines if index == len(wanted) else None
        return self.opcode_lines[code]

    def _frame_trace(self, key, state, deferred_state, *, line_filter=None):
        """Keep immutable activation ownership out of the per-opcode path.

        The hook captures no frame and its self reference is weak. Suspensions
        retain proof state in the existing registries; each resume selects its
        plans again and gets a fresh hook. Fork, stop and trace conflicts retire
        old hooks even when their frames have not emitted another call event.
        """
        epoch = self.trace_epoch
        opcode_end = None

        def release():
            nonlocal state, deferred_state
            state = deferred_state = None

        def trace(frame, event, arg):
            nonlocal state, deferred_state, opcode_end
            try:
                if self.status != "recording" or self.trace_epoch != epoch:
                    release()
                    return None
                if event == "line" and line_filter is not None:
                    opcode_end = line_filter.get(frame.f_lineno, -1)
                    # A pending STORE/iteration receipt must see the next
                    # instruction, even on a line without a static call plan.
                    frame.f_trace_opcodes = (
                        frame.f_lasti <= opcode_end or (deferred_state is not None and (
                            deferred_state["binding"] is not None
                            or deferred_state["iteration"] is not None))
                    )
                    return reference()
                if event == "opcode":
                    offset = frame.f_lasti
                    yield_state = state if state is not None else deferred_state
                    if yield_state is not None and offset in yield_state["yield_offsets"]:
                        yield_state["exception_pending"] = False
                    if state is not None and offset in state["opcode_offsets"]:
                        self._opcode(frame, state)
                    if deferred_state is not None and (
                            offset in deferred_state["opcode_offsets"]
                            or deferred_state["binding"] is not None
                            or deferred_state["iteration"] is not None):
                        self.deferred.opcode(frame, deferred_state)
                    if (opcode_end is not None and offset >= opcode_end
                            and not (deferred_state is not None and (
                                deferred_state["binding"] is not None
                                or deferred_state["iteration"] is not None))):
                        frame.f_trace_opcodes = False
                    return reference()
                result = self._trace(frame, event, arg)
                if event == "return":
                    # A completed frame can survive in a traceback. Its local
                    # hook must not retain the activation's callable snapshots.
                    release()
                    if key not in self.active and key not in self.deferred.active:
                        self.frame_traces.pop(frame_id, None)
                return reference() if result is self.hook else None
            except Exception:
                self.collector.errors += 1
                self.active.pop(key, None)
                release()
                return None

        frame_id = key[1]
        # A Python weakref callback can run after the real profile return and
        # invalidate a factory receipt before its immediate method call. Retire
        # completed entries above, with tracing suppressed, and prune dead
        # suspended-frame identities only inside this trace callback.
        reference = weakref.ref(trace)
        previous = self.frame_traces.get(frame_id)
        if previous is not None:
            previous[1]()
        # Frame identity survives thread migration and fork. Keep weak hook
        # ownership after retirement so a suspended frame can start fresh when
        # resumed, while releasing its captured snapshots immediately.
        self.frame_traces[frame_id] = reference, release
        if len(self.frame_traces) > len(self.active) + len(self.deferred.active) + 64:
            self._prune_traces()
        if self.status != "recording" or self.trace_epoch != epoch:
            release()
            return None
        return trace

    def _prune_traces(self):
        for frame_id, (reference, release) in tuple(self.frame_traces.items()):
            if reference() is None:
                release()
                self.frame_traces.pop(frame_id, None)

    def _release_traces(self):
        for _, release in tuple(self.frame_traces.values()):
            release()
        self._prune_traces()

    def _opcode(self, frame, state):
        offset = frame.f_lasti
        if offset not in state["opcode_offsets"]:
            return
        for plan in state["plans"]:
            load, call, outer = plan[:3]
            if offset == load:
                # This executes immediately before the actual interpreter LOAD.
                state["loads"][call] = self._loaded_function(frame, plan)
                state["created"].pop(outer, None)
            elif offset == call:
                function = state["loads"].pop(call, None)
                # Arguments may mutate the function's __code__ after its LOAD.
                # Capture the code immediately before CALL, never after it.
                if function is not None and function.__code__.co_flags & inspect.CO_COROUTINE:
                    state["created"][outer] = (function, function.__code__, plan)

    def _loaded_function(self, frame, plan):
        # Follow the actual opcode namespace. A LOAD_GLOBAL must ignore keys
        # inserted into locals(), and a customized exec mapping can run user
        # code on LOAD_NAME, so it cannot supply a proven pre-load value.
        name, operation = plan[3].id, plan[7]
        if type(frame.f_globals) is not dict or type(frame.f_builtins) is not dict:
            return None
        if operation.startswith("LOAD_GLOBAL"):
            value = frame.f_globals.get(name, frame.f_builtins.get(name))
        elif operation.startswith(("LOAD_FAST", "LOAD_DEREF")):
            value = frame.f_locals.get(name)
        elif type(frame.f_locals) is dict:
            value = frame.f_locals.get(name, frame.f_globals.get(name, frame.f_builtins.get(name)))
        else:
            value = None
        return self.function(value)

    def observe(self, frame, event, arg):
        if self.status != "recording":
            return
        # If an application installs its debugger after recording begins, keep
        # its hooks and invalidate any evidence gathered by our old tracer.
        if sys.gettrace() is not self.expected_trace:
            self._conflict()
            return
        if not (self.active or self.pending or self.deferred.active
                or self.deferred.pending or self.deferred.wrappers):
            # Trace call events have already started any relevant frame. With
            # no outstanding proof or active frame there is nothing to relay,
            # including for the many C events emitted by library imports.
            return
        self.deferred.observe(frame, event, arg)
        if event != "call":
            return
        pending = self.pending.pop(id(frame), None)
        if pending is not None:
            coroutine = pending[0]()
            function = pending[4]()
            if (coroutine is not None and coroutine.cr_frame is frame
                    and function is not None and coroutine.cr_code is frame.f_code
                    and self.collector._initial_coroutine(frame)):
                self.collector.confirm_deferred(frame, *pending[1:4], function)
        if frame.f_code is not self._scheduler():
            return
        caller = frame.f_back
        if caller is None:
            return
        state = self.active.get((threading.get_ident(), id(caller)))
        offsets = self.collector._instructions(caller.f_code)[0] if state else ()
        index = bisect.bisect_right(offsets, caller.f_lasti) - 1
        offset = offsets[index] if index >= 0 else -1
        creation = state["created"].pop(offset, None) if state else None
        if creation is None:
            return
        function, code, plan = creation
        coroutine = frame.f_locals.get("coro")
        if (type(coroutine) is not types.CoroutineType or coroutine.cr_frame is None
                or coroutine.cr_code is not code or coroutine.cr_running
                or inspect.getcoroutinestate(coroutine) != inspect.CORO_CREATED):
            return
        key = id(coroutine.cr_frame)
        reference = weakref.ref(coroutine, lambda ref: self._discard(key, ref))
        self.pending[key] = (reference, caller.f_code, plan[1], plan[5], weakref.ref(function))

    def _discard(self, key, reference):
        pending = self.pending.get(key)
        if pending is not None and pending[0] is reference:
            self.pending.pop(key, None)

    def after_fork(self):
        self.trace_epoch += 1
        self.monitoring.after_fork()
        self._release_traces()
        self.active.clear()
        self.pending.clear()
        self.deferred.clear()
        if self.status == "recording" and sys.gettrace() is not self.expected_trace:
            self._conflict()

    def stop(self):
        if self.status == "recording":
            self.status = "complete"
        self.trace_epoch += 1
        self.monitoring.stop()
        self._release_traces()
        self.active.clear()
        self.pending.clear()
        self.deferred.clear()
        if sys.gettrace() is self.hook:
            sys.settrace(self.previous_trace)
        if threading.gettrace() is self.hook:
            threading.settrace(self.previous_thread_trace)
