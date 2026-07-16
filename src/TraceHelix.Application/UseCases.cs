using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Reports;
using TraceHelix.Domain.Runs;
namespace TraceHelix.Application;

public enum ImportOutcome { Imported, Duplicate, Partial }
public sealed record ImportResult(ImportOutcome Outcome, TraceRun Run);
public sealed class ImportTrace(ITraceAdapter adapter, ITraceRepository repository)
{
    public async Task<ImportResult> ExecuteAsync(string path, CancellationToken cancellationToken) { var id = Guid.NewGuid(); var parsed = await adapter.ReadAsync(path, id, cancellationToken); var duplicate = await repository.FindByImportAsync(parsed.InputSha256, adapter.Id, adapter.Version, cancellationToken); if (duplicate is not null) return new(ImportOutcome.Duplicate, duplicate); var run = new TraceRun(id, Path.GetFileName(path), parsed.InputSha256, adapter.Id, adapter.Version, DateTimeOffset.UtcNow, parsed.Events, parsed.Diagnostics); if (!await repository.TrySaveAsync(run, cancellationToken)) { duplicate = await repository.FindByImportAsync(parsed.InputSha256, adapter.Id, adapter.Version, cancellationToken); return new(ImportOutcome.Duplicate, duplicate ?? throw new InvalidOperationException("Conflicting import was not found.")); } return new(parsed.Diagnostics.Count > 0 ? ImportOutcome.Partial : ImportOutcome.Imported, run); }
}
public enum AnalyzeOutcome { Completed, NotFound, NoEvents }
public sealed record AnalyzeResult(AnalyzeOutcome Outcome, AnalysisRevision? Revision);
public sealed class AnalyzeRun(ITraceRepository traces, IAnalysisRepository analyses, IStepClassifier classifier, IReadOnlyList<IPatternDetector> detectors)
{
    public async Task<AnalyzeResult> ExecuteAsync(Guid runId, CancellationToken cancellationToken) { var run = await traces.GetAsync(runId, cancellationToken); if (run is null) return new(AnalyzeOutcome.NotFound, null); if (run.Events.Count == 0) return new(AnalyzeOutcome.NoEvents, null); var classifications = await classifier.ClassifyAsync(run.Events, cancellationToken); var alerts = new List<PatternAlert>(); foreach (var detector in detectors) alerts.AddRange(await detector.DetectAsync(run.Events, classifications, cancellationToken)); var hash = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { runId, classifications, alerts })))); var revision = AnalysisRevision.Completed(Guid.NewGuid(), runId, classifier.Id, classifier.Version, classifications, alerts, hash); await analyses.SaveAsync(revision, cancellationToken); return new(AnalyzeOutcome.Completed, revision); }
}
public sealed class CompareRuns(ITraceRepository traces, IAnalysisRepository analyses)
{
    public async Task<RunComparison?> ExecuteAsync(Guid leftId, Guid rightId, CancellationToken cancellationToken) { var left = await traces.GetAsync(leftId, cancellationToken); var right = await traces.GetAsync(rightId, cancellationToken); if (left is null || right is null) return null; var la = await analyses.GetLatestAsync(leftId, cancellationToken); var ra = await analyses.GetLatestAsync(rightId, cancellationToken); static IReadOnlyDictionary<string, int> Counts(AnalysisRevision? r) => r?.Classifications.GroupBy(x => x.Label.ToString()).ToDictionary(x => x.Key, x => x.Count()) ?? new Dictionary<string, int>(); return new(leftId, rightId, left.Events.Count, right.Events.Count, Counts(la), Counts(ra), la?.Alerts.Count ?? 0, ra?.Alerts.Count ?? 0, "Independent metrics; no semantic alignment is asserted."); }
}
public sealed class ExportReport(ITraceRepository traces, IAnalysisRepository analyses)
{
    public async Task<AnalysisReport?> CreateAsync(Guid runId, CancellationToken cancellationToken) { var run = await traces.GetAsync(runId, cancellationToken); var analysis = await analyses.GetLatestAsync(runId, cancellationToken); if (run is null || analysis is null) return null; var core = JsonSerializer.Serialize(new { run.InputSha256, analysis.ContentSha256 }); var hash = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(core))); return new("1.0", DateTimeOffset.UtcNow, run, analysis, "Observed patterns are not causal proof.", hash); }
}
