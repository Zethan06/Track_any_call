"""Deliver only structurally relevant instruction events on Python 3.12+.

Activation selection and lifetime remain owned by ScheduledCalls' trace hook.
Monitoring disables individual irrelevant offsets, never entire activations:
another thread or a later invocation can need the same code's proof sites.
"""

import dis
import sys
import threading

from .cache import CodeIdentityMap


_UNWATCHED = object()


def _proof_offsets(code, operations, plans, deferred_state):
    offsets = {offset for plan in plans for offset in plan[:2]}
    offsets.update(op.offset for op in operations
                   if op.opname in ("YIELD_VALUE", "YIELD_FROM"))
    transitions = set()
    if deferred_state is not None:
        offsets.update(deferred_state["opcode_offsets"])
        # STORE receipts inspect the very next executed instruction. Async
        # iteration receipts validate every VM protocol step and reject the
        # first instruction after it. None may wait for another call site.
        transitions.update(deferred_state["assignments"])
        transitions.update(store for store, _, _ in deferred_state["tuple_builds"].values())
        for call, protocol in deferred_state["iterations"].items():
            transitions.add(call)
            transitions.update(protocol)
            offsets.update(protocol)
    for index, operation in enumerate(operations):
        if operation.offset in transitions and index + 1 < len(operations):
            offsets.add(operations[index + 1].offset)
        # A receipt can cross a SEND or an exception before its next regular
        # instruction. Keep every branch/handler destination, even when it
        # has no source call plan, so unexpected control flow invalidates it.
        if operation.is_jump_target:
            offsets.add(operation.offset)
    offsets.update(entry.target for entry in dis.Bytecode(code).exception_entries)
    return frozenset(offsets)


class InstructionMonitoring:
    def __init__(self, scheduled):
        self.scheduled = scheduled
        self.api = getattr(sys, "monitoring", None)
        self.tool_id = None
        self.name = f"callnav.instructions.{id(self):x}"
        self.offsets = CodeIdentityMap()
        self.native_codes = CodeIdentityMap()
        self.python_codes = CodeIdentityMap()
        self.profile_client = None
        self.lock = threading.RLock()
        self.accepting = True

    def install(self):
        with self.lock:
            self.accepting = True
            self._install()

    def _install(self):
        if self.api is None:
            return
        # Leave the debugger, coverage, profiler and optimizer IDs alone.
        # An occupied user slot is an ordinary full-tracing fallback.
        for tool_id in (3, 4):
            try:
                self.api.use_tool_id(tool_id, self.name)
            except ValueError:
                continue
            except Exception:
                return
            self.tool_id = tool_id
            try:
                self.api.register_callback(tool_id, self.api.events.INSTRUCTION, self._instruction)
            except Exception:
                self.stop()
            return

    def _owns_tool(self):
        tool_id = self.tool_id
        if tool_id is None:
            return False
        try:
            owned = self.api.get_tool(tool_id) == self.name
        except Exception:
            owned = False
        if not owned:
            # Another tool may free and reuse our slot. Its callbacks/events
            # belong to it, even though the numeric ID is unchanged.
            with self.lock:
                if self.tool_id == tool_id:
                    self.tool_id = None
                    self.accepting = False
                    self.offsets.clear()
                    self.native_codes.clear()
                    self.python_codes.clear()
                    if self.profile_client is not None:
                        self.profile_client.active = False
                    self.scheduled.collector.errors += 1
        return owned

    def _local_events(self, code, *, instructions=True, native=True, python=True):
        events = 0
        if instructions and code in self.offsets:
            events |= self.api.events.INSTRUCTION
        if native and self.native_codes.get(code):
            events |= self.api.events.CALL
        if python and self.python_codes.get(code) and self.profile_client is not None:
            events |= getattr(self.profile_client, "local_python_events", self.profile_client.python_events)
        return events

    def watch_python(self, code):
        with self.lock:
            if not self._owns_tool():
                return False
            if self.python_codes.get(code):
                return True
            self.python_codes[code] = True
            try:
                self.api.set_local_events(self.tool_id, code, self._local_events(code))
                return True
            except Exception:
                self.python_codes[code] = False
                try:
                    self.api.set_local_events(self.tool_id, code, self._local_events(code, python=False))
                except Exception:
                    self.scheduled.collector.errors += 1
                return False

    def reset_python(self):
        with self.lock:
            if self._owns_tool():
                for code in tuple(self.python_codes):
                    try:
                        self.api.set_local_events(self.tool_id, code, self._local_events(code, python=False))
                    except Exception:
                        self.scheduled.collector.errors += 1
            self.python_codes.clear()

    def watch_native(self, code):
        with self.lock:
            if not self._owns_tool():
                return False
            if not self.native_codes.get(code):
                self.native_codes[code] = True
                try:
                    self.api.set_local_events(self.tool_id, code, self._local_events(code))
                except Exception:
                    # Retain the key for stop/fork cleanup if an API mutates
                    # its mask before raising. The desired CALL bit is false.
                    self.native_codes[code] = False
                    try:
                        global_instructions = (self.profile_client is not None
                                               and self.profile_client.instruction_lease)
                        self.api.set_local_events(
                            self.tool_id, code, self._local_events(
                                code, instructions=not global_instructions, native=False),
                        )
                    except Exception:
                        self.scheduled.collector.errors += 1
                    return False
            return True

    def reset_native(self):
        with self.lock:
            if self._owns_tool():
                for code in tuple(self.native_codes):
                    try:
                        global_instructions = (self.profile_client is not None
                                               and self.profile_client.instruction_lease)
                        self.api.set_local_events(
                            self.tool_id, code, self._local_events(
                                code, instructions=not global_instructions, native=False),
                        )
                    except Exception:
                        self.scheduled.collector.errors += 1
            self.native_codes.clear()

    def watch(self, code, operations, deferred_state):
        with self.lock:
            return self._watch(code, operations, deferred_state)

    def _watch(self, code, operations, deferred_state):
        if not self.accepting or self.tool_id is None:
            return False
        if not self._owns_tool():
            self.scheduled.stop()
            return False
        if code not in self.offsets:
            # Use every static scheduled plan, including plans not selected in
            # this activation. A later namespace may select a different one.
            self.offsets[code] = _proof_offsets(
                code, operations, self.scheduled._plans(code), deferred_state,
            )
            try:
                self.api.set_local_events(self.tool_id, code, self._local_events(code))
            except Exception:
                # Keep other live codes instrumented. They have already
                # disabled their trace opcode events and still need us.
                try:
                    self.api.set_local_events(
                        self.tool_id, code, self._local_events(code, instructions=False),
                    )
                except Exception:
                    pass
                self.offsets.pop(code, None)
                return False
        return True

    def _instruction(self, code, offset):
        scheduled = self.scheduled
        try:
            if scheduled.status != "recording":
                return
            if not self._owns_tool():
                scheduled.stop()
                return
            offsets = self.offsets.get(code, _UNWATCHED)
            if offsets is _UNWATCHED:
                return
            if offset not in offsets:
                return self.api.DISABLE
            if scheduled.direct_monitoring and sys.gettrace() is not scheduled.expected_trace:
                scheduled._conflict()
                return
            frame = sys._getframe(1)
            registered = scheduled.frame_traces.get(id(frame))
            if registered is not None:
                trace = registered[0]()
                if trace is not None and frame.f_trace is trace:
                    trace(frame, "opcode", None)
                    client = self.profile_client
                    sync = getattr(client, "sync_proof_lease", None)
                    if sync is not None:
                        key = threading.get_ident(), id(frame)
                        deferred = scheduled.deferred.active.get(key)
                        creation = scheduled.active.get(key)
                        if ((deferred is not None and (
                                client.proof_lease or deferred["creations"]))
                                or (creation is not None and (
                                    client.proof_lease or creation["created"]))):
                            sync()
                elif trace is not None and scheduled.direct_monitoring:
                    scheduled._conflict()
        except Exception:
            # Monitoring callbacks, including frame-access audit failures,
            # must never raise into the application being recorded.
            scheduled.collector.errors += 1
            try:
                scheduled.stop()
            except Exception:
                # stop retires proof state before restoring trace hooks,
                # whose audit hooks may independently refuse that change.
                scheduled.collector.errors += 1

    def watch_global(self, code, operations, deferred_state):
        """A local-mask failure keeps proof delivery on the owned global mask."""
        with self.lock:
            client = self.profile_client
            if not self.accepting or client is None or not client.active or not self._owns_tool():
                return False
            self.offsets[code] = _proof_offsets(
                code, operations, self.scheduled._plans(code), deferred_state,
            )
            try:
                client.instruction_lease = True
                self.api.set_events(self.tool_id, client.mask())
                return True
            except Exception:
                self.offsets.pop(code, None)
                return False

    def reset(self):
        with self.lock:
            self._reset()

    def _reset(self):
        if self._owns_tool():
            for code in tuple(self.offsets):
                try:
                    self.api.set_local_events(
                        self.tool_id, code, self._local_events(code, instructions=False),
                    )
                except Exception:
                    self.scheduled.collector.errors += 1
        self.offsets.clear()

    def after_fork(self):
        # A vanished parent thread may have held the configuration lock.
        self.lock = threading.RLock()
        self.reset()

    def stop(self):
        self.accepting = False
        with self.lock:
            self._reset()
            # A debugger conflict retires proof events, while the independent
            # profile client retains ordinary graph capture on this same ID.
            if self.profile_client is not None and self.profile_client.active:
                self.profile_client.instruction_lease = False
                if self._owns_tool():
                    try:
                        self.api.set_events(self.tool_id, self.profile_client.mask())
                    except Exception:
                        self.scheduled.collector.errors += 1
                return
            self.reset_native()
            if self._owns_tool():
                tool_id, self.tool_id = self.tool_id, None
                try:
                    self.api.register_callback(tool_id, self.api.events.INSTRUCTION, None)
                except Exception:
                    self.scheduled.collector.errors += 1
                try:
                    self.api.free_tool_id(tool_id)
                except Exception:
                    self.scheduled.collector.errors += 1
