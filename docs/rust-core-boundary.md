# Rust Core Boundary

ForkCell should not be rewritten wholesale in Rust just to match OpenShell.
OpenShell already owns the runtime enforcement substrate. ForkCell currently
owns the transaction/control plane: checkpoint identity, restore decisions,
policy binding, receipts, and backend selection.

## Keep In Python For Now

- CLI routing and product workflow glue;
- preview demos and smoke scripts;
- receipt pretty-printing;
- high-level backend orchestration;
- fast iteration on policy/receipt semantics.

These paths are not the current dominant latency source.

## Candidate Rust Core

A future `forkcell-core` Rust component should be narrow and measurable:

- workspace scan/index and file metadata cache;
- delta manifest and restore planner;
- platform filesystem primitives such as APFS clonefile, Linux reflink, and
  overlay generation switching helpers;
- receipt hash/sign helper;
- optional resident daemon to avoid repeated CLI/process startup.

## Decision Gates

Do not start a Rust rewrite until benchmark data shows one of these conditions:

- Python/filesystem indexing is a measurable bottleneck on medium workspaces;
- checkpoint or restore planning exceeds the target budget after Docker/OpenShell
  lifecycle is excluded;
- receipt signing or tamper-evident logging needs a smaller trusted component;
- a resident service is required to cache indexes and runtime locks.

## Current Priority

1. Maintain the Python control plane.
2. Keep improving benchmark breakdowns.
3. Optimize Docker/OpenShell lifecycle before language rewrites.
4. Prototype Rust only for hot-path filesystem/index/signing code.
5. Promote Rust code only when it moves a tracked metric.
