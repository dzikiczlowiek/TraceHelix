using System.Text.Json;
using TraceHelix.Domain.Traces;
using TraceHelix.Domain.Analysis;
using TraceHelix.Infrastructure.Classification;
using Xunit;
namespace TraceHelix.Infrastructure.Tests.Classification;

public sealed class RuleClassifierTests
{
    private static TraceEvent E(TraceEventKind k, string s) => new(Guid.NewGuid(), Guid.NewGuid(), 0, DateTimeOffset.UtcNow, k, "agent", s, JsonDocument.Parse("{}").RootElement, new SourceReference("generic-jsonl", "aa", "x", 0, 1, "/"), [], "bb");
    [Theory]
    [InlineData(TraceEventKind.ToolCall, "search repository", StepLabel.Explore)]
    [InlineData(TraceEventKind.Reasoning, "plan checklist", StepLabel.Plan)]
    [InlineData(TraceEventKind.FileChange, "apply patch", StepLabel.Execute)]
    [InlineData(TraceEventKind.ToolResult, "tests passed", StepLabel.Verify)]
    [InlineData(TraceEventKind.ToolCall, "dotnet build verification", StepLabel.Verify)]
    [InlineData(TraceEventKind.Error, "retry after failure", StepLabel.Recover)]
    [InlineData(TraceEventKind.Message, "hello", StepLabel.Unknown)]
    public async Task Is_deterministic(TraceEventKind kind, string summary, StepLabel expected) { var e = E(kind, summary); var c = await new RuleBasedStepClassifier().ClassifyAsync([e], TestContext.Current.CancellationToken); Assert.Equal(expected, c[0].Label); Assert.Contains(e.Id, c[0].EvidenceEventIds); }
}
