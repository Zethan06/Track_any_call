"""Monitoring-to-profile delivery adapter retained for backend diagnostics.

Keep every Python entry/exit. Native events stay local to source/proof code,
with a conservative global lease once a trusted Task registration is live.
Collector.profile remains the authoritative legacy-compatible event consumer.
"""

import os
import sys
import threading
import types
import weakref

from .callbacks import _TASK


_IGNORED = object()
_TRACEBACK = BaseException.__traceback__


def _legacy_callable(callable_value, arg0, missing):
    """Mirror CPython 3.12/3.13 sys_profile_call_or_return's exact filter."""
    # PyCFunction_Check uses the real type, never an object's __class__ hook.
    if issubclass(type(callable_value), types.BuiltinFunctionType):
        return callable_value
    if type(callable_value) is types.MethodDescriptorType:
        if arg0 is missing:
            return _IGNORED
        return types.MethodDescriptorType.__get__(callable_value, arg0, type(arg0))
    if type(callable_value) is types.MethodType:
        function = callable_value.__func__
        if issubclass(type(function), types.BuiltinFunctionType):
            return function
    return _IGNORED


class _DeliveryState(threading.local):
    """Initialize participation metadata independently for each thread."""

    def __init__(self):
        self.enabled = False
        self.last_monitor = None
        self.skip_once = None


class ProfileMonitoring:
    def __init__(self, collector):
        self.collector = collector
        self.monitoring = collector.scheduled.monitoring
        self.api = self.monitoring.api
        self.active = False
        self.started = False
        self.installing = False
        self.native_lease = False
        self.native_fallback = False
        self.task_epoch = 0
        self.bound_codes = {}
        self.leases_acquired = 0
        self.leases_released = 0
        self.instruction_lease = False
        self.direct_requested = os.environ.get("CALLNAV_DIRECT_PROOFS") == "1"
        self.local = _DeliveryState()
        self.bootstrap_hook = self._bootstrap
        self.failure = None
        self.python_events = 0
        self.callbacks = ()

    def install(self):
        if self.api is None or sys.version_info[:2] not in ((3, 12), (3, 13)):
            return False
        # Manual Collector.profile users keep the independent legacy path.
        if sys.getprofile() is not None or threading.getprofile() is not None:
            return False
        monitoring = self.monitoring
        with monitoring.lock:
            if monitoring.tool_id is None:
                monitoring.install()
            if not monitoring._owns_tool():
                return False
            events = self.api.events
            self.python_events = (events.PY_START | events.PY_RESUME | events.PY_THROW
                                  | events.PY_RETURN | events.PY_YIELD | events.PY_UNWIND)
            self.callbacks = (
                (events.PY_START, self._py_start),
                (events.PY_RESUME, self._py_start),
                (events.PY_THROW, self._py_throw),
                (events.PY_RETURN, self._py_return),
                (events.PY_YIELD, self._py_return),
                (events.PY_UNWIND, self._py_unwind),
                (events.CALL, self._c_call),
                (events.C_RETURN, self._c_return),
                (events.C_RAISE, self._c_raise),
            )
            if self.direct_requested:
                self.python_events |= events.RAISE | events.RERAISE | events.STOP_ITERATION
                self.callbacks += (
                    (events.RAISE, self._exception),
                    (events.RERAISE, self._exception),
                    (events.STOP_ITERATION, self._exception),
                )
            monitoring.profile_client = self
            self.installing = True
            try:
                for event, callback in self.callbacks:
                    self.api.register_callback(monitoring.tool_id, event, callback)
                self.local.enabled = True
                # start() may be called from a source frame already executing.
                # Its native events still need coverage without a new PY_START.
                self._seed_active_sources()
                if self.direct_requested:
                    self.collector.scheduled.activate_direct()
                self.api.set_events(monitoring.tool_id, self.mask())
                if self.native_lease:
                    self.leases_acquired += 1
                self.active = True
                self.started = True
                threading.setprofile(self.bootstrap_hook)
                if not self.active:
                    raise RuntimeError("profile monitoring failed during installation")
                self.installing = False
                return True
            except Exception:
                # No application body has resumed. Initial setup can retain
                # the established legacy backend, including its 3.12 prearm.
                try:
                    self.collector.scheduled.restore_legacy()
                    self.stop()
                except Exception:
                    self.collector.errors += 1
                finally:
                    self.started = False
                    self.installing = False
                return False

    def delegates_entry(self):
        """Legacy trace calls never prepare an entry owned by this adapter."""
        if not self.active:
            return False
        current = sys.getprofile()
        return current is None or current is self.bootstrap_hook

    def _seed_active_sources(self):
        frame = sys._getframe(1)
        try:
            while frame is not None:
                if self.collector.path(frame.f_code.co_filename):
                    if not self.monitoring.watch_native(frame.f_code):
                        self.native_lease = True
                        self.native_fallback = True
                frame = frame.f_back
        finally:
            del frame

    @staticmethod
    def _token(frame, event):
        return id(frame), id(frame.f_code), frame.f_lasti, event

    def _bootstrap(self, frame, event, arg):
        """Mark future threading.Thread workers, then remove the one-shot hook."""
        try:
            if sys.getprofile() is self.bootstrap_hook:
                sys.setprofile(None)
            if not self.active:
                return
            state = self.local
            state.enabled = True
            token = self._token(frame, event)
            # Either public or current private tool delivery order is allowed.
            # An earlier monitor notification was ignored while ineligible;
            # otherwise the later notification must skip this one event.
            state.skip_once = None if state.last_monitor == token else token
            self._deliver(frame, event, arg)
        except Exception:
            self._fail("future-thread bootstrap failed")

    def _receive(self, frame, code, event, arg):
        if not self.active:
            return
        if frame.f_code is not code:
            raise RuntimeError("monitoring callback did not expose its application frame")
        current = sys.getprofile()
        if current is not None and current is not self.bootstrap_hook:
            # An explicitly installed legacy Collector.profile or foreign
            # profiler owns this thread. Never deliver a duplicate event.
            return
        state = self.local
        if not state.enabled:
            # Only a future Thread with the one-shot bootstrap installed
            # needs to remember a notification that ran before its profile
            # callback. Old/raw threads with no hook never use this token.
            if current is self.bootstrap_hook:
                state.last_monitor = self._token(frame, event)
            return
        skip = state.skip_once
        if skip is not None:
            state.skip_once = None
            if skip == self._token(frame, event):
                return
        self._deliver(frame, event, arg)

    def _arm_native(self, frame):
        collector = self.collector
        scheduled = collector.scheduled
        code = frame.f_code
        if self.monitoring.native_codes.get(code):
            return
        try:
            path = collector.paths[code.co_filename]
        except KeyError:
            path = collector.path(code.co_filename)
        if (path or code in scheduled.deferred.wrapper_codes
                or id(frame) in collector.frame_functions or id(frame) in collector.relays):
            # Keep the local mask physically armed during a Task lease too.
            # Removing the global bit must not strand a source/proof C pair.
            if not self.monitoring.watch_native(code):
                self._global_native_lease(persistent=True)

    def _arm_bound_codes(self):
        collector = self.collector
        with self.monitoring.lock:
            while self.bound_codes:
                frame_id, code = self.bound_codes.popitem()
                if frame_id in collector.frame_functions or frame_id in collector.relays:
                    if not self.monitoring.watch_native(code):
                        self._global_native_lease(persistent=True)

    def _native_interest(self, frame, code):
        if code in self.monitoring.native_codes:
            return True
        # An adapter can bind an already executing ancestor. Its current
        # frame interest remains authoritative even before its next entry.
        collector = self.collector
        if collector.frame_functions or collector.relays:
            frame_id = id(frame)
            return frame_id in collector.frame_functions or frame_id in collector.relays
        return False

    def _global_native_lease(self, *, persistent=False):
        monitoring = self.monitoring
        with monitoring.lock:
            if persistent:
                self.native_fallback = True
            if self.native_lease:
                return
            if not monitoring._owns_tool():
                raise RuntimeError("profile monitoring tool ownership lost")
            self.native_lease = True
            self.api.set_events(monitoring.tool_id, self.mask())
            self.leases_acquired += 1

    def _sync_task_lease(self):
        callbacks = self.collector.callbacks
        monitoring = self.monitoring
        with callbacks.task_lock:
            required = bool(self.native_fallback or callbacks.task_origins or callbacks.active)
        # Unrelated Python exits must not wait behind an in-progress release.
        # New/live registrations do wait, before their native body resumes.
        if not monitoring.lock.acquire(blocking=required):
            return
        # Registry changes use task_lock only; configuration always acquires
        # monitoring.lock first. Never hold task_lock across monitoring APIs.
        try:
            if not monitoring._owns_tool():
                self.collector.stop()
                return
            with callbacks.task_lock:
                epoch = callbacks.task_epoch
                required = self.native_fallback or callbacks.task_origins or callbacks.active
            if required:
                self._global_native_lease()
            elif self.native_lease:
                self._arm_bound_codes()
                with callbacks.task_lock:
                    unchanged = (callbacks.task_epoch == epoch and not callbacks.task_origins
                                 and not callbacks.active)
                if unchanged and not self.native_fallback:
                    # All relevant native code has local CALL, including late
                    # ancestor bindings. Excluded native pairs only affect
                    # Task removal, and no registration origin remains here.
                    self.native_lease = False
                    try:
                        self.api.set_events(monitoring.tool_id, self.mask())
                    except Exception:
                        # set_events may mutate then raise. Keep the complete
                        # global fallback if restoration succeeds; otherwise
                        # the callback's failure handler retires the recorder.
                        self.native_lease = True
                        self.native_fallback = True
                        self.api.set_events(monitoring.tool_id, self.mask())
                    else:
                        self.leases_released += 1
            # A concurrent registration must not be acknowledged by this
            # earlier snapshot: its own callback reacquires CALL before the
            # native registration body resumes.
            self.task_epoch = epoch
        finally:
            monitoring.lock.release()

    def mask(self):
        return (self.python_events | (self.api.events.CALL if self.native_lease else 0)
                | (self.api.events.INSTRUCTION if self.instruction_lease else 0))

    def _deliver(self, frame, event, arg):
        if not self.active or self.collector.status != "recording":
            return
        if not self.monitoring._owns_tool():
            self.collector.stop()
            return
        collector = self.collector
        if os.getpid() != collector.pid:
            collector.after_fork()
        if event == "call":
            scheduled = collector.scheduled
            if scheduled.status == "recording":
                if sys.gettrace() is scheduled.expected_trace:
                    trace = scheduled.prepare_profile_entry(frame)
                    if trace is not None:
                        frame.f_trace = trace
                else:
                    scheduled._conflict()
        elif (event == "return" and collector.scheduled.direct_monitoring
              and id(frame) in collector.scheduled.frame_traces):
            collector.scheduled.monitor_event(frame, "return", arg)
        if not self.active or collector.status != "recording":
            return
        collector.profile(frame, event, arg)
        if not self.active or collector.status != "recording":
            return
        if self.bound_codes:
            self._arm_bound_codes()
        if event == "call":
            # The application body cannot execute before this callback ends.
            # Arm once, after proof preparation and Ray/Hydra promotion, so
            # the first body CALL is covered without two external lookups.
            self._arm_native(frame)
        if collector.callbacks.task_epoch != self.task_epoch:
            self._sync_task_lease()

    def _py_start(self, code, offset):
        try:
            self._receive(sys._getframe(1), code, "call", None)
        except Exception:
            self._fail("Python entry callback failed")

    def _py_throw(self, code, offset, exception):
        try:
            arg = exception if sys.version_info[:2] == (3, 12) else None
            self._receive(sys._getframe(1), code, "call", arg)
        except Exception:
            self._fail("Python throw callback failed")

    def _py_return(self, code, offset, value):
        try:
            self._receive(sys._getframe(1), code, "return", value)
        except Exception:
            self._fail("Python return callback failed")

    def _py_unwind(self, code, offset, exception):
        try:
            frame = sys._getframe(1)
            self._proof_exception(frame, code, exception)
            self._receive(frame, code, "return", None)
        except Exception:
            self._fail("Python unwind callback failed")

    def _proof_exception(self, frame, code, exception):
        if (not self.active or not self.collector.scheduled.direct_monitoring
                or not self.local.enabled):
            return
        current = sys.getprofile()
        if current is not None and current is not self.bootstrap_hook:
            return
        if not self.monitoring._owns_tool():
            self.collector.stop()
            return
        if frame.f_code is not code:
            raise RuntimeError("exception callback did not expose its application frame")
        if exception is not None:
            self.collector.scheduled.monitor_event(
                frame, "exception", (type(exception), exception,
                                     _TRACEBACK.__get__(exception, type(exception))),
            )

    def _exception(self, code, offset, exception):
        try:
            self._proof_exception(sys._getframe(1), code, exception)
        except Exception:
            self._fail("proof exception callback failed")

    def _c_call(self, code, offset, callable_value, arg0):
        try:
            kind = type(callable_value)
            if (kind is types.FunctionType or (kind is types.MethodType
                    and type(callable_value.__func__) is types.FunctionType)):
                return
            callee = _legacy_callable(callable_value, arg0, self.api.MISSING)
            if callee is not _IGNORED:
                frame = sys._getframe(1)
                if self.native_lease and not self._native_interest(frame, code):
                    return
                self._receive(frame, code, "c_call", callee)
        except Exception:
            self._fail("native entry callback failed")

    def _c_return(self, code, offset, callable_value, arg0):
        try:
            callee = _legacy_callable(callable_value, arg0, self.api.MISSING)
            if callee is not _IGNORED:
                frame = sys._getframe(1)
                if self.native_lease and not self._native_interest(frame, code):
                    # Excluded C events have no source/native proof. Their
                    # only callback-registry effect is exact Task removal at
                    # successful return; keep it even outside selected roots.
                    if (not self.collector.callbacks.tasks
                            or not issubclass(type(callee), types.BuiltinMethodType)
                            or type(callee.__self__) is not _TASK
                            or callee.__name__ != "remove_done_callback"):
                        return
                self._receive(frame, code, "c_return", callee)
        except Exception:
            self._fail("native return callback failed")

    def _c_raise(self, code, offset, callable_value, arg0):
        try:
            callee = _legacy_callable(callable_value, arg0, self.api.MISSING)
            if callee is not _IGNORED:
                frame = sys._getframe(1)
                if self.native_lease and not self._native_interest(frame, code):
                    return
                self._receive(frame, code, "c_exception", callee)
        except Exception:
            self._fail("native exception callback failed")

    def _fail(self, reason):
        self.collector.errors += 1
        self.failure = reason
        self.active = False
        try:
            if self.started and not self.installing:
                # No profile entries remain to confirm or release pending
                # proofs. Retire the whole recorder and its callable snapshots.
                self.collector.stop()
            else:
                self.stop()
        except Exception:
            self.collector.errors += 1
        # This private prototype explicitly does not claim a complete live
        # handoff after global tool failure: worker-thread repair is unproved.

    def after_fork(self):
        monitoring = self.monitoring
        monitoring.lock = threading.RLock()
        try:
            with monitoring.lock:
                # Only the surviving thread remains in the child. Preserve
                # its enrollment: an old/raw thread had no legacy profile
                # hook and must not acquire capture merely by forking.
                enabled = self.local.enabled
                self.local = _DeliveryState()
                self.local.enabled = enabled
                # Parent activations will be retired by ScheduledCalls. Clear
                # their local proof bits before dropping a global fallback
                # lease, including codes which refused every local mask.
                monitoring.reset()
                monitoring.reset_native()
                self.native_lease = False
                self.native_fallback = False
                self.task_epoch = -1
                self.bound_codes.clear()
                self.leases_acquired = self.leases_released = 0
                self.instruction_lease = False
                if self.active and monitoring._owns_tool():
                    if enabled:
                        self._seed_active_sources()
                    self.api.set_events(monitoring.tool_id, self.mask())
                    if self.native_lease:
                        self.leases_acquired += 1
        except Exception:
            self._fail("fork reconfiguration failed")

    def stop(self):
        self.active = False
        monitoring = self.monitoring
        with monitoring.lock:
            monitoring.reset_native()
            if monitoring._owns_tool():
                try:
                    self.api.set_events(monitoring.tool_id, 0)
                except Exception:
                    self.collector.errors += 1
                for event, _ in self.callbacks:
                    if not monitoring._owns_tool():
                        break
                    try:
                        self.api.register_callback(monitoring.tool_id, event, None)
                    except Exception:
                        self.collector.errors += 1
            monitoring.profile_client = None
            if threading.getprofile() is self.bootstrap_hook:
                threading.setprofile(self.collector.previous_thread_profile)
            self.local = _DeliveryState()
            self.native_lease = False
            self.bound_codes.clear()
            self.instruction_lease = False
            if self.collector.scheduled.status != "recording":
                monitoring.stop()


# A single audit listener keeps only weak references to local clients.
# With no live client it returns immediately; it never evaluates app objects.
_LOCAL_CLIENTS = weakref.WeakSet()
_LOCAL_AUDIT_INSTALLED = False


def _local_audit(event, arguments):
    # Frame/code reads produce millions of unrelated audit events. Filter
    # before touching weak registries; never call a supplied str subclass hook.
    if type(event) is not str or event not in (
            "exec", "function.__new__", "callnav.local.audit"):
        return
    # Explicit install/stop may mutate the registry in another thread. A
    # stable set snapshot avoids exposing WeakSet iterator races to app exec.
    for reference in _LOCAL_CLIENTS.data.copy():
        client = reference()
        if client is None or not (client.active or client.arming):
            continue
        if event == "callnav.local.audit":
            if arguments and arguments[0] is client.audit_token:
                client.audit_seen = True
        elif client.python_events:
            # Deserializers construct functions without exec. Their code must
            # be armed before an implicit property or slot entry can run.
            try:
                if arguments and type(arguments[0]) is types.CodeType:
                    client._source_tree(arguments[0])
            except Exception:
                client._fail("source audit arming failed")


class LocalProfileMonitoring(ProfileMonitoring):
    """Selective Python events on source and proven call codes."""

    backend_name = "local"

    def __init__(self, collector):
        super().__init__(collector)
        from . import collector as helpers
        from .cache import CodeIdentitySet

        self.helpers = helpers
        self.source_codes = CodeIdentitySet()
        events = self.api.events if self.api is not None else None
        self.local_capabilities = (events.PY_START | events.PY_RESUME | events.PY_RETURN
                                   | events.PY_YIELD | events.STOP_ITERATION) if events else 0
        self.proof_lease = False
        self.arming = False
        self.audit_token = object()
        self.audit_seen = False
        self.direct_requested = os.environ.get("CALLNAV_DIRECT_PROOFS", "1") == "1"

    def install(self):
        global _LOCAL_AUDIT_INSTALLED
        if self.api is None or sys.version_info[:2] not in ((3, 12), (3, 13)):
            return False
        self.arming = True
        try:
            if not _LOCAL_AUDIT_INSTALLED:
                sys.addaudithook(_local_audit)
                _LOCAL_AUDIT_INSTALLED = True
            _LOCAL_CLIENTS.add(self)
            sys.audit("callnav.local.audit", self.audit_token)
            if not self.audit_seen:
                return False
            return super().install()
        except Exception:
            self.stop()
            return False
        finally:
            self.arming = False

    @property
    def local_python_events(self):
        return self.python_events & self.local_capabilities

    def mask(self):
        # CPython requires unwind/throw/raise/reraise events to remain global.
        # Only start/resume/return/yield/stop-iteration may use local masks.
        # Every live source/proof frame already has its own physical CALL bit;
        # construction receipts need global Python barriers, not global C calls.
        return ((self.python_events if self.proof_lease else
                 self.python_events & ~self.local_python_events)
                | (self.api.events.CALL if self.native_lease else 0)
                | (self.api.events.INSTRUCTION if self.instruction_lease else 0))

    def _python(self, code):
        if self.monitoring.python_codes.get(code):
            return
        if not self.monitoring.watch_python(code):
            raise RuntimeError("local Python event mask failed")

    def _source_tree(self, code):
        pending = [code]
        while pending:
            current = pending.pop()
            if current in self.source_codes:
                continue
            self.source_codes.add(current)
            # Optional callback backends are discovered on module completion
            # even when their ordinary function bodies are outside all roots.
            if current.co_name == "<module>":
                self._python(current)
            if self.collector.path(current.co_filename) is None:
                continue
            self._python(current)
            pending.extend(value for value in current.co_consts if type(value) is types.CodeType)

    def _scan_loaded_sources(self):
        seen = set()
        for module in tuple(sys.modules.values()):
            if type(module) is not types.ModuleType:
                continue
            namespace = types.ModuleType.__getattribute__(module, "__dict__")
            filename = namespace.get("__file__")
            if type(filename) is not str or self.collector.path(filename) is None:
                continue
            pending = list(namespace.values())
            while pending:
                value = pending.pop()
                if id(value) in seen:
                    continue
                seen.add(id(value))
                function = self.helpers._function(value)
                if function is not None:
                    self._source_tree(function.__code__)
                elif issubclass(type(value), type):
                    namespace = self.helpers._TYPE_NAMESPACE.__get__(value)
                    pending.extend(namespace.values())
                elif type(value) in (staticmethod, classmethod):
                    pending.append(value.__func__)

    def _seed_active_sources(self):
        # Already imported source functions and an already executing start()
        # caller require masks before the next application instruction.
        self._scan_loaded_sources()
        frame = sys._getframe(1)
        try:
            while frame is not None:
                self._source_tree(frame.f_code)
                frame = frame.f_back
        finally:
            del frame
        callbacks = self.collector.callbacks
        for code in (callbacks.thread_init, callbacks.thread_run,
                     callbacks.handle_run, callbacks.call_soon):
            self._python(code)
        super()._seed_active_sources()

    def _class_code(self, value, names):
        for name in names:
            for owner in self.helpers._TYPE_MRO.__get__(value):
                namespace = self.helpers._TYPE_NAMESPACE.__get__(owner)
                if name not in namespace:
                    continue
                method = namespace[name]
                if type(method) in (staticmethod, classmethod):
                    method = method.__func__
                function = self.helpers._function(method)
                if function is not None:
                    self._python(function.__code__)
                break

    def arm_callable(self, value):
        # These exact immutable builtin callable types cannot expose Python
        # __call__ code. Their C events still use the unchanged legacy adapter.
        # Identity checks avoid a user metaclass's equality/hash hooks.
        kind = type(value)
        if (kind is types.BuiltinFunctionType or kind is types.MethodDescriptorType
                or kind is types.WrapperDescriptorType or kind is types.MethodWrapperType
                or kind is types.ClassMethodDescriptorType):
            return
        function = self.helpers._function(value)
        if function is not None:
            self._python(function.__code__)
        elif kind is types.MethodType:
            # _function already accepted every actual Python method above.
            return
        elif issubclass(type(value), type):
            self._class_code(value, ("__new__", "__init__"))
            self._class_code(type(value), ("__call__",))
        else:
            self._class_code(type(value), ("__call__",))

    def _c_call(self, code, offset, callable_value, arg0):
        try:
            frame = sys._getframe(1)
            if frame.f_code is not code:
                raise RuntimeError("CALL callback did not expose its application frame")
            # A global native Task lease needs C invalidation outside roots;
            # it must not recursively enroll every unrelated Python call.
            if not (self.native_lease or self.proof_lease) or self._native_interest(frame, code):
                self.arm_callable(callable_value)
            # This adapter passes no synthetic Python entry; the VM must emit
            # a separate PY_START/PY_RESUME after the call was actually accepted.
            kind = type(callable_value)
            if (kind is types.FunctionType or (kind is types.MethodType
                    and type(callable_value.__func__) is types.FunctionType)):
                return
            callee = _legacy_callable(callable_value, arg0, self.api.MISSING)
            if callee is not _IGNORED:
                if self.native_lease and not self._native_interest(frame, code):
                    return
                self._receive(frame, code, "c_call", callee)
        except Exception:
            self._fail("local CALL arming failed")

    def sync_proof_lease(self):
        if not self.active:
            return
        with self.monitoring.lock:
            if not self.monitoring._owns_tool():
                self.collector.stop()
                return
            sensitive = any(
                state["creations"] or state["binding"] is not None
                or any(any(entries) for entries in state["comprehensions"].copy().values())
                for state in self.collector.scheduled.deferred.active.copy().values()
            ) or any(state["created"] for state in self.collector.scheduled.active.copy().values())
            if sensitive != self.proof_lease:
                self.proof_lease = sensitive
                self.api.set_events(self.monitoring.tool_id, self.mask())

    def _deliver(self, frame, event, arg):
        # Only proof instructions create new construction receipts. An unrelated
        # external entry/return cannot acquire or release this global lease.
        tracked = False
        if self.proof_lease:
            scheduled = self.collector.scheduled
            key = threading.get_ident(), id(frame)
            tracked = key in scheduled.deferred.active or key in scheduled.active
            if event == "call" and not tracked and frame.f_back is not None:
                parent = key[0], id(frame.f_back)
                tracked = parent in scheduled.deferred.active or parent in scheduled.active
        super()._deliver(frame, event, arg)
        if tracked and self.proof_lease:
            self.sync_proof_lease()

    def _proof_exception(self, frame, code, exception):
        super()._proof_exception(frame, code, exception)
        if self.proof_lease:
            self.sync_proof_lease()

    def _arm_native(self, frame):
        super()._arm_native(frame)
        if self.monitoring.native_codes.get(frame.f_code):
            self._python(frame.f_code)

    def _arm_bound_codes(self):
        for code in tuple(self.bound_codes.copy().values()):
            self._python(code)
        super()._arm_bound_codes()

    def stop(self):
        self.arming = False
        self.proof_lease = False
        _LOCAL_CLIENTS.discard(self)
        self.monitoring.reset_python()
        super().stop()
