using System.Text.Json;
namespace TraceHelix.Domain.Traces;

public enum TraceEventKind { Message, Reasoning, ToolCall, ToolResult, FileChange, Artifact, Status, Error, Unknown }
public sealed record ArtifactReference(string Name, string? Path, string? Sha256);
public sealed record SourceReference
{
    public SourceReference(string adapter, string inputSha256, string relativePath, long? byteOffset, int? line, string? jsonPointer)
    {
        if (string.IsNullOrWhiteSpace(adapter)) throw new ArgumentException("Adapter is required.", nameof(adapter));
        if (string.IsNullOrWhiteSpace(inputSha256)) throw new ArgumentException("Input hash is required.", nameof(inputSha256));
        if (string.IsNullOrWhiteSpace(relativePath)) throw new ArgumentException("Relative path is required.", nameof(relativePath));
        if (byteOffset < 0) throw new ArgumentOutOfRangeException(nameof(byteOffset));
        if (line < 1) throw new ArgumentOutOfRangeException(nameof(line));
        Adapter = adapter; InputSha256 = inputSha256; RelativePath = relativePath; ByteOffset = byteOffset; Line = line; JsonPointer = jsonPointer;
    }
    public string Adapter { get; init; }
    public string InputSha256 { get; init; }
    public string RelativePath { get; init; }
    public long? ByteOffset { get; init; }
    public int? Line { get; init; }
    public string? JsonPointer { get; init; }
}
public sealed record TraceEvent
{
    public TraceEvent(Guid id, Guid runId, long sequence, DateTimeOffset timestamp, TraceEventKind kind, string actor, string? summary, JsonElement payload, SourceReference source, IReadOnlyList<ArtifactReference> artifacts, string contentSha256)
    {
        if (id == Guid.Empty) throw new ArgumentException("Event ID is required.", nameof(id)); if (runId == Guid.Empty) throw new ArgumentException("Run ID is required.", nameof(runId));
        if (sequence < 0) throw new ArgumentOutOfRangeException(nameof(sequence)); if (string.IsNullOrWhiteSpace(actor)) throw new ArgumentException("Actor is required.", nameof(actor));
        if (string.IsNullOrWhiteSpace(contentSha256)) throw new ArgumentException("Content hash is required.", nameof(contentSha256));
        Id = id; RunId = runId; Sequence = sequence; Timestamp = timestamp; Kind = kind; Actor = actor; Summary = summary; Payload = payload.Clone(); Source = source ?? throw new ArgumentNullException(nameof(source)); Artifacts = artifacts ?? throw new ArgumentNullException(nameof(artifacts)); ContentSha256 = contentSha256;
    }
    public Guid Id { get; init; }
    public Guid RunId { get; init; }
    public long Sequence { get; init; }
    public DateTimeOffset Timestamp { get; init; }
    public TraceEventKind Kind { get; init; }
    public string Actor { get; init; }
    public string? Summary { get; init; }
    public JsonElement Payload { get; init; }
    public SourceReference Source { get; init; }
    public IReadOnlyList<ArtifactReference> Artifacts { get; init; }
    public string ContentSha256 { get; init; }
}
