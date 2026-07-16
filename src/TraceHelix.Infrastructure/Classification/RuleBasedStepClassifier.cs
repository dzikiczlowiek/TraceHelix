using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Traces;

namespace TraceHelix.Infrastructure.Classification;

public sealed class RuleBasedStepClassifier : IStepClassifier
{
    public string Id => "rules";

    public string Version => "1.0.0";

    public Task<IReadOnlyList<StepClassification>> ClassifyAsync(
        IReadOnlyList<TraceEvent> events,
        CancellationToken cancellationToken)
    {
        var result = new List<StepClassification>(events.Count);
        foreach (var traceEvent in events)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var text = $"{traceEvent.Summary ?? string.Empty} {traceEvent.Payload.GetRawText()}"
                .ToLowerInvariant();
            var labels = new HashSet<StepLabel>();

            if (traceEvent.Kind == TraceEventKind.ToolCall &&
                Has(text, "read", "search", "grep", "find", "list"))
            {
                labels.Add(StepLabel.Explore);
            }

            if ((traceEvent.Kind == TraceEventKind.Reasoning || traceEvent.Kind == TraceEventKind.Message) &&
                Has(text, "plan", "checklist", "steps", "todo"))
            {
                labels.Add(StepLabel.Plan);
            }

            if (traceEvent.Kind == TraceEventKind.FileChange ||
                (traceEvent.Kind == TraceEventKind.ToolCall &&
                 Has(text, "write", "patch", "edit", "delete", "create", "mkdir", " rm ", " mv ", " cp ", "touch", "chmod")))
            {
                labels.Add(StepLabel.Execute);
            }

            if ((traceEvent.Kind == TraceEventKind.ToolCall || traceEvent.Kind == TraceEventKind.ToolResult) &&
                Has(text, "test", "passed", "build", "hash", "verify", "health", "check"))
            {
                labels.Add(StepLabel.Verify);
            }

            if (traceEvent.Kind == TraceEventKind.Error ||
                Has(text, "retry", "rollback", "recover", "fix after", "failure"))
            {
                labels.Add(StepLabel.Recover);
            }

            var label = labels.Count == 1 ? labels.Single() : StepLabel.Unknown;
            var confidence = labels.Count == 1 ? .85f : labels.Count == 0 ? .25f : .35f;
            result.Add(new StepClassification(
                traceEvent.Id,
                label,
                confidence,
                Id,
                Version,
                [traceEvent.Id],
                new Dictionary<string, float> { [label.ToString()] = confidence }));
        }

        return Task.FromResult<IReadOnlyList<StepClassification>>(result);
    }

    private static bool Has(string value, params string[] terms) => terms.Any(value.Contains);
}
