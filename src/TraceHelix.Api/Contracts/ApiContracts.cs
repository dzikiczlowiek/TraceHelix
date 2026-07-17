using System.Text.Json;

namespace TraceHelix.Api.Contracts;

public sealed record RunSummaryDto(Guid Id, string Name, DateTimeOffset ImportedAt, string Adapter, string AdapterVersion, int EventCount, int DiagnosticCount);
public sealed record RunDetailDto(Guid Id, string Name, DateTimeOffset ImportedAt, string Adapter, string AdapterVersion, string InputSha256, int EventCount, int DiagnosticCount);
public sealed record EventDto(Guid Id, long Sequence, DateTimeOffset Timestamp, string Kind, string Actor, string? Summary, JsonElement Payload, string ContentSha256);
public sealed record EventPageDto(IReadOnlyList<EventDto> Items, long? NextCursor, int Limit);
public sealed record ClassificationDto(Guid EventId, string Label, float Confidence, IReadOnlyList<Guid> EvidenceEventIds);
public sealed record AlertDto(string Code, string Severity, long StartSequence, long EndSequence, IReadOnlyList<Guid> EvidenceEventIds, string Explanation);
public sealed record AnalysisDto(Guid Id, Guid RunId, string Status, DateTimeOffset CreatedAt, string ClassifierId, string ClassifierVersion, IReadOnlyList<ClassificationDto> Classifications);
public sealed record AlertsDto(Guid AnalysisId, Guid RunId, IReadOnlyList<AlertDto> Items);
public sealed record CompareSideDto(Guid RunId, int EventCount, IReadOnlyDictionary<string, int> ClassificationCounts, int AlertCount);
public sealed record ComparisonDto(CompareSideDto Left, CompareSideDto Right, string Summary);
public sealed record HealthDto(string Status);
