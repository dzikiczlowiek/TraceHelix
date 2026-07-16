using TraceHelix.Domain.Traces;
namespace TraceHelix.Domain.Runs;

public sealed record TraceRun(Guid Id, string Name, string InputSha256, string Adapter, string AdapterVersion, DateTimeOffset ImportedAt, IReadOnlyList<TraceEvent> Events, IReadOnlyList<ImportDiagnostic> Diagnostics);
public sealed record ImportDiagnostic(int? Line, long? ByteOffset, string Code, string Message);
