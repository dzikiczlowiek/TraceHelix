# Architecture

The .NET 10 solution is a modular monolith:

- **Domain** owns immutable run, canonical event, classification, alert, analysis revision, comparison, and report contracts.
- **Application** owns ports and use-case orchestration. `AnalyzeRun` always creates a new analysis revision and never overwrites another classifier's result.
- **Infrastructure** supplies the streaming generic JSONL adapter, SHA-256 hashing, raw SQLite repositories, deterministic rules, six versioned detectors, and report writers.
- **CLI** is a thin manual parser over the same use cases. JSON is the stable machine interface for this slice.
- **API**, **web**, and **training** are buildable shells only and are deliberately outside this vertical slice.

SQLite is implemented with `Microsoft.Data.Sqlite` and explicit schema creation rather than EF Core migrations. This retains real, transactional SQLite behavior while minimizing the persistence surface. Runs have a unique `(input_hash, adapter, adapter_version)` identity. Events are stored as an immutable aggregate JSON document; analysis revisions are append-only rows. This is a documented deviation from the plan's preferred EF Core implementation, not from SQLite.

All data stays local. Databases, raw/private traces, generated reports, model artifacts, dependency trees, and build/cache output are ignored by Git. A report says “Observed patterns are not causal proof.”
