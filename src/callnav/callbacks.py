"""Join trusted stdlib callback registrations to their actual Python entries.

Registrations alone never produce navigation. Thread targets must enter directly
from Thread.run; Task callbacks must enter from Handle._run with that exact Task
as their argument. Only weak references survive registration.
"""

import asyncio
from collections import Counter
import sys
import threading
import types
import weakref

_THREAD = threading.Thread
_TASK = asyncio.Task
_HANDLE = asyncio.Handle
_TASK_CALLBACKS = _TASK._callbacks
_HANDLE_CALLBACK = _HANDLE._callback
_HANDLE_ARGS = _HANDLE._args
_TASK_DONE = _TASK.done
_TASK_COROUTINE = _TASK.get_coro
_CURRENT_TASK = asyncio.current_task
_COROUTINE_FRAME = types.CoroutineType.cr_frame
_ABSTRACT_LOOP = asyncio.AbstractEventLoop
_TYPE_NAMESPACE = vars(type)["__dict__"]
_TYPE_NAME = vars(type)["__name__"]


class RegisteredCallbacks:
    def __init__(self, collector):
        from .collector import _function

        self.collector = collector
        self.function = _function
        self.thread_init = _THREAD.__init__.__code__
        self.thread_run = _THREAD.run.__code__
        self.handle_run = _HANDLE._run.__code__
        self.call_soon = asyncio.BaseEventLoop.call_soon.__code__
        self.threads = {}
        self.thread_initializing = {}
        self.tasks = {}
        self.active = {}
        self.task_lock = threading.RLock()
        self.task_origins = 0
        self.task_epoch = 0
        self.scheduling = {}
        self.handles = {}
        self.uvloop_type = None
        self.uvloop_patch = None
        self.uvloop_status = "not_loaded"
        self.uv_callback_code = None
        self.uv_dispatch = {}
        self._install_uvloop()

    def _install_uvloop(self):
        """Install only after the application imports an actual uvloop module."""
        module, native_module = sys.modules.get("uvloop"), sys.modules.get("uvloop.loop")
        if type(module) is not types.ModuleType or type(native_module) is not types.ModuleType:
            return
        if self.uvloop_patch is not None:
            return
        loop_type = vars(module).get("Loop")
        native_type = vars(native_module).get("Loop")
        bases = type.__getattribute__(loop_type, "__bases__") if type(loop_type) is type else ()
        if (type(loop_type) is not type or type(native_type) is not type
                or len(bases) != 2 or bases[0] is not native_type or bases[1] is not _ABSTRACT_LOOP
                or not type.__getattribute__(native_type, "__flags__") & (1 << 8)):
            self.uvloop_status = "unsupported_loop"
            return
        namespace = type.__getattribute__(loop_type, "__dict__")
        original = type.__getattribute__(native_type, "__dict__").get("call_soon")
        if (any(name not in ("__module__", "__dict__", "__doc__") for name in namespace)
                or namespace.get("__module__") != "uvloop"
                or original is None or type(original) is types.FunctionType):
            self.uvloop_status = "unsupported_loop"
            return
        manager_reference = weakref.ref(self)

        def call_soon(loop, callback, *args, context=None):
            manager = manager_reference()
            if manager is not None:
                try:
                    callback = manager._uv_callback(loop, callback, args)
                except Exception:
                    manager.collector.errors += 1
            return original(loop, callback, *args, context=context)

        self.uvloop_type = loop_type
        self.uvloop_patch = call_soon
        type.__setattr__(loop_type, "call_soon", call_soon)
        self.uvloop_status = "recording"

    def _uv_callback(self, loop, callback, arguments):
        """Mint a receipt only while a native Task schedules its completion."""
        if type(loop) is not self.uvloop_type or len(arguments) != 1:
            return callback
        namespace = type.__getattribute__(self.uvloop_type, "__dict__")
        if (namespace.get("call_soon") is not self.uvloop_patch
                or any(name not in ("__module__", "__dict__", "__doc__", "call_soon")
                       for name in namespace)):
            return callback
        task = arguments[0]
        with self.task_lock:
            registered = self.tasks.get(id(task))
            if type(task) is not _TASK or registered is None or registered[0]() is not task:
                return callback
        # These trusted helpers can emit Python profile events when uvloop
        # calls us outside a recorder callback. Keep them outside task_lock.
        if _CURRENT_TASK(loop) is not task or not _TASK_DONE(task):
            return callback
        coroutine = _TASK_COROUTINE(task)
        if type(coroutine) is not types.CoroutineType or _COROUTINE_FRAME.__get__(coroutine) is not None:
            return callback
        receipt = None
        with self.task_lock:
            if self.tasks.get(id(task)) is not registered:
                return callback
            for index, origin in enumerate(registered[1]):
                if origin[0]() is callback:
                    registered[1].pop(index)
                    self.task_origins -= 1
                    self.task_epoch += 1
                    receipt = registered[0], origin
                    break
        return self._uv_receipt(callback, *receipt) if receipt is not None else callback

    def _uv_receipt(self, callback, task_reference, origin):
        manager_reference = weakref.ref(self)

        def invoke(*values):
            manager = manager_reference()
            key = threading.get_ident(), id(sys._getframe())
            if manager is not None:
                manager.uv_dispatch[key] = task_reference, origin
            try:
                return callback(*values)
            finally:
                if manager is not None:
                    manager.uv_dispatch.pop(key, None)

        self.uv_callback_code = invoke.__code__
        return invoke

    def _origin(self, caller, callback):
        function = self.function(callback)
        if function is None or caller is None:
            return None
        info = self.collector._site(caller, function.__code__)
        if info is None or info[0].get("ambiguous") or info[0].get("precision") != "instruction":
            return None
        try:
            reference = weakref.ref(callback)
        except TypeError:
            return None
        profile = self.collector.profile_monitoring
        if profile is not None and profile.active:
            arm = getattr(profile, "arm_callable", None)
            if arm is not None:
                arm(function)
        return reference, caller.f_code, caller.f_lasti, dict(info[0])

    @staticmethod
    def _snapshot(task):
        # Exact native Task checked by the caller: this descriptor cannot run
        # application properties or an overridden __getattribute__.
        values = _TASK_CALLBACKS.__get__(task, _TASK)
        return list(values or ())

    def _keep(self, mapping, owner, origin):
        if mapping is self.tasks:
            with self.task_lock:
                self._keep_origin(mapping, owner, origin)
        else:
            self._keep_origin(mapping, owner, origin)

    def _keep_origin(self, mapping, owner, origin):
        key = id(owner)

        def discard(reference):
            if mapping is self.tasks:
                with self.task_lock:
                    current = mapping.get(key)
                    if current is not None and current[0] is reference:
                        self.task_origins -= len(current[1])
                        self.task_epoch += 1
                        mapping.pop(key, None)
            else:
                current = mapping.get(key)
                if current is not None and current[0] is reference:
                    mapping.pop(key, None)

        current = mapping.get(key)
        if current is None or current[0]() is not owner:
            if mapping is self.tasks:
                self.task_origins -= len(current[1]) if current is not None else 0
            mapping[key] = (weakref.ref(owner, discard), [origin])
        else:
            current[1].append(origin)
        if mapping is self.tasks:
            self.task_origins += 1
            self.task_epoch += 1

    def observe(self, frame, event, arg):
        if event == "return":
            code = frame.f_code
            if self.uvloop_patch is None and code.co_name == "<module>":
                module = sys.modules.get("uvloop")
                if type(module) is types.ModuleType and frame.f_globals is vars(module):
                    self._install_uvloop()
            if not ((code is self.thread_init and self.thread_initializing)
                    or (code is self.call_soon and self.scheduling)):
                return
            key = (threading.get_ident(), id(frame))
            pending_thread = self.thread_initializing.pop(key, None) if code is self.thread_init else None
            if pending_thread is not None:
                owner_reference, origin = pending_thread
                owner = owner_reference()
                if owner is not None:
                    self.threads.pop(id(owner), None)
                operations = self.collector._instructions(frame.f_code)[1]
                completed = any(operation.offset == frame.f_lasti
                                and operation.opname in ("RETURN_VALUE", "RETURN_CONST")
                                for operation in operations)
                if (completed and owner is not None
                        and object.__getattribute__(owner, "__dict__").get("_target") is origin[0]()):
                    self._keep(self.threads, owner, origin)
            pending = self.scheduling.pop(key, None) if code is self.call_soon else None
            if pending is not None and type(arg) is _HANDLE:
                owner, origin = pending
                if _HANDLE_CALLBACK.__get__(arg, _HANDLE) is origin[0]():
                    self._keep(self.handles, arg, (owner, origin))
            return
        if event in ("c_call", "c_return", "c_exception"):
            if not issubclass(type(arg), types.BuiltinMethodType) or type(arg.__self__) is not _TASK:
                return
            method = arg.__name__
            if method not in ("add_done_callback", "remove_done_callback"):
                return
            task = arg.__self__
            with self.task_lock:
                if method == "remove_done_callback":
                    if event == "c_return":
                        current = self.tasks.get(id(task))
                        if current is not None and current[0]() is task:
                            remaining = {id(callback) for callback, _ in self._snapshot(task)}
                            previous_count = len(current[1])
                            current[1][:] = [origin for origin in current[1]
                                            if origin[0]() is not None and id(origin[0]()) in remaining]
                            self.task_origins += len(current[1]) - previous_count
                            self.task_epoch += 1
                    return
                if event == "c_call":
                    if self.collector.path(frame.f_code.co_filename):
                        key = (threading.get_ident(), id(frame))
                        self.active[key] = (weakref.ref(task), Counter(
                            (id(callback), id(context)) for callback, context in self._snapshot(task)))
                        self.task_epoch += 1
                        return True
                    return
                if not self.active:
                    return
                key = (threading.get_ident(), id(frame))
                pending = self.active.pop(key, None)
                if pending is not None:
                    self.task_epoch += 1
                if event != "c_return" or pending is None or pending[0]() is not task:
                    return
                before = pending[1]
                for callback, context in self._snapshot(task):
                    identity = (id(callback), id(context))
                    if before[identity]:
                        before[identity] -= 1
                        continue
                    origin = self._origin(frame, callback)
                    if origin is not None:
                        self._keep(self.tasks, task, origin)
                return
        if event != "call":
            return
        if frame.f_code is self.call_soon:
            if not self.tasks:
                return
            callback, arguments = frame.f_locals.get("callback"), frame.f_locals.get("args")
            if type(arguments) is not tuple or len(arguments) != 1:
                return
            task = arguments[0]
            with self.task_lock:
                registered = self.tasks.get(id(task))
                if (type(task) is not _TASK or registered is None or registered[0]() is not task
                        or not self._task_dispatch(frame.f_back, task)):
                    return
                # The native Task schedules its callbacks in registration order.
                for index, origin in enumerate(registered[1]):
                    if origin[0]() is callback:
                        key = (threading.get_ident(), id(frame))
                        self.scheduling[key] = registered[0], registered[1].pop(index)
                        self.task_origins -= 1
                        self.task_epoch += 1
                        break
                return
        if frame.f_code is self.thread_init:
            owner, callback = frame.f_locals.get("self"), frame.f_locals.get("target")
            if type(owner) is _THREAD:
                # Reinitializing an unstarted Thread supersedes its old target
                # registration. A constructor that fails must mint no receipt.
                self.threads.pop(id(owner), None)
                origin = self._origin(frame.f_back, callback)
                if origin is not None:
                    key = (threading.get_ident(), id(frame))
                    self.thread_initializing[key] = weakref.ref(owner), origin
            return
        if not (self.threads or self.handles or self.uv_dispatch):
            return
        parent = frame.f_back
        if parent is None:
            return
        if parent.f_code is self.uv_callback_code:
            receipt = self.uv_dispatch.pop((threading.get_ident(), id(parent)), None)
            if receipt is not None:
                task_reference, origin = receipt
                callback, arguments = parent.f_locals.get("callback"), parent.f_locals.get("values")
                function = self.function(callback)
                if (origin[0]() is callback and type(arguments) is tuple and len(arguments) == 1
                        and task_reference() is arguments[0] and function is not None
                        and function.__code__ is frame.f_code):
                    self.collector.confirm_callback(frame, *origin[1:], function, dispatch="uvloop")
            return
        if parent.f_code is self.thread_run:
            owner = parent.f_locals.get("self")
            if type(owner) is not _THREAD:
                return
            current = object.__getattribute__(owner, "__dict__").get("_target")
            registered = self.threads.get(id(owner))
        elif parent.f_code is self.handle_run:
            handle = parent.f_locals.get("self")
            if type(handle) is not _HANDLE:
                return
            arguments = _HANDLE_ARGS.__get__(handle, _HANDLE)
            if type(arguments) is not tuple or len(arguments) != 1:
                return
            owner = arguments[0]
            if type(owner) is not _TASK:
                return
            current = _HANDLE_CALLBACK.__get__(handle, _HANDLE)
            registered_handle = self.handles.pop(id(handle), None)
            if registered_handle is None or registered_handle[0]() is not handle:
                return
            entries = [origin for task_ref, origin in registered_handle[1] if task_ref() is owner]
            registered = (weakref.ref(owner), entries)
        else:
            return
        if registered is None or registered[0]() is not owner:
            return
        for origin in registered[1]:
            callback = origin[0]()
            if callback is not None and callback is current:
                function = self.function(callback)
                if function is not None and function.__code__ is frame.f_code:
                    self.collector.confirm_callback(frame, *origin[1:], function)

    def _task_dispatch(self, parent, task):
        """Only native Task completion may mint a callback Handle receipt."""
        if parent is None or parent.f_code is not self.handle_run:
            return False
        handle = parent.f_locals.get("self")
        if type(handle) is not _HANDLE:
            return False
        callback = _HANDLE_CALLBACK.__get__(handle, _HANDLE)
        if type(callback) is types.BuiltinMethodType:
            return callback.__self__ is task and callback.__name__ == "task_wakeup"
        kind = type(callback)
        descriptor = _TYPE_NAMESPACE.__get__(kind).get("__self__")
        # Python 3.13 uses a heap C type here. A Python class with a spoofed
        # name cannot create a native getset descriptor owned by that class.
        return (_TYPE_NAME.__get__(kind) == "TaskStepMethWrapper"
                and type(descriptor) is types.GetSetDescriptorType
                and descriptor.__objclass__ is kind
                and descriptor.__get__(callback, kind) is task)

    def after_fork(self):
        self.task_lock = threading.RLock()
        with self.task_lock:
            self._clear()

    def _clear(self):
        self.threads.clear()
        self.thread_initializing.clear()
        self.tasks.clear()
        self.active.clear()
        self.task_origins = 0
        self.task_epoch += 1
        self.scheduling.clear()
        self.handles.clear()
        self.uv_dispatch.clear()

    def stop(self):
        if (self.uvloop_type is not None and self.uvloop_patch is not None
                and type.__getattribute__(self.uvloop_type, "__dict__").get("call_soon") is self.uvloop_patch):
            type.__delattr__(self.uvloop_type, "call_soon")
        self.uvloop_status = "complete" if self.uvloop_patch is not None else self.uvloop_status
        with self.task_lock:
            self._clear()
