using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Runs;
namespace TraceHelix.Domain.Reports;

public sealed record AnalysisReport(string SchemaVersion, DateTimeOffset GeneratedAt, TraceRun Run, AnalysisRevision Analysis, string Disclaimer, string ContentIdentitySha256);
public sealed record RunComparison(Guid LeftRunId, Guid RightRunId, int LeftEventCount, int RightEventCount, IReadOnlyDictionary<string, int> LeftLabels, IReadOnlyDictionary<string, int> RightLabels, int LeftAlertCount, int RightAlertCount, string Note);
