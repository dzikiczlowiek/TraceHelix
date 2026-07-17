# Training data contract v1

TraceHelix training artifacts use JSON Schema draft 2020-12 and the matching Pydantic v2 models in `training/src/tracehelix_training/contracts.py`. Version `1.0.0` is intentionally closed: unknown properties are errors. Producers must emit a new schema version for incompatible changes.

## Files and lifecycle

- `training-candidate.schema.json` is the honest, unlabeled Task-2 export contract. A candidate identifies the run, current event, task group, lineage, bounded context and extraction/redaction provenance.
- `teacher-label.schema.json` is a standalone teacher decision. It includes the candidate's available event IDs so evidence can be checked without hidden state. Exactly one state is legal: accepted and non-abstaining with a complete label, or rejected and abstaining with no label and a quarantine reason.
- `training-example.schema.json` is either an accepted labeled candidate or a quarantined candidate. Accepted rows require evidence, reason, confidence and split. Quarantined rows have no label, confidence, evidence, reason or split, and require `quarantine_reason`.
- `dataset-manifest.schema.json` freezes artifact files and aggregate accounting.

The labels are exactly `Explore`, `Plan`, `Execute`, `Verify`, `Recover`, and `Unknown`. `Unknown` is a real accepted classification, not an abstention. Sources are exactly `fake`, `fixture`, and `live`. Label statuses are exactly `rule_derived`, `teacher_single`, `teacher_consensus`, `judge_resolved`, `synthetic_invariant`, and `quarantined`.

Teacher-derived accepted examples require model/revision, prompt version/hash, and request/response hashes. Rule-derived and synthetic accepted examples must leave those fields null. A quarantined example preserves complete teacher provenance when a teacher was contacted (including abstention, invalid evidence, low confidence, or retry exhaustion); its six teacher fields may instead all be null, but partial provenance is invalid. All context event IDs, available event IDs, and evidence IDs are unique. Evidence must refer to the current or supplied context set.

## Hashes and canonical JSON

Every hash field is a lowercase, zero-padded SHA-256 digest: exactly 64 characters in `[0-9a-f]`. It represents the digest as plain hexadecimal, without `sha256:`, whitespace, or base64.

`canonical_json_bytes` serializes UTF-8 with lexicographically sorted object keys, compact separators, Unicode preserved, and NaN/Infinity forbidden. Arrays retain order. Pydantic JSON-mode normalization runs before serialization. `example_id` is SHA-256 over exactly the canonical JSON object containing every `TrainingCandidate` field except `example_id`; label, teacher, timestamp, split, and quarantine fields inherited by `TrainingExample` are excluded. Thus a labeled row retains its candidate identity. Use `candidate_identity` or `construct_candidate` rather than inventing IDs. `manifest_id` is SHA-256 over canonical manifest content with the `manifest_id` property omitted; changing a file, count, version, or creation time therefore changes the identity.

`construct_candidate` is the canonical Python export constructor. It ignores any supplied `example_id`, applies the packaged `redaction-v1` policy deterministically to the complete raw candidate-content mapping, validates the first report, and only then normalizes the redacted mapping. It applies the policy again to that exact normalized payload, requires the second pass to be idempotent, and verifies that the second scan-passed report's version, configuration hash, input hash, and output hash bind the exact payload used to derive `example_id`. The export gate rejects other redaction versions and caller-injected reports. Direct `TrainingCandidate.model_validate` and JSON Schema validation remain backward-compatible data-validation APIs for the public `1.0.0` contract: they accept any nonempty `redaction_version`, enforce shape and identity, but do not run redaction and must not be described as an export gate. Code outside this Python constructor is not implicitly protected; other producers need an equivalent enforced boundary.

Creation times must be timezone-aware UTC; naive and non-UTC offsets are rejected and output is canonicalized to `Z`. Manifest file paths use a deliberately portable, nonempty, normalized relative POSIX subset. `.`, absolute paths, parent traversal, empty segments, backslashes, colons (including Windows ADS and drive forms), control characters, and components ending in a dot or space are rejected. Windows device components (`CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9`, and `LPT1`–`LPT9`) are forbidden case-insensitively, including when followed by an extension. Paths must also be unique under Unicode case folding (for example, `Data/x` and `data/X` conflict), and every manifest has at least one file. File hashes cover immutable file bytes, not parsed content.

## Manifest accounting

Counts are nonnegative and all defined keys must be present. `completed = accepted + quarantined` and `completed <= requested`. `retried_count` counts retry attempts, not unique examples, so it may exceed completed rows. `split_source_counts` contains every split×source accepted cell; each row equals its split count. Each source column plus `quarantined_source_counts` equals its source total, and quarantined-source cells total `quarantined_count`. Source and status counts total completed rows. Split and class counts total accepted rows because quarantined rows have neither split nor class. The `quarantined` status count equals `quarantined_count`.

## JSON Schema relational extension

Draft 2020-12 cannot express arbitrary cross-array membership, arithmetic sums, or a digest of the containing instance. Each schema therefore declares `x-tracehelix-invariants: true`. This is a required TraceHelix vocabulary extension, not decorative metadata. Python consumers must use `contract_validator(schema)`; it runs ordinary draft validation and the same Pydantic relational checks. Consumers that ignore unknown JSON Schema keywords validate only structural constraints and are **not** contract-conformant. Tests apply identical positive and negative fixtures to both validation paths.
