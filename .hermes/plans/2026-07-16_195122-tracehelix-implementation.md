# TraceHelix Implementation Plan

> **For Hermes:** Implement this plan task-by-task using Pi as the coding agent, with Hermes orchestrating and independently verifying artifacts, tests, and claims.

**Goal:** Zbudować lokalne, audytowalne narzędzie do importu, klasyfikowania, analizowania i porównywania śladów wykonania agentów AI, dostępne jako CLI oraz web UI, z rdzeniem i API w C#, interfejsem w React oraz pipeline’em treningowym małego modelu językowego w Pythonie.

**Architecture:** Monorepo z modularnym monolitem .NET: współdzielony model domenowy i przypadki użycia obsługują CLI oraz ASP.NET Core API. Adaptery wejściowe normalizują różne formaty trace do kanonicznego event schema, reguły deterministyczne i opcjonalny model ONNX klasyfikują kroki, a detektory sekwencji generują alerty zawsze powiązane z konkretnymi eventami źródłowymi. React korzysta wyłącznie z wersjonowanego API i prezentuje „DNA runu”, porównanie dwóch wykonań oraz dowody stojące za predykcjami.

**Tech Stack:** .NET 10, C# 14, ASP.NET Core Minimal APIs, `System.CommandLine`, SQLite + EF Core, ONNX Runtime, xUnit + FluentAssertions; React 19, TypeScript, Vite, TanStack Query, Zustand, D3 scales/zoom (renderowanie SVG/Canvas bez pełnego D3 DOM), Vitest, Testing Library, Playwright; Python 3.11, uv, PyTorch, Transformers, PEFT/LoRA, Datasets, Evaluate/scikit-learn, Optimum ONNX, MLflow opcjonalnie lokalnie; GitHub Actions.

---

## 1. Zakres MVP i zasady produktu

### MVP obejmuje

1. Import rzeczywistego trace z co najmniej jednego formatu `pi_tar` i jednego ogólnego formatu JSONL; następne adaptery (`sloopi`, Codex) korzystają z tego samego kontraktu.
2. Zachowanie surowego wejścia albo jego adresowalnej kopii oraz mapowanie każdego kanonicznego eventu do lokalizacji źródłowej.
3. Klasyfikację kroków do: `Explore`, `Plan`, `Execute`, `Verify`, `Recover`, `Unknown`.
4. Deterministyczny baseline klasyfikacji i opcjonalny klasyfikator ONNX trenowany w Pythonie.
5. Detektory: pętla bez postępu, nadmierne planowanie, brak weryfikacji, przedwczesne ogłoszenie sukcesu, burza recovery i kaskada błędów narzędzi.
6. CLI do importu, analizy, listowania, inspekcji, porównania, eksportu raportu i uruchomienia UI/API.
7. React UI: lista runów, widok genomu/timeline, panel źródłowego eventu i artefaktów, alerty oraz porównanie dwóch runów.
8. Raport JSON i samowystarczalny HTML z pełną informacją o pochodzeniu ustaleń.
9. Ewaluację na oddzielonych danych `fake`, `fixture`, `live`; raportowanie requested/completed i bez łączenia tych kategorii.

### Poza MVP

- Trace ingestion w czasie rzeczywistym, multi-user SaaS, zdalna telemetria i logowanie użytkowników.
- Automatyczne twierdzenia przyczynowe; alert opisuje obserwowany wzorzec i dowody, nie „powód” porażki.
- Trenowanie modelu z poziomu aplikacji C# lub przeglądarki.
- Wysyłanie prywatnych trace do usług zewnętrznych.
- Generatywne podsumowania jako źródło prawdy.

### Niezmienne zasady audytowalności

- Każda klasyfikacja ma `classifier_id`, `classifier_version`, `confidence`, `evidence_event_ids` oraz wskazanie fragmentu źródłowego.
- Każdy alert ma stabilny kod, parametry detektora, zakres eventów, wynik i dowody.
- Surowe dane, kanoniczne eventy, predykcje i raport mają SHA-256.
- Wyniki heurystyki i modelu nie nadpisują się; są osobnymi analysis revisions.
- UI rozróżnia fakt źródłowy, regułę, predykcję modelu i interpretację.
- Brak dowodu jest widoczny jako `Unknown`/`NotEvaluated`, nie jako wynik negatywny.

---

## 2. Docelowa struktura repozytorium

```text
TraceHelix/
├── .config/dotnet-tools.json
├── .github/workflows/ci.yml
├── .github/workflows/model-eval.yml
├── .hermes/plans/
├── artifacts/.gitkeep
├── docs/
│   ├── architecture.md
│   ├── event-schema.md
│   ├── model-card.md
│   ├── threat-model.md
│   └── verification.md
├── schemas/
│   ├── trace-event.schema.json
│   ├── analysis-report.schema.json
│   └── model-manifest.schema.json
├── samples/
│   ├── generic-jsonl/minimal.jsonl
│   └── expected/minimal-report.json
├── src/
│   ├── TraceHelix.Domain/
│   ├── TraceHelix.Application/
│   ├── TraceHelix.Infrastructure/
│   ├── TraceHelix.Api/
│   └── TraceHelix.Cli/
├── tests/
│   ├── TraceHelix.Domain.Tests/
│   ├── TraceHelix.Application.Tests/
│   ├── TraceHelix.Infrastructure.Tests/
│   ├── TraceHelix.Api.Tests/
│   ├── TraceHelix.Cli.Tests/
│   └── TraceHelix.IntegrationTests/
├── training/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── configs/smol-lora.yaml
│   ├── src/tracehelix_training/
│   │   ├── prepare.py
│   │   ├── split.py
│   │   ├── train.py
│   │   ├── evaluate.py
│   │   ├── export_onnx.py
│   │   ├── validate_onnx.py
│   │   └── model_manifest.py
│   └── tests/
├── web/
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   ├── features/runs/
│   │   ├── features/genome/
│   │   ├── features/compare/
│   │   └── routes/
│   ├── e2e/
│   └── package.json
├── Directory.Build.props
├── Directory.Packages.props
├── TraceHelix.slnx
├── global.json
├── Makefile
├── README.md
├── LICENSE
└── .gitignore
```

`artifacts/`, bazy SQLite, surowe trace, checkpointy, pliki ONNX i raporty live pozostają poza Git; repo zawiera jedynie małe syntetyczne fixtures, manifesty, schematy i sumy kontrolne.

---

## 3. Kontrakty domenowe

### Kanoniczny event

Plik: `src/TraceHelix.Domain/Traces/TraceEvent.cs`

```csharp
public sealed record TraceEvent(
    Guid Id,
    Guid RunId,
    long Sequence,
    DateTimeOffset Timestamp,
    TraceEventKind Kind,
    string Actor,
    string? Summary,
    JsonElement Payload,
    SourceReference Source,
    IReadOnlyList<ArtifactReference> Artifacts,
    string ContentSha256);

public sealed record SourceReference(
    string Adapter,
    string InputSha256,
    string RelativePath,
    long? ByteOffset,
    int? Line,
    string? JsonPointer);
```

`TraceEventKind`: `Message`, `Reasoning`, `ToolCall`, `ToolResult`, `FileChange`, `Artifact`, `Status`, `Error`, `Unknown`.

### Klasyfikacja kroku

Plik: `src/TraceHelix.Domain/Analysis/StepClassification.cs`

```csharp
public enum StepLabel { Explore, Plan, Execute, Verify, Recover, Unknown }

public sealed record StepClassification(
    Guid EventId,
    StepLabel Label,
    float Confidence,
    string ClassifierId,
    string ClassifierVersion,
    IReadOnlyList<Guid> EvidenceEventIds,
    IReadOnlyDictionary<string, float>? Scores);
```

### Alert wzorca

Plik: `src/TraceHelix.Domain/Analysis/PatternAlert.cs`

```csharp
public sealed record PatternAlert(
    string Code,
    AlertSeverity Severity,
    long StartSequence,
    long EndSequence,
    string DetectorVersion,
    IReadOnlyDictionary<string, string> Parameters,
    IReadOnlyList<Guid> EvidenceEventIds,
    string Explanation);
```

Kody MVP: `THX001_NO_PROGRESS_LOOP`, `THX002_PLAN_LOOP`, `THX003_VERIFICATION_GAP`, `THX004_PREMATURE_SUCCESS`, `THX005_RECOVERY_STORM`, `THX006_TOOL_ERROR_CASCADE`.

### Manifest modelu

Pliki: `schemas/model-manifest.schema.json`, `src/TraceHelix.Domain/Models/ModelManifest.cs`.

Pola obowiązkowe: nazwa i wersja modelu bazowego, commit treningu, hash datasetu i splitu, etykiety w kolejności, tokenizer files i ich hashe, ONNX hash/opset, max sequence length, preprocessing contract, metryki per split/category, data utworzenia, znane ograniczenia.

---

## 4. Plan implementacji

### Task 1: Bootstrap monorepo i powtarzalny toolchain

**Objective:** Utworzyć rozwiązanie, projekty, wspólne ustawienia kompilatora i komendy developerskie bez implementacji domeny.

**Files:**
- Create: `global.json`, `TraceHelix.slnx`, `Directory.Build.props`, `Directory.Packages.props`
- Create: wszystkie projekty `src/*/*.csproj` i `tests/*/*.csproj`
- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`
- Create: `training/pyproject.toml`
- Create: `.gitignore`, `Makefile`, `README.md`, `LICENSE`

**Steps:**
1. Dodać test smoke `tests/TraceHelix.Domain.Tests/SmokeTests.cs`, uruchomić `dotnet test` i potwierdzić oczekiwany błąd przed utworzeniem solution.
2. Utworzyć projekty na `net10.0`, włączyć nullable, warnings-as-errors, analyzers i central package management.
3. Utworzyć React + TypeScript przez Vite oraz Python package zarządzany przez uv.
4. Dodać komendy `make restore`, `make build`, `make test`, `make lint`.
5. Uruchomić `dotnet build TraceHelix.slnx`, `npm --prefix web run build`, `uv run --project training pytest`.
6. Commit: `chore: bootstrap TraceHelix monorepo`.

**Acceptance:** trzy toolchainy budują się offline po jednorazowym restore; repo nie zawiera wygenerowanych artefaktów.

### Task 2: Kanoniczny event schema i walidacja

**Objective:** Zamrozić wersjonowany format wymiany danych przed implementacją adapterów.

**Files:**
- Create: `schemas/trace-event.schema.json`
- Create: `src/TraceHelix.Domain/Traces/*.cs`
- Test: `tests/TraceHelix.Domain.Tests/Traces/TraceEventTests.cs`
- Create: `docs/event-schema.md`

**Steps:**
1. Testy RED dla sekwencji ujemnej, pustego hasha, niepoprawnej referencji źródłowej i serializacji round-trip.
2. Implementacja value objects i walidacji przy tworzeniu.
3. Walidacja przykładu `samples/generic-jsonl/minimal.jsonl` przeciw JSON Schema.
4. Golden test stabilnej serializacji.
5. Commit: `feat(domain): define canonical trace event schema`.

**Acceptance:** event zawsze wskazuje źródło; serializacja nie zależy od bieżącej kultury ani kolejności słowników.

### Task 3: Model runu, rewizji analizy i raportu

**Objective:** Rozdzielić niezmienne dane importu od wielokrotnie wykonywanych analiz.

**Files:**
- Create: `src/TraceHelix.Domain/Runs/TraceRun.cs`
- Create: `src/TraceHelix.Domain/Analysis/AnalysisRevision.cs`
- Create: `src/TraceHelix.Domain/Analysis/{StepClassification,PatternAlert}.cs`
- Create: `schemas/analysis-report.schema.json`
- Test: `tests/TraceHelix.Domain.Tests/Analysis/*Tests.cs`

**Steps:** testy niezmienności, statusów `Pending/Completed/Failed`, identyfikacji classifier/detector version, evidence IDs i hashy; implementacja; golden schema test; commit `feat(domain): model auditable analysis revisions`.

### Task 4: Porty aplikacyjne i przypadki użycia

**Objective:** Uniezależnić CLI/API od SQLite, parserów i ONNX.

**Files:**
- Create: `src/TraceHelix.Application/Abstractions/ITraceAdapter.cs`
- Create: `ITraceRepository.cs`, `IAnalysisRepository.cs`, `IStepClassifier.cs`, `IPatternDetector.cs`, `IReportWriter.cs`
- Create: `ImportTrace.cs`, `AnalyzeRun.cs`, `CompareRuns.cs`, `ExportReport.cs`
- Test: `tests/TraceHelix.Application.Tests/*`

**Steps:**
1. Testy przypadków użycia na in-memory fakes: duplicate import, partial parse failure, analiza bez eventów, niezgodne rewizje porównania.
2. Minimalne handlery z `CancellationToken` i jawnie typowanymi rezultatami.
3. Jedno miejsce orkiestracji classifier → detectors → immutable revision.
4. Commit: `feat(application): add trace analysis use cases`.

### Task 5: Generic JSONL adapter i provenance

**Objective:** Dostarczyć referencyjny, udokumentowany import line-by-line.

**Files:**
- Create: `src/TraceHelix.Infrastructure/Adapters/GenericJsonlAdapter.cs`
- Create: `src/TraceHelix.Infrastructure/Hashing/Sha256ContentHasher.cs`
- Test: `tests/TraceHelix.Infrastructure.Tests/Adapters/GenericJsonlAdapterTests.cs`
- Fixtures: `samples/generic-jsonl/*`

**Steps:** testy RED dla poprawnego pliku, CRLF, Unicode, pustej linii, błędnego JSON, przerwanego streamu i stabilnych offsetów; streaming parser bez wczytywania całości; hash pliku i każdego eventu; import diagnostics zamiast cichego pomijania; commit `feat(import): add provenance-preserving JSONL adapter`.

### Task 6: Adapter `pi_tar`

**Objective:** Zaimportować pierwszy realny format projektu bez sprzęgania domeny z formatem producenta.

**Files:**
- Create: `src/TraceHelix.Infrastructure/Adapters/PiTar/*`
- Test: `tests/TraceHelix.Infrastructure.Tests/Adapters/PiTar/*`
- Create: `docs/adapters/pi-tar.md`

**Steps:**
1. Najpierw zinwentaryzować 3–5 prawdziwych, prywatnych trace lokalnie; zapisać wyłącznie zanonimizowaną specyfikację wariantów.
2. Dodać ręcznie zbudowane fixtures reprezentujące warianty, bez kopiowania sekretów i prywatnych promptów.
3. Test contract wspólny dla wszystkich adapterów.
4. Parser odporny na nieznane rekordy; unknown zachowany w payload, nie odrzucony.
5. Test „source click-through”: każdy event wraca do dokładnej lokalizacji w archiwum/pliku.
6. Commit: `feat(import): support pi_tar traces`.

### Task 7: SQLite persistence i migracje

**Objective:** Zapewnić lokalną, transakcyjną bazę runów i rewizji.

**Files:**
- Create: `src/TraceHelix.Infrastructure/Persistence/TraceHelixDbContext.cs`
- Create: `Persistence/Configurations/*.cs`, `Persistence/Repositories/*.cs`
- Create: pierwsza migracja EF Core
- Test: `tests/TraceHelix.Infrastructure.Tests/Persistence/*`

**Steps:** testy z prawdziwym tymczasowym SQLite (nie EF InMemory), unikalność `(input_hash, adapter, adapter_version)`, transakcja importu, kaskadowanie tylko tam gdzie jawnie wymagane, indeksy po run/sequence/kodzie alertu; commit `feat(storage): persist runs and analysis revisions in SQLite`.

### Task 8: Deterministyczny klasyfikator baseline

**Objective:** Udostępnić działający, wyjaśnialny baseline przed modelem ML.

**Files:**
- Create: `src/TraceHelix.Infrastructure/Classification/RuleBasedStepClassifier.cs`
- Create: `Classification/Rules/*.cs`
- Test: `tests/TraceHelix.Infrastructure.Tests/Classification/RuleBasedStepClassifierTests.cs`

**Rules:** tool read/search → zwykle `Explore`; jawne plan/checklist → `Plan`; write/patch/command z mutacją → `Execute`; test/build/hash/read-back/health-check → `Verify`; retry/rollback/fix po błędzie → `Recover`; konflikt sygnałów lub brak kontekstu → `Unknown` z obniżoną confidence.

**Steps:** table-driven tests, negacje i przypadki konfliktowe; reguły zwracają feature evidence; brak dopasowania nie używa losowania; commit `feat(classification): add explainable rule baseline`.

### Task 9: Detektory sekwencji

**Objective:** Wykryć jawne, wersjonowane wzorce bez zależności od modelu.

**Files:**
- Create: `src/TraceHelix.Infrastructure/Detection/*.cs`
- Test: `tests/TraceHelix.Infrastructure.Tests/Detection/*Tests.cs`

**Definicje początkowe:**
- `NO_PROGRESS_LOOP`: powtarzający się znormalizowany tool call/output lub brak zmiany hashy artefaktów w oknie N kroków.
- `PLAN_LOOP`: co najmniej N etykiet `Plan` bez `Execute`/`Verify`.
- `VERIFICATION_GAP`: sukces końcowy bez dowodu weryfikacji po ostatniej mutacji.
- `PREMATURE_SUCCESS`: deklaracja sukcesu poprzedzająca błąd lub niezgodny rezultat w krótkim oknie.
- `RECOVERY_STORM`: co najmniej N przejść do `Recover` w oknie M.
- `TOOL_ERROR_CASCADE`: kolejne błędy tool calls bez udokumentowanej zmiany strategii.

Każdy detector ma test pozytywny, graniczny i negatywny, jawne domyślne parametry i dowody. Commit: `feat(detection): add auditable sequence detectors`.

### Task 10: CLI end-to-end

**Objective:** Udostępnić pełny workflow bez web UI.

**Files:**
- Create: `src/TraceHelix.Cli/Program.cs`
- Create: `Commands/{Import,Analyze,List,Show,Compare,Report,Serve}Command.cs`
- Test: `tests/TraceHelix.Cli.Tests/*`

**Kontrakt komend:**

```bash
tracehelix import ./run.tar --adapter pi-tar --db ./tracehelix.db --json
tracehelix analyze <run-id> --classifier rules --detectors default --json
tracehelix list --db ./tracehelix.db
tracehelix show <run-id> --events --alerts
tracehelix compare <run-a> <run-b> --json
tracehelix report <run-id> --format json --out report.json
tracehelix report <run-id> --format html --out report.html
tracehelix serve --db ./tracehelix.db --urls http://127.0.0.1:5180
```

**Steps:** parser tests bez uruchamiania procesu; integration tests uruchamiają rzeczywisty binary na fixture; stdout zawiera wyłącznie wynik, diagnostyka trafia na stderr; stabilne exit codes `0 success`, `2 usage`, `3 import`, `4 analysis`, `5 storage`; commit `feat(cli): expose import analysis compare and report workflows`.

### Task 11: Wersjonowane ASP.NET Core API

**Objective:** Udostępnić Reactowi ten sam application layer co CLI.

**Files:**
- Create: `src/TraceHelix.Api/Program.cs`
- Create: `Endpoints/{Runs,Analysis,Compare,Artifacts}.cs`
- Create: `Contracts/V1/*.cs`
- Test: `tests/TraceHelix.Api.Tests/*`

**Endpoints MVP:**
- `POST /api/v1/imports` multipart lub path tylko w trybie trusted-local.
- `GET /api/v1/runs`, `GET /api/v1/runs/{id}`.
- `GET /api/v1/runs/{id}/events?cursor=&limit=`.
- `POST /api/v1/runs/{id}/analyses`.
- `GET /api/v1/analyses/{id}` i `/alerts`.
- `GET /api/v1/compare?left=&right=&analysisLeft=&analysisRight=`.
- `GET /api/v1/events/{id}/source` z ograniczeniem do katalogu importów.
- `GET /health/live`, `GET /health/ready`.

**Security:** bind domyślnie do loopback; żadnego dowolnego odczytu ścieżki; canonical path check; limit uploadu; redakcja wyjątków. OpenAPI snapshot test i WebApplicationFactory integration tests. Commit `feat(api): expose local versioned HTTP API`.

### Task 12: Kontrakt porównania dwóch runów

**Objective:** Zestawić przebiegi o różnej długości bez udawania semantycznej równoważności.

**Files:**
- Create: `src/TraceHelix.Application/Comparison/*`
- Test: `tests/TraceHelix.Application.Tests/Comparison/*`

**Approach:** najpierw metryki niezależne (udział klas, liczba tool calls/errors/verifications, alerty, czas), potem opcjonalne wyrównanie sekwencji według milestone/artifact hash/tool signature. UI oznacza luki i niepewne dopasowania. Testy: identyczne runy, różne długości, brak timestampów, przesunięta sekwencja, brak wspólnych kotwic. Commit `feat(compare): add evidence-aware run comparison`.

### Task 13: React shell, typed API i routing

**Objective:** Utworzyć dostępny shell aplikacji i klienta API generowanego z OpenAPI.

**Files:**
- Create: `web/src/api/generated/*`, `web/src/api/client.ts`
- Create: `web/src/routes/{Runs,RunDetail,Compare}.tsx`
- Create: `web/src/components/{AppShell,ErrorBoundary,LoadingState}.tsx`
- Test: `web/src/**/*.test.tsx`

**Steps:** wygenerować TS client w CI i testować brak diffu; TanStack Query dla server state, Zustand tylko dla lokalnej selekcji/zoom; keyboard navigation i focus states; MSW do testów; commit `feat(web): add React shell and typed API client`.

### Task 14: Lista runów i import

**Objective:** Pozwolić wybrać/importować run i widzieć stan przetwarzania.

**Files:**
- Create: `web/src/features/runs/RunList.tsx`, `ImportTraceDialog.tsx`, `RunSummaryCard.tsx`
- Test: odpowiadające testy komponentów i `web/e2e/import.spec.ts`

**Acceptance:** drag/drop, wybór adaptera lub autodetect, wyświetlenie diagnostics, brak „success” przed odpowiedzią API i odczytem zaimportowanego runu; commit `feat(web): add trace import and run browser`.

### Task 15: Wizualizacja „DNA runu”

**Objective:** Pokazać sekwencję kroków, alerty i dowody przy tysiącach eventów.

**Files:**
- Create: `web/src/features/genome/{GenomeView,GenomeTrack,GenomeLegend,GenomeTooltip,EvidencePanel}.tsx`
- Create: `web/src/features/genome/genomeLayout.ts`
- Test: unit layout tests, component interaction tests, Playwright screenshot baseline

**Design:** jeden segment na event; kolor klasy, jasność confidence, obramowanie alertu; osobne tracki tool/error/artifact; zoom/pan; wirtualizacja/Canvas powyżej ustalonego progu, dostępna tabela jako alternatywa. Klik segmentu otwiera źródło, payload, klasyfikator, scores i evidence. Nie polegać wyłącznie na kolorze. Commit `feat(web): visualize auditable execution genome`.

### Task 16: UI porównania i heatmapa wzorców

**Objective:** Porównać dwa runy bez zacierania różnic i niepewności wyrównania.

**Files:**
- Create: `web/src/features/compare/{RunPicker,ComparisonView,MetricDelta,AlignedGenome,PatternHeatmap}.tsx`
- Test: component i E2E `web/e2e/compare.spec.ts`

**Acceptance:** niezależne osie pozostają dostępne obok aligned view; delta pokazuje mianownik; tooltip wskazuje raw counts; niepewne alignment oznaczone; deep link przechowuje ID runów i rewizji analizy. Commit `feat(web): add two-run comparison and pattern heatmap`.

### Task 17: Audytowalny eksport raportu

**Objective:** Wygenerować JSON zgodny ze schematem i samowystarczalny HTML.

**Files:**
- Create: `src/TraceHelix.Infrastructure/Reporting/{JsonReportWriter,HtmlReportWriter}.cs`
- Test: `tests/TraceHelix.Infrastructure.Tests/Reporting/*`
- Fixture: `samples/expected/minimal-report.json`

**Acceptance:** raport zawiera hash inputu, wersje adaptera/classifier/detectors, status analizy, pełne evidence refs, wszystkie ostrzeżenia i jawny disclaimer „observed pattern, not causal proof”; golden tests normalizują tylko timestamp wygenerowania; commit `feat(reporting): export self-contained auditable reports`.

### Task 18: Przygotowanie i kontrola jakości datasetu

**Objective:** Zbudować reprodukowalny dataset bez leakage między runami.

**Files:**
- Create: `training/src/tracehelix_training/{prepare,split}.py`
- Create: `training/tests/test_prepare.py`, `test_split.py`
- Create: `training/configs/smol-lora.yaml`
- Create: `docs/model-card.md`

**Dataset row:** `example_id`, `run_id`, `event_id`, `context_before`, `event_text`, `context_after`, `event_kind`, `tool_name`, `label`, `annotator`, `source_category` (`fake|fixture|live`), `source_hash`, `license/consent`, `redaction_version`.

**Steps:**
1. Walidacja etykiet i source provenance.
2. Redakcja sekretów przed utrwaleniem danych treningowych.
3. Split wyłącznie po `run_id`/grupie tasku, nigdy losowo po eventach.
4. Dedup exact + near-duplicate przed splitem.
5. Osobne statystyki fake/fixture/live i hash manifestu datasetu.
6. Tests deterministycznego splitu i leakage guard.
7. Commit `feat(training): build leakage-resistant trace dataset`.

### Task 19: Trening małego modelu językowego w Pythonie

**Objective:** Fine-tunować mały model do klasyfikacji kroku, zachowując możliwość eksportu do C#.

**Files:**
- Create: `training/src/tracehelix_training/train.py`
- Create: `training/src/tracehelix_training/evaluate.py`
- Create: `training/tests/test_train_smoke.py`, `test_metrics.py`

**Model bazowy MVP:** `HuggingFaceTB/SmolLM2-135M` z `AutoModelForSequenceClassification`, LoRA/QLoRA zależnie od hardware; 6 etykiet i stały prompt/template wejściowy. Jeśli eksport klasyfikacyjnej głowy tego checkpointu okaże się niestabilny, spike porównuje mały encoder (np. ModernBERT-base) i dokumentuje trade-off; nie zmieniać modelu bez raportu interoperacyjności ONNX.

**Steps:**
1. CPU smoke na 16 syntetycznych przykładach, 2 kroki, bez pobierania w zwykłym CI (tiny local test model).
2. Seed dla Python/NumPy/PyTorch; zapis pełnej konfiguracji i wersji bibliotek.
3. Weighted loss lub sampler tylko po raporcie rozkładu klas.
4. Early stopping wg macro-F1 na validation; test pozostaje zamknięty do końca.
5. Metryki: macro/micro/per-class F1, confusion matrix, ECE/calibration, coverage przy progach confidence.
6. Raport metryk osobno dla `fake`, `fixture`, `live`; requested/completed counts.
7. Zapis adapter weights/checkpointu lokalnie poza Git oraz manifestu z hashami.
8. Commit `feat(training): fine-tune small step classifier with LoRA`.

**Ważne:** pełny live trening wymaga wcześniejszego ostrzeżenia użytkownika, jawnego budżetu i frontierowych modeli chmurowych tylko wtedy, gdy etykietowanie/teacher model jest rzeczywiście używane. Pipeline treningowy sam w sobie działa lokalnie lub na wybranym compute bez wysyłania trace.

### Task 20: Eksport ONNX i parity Python ↔ C#

**Objective:** Udowodnić, że model produkcyjny daje zgodne wyniki w Pythonie i .NET.

**Files:**
- Create: `training/src/tracehelix_training/{export_onnx,validate_onnx,model_manifest}.py`
- Create: `schemas/model-manifest.schema.json`
- Create: `src/TraceHelix.Infrastructure/Classification/OnnxStepClassifier.cs`
- Test: `training/tests/test_onnx_parity.py`
- Test: `tests/TraceHelix.IntegrationTests/OnnxParityTests.cs`

**Steps:** eksport Optimum ONNX do statycznego kontraktu wejść; dołączenie tokenizera i label map; Python parity logits/top-1 na golden corpus; C# ONNX Runtime ładuje manifest, sprawdza hashe i tokenizuje identycznie; tolerance test logits oraz identyczne top-1; fallback do rules przy braku/niezgodnym modelu, ale z jawnym warningiem; commit `feat(model): run verified ONNX classifier in .NET`.

### Task 21: Ryzyko następnego kroku jako eksperyment, nie część krytyczna MVP

**Objective:** Ocenić opcjonalną predykcję failure onset bez mieszania jej z klasyfikacją kroku.

**Files:**
- Create: `training/src/tracehelix_training/train_risk.py`
- Create: `training/src/tracehelix_training/evaluate_risk.py`
- Create: `docs/experiments/next-step-risk.md`

**Gate:** wdrożyć tylko jeśli zdefiniowano failure onset z wysoką zgodnością annotatorów i baseline przewyższa prostą heurystykę. Metryki: precision alertów, PR-AUC, false alerts/run, lead time do onset. Predykcja zawsze wskazuje okno eventów wejściowych. Bez spełnienia gate pozostaje eksperymentem offline.

### Task 22: Testy integracyjne na realnym procesie

**Objective:** Zweryfikować rzeczywiste granice procesu, pliki i HTTP zamiast wyłącznie mocków.

**Files:**
- Create: `tests/TraceHelix.IntegrationTests/EndToEndWorkflowTests.cs`
- Create: `web/e2e/full-workflow.spec.ts`
- Create: `scripts/verify-release.sh`

**Scenario:** fresh temp dir → CLI import fixture → analyze rules → uruchom API na losowym porcie → UI pobiera run → klik event/source → compare → export → walidacja JSON Schema i hashy. Dodatkowo malformed trace, brak modelu, uszkodzony manifest, przerwany import, port zajęty. Commit `test: verify full TraceHelix workflow across real processes`.

### Task 23: CI, bezpieczeństwo i supply chain

**Objective:** Wymusić jakość bez wykonywania kosztownego treningu w zwykłym CI.

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/workflows/model-eval.yml`
- Create: `docs/threat-model.md`
- Modify: `Makefile`, `README.md`

**CI jobs:** dotnet format/build/test; npm ci/lint/typecheck/test/build; uv sync --locked/ruff/mypy/pytest; JSON Schema validation; OpenAPI generated-client no-diff; secret scan; dependency audit; E2E na Linux. `model-eval.yml` jest manualny, wymaga artefaktu modelu i publikuje metryki bez checkpointu. Pin actions po SHA. Commit `ci: add cross-stack quality and security gates`.

### Task 24: Weryfikacja DoD na realnych trace

**Objective:** Udowodnić ukończenie na artefaktach, nie na headline metrics.

**Files:**
- Create: `docs/verification.md`
- Local only: `artifacts/verification/<timestamp>/...`

**Matrix:**
- fixtures deterministyczne: minimum 4 scenariusze,
- live traces: minimum 2 runy tego samego tasku, najlepiej różne modele/strategie,
- osobne wyniki rules i ONNX,
- osobne requested/completed, fake/fixture/live.

**Dowody:** pełne logi komend, exit codes, wersje, hashes wejść/wyjść, DB snapshot, raporty JSON/HTML, screenshots UI, raw model predictions i parity results. Zweryfikować ręcznie losową próbkę linków source/evidence. Commit tylko dokumentacji i zanonimizowanego manifestu: `docs: record reproducible MVP verification`.

---

## 5. Kolejność milestone’ów

### M0 — Foundation
Tasks 1–4. Wynik: budujące się monorepo, kontrakty i application layer.

### M1 — CLI-first vertical slice
Tasks 5, 7–10, 17. Wynik: import → analiza rules → raport działa bez UI i ML.

### M2 — Web product slice
Tasks 11–16. Wynik: realne API i React UI z genomem oraz porównaniem.

### M3 — Learned classifier
Tasks 18–20. Wynik: reprodukowalny trening Python, ONNX i parity w C#.

### M4 — Hardening
Tasks 22–24. Wynik: CI, security, real-process E2E i audytowalny DoD.

Task 6 (`pi_tar`) można wykonać równolegle po zamrożeniu schema. Task 21 jest eksperymentem po MVP i nie blokuje release.

---

## 6. Strategia testów i komendy weryfikacyjne

```bash
# C#
dotnet restore TraceHelix.slnx --locked-mode
dotnet format TraceHelix.slnx --verify-no-changes
dotnet build TraceHelix.slnx -c Release --no-restore
dotnet test TraceHelix.slnx -c Release --no-build --collect:"XPlat Code Coverage"

# React
npm --prefix web ci
npm --prefix web run lint
npm --prefix web run typecheck
npm --prefix web run test -- --run
npm --prefix web run build
npm --prefix web run e2e

# Python
uv sync --project training --locked
uv run --project training ruff check .
uv run --project training mypy src
uv run --project training pytest -q

# Contract / full workflow
make verify-contracts
make verify-e2e
./scripts/verify-release.sh
```

Testy jednostkowe muszą być szybkie i offline. Testy wymagające pobrania bazowego modelu, GPU, live traces lub teacher modelu mają jawne markery i osobny workflow/manual command. Przed każdym real-AI runem podać użytkownikowi model, zakres, liczbę prób i przewidywany koszt.

---

## 7. Acceptance criteria / Definition of Done

- [ ] `tracehelix import` importuje prawdziwy trace i zachowuje źródłowe referencje oraz hashe.
- [ ] `tracehelix analyze` tworzy nową, niezmienną rewizję z klasyfikacjami i co najmniej 4 działającymi detektorami.
- [ ] `tracehelix compare` porównuje dwa runy i nie ukrywa braków/niepewnego alignmentu.
- [ ] React pokazuje DNA runu, alerty, confidence i po kliknięciu prowadzi do źródłowego eventu/artefaktu.
- [ ] JSON/HTML report przechodzi walidację i zawiera provenance wszystkich wniosków.
- [ ] Python pipeline trenuje mały model, raportuje holdout macro-F1 i per-class F1 osobno dla kategorii danych.
- [ ] Eksportowany ONNX przechodzi parity tests w Pythonie i C#.
- [ ] Brak modelu lub jego uszkodzenie daje jawny fallback/error, nigdy ciche udawanie predykcji.
- [ ] Pełny workflow przechodzi na realnych procesach CLI/API/browser.
- [ ] Wyniki fake/fixture/live oraz requested/completed są zawsze rozdzielone.
- [ ] Dokumentacja nie przypisuje korelacji znaczenia przyczynowego.

---

## 8. Ryzyka i decyzje architektoniczne

1. **Różnorodność formatów trace:** adapter contract + preservation of unknown payload; najpierw 2 formaty, bez uniwersalnego parsera „na zapas”.
2. **Prywatność:** local-first, loopback API, redakcja przed datasetem, raw traces poza Git, brak telemetryki domyślnie.
3. **Tokenizacja w C#:** największe ryzyko interoperacyjności; parity gate blokuje publikację modelu. Rozważyć dołączenie tokenizera ONNX lub dobrze utrzymanej biblioteki tokenizerów zamiast własnej implementacji.
4. **SmolLM jako classifier:** zrobić krótki spike eksportu przed kosztownym treningiem. Jeśli ONNX jest niestabilny, wybrać mały encoder i udokumentować, że priorytetem produktu jest klasyfikacja, nie generowanie.
5. **Duże runy:** import streaming, paginacja eventów, indeksy SQLite, Canvas/wirtualizacja powyżej progu; benchmark na 10k/100k eventów.
6. **Fałszywe alerty:** każdy detector ma jawne parametry i precision na holdout; UI umożliwia filtrowanie, nie ukrywa niskiej confidence.
7. **Leakage:** split grupowy po run/task, dedup przed splitem, zamknięty holdout i hash manifestu.
8. **Causality:** nazwy i teksty alertów opisują obserwacje; raport zawiera disclaimer i dowody.
9. **Scope creep:** prediction of failure risk jest eksperymentem po core MVP.

---

## 9. Otwarte pytania do rozstrzygnięcia przed implementacją odpowiednich tasków

Nie blokują bootstrapu i vertical slice z Generic JSONL:

1. Dokładne lokalizacje i warianty artefaktów `pi_tar`/`sloopi` dostępnych do lokalnej inspekcji.
2. Czy repo ma dystrybuować pojedynczy self-contained binary z osadzonym buildem React, czy osobne paczki; rekomendacja MVP: `dotnet publish` kopiuje `web/dist` do `wwwroot`, CLI `serve` uruchamia całość.
3. Licencja docelowa; rekomendacja dla publicznego portfolio: Apache-2.0 ze względu na jawny grant patentowy.
4. Budżet i compute dla pełnego treningu; nie jest potrzebny do rules-first MVP.
5. Polityka przechowywania prywatnych raw traces i danych anotacyjnych; rekomendacja: lokalny katalog artefaktów poza Git z manifestem hashy.

---

## 10. Pierwszy vertical slice do implementacji

Najkrótsza ścieżka do sprawdzalnej wartości to Tasks `1 → 2 → 3 → 4 → 5 → 7 → 8 → 9 → 10 → 17`: jeden syntetyczny JSONL importowany przez prawdziwy binary, zapisany w prawdziwym SQLite, przeanalizowany regułami i wyeksportowany jako walidowany raport. Dopiero po tym należy budować API/UI, a dopiero po zamknięciu kontraktu predykcji — trenować model. Takie ułożenie zapobiega uzależnieniu produktu od niedojrzałego modelu i daje natychmiastowy baseline do porównania ML.
