# How CallNav works

CallNav turns an observed Python call into a VS Code definition location. Its components are a startup bootstrap, a per-process collector, a merger, optional framework/Ray integrations, and an editor definition provider.

## From command to navigation

```text
callnav record --root "$PWD" -- bash ./launch.sh
    |
    +-- launch command with an opt-in Python startup bootstrap
           |
           +-- instrumented Python processes / new threads
           |       |
           |       +-- observed calls + source fingerprints -> per-process shards
           |
           +-- optional Ray submission / worker-entry receipts
    |
    +-- merge flushed data -> index.json + .callnav/latest.json
                                  |
                                  +-- VS Code definition provider
                                         |
                                         +-- verify source and call range
                                         +-- open observed target / show candidates
```

## Collection without source rewriting

The CLI injects a temporary `sitecustomize` through `PYTHONPATH`, enabled by per-run `CALLNAV_*` variables. It chains an existing startup customization. Compatible Python children that inherit this environment can activate collection without editing their application entry points.

The collector uses local `sys.monitoring` events on Python 3.12/3.13, with `sys.setprofile` as a fallback, and Python frame/code metadata. Source execution, function reconstruction, and observed callable dispatch arm local Python events; sensitive construction and binding proofs retain global Python barriers. Audit callbacks filter unrelated events before registry processing. It selects calls whose endpoints are within configured source roots, resolves supported caller expressions, and records source ranges and concrete target definitions. CallNav requires Python 3.11+ for the recorder and participating processes; its instruction positions allow distinct calls on one line to be separated.

Call-site resolution stays conservative. A source expression that cannot be resolved safely does not become a definite navigable edge merely because a callee ran nearby. Implicit comprehension frames need a proven helper lineage before their ambiguity can be replaced by a more precise contained call.

An implicit Python entry from an operation such as attribute access or indexing keeps line precision. Its exported offset identifies the owning instruction rather than an inline cache slot, so interpreter specialization cannot split repeated observations of one operation into separate edges. Explicit `CALL*` sites preserve their existing observed-offset convention. Live native-call and immediate-return receipts still match the actual runtime offsets; this metadata normalization supplies no additional navigation proof.

The recorder does not serialize function argument or return values. Launch and process metadata still contain arguments and local paths.

For immediate `asyncio.create_task(fn(...))`, selective opcode tracing observes the native async callable at its actual name load and captures its code before the call. The standard scheduler's real coroutine object must match that creation, and the coroutine must actually enter its initial instruction before a `scheduled` edge is confirmed. This also supplies the initial binding for ordinary decorator forwarding. Existing debugger trace hooks disable this feature rather than being replaced.

The same pre-call tracing can join newly created coroutine, async-generator and generator objects observed in real Python parameters or literal returns to their first actual frame entry. A compiler-verified literal tuple loop can also identify each newly created object from the actual loop variable. Synchronous decorators project an origin only when their exact `__wrapped__` function created and returned that same object. Overlapping scheduler and generic proofs for the same creation are confirmed once, preserving forwarding counts. Weak references prevent the recorder from keeping suspended application objects alive. Thread targets require a real Thread.run dispatch; Task callback registrations require the native Task to schedule the exact returned Handle and then that Handle to enter its registered callback. The optional uvloop bridge installs only when the application imports uvloop. It joins an exact registered native Task to its completion while that closed coroutine is still the current Task; a one-shot scheduling receipt must then actually call the registered function. Context, arguments and callback exceptions are preserved, and stopping restores the bridge when it still owns the patch.

Safe `super()` and exact builtin `getattr()` resolution inspect ordinary MRO bindings without evaluating properties. A returned object method is accepted only with an immediately observed producer return. Dictionary dispatch uses exact builtin containers and safe keys.

Native generator assignments use an immediate compiler-verified local store and its actual value. Simple one-clause list comprehensions retain a separate pre-call binding for each appended creation, matched against the actual returned list or Python consumer arguments. Direct native async-generator iteration requires the executed `CALL`, `GET_AITER`, `GET_ANEXT` and `SEND` lineage in the same caller activation before accepting the actual first body entry. Factories and custom async iterators do not satisfy this native protocol proof. Intervening application callbacks invalidate provisional store and list-comprehension receipts, including destructor and argument callbacks that could substitute an older object. A proven framework wrapper outside the source roots can seed forwarding; it creates no external navigation target, and projection still requires the application body to enter and the forwarding source fingerprint to match.

An immediate returned-callable expression such as `registry.get(key)(*args, **kwargs)` uses a weak binding from the observed factory return. It expires at the next profile event and only applies to the exact outer AST expression and caller frame. The target must enter with matching code. No factory expression is evaluated by the recorder.

The optional-import Hydra integration observes its resolved root callable at the invocation boundary and confirms an actual Python entry directly from that boundary. It records the original Hydra forwarding frames as source fingerprints. Config names, recursive argument factories, deferred partials, and failed calls without a target entry provide no root target evidence.

For `@hydra.main`, the integration preserves Hydra's original task function and decorator. A per-invocation context joins the source entrypoint to the actual task body only after Hydra calls that exact task function through an observed call instruction. The complete intervening stack must belong to the loaded Hydra package and contain its matching decorated entrypoint. Help-only invocations and unrelated helper reentries supply no target.

## Concrete targets, partials, and wrappers

Observed calls identify code that actually entered, so a registry lookup or selected subclass can point to its runtime implementation rather than its static interface.

Exact built-in `functools.partial` objects can resolve through `.func` to an observed Python target. Arbitrary user-defined callable behavior is not treated as identical to a built-in partial.

Decorator projection requires an actually executed forwarding chain, bounded to eight hops. A declared `__wrapped__` value alone is insufficient. Raw wrapper edges remain in the index alongside projected implementation edges; projection carries the intervening source fingerprints. The extension hides a wrapper only when all its recorded invocations forwarded and the matching projection passes verification. Conditional nonforwarding calls retain the wrapper as a candidate.

## Cross-process relationships

Ordinary subprocess collection gives each process its own Python call edges. It does not by itself reconstruct a causal edge from a parent process's API submission to a child process's work.

The optional Ray backend addresses supported Actor methods and constructors. It hooks submissions, records task/actor identities, and joins those identities to observed worker entries. For constructors it derives the actor creation task identity from the returned handle. A submitted request without a matching execution receipt produces no confirmed target.

Environment propagation is necessary for both halves of this match. Shared paths and explicit integration are required when a job server, container, existing worker, or launcher changes the environment. With `--ray`, a PATH-based CLI shim propagates collection into blocking Ray Job submissions via their JSON runtime environments and strips the opt-in from `ray start` daemons. The synchronous Slime adapter reuses that shim and supplies a specific iteration seam; it is not a universal lifecycle controller.

## Index and editor behavior

The merger aggregates repeated edges by caller, target, kind, and wrapper chain while retaining counts and process identities. The index contains source fingerprints, reported processes, warnings, and optional backend diagnostics. The schema version is currently 1; newer optional fields preserve older readable indices.

The VS Code extension contributes definitions to the ordinary `python` language. It uses the clicked source range to find observed targets, verifies the caller, target, and wrapper source hashes, and returns standard definition links. Multiple valid targets use VS Code's built-in chooser. Other Python definition providers continue to run.

Fingerprints prevent stale recordings from sending users to a mismatching definition. Unsaved edits are checked too. The current index uses runtime-resolved absolute paths; installation is independent of any development server, but relocating a completed recording is not supported.

## Coverage is evidence, not completeness

Only participating processes and flushed events appear in a recording. A worker that never inherited the bootstrap can be absent without producing a process report. A killed process can leave a partial shard. Native internals, HTTP relationships, unsupported scheduling forms, unsupported Ray forms, and unresolved caller expressions have separate limits.

Read process statuses and warnings together with the graph. An observed edge is evidence of a supported call during that run; an absent edge is not proof that the relationship can never occur.
