"""CPython profile dispatch with direct proof monitoring on Python 3.12/3.13.

The interpreter supplies the original profile frame, C callable and raw offset.
The independent monitor only supplies proof instructions and exceptions; no
Python CALL adapter, native masks, or Task-dependent global CALL lease is used.
"""

import os
import sys
import threading
import types

from .callbacks import _TASK
from .profile_monitoring import ProfileMonitoring, _TRACEBACK


_UNKNOWN_PATH = object()


class HybridProfile(ProfileMonitoring):
    backend_name = "hybrid"
    requires_native_masks = False

    def __init__(self, collector):
        super().__init__(collector)
        self.hook = self._profile
        self.direct_requested = os.environ.get("CALLNAV_DIRECT_PROOFS", "1") == "1"

    def install(self):
        if self.api is None or sys.version_info[:2] not in ((3, 12), (3, 13)):
            return False
        if sys.getprofile() is not None or threading.getprofile() is not None:
            return False
        monitoring = self.monitoring
        with monitoring.lock:
            if not monitoring._owns_tool():
                return False
            events = self.api.events
            self.callbacks = tuple((event, self._exception) for event in (
                events.RAISE, events.RERAISE, events.STOP_ITERATION,
            ))
            monitoring.profile_client = self
            self.installing = True
            try:
                for event, callback in self.callbacks:
                    self.api.register_callback(monitoring.tool_id, event, callback)
                if self.direct_requested:
                    self.collector.scheduled.activate_direct()
                self.api.set_events(monitoring.tool_id, self.mask())
                self.active = self.started = True
                threading.setprofile(self.hook)
                sys.setprofile(self.hook)
                if not self.active:
                    raise RuntimeError("hybrid profile failed during installation")
                self.installing = False
                return True
            except Exception:
                try:
                    self.collector.scheduled.restore_legacy()
                    self.stop()
                except Exception:
                    self.collector.errors += 1
                finally:
                    self.started = self.installing = False
                return False

    def delegates_entry(self):
        return self.active and self.collector.scheduled.direct_monitoring

    def mask(self):
        events = self.api.events
        exceptions = (events.RAISE | events.RERAISE | events.STOP_ITERATION
                      if self.collector.scheduled.direct_monitoring else 0)
        return exceptions | (events.INSTRUCTION if self.instruction_lease else 0)

    def _global_native_lease(self, *, persistent=False):
        # CPython's profile dispatcher already covers native events everywhere.
        pass

    def _profile(self, frame, event, arg):
        try:
            if not self.active:
                return
            collector = self.collector
            scheduled = collector.scheduled
            if event not in ("call", "return"):
                code = frame.f_code
                # An unknown filename is delivered conservatively. Never
                # cache exclusion by code: wrappers can be promoted later.
                if collector.paths.get(code.co_filename, _UNKNOWN_PATH) is None:
                    frame_id = id(frame)
                    deferred = scheduled.deferred
                    proof = (code in deferred.wrapper_codes
                             or frame_id in collector.frame_functions
                             or frame_id in collector.relays
                             or (deferred.active and
                                 (threading.get_ident(), frame_id) in deferred.active))
                    if not proof:
                        # Excluded native events cannot supply a source C
                        # stack or create a Task registration origin. A
                        # successful native Task removal can invalidate an
                        # existing origin, even when performed outside roots.
                        if (event != "c_return" or not collector.callbacks.tasks
                                or not issubclass(type(arg), types.BuiltinMethodType)
                                or type(arg.__self__) is not _TASK
                                or arg.__name__ != "remove_done_callback"):
                            return
            if not self.monitoring._owns_tool():
                collector.stop()
                return
            if scheduled.direct_monitoring:
                if event == "call" and scheduled.status == "recording":
                    if sys.gettrace() is scheduled.expected_trace:
                        trace = scheduled.prepare_profile_entry(frame)
                        if trace is not None:
                            frame.f_trace = trace
                    else:
                        scheduled._conflict()
                elif event == "return" and id(frame) in scheduled.frame_traces:
                    scheduled.monitor_event(frame, "return", arg)
            if self.active and collector.status == "recording":
                collector.profile(frame, event, arg)
        except Exception:
            self._fail("hybrid profile callback failed")

    def _exception(self, code, offset, exception):
        try:
            # Every live direct proof frame was admitted through watch() or
            # watch_global() before application instructions could execute.
            # Unwatched code has no local proof to invalidate. Keep the global
            # event enabled: this code can be promoted on a later invocation.
            if code not in self.monitoring.offsets:
                return
            self._proof_exception(sys._getframe(1), code, exception)
        except Exception:
            self._fail("proof exception callback failed")

    def _proof_exception(self, frame, code, exception):
        if (not self.active or not self.collector.scheduled.direct_monitoring
                or sys.getprofile() is not self.hook):
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

    def after_fork(self):
        self.monitoring.lock = threading.RLock()
        with self.monitoring.lock:
            self.monitoring.reset()
            self.instruction_lease = False
            if self.active and self.monitoring._owns_tool():
                self.api.set_events(self.monitoring.tool_id, self.mask())

    def stop(self):
        self.active = False
        monitoring = self.monitoring
        with monitoring.lock:
            if sys.getprofile() is self.hook:
                sys.setprofile(self.collector.previous_profile)
            if threading.getprofile() is self.hook:
                threading.setprofile(self.collector.previous_thread_profile)
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
            self.instruction_lease = False
            if self.collector.scheduled.status != "recording":
                monitoring.stop()
