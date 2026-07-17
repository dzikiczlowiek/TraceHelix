# Automated LLM Distillation Pipeline Implementation Plan

> **For Hermes:** Use Pi for implementation task-by-task. Hermes independently verifies staged artifacts, tests, security, dataset lineage, costs, and model parity before each commit.

**Goal:** Build a fully automated, auditable pipeline that converts TraceHelix traces into a leakage-resistant labeled dataset using frontier cloud LLM teacher/judge roles, trains a small classifier, evaluates it separately on `fake`, `fixture`, and `live`, and exports a hash-verified ONNX model for C# inference—without requiring manual data preparation or labeling from the user.

**Architecture:** Deterministic local stages normalize, redact, extract, deduplicate, split, and manifest data. Frontier cloud LLM calls are isolated behind a budgeted, resumable teacher gateway with immutable request/response hashes; independent teacher/judge passes accept, retry, or quarantine labels. Training and evaluation consume only frozen manifests, while active learning selects high-value uncertain examples for later teacher calls. CI remains offline and uses fake providers/tiny local fixtures; every live-AI run requires a prior model/run/token/cost brief and explicit user approval.

**Tech Stack:** Python 3.11, uv, Pydantic, PyArrow/Parquet, Hugging Face Datasets/Transformers/PEFT, PyTorch, scikit-learn, sentence-transformers or MinHash for near-dedup, ONNX/Optimum/ONNX Runtime, JSON Schema, pytest, Ruff, mypy; existing C#/.NET 10 TraceHelix domain and ONNX Runtime integration.

---

## Non-negotiable invariants

1. The user does not manually prepare or label data.
2. Raw traces never enter public Git; valuable generated artifacts remain local/private outside Git.
3. Redaction runs before any cloud request or persisted training row.
4. `fake`, `fixture`, and `live` remain separate in manifests, splits, metrics, and reports.
5. Split is grouped by run/task/repository/agent lineage, never randomly by event.
6. Teacher labels are provenance-bearing claims, not ground truth. Every row records label origin/status.
7. Live-AI labeling/generation never runs in ordinary CI and never starts without a cost brief and explicit approval.
8. Interrupted runs resume from content-addressed cache without repeating paid calls.
9. Requested/completed/accepted/retried/quarantined counts are always reported.
10. No model is shipped without rule-baseline comparison, calibration results, and Python↔C# ONNX parity.

## Delivery sequence

- **Phase A — offline data plane:** Tasks 1–5.
- **Phase B — cloud teacher/judge:** Tasks 6–8, implemented with fake providers first; limited live smoke only after approval.
- **Phase C — student training:** Tasks 9–11.
- **Phase D — active learning and release gates:** Tasks 12–14.

---

### Task 1: Freeze automated dataset and labeling contracts

**Objective:** Define versioned schemas before implementing data movement or LLM calls.

**Files:**
- Create: `schemas/training-example.schema.json`
- Create: `schemas/teacher-label.schema.json`
- Create: `schemas/dataset-manifest.schema.json`
- Create: `training/src/tracehelix_training/contracts.py`
- Create: `training/tests/test_contracts.py`
- Create: `docs/training-data-contract.md`

**Required fields:**
- Identity: `example_id`, `run_id`, `event_id`, `task_group_id`, `lineage_id`.
- Input: `context_before`, `event_text`, `context_after`, `event_kind`, `tool_name`.
- Label: `label`, `label_status`, `confidence`, `evidence_event_ids`, `observable_reason`.
- Provenance: `source_category`, `source_hash`, `adapter`, `adapter_version`, `redaction_version`.
- Teacher: `teacher_model`, `teacher_revision`, `prompt_version`, `prompt_hash`, `request_hash`, `response_hash`.
- Governance: `license_or_consent`, `created_at`, `split`, `quarantine_reason`.

**Label statuses:** `rule_derived`, `teacher_single`, `teacher_consensus`, `judge_resolved`, `synthetic_invariant`, `quarantined`.

**TDD:** Write schema/model round-trip tests first; test unknown labels, missing provenance, invalid confidence, evidence IDs absent from context, and source-category mixing. Run `uv run pytest training/tests/test_contracts.py -v`.

**Acceptance:** JSON Schema and Pydantic validation agree; canonical JSON serialization is deterministic and hashable.

---

### Task 2: Export canonical examples from TraceHelix runs

**Objective:** Convert stored canonical events into deterministic training candidates without manual handling.

**Files:**
- Create: `training/src/tracehelix_training/extract.py`
- Create: `training/src/tracehelix_training/io.py`
- Create: `training/tests/test_extract.py`
- Modify: `src/TraceHelix.Cli/CliProgram.cs`
- Test: `tests/TraceHelix.Cli.Tests/TrainingExportTests.cs`

**CLI contract:**
```text
tracehelix dataset export --db tracehelix.db --out candidates.jsonl \
  --source-category fixture --context-before 4 --context-after 0
```

**Steps:**
1. Add failing C# real-process test for deterministic export order and complete provenance.
2. Export one row per event with fixed context-window semantics.
3. Support two explicit modes: `online` (`context_after=0`) and `offline-analysis` (bounded future context).
4. Reject missing source category/license metadata rather than guessing.
5. Hash canonical row content into `example_id`.

**Acceptance:** Two exports from the same DB are byte-identical; online examples contain no future events.

---

### Task 3: Deterministic secret/PII redaction before persistence or cloud use

**Objective:** Prevent secrets and private data from entering datasets or teacher requests.

**Files:**
- Create: `training/src/tracehelix_training/redact.py`
- Create: `training/configs/redaction-v1.yaml`
- Create: `training/tests/test_redact.py`
- Create: `docs/redaction-policy.md`

**Coverage:** API keys, bearer tokens, cookies, authorization headers, connection strings, `.env` values, private keys, emails, obvious phone numbers, user-home paths, and configurable project-specific patterns.

**Steps:**
1. Write failing table-driven tests with canary secrets in event text, nested payloads, paths, and context.
2. Implement deterministic placeholders such as `<REDACTED:API_KEY:1>`.
3. Emit counts by redaction type and `redaction_version`.
4. Add a post-redaction secret scanner that fails closed.
5. Ensure logs contain hashes/counts but never original secret values.

**Acceptance:** Canary corpus has zero recoverable secrets after redaction; reruns are byte-identical.

---

### Task 4: Exact/near deduplication and lineage-safe grouping

**Objective:** Remove duplicate evidence before splitting and teacher spending.

**Files:**
- Create: `training/src/tracehelix_training/deduplicate.py`
- Create: `training/src/tracehelix_training/lineage.py`
- Create: `training/tests/test_deduplicate.py`
- Create: `training/tests/test_lineage.py`

**Steps:**
1. Exact dedup by canonical example hash.
2. Near-dedup using deterministic normalized text fingerprints and a configured similarity threshold.
3. Group by `task_group_id`, repository, run lineage, agent family, and mutation parent.
4. Preserve a duplicate map rather than silently discarding lineage.
5. Report counts separately by `fake|fixture|live` and label class.

**Acceptance:** Known paraphrases/mutations stay in one lineage group; no near-duplicate crosses a later split.

---

### Task 5: Frozen group split and leakage audit

**Objective:** Produce reproducible train/validation/test manifests without event-level leakage.

**Files:**
- Create: `training/src/tracehelix_training/split.py`
- Create: `training/src/tracehelix_training/leakage.py`
- Create: `training/tests/test_split.py`
- Create: `training/tests/test_leakage.py`
- Create: `training/configs/split-v1.yaml`

**Steps:**
1. Write failing tests for shared run, task, repository lineage, and near-duplicate clusters.
2. Implement seeded group assignment with stable ordering.
3. Freeze test manifest before prompt/model selection.
4. Generate a leakage report with exact offending IDs on failure.
5. Keep source categories visible; never aggregate them away.

**Acceptance:** Same input/config yields the same manifest hash; any lineage crossing fails the pipeline.

---

### Task 6: Budgeted, resumable frontier-teacher gateway

**Objective:** Isolate all paid cloud labeling behind auditable controls and cache.

**Files:**
- Create: `training/src/tracehelix_training/teacher/base.py`
- Create: `training/src/tracehelix_training/teacher/openai_compatible.py`
- Create: `training/src/tracehelix_training/teacher/cache.py`
- Create: `training/src/tracehelix_training/teacher/budget.py`
- Create: `training/src/tracehelix_training/teacher/prompts.py`
- Create: `training/tests/teacher/test_cache.py`
- Create: `training/tests/teacher/test_budget.py`
- Create: `training/tests/teacher/test_fake_provider.py`
- Create: `training/configs/teacher-v1.yaml`

**Controls:**
- Provider/model/revision pinned in run manifest.
- Structured JSON response validated against `teacher-label.schema.json`.
- Content-addressed request cache outside Git.
- Retry only transient failures; malformed/semantic failures counted separately.
- Hard caps for requests, input tokens, output tokens, estimated cost, and concurrency.
- Dry-run prints requested calls/tokens/cost estimate without network.
- Resume skips completed request hashes.

**CI:** Fake provider only; tests assert zero network access.

**Live gate:** Before the first cloud call Hermes must present model(s), requested examples, max calls/tokens, expected cost, cache path, and abort conditions, then wait for explicit user approval.

---

### Task 7: Teacher labeling, independent judging, consensus, and quarantine

**Objective:** Automatically label examples without treating one LLM response as truth.

**Files:**
- Create: `training/src/tracehelix_training/label.py`
- Create: `training/src/tracehelix_training/judge.py`
- Create: `training/src/tracehelix_training/consensus.py`
- Create: `training/tests/test_label.py`
- Create: `training/tests/test_judge.py`
- Create: `training/tests/test_consensus.py`
- Create: `training/prompts/teacher-v1.md`
- Create: `training/prompts/judge-v1.md`

**Policy:**
1. Teacher returns label, confidence, evidence IDs, short observable reason, and abstain flag.
2. Deterministic validator verifies evidence IDs exist and reason does not introduce unseen facts.
3. Judge sees the redacted example and teacher result but must independently classify before comparing.
4. Agreement above configured confidence → `teacher_consensus`.
5. Disagreement → one bounded adjudication retry.
6. Persistent ambiguity → `quarantined`; never forced into `Unknown` merely to balance classes.

**Acceptance:** Fake-provider scenarios cover agreement, disagreement, invalid evidence, low confidence, abstention, malformed JSON, retry exhaustion, and resume.

---

### Task 8: Automated synthetic tasks, trace generation, and counterfactual mutations

**Objective:** Generate missing behavior coverage without manually authored datasets.

**Files:**
- Create: `training/src/tracehelix_training/generate_tasks.py`
- Create: `training/src/tracehelix_training/mutate.py`
- Create: `training/src/tracehelix_training/invariants.py`
- Create: `training/tests/test_mutate.py`
- Create: `training/tests/test_invariants.py`
- Create: `training/configs/generation-v1.yaml`

**Generation streams:**
- `fake`: direct schema-valid synthetic traces for deterministic tests.
- `fixture`: generated tasks executed in controlled disposable repositories with captured traces.
- `live`: real agent executions, always separately approved and counted.

**Counterfactuals:** Remove verification, move success before evidence, repeat planning, turn tool success into error, insert recovery attempts, redact tool names, truncate context.

**Metamorphic checks:** Mutations must satisfy declared relations (for example removing verification cannot increase verified evidence). Failed invariants are quarantined.

**Acceptance:** Generated and mutated descendants retain parent lineage and can never cross splits.

---

### Task 9: Dataset build, manifest, and quality report

**Objective:** Materialize a frozen, auditable dataset from accepted labels.

**Files:**
- Create: `training/src/tracehelix_training/prepare.py`
- Create: `training/src/tracehelix_training/manifest.py`
- Create: `training/src/tracehelix_training/report_dataset.py`
- Create: `training/tests/test_prepare.py`
- Create: `training/tests/test_manifest.py`
- Create: `schemas/dataset-quality-report.schema.json`

**Outputs outside Git:**
- `dataset/train.parquet`, `validation.parquet`, `test.parquet`.
- `dataset-manifest.json` with file SHA-256 values.
- `quality-report.json` with requested/completed/accepted/retried/quarantined counts.
- Per-class and per-source counts, duplication rate, redaction counts, teacher/judge agreement, and confidence distribution.

**Acceptance:** Build is atomic; interrupted build leaves no valid-looking partial manifest; manifest verification detects any byte change.

---

### Task 10: Rule baseline and student-model spike

**Objective:** Select the student architecture using evidence rather than assumption.

**Files:**
- Create: `training/src/tracehelix_training/baseline_rules.py`
- Create: `training/src/tracehelix_training/train.py`
- Create: `training/src/tracehelix_training/evaluate.py`
- Create: `training/src/tracehelix_training/metrics.py`
- Create: `training/configs/student-encoder.yaml`
- Create: `training/configs/student-smollm2.yaml`
- Create: `training/tests/test_train_smoke.py`
- Create: `training/tests/test_metrics.py`
- Create: `docs/model-card.md`

**Candidates:**
- Small encoder classifier suitable for straightforward ONNX export.
- `HuggingFaceTB/SmolLM2-135M` classification head with LoRA/QLoRA if interoperable.

**Evaluation:** Macro/micro/per-class F1, confusion matrix, ECE/calibration, selective coverage/accuracy, latency, memory, and metrics separately for `fake|fixture|live` and label status.

**CI:** Tiny local tokenizer/model fixture, 16 synthetic examples, two optimization steps, no model download.

**Acceptance:** Student must beat or complement the deterministic rules baseline on the frozen validation set without degrading held-out source categories beyond declared tolerances.

---

### Task 11: ONNX export, signed manifest, and Python↔C# parity

**Objective:** Ship only a model whose production inference matches training inference.

**Files:**
- Create: `training/src/tracehelix_training/export_onnx.py`
- Create: `training/src/tracehelix_training/validate_onnx.py`
- Create: `training/src/tracehelix_training/model_manifest.py`
- Create: `schemas/model-manifest.schema.json`
- Create: `training/tests/test_onnx_parity.py`
- Create: `src/TraceHelix.Infrastructure/Classification/OnnxStepClassifier.cs`
- Create: `tests/TraceHelix.IntegrationTests/OnnxParityTests.cs`

**Manifest:** Model/tokenizer/config/label-map hashes, input contract, max length, preprocessing version, calibration parameters, dataset manifest hash, metric report hash.

**Acceptance:** Golden corpus has matching token IDs, identical top-1 labels, and logits within documented tolerance. Missing/hash-invalid model triggers explicit rules fallback warning.

---

### Task 12: Active-learning and cost-efficient relabel loop

**Objective:** Spend future teacher budget only on high-value examples.

**Files:**
- Create: `training/src/tracehelix_training/select_active.py`
- Create: `training/src/tracehelix_training/active_loop.py`
- Create: `training/tests/test_select_active.py`
- Create: `training/tests/test_active_loop.py`
- Create: `training/configs/active-v1.yaml`

**Selection signals:** Student entropy/margin, student–rules disagreement, student–teacher disagreement, teacher–judge disagreement, unseen tools/task groups, class/source undercoverage, calibration failures.

**Guards:** Per-group caps, duplicate exclusion, frozen-test exclusion, deterministic ranking, budget simulation, resumable rounds.

**Acceptance:** Dry-run produces a stable ranked request manifest and cost estimate without network; selected examples never include frozen test rows.

---

### Task 13: CLI orchestration and operational runbook

**Objective:** Make the full process executable without hand-editing files.

**Files:**
- Create: `training/src/tracehelix_training/cli.py`
- Modify: `training/pyproject.toml`
- Create: `docs/training-runbook.md`
- Modify: `Makefile`
- Modify: `README.md`

**Commands:**
```text
tracehelix-train collect
tracehelix-train redact
tracehelix-train dedup
tracehelix-train split
tracehelix-train label --dry-run
tracehelix-train label --resume
tracehelix-train prepare
tracehelix-train train
tracehelix-train evaluate
tracehelix-train export-onnx
tracehelix-train validate
tracehelix-train active-select --dry-run
```

**Acceptance:** Every command supports `--config`, `--workdir`, `--json`, cancellation, resumable state, and nonzero typed failures. Stdout JSON contains results; diagnostics go to stderr.

---

### Task 14: CI, adversarial verification, and release gate

**Objective:** Prevent dataset/model claims from outrunning evidence.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Create: `.github/workflows/model-eval.yml`
- Create: `scripts/verify-training-offline.sh`
- Create: `scripts/verify-model-artifact.py`
- Create: `docs/training-threat-model.md`

**Ordinary CI:** Schemas, redaction canaries, dedup/split/leakage, fake teacher/judge, tiny train smoke, metric tests, ONNX fixture parity, secret scan. No cloud calls or model downloads.

**Manual model workflow:** Accepts a private model/dataset artifact, verifies manifests/hashes, evaluates frozen splits, publishes metrics only—not checkpoints or raw data.

**Final DoD:**
- Rebuild from frozen manifests is reproducible.
- No split leakage or unredacted canary.
- Requested/completed and source categories are explicit.
- Teacher/judge provenance is complete.
- Rule baseline and student reports are side-by-side.
- ONNX parity passes in Python and C#.
- Live results are never inferred from fake/fixture metrics.
- A fresh independent reviewer validates exact artifacts before model registration.

---

## First live-AI approval brief

Before Task 7 or Task 8 makes real cloud calls, Hermes must present:

1. Teacher and judge provider/model/revision.
2. Source categories and exact requested example counts.
3. Maximum request/input/output token counts and concurrency.
4. Current provider pricing source and worst-case/expected cost.
5. Cache/work directory and resume semantics.
6. Redaction and secret-scan evidence.
7. Abort thresholds for malformed output, disagreement, cost, and provider errors.
8. Explicit statement that ordinary CI remains offline.

No paid call proceeds until the user approves this brief.

## Implementation order from current repository state

1. Finish final review, commit, push, and remote CI for the currently staged API/UI slice.
2. Implement Tasks 1–5 offline and independently verify artifacts.
3. Implement Tasks 6–8 entirely against fake providers and fixtures.
4. Produce the first live-AI approval brief; run a small teacher/judge smoke only after approval.
5. Build the first frozen dataset manifest.
6. Implement Tasks 9–11 and compare student candidates.
7. Add Tasks 12–14 after the first verified student exists.
