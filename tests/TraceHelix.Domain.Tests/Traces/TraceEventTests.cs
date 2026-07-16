using System.Text.Json;
using TraceHelix.Domain.Traces;
using Xunit;
namespace TraceHelix.Domain.Tests.Traces;

public sealed class TraceEventTests
{
    private static SourceReference Source() => new("generic-jsonl", "aa", "run.jsonl", 0, 1, "/");
    [Fact] public void Rejects_negative_sequence() => Assert.Throws<ArgumentOutOfRangeException>(() => new TraceEvent(Guid.NewGuid(), Guid.NewGuid(), -1, DateTimeOffset.UtcNow, TraceEventKind.Message, "agent", null, JsonDocument.Parse("{}").RootElement, Source(), [], "bb"));
    [Fact] public void Rejects_empty_hash() => Assert.Throws<ArgumentException>(() => new TraceEvent(Guid.NewGuid(), Guid.NewGuid(), 0, DateTimeOffset.UtcNow, TraceEventKind.Message, "agent", null, JsonDocument.Parse("{}").RootElement, Source(), [], ""));
    [Fact] public void Source_requires_provenance() => Assert.Throws<ArgumentException>(() => new SourceReference("", "aa", "x", null, null, null));
    [Fact] public void Serializes_round_trip() { var value = new TraceEvent(Guid.NewGuid(), Guid.NewGuid(), 0, DateTimeOffset.UnixEpoch, TraceEventKind.ToolCall, "agent", "read file", JsonDocument.Parse("{\"tool\":\"read\"}").RootElement, Source(), [], "bb"); var json = JsonSerializer.Serialize(value); var copy = JsonSerializer.Deserialize<TraceEvent>(json); Assert.NotNull(copy); Assert.Equal(value.Id, copy.Id); Assert.Equal("read", copy.Payload.GetProperty("tool").GetString()); }
}
