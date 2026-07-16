using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Traces;
namespace TraceHelix.Infrastructure.Detection;

public static class DefaultDetectors { public static IReadOnlyList<IPatternDetector> Create() => [new NoProgressLoopDetector(), new PlanLoopDetector(), new VerificationGapDetector(), new PrematureSuccessDetector(), new RecoveryStormDetector(), new ToolErrorCascadeDetector()]; }
internal abstract class DetectorBase(string code, IReadOnlyDictionary<string, string> parameters) : IPatternDetector
{
    public string Code { get; } = code; public string Version => "1.0.0"; public IReadOnlyDictionary<string, string> Parameters { get; } = parameters;
    public abstract Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken);
    protected PatternAlert Alert(IReadOnlyList<Guid> ids, long start, long end, string explanation, AlertSeverity severity = AlertSeverity.Warning) => new(Code, severity, start, end, Version, Parameters, ids, explanation);
    protected static IReadOnlyList<PatternAlert> Result(PatternAlert? alert) => alert is null ? [] : [alert];
}
internal sealed class NoProgressLoopDetector() : DetectorBase("THX001_NO_PROGRESS_LOOP", new Dictionary<string, string> { { "threshold", "3" }, { "window", "5" } })
{
    public override Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken) { cancellationToken.ThrowIfCancellationRequested(); for (var i = 0; i + 2 < events.Count; i++) { cancellationToken.ThrowIfCancellationRequested(); var group = events.Skip(i).Take(3).ToArray(); if (group.Select(x => x.ContentSha256).Distinct().Count() == 1) return Task.FromResult(Result(Alert(group.Select(x => x.Id).ToArray(), group[0].Sequence, group[^1].Sequence, "Observed repeated event content without a changed content hash."))); } return Task.FromResult<IReadOnlyList<PatternAlert>>([]); }
}
internal sealed class PlanLoopDetector() : DetectorBase("THX002_PLAN_LOOP", new Dictionary<string, string> { { "threshold", "3" } })
{
    public override Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken) { cancellationToken.ThrowIfCancellationRequested(); for (var i = 0; i + 2 < classifications.Count; i++) { cancellationToken.ThrowIfCancellationRequested(); var group = classifications.Skip(i).Take(3).ToArray(); if (group.All(x => x.Label == StepLabel.Plan)) { var ids = group.Select(x => x.EventId).ToArray(); return Task.FromResult(Result(Alert(ids, i, i + 2, "Observed three consecutive planning classifications without execute or verify."))); } } return Task.FromResult<IReadOnlyList<PatternAlert>>([]); }
}
internal sealed class VerificationGapDetector() : DetectorBase("THX003_VERIFICATION_GAP", new Dictionary<string, string> { { "success_window", "final" } })
{
    public override Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken) { cancellationToken.ThrowIfCancellationRequested(); var mutation = classifications.Select((c, i) => (c, i)).LastOrDefault(x => x.c.Label == StepLabel.Execute); if (mutation.c is null) return Task.FromResult<IReadOnlyList<PatternAlert>>([]); var verified = classifications.Skip(mutation.i + 1).Any(x => x.Label == StepLabel.Verify); var final = events.LastOrDefault(); var success = final is not null && ((final.Summary ?? "").Contains("success", StringComparison.OrdinalIgnoreCase) || (final.Summary ?? "").Contains("complete", StringComparison.OrdinalIgnoreCase)); return Task.FromResult(Result(!verified && success ? Alert([mutation.c.EventId, final!.Id], mutation.i, final.Sequence, "Observed final success status without verification evidence after the last mutation.") : null)); }
}
internal sealed class PrematureSuccessDetector() : DetectorBase("THX004_PREMATURE_SUCCESS", new Dictionary<string, string> { { "window", "3" } })
{
    public override Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken) { cancellationToken.ThrowIfCancellationRequested(); for (var i = 0; i < events.Count; i++) { cancellationToken.ThrowIfCancellationRequested(); if (!HasSuccess(events[i])) continue; var error = events.Skip(i + 1).Take(3).FirstOrDefault(x => x.Kind == TraceEventKind.Error); if (error is not null) return Task.FromResult(Result(Alert([events[i].Id, error.Id], events[i].Sequence, error.Sequence, "Observed a success declaration followed by an error in the configured window."))); } return Task.FromResult<IReadOnlyList<PatternAlert>>([]); }
    private static bool HasSuccess(TraceEvent e) => (e.Summary ?? "").Contains("success", StringComparison.OrdinalIgnoreCase) || (e.Summary ?? "").Contains("complete", StringComparison.OrdinalIgnoreCase);
}
internal sealed class RecoveryStormDetector() : DetectorBase("THX005_RECOVERY_STORM", new Dictionary<string, string> { { "threshold", "3" }, { "window", "5" } })
{
    public override Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken) { cancellationToken.ThrowIfCancellationRequested(); for (var i = 0; i < classifications.Count; i++) { cancellationToken.ThrowIfCancellationRequested(); var group = classifications.Skip(i).Take(5).Where(x => x.Label == StepLabel.Recover).ToArray(); if (group.Length >= 3) return Task.FromResult(Result(Alert(group.Select(x => x.EventId).ToArray(), i, Math.Min(i + 4, classifications.Count - 1), "Observed at least three recovery classifications in a five-step window."))); } return Task.FromResult<IReadOnlyList<PatternAlert>>([]); }
}
internal sealed class ToolErrorCascadeDetector() : DetectorBase("THX006_TOOL_ERROR_CASCADE", new Dictionary<string, string> { { "threshold", "3" } })
{
    public override Task<IReadOnlyList<PatternAlert>> DetectAsync(IReadOnlyList<TraceEvent> events, IReadOnlyList<StepClassification> classifications, CancellationToken cancellationToken) { cancellationToken.ThrowIfCancellationRequested(); for (var i = 0; i + 2 < events.Count; i++) { cancellationToken.ThrowIfCancellationRequested(); var group = events.Skip(i).Take(3).ToArray(); if (group.All(x => x.Kind == TraceEventKind.Error)) return Task.FromResult(Result(Alert(group.Select(x => x.Id).ToArray(), group[0].Sequence, group[^1].Sequence, "Observed three consecutive tool/error events without recorded strategy change.", AlertSeverity.Critical))); } return Task.FromResult<IReadOnlyList<PatternAlert>>([]); }
}
