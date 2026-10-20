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

The collector uses `sys.setprofile` and Python frame/code metadata. It selects calls whose endpoints are within configured source roots, resolves supported caller expressions, and records source ranges and concrete target definitions. CallNav requires Python 3.11+ for the recorder and participating processes; its instruction positions allow distinct calls on one line to be separated.

Call-site resolution stays conservative. A source expression that cannot be resolved safely does not become a definite navigable edge merely because a callee ran nearby. Implicit comprehension frames need a proven helper lineage before their ambiguity can be replaced by a more precise contained call.

The recorder does not serialize function argument or return values. Launch and process metadata still contain arguments and local paths.

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

Only participating processes and flushed events appear in a recording. A worker that never inherited the bootstrap can be absent without producing a process report. A killed process can leave a partial shard. Native internals, HTTP relationships, deferred async scheduling, unsupported Ray forms, and unresolved caller expressions have separate limits.

Read process statuses and warnings together with the graph. An observed edge is evidence of a supported call during that run; an absent edge is not proof that the relationship can never occur.
