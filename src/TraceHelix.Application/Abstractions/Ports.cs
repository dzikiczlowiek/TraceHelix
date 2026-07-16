using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Reports;
using TraceHelix.Domain.Runs;
using TraceHelix.Domain.Traces;
namespace TraceHelix.Application.Abstractions;

public sealed record AdapterReadResult(string InputSha256, IReadOnlyList<TraceEvent> Events, IReadOnlyList<ImportDiagnostic> Diagnostics);
public interface ITraceAdapter { string Id { get; } string Version { get; } Task<AdapterReadResult> ReadAsync(string path, Guid runId, CancellationToken cancellationToken); }
public interface ITraceRepository { Task<TraceRun?> FindByImportAsync(string hash, string adapter, string version, CancellationToken cancellationToken); Task<TraceRun?> GetAsync(Guid id, CancellationToken cancellationToken); Task<IReadOnlyList<TraceRun>> ListAsync(CancellationToken cancellationToken); Task<bool> TrySaveAsync(TraceRun run, CancellationToken cancellationToken); }
public interface IAnalysisRepository { Task SaveAsync(AnalysisRevision revision, CancellationToken cancellationToken); Task<AnalysisRevision?> GetLatestAsync(Guid runId, CancellationToken cancellationToken); }
public interface IStepClassifier { string Id { get; } string Version { get; } Task<IReadOnlyList<StepClassification>> ClassifyAsync(IReadOnlyList<TraceEvent> events, CancellationToken cancellationToken); }
public interface IPatternDetector { string Code { get; } string Version { get; } IReadOnlyDictionary<string, string> Parameters { get; } Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken); }
public interface IReportWriter { string Format { get; } Task WriteAsync(AnalysisReport report, string outputPath, CancellationToken cancellationToken); }
