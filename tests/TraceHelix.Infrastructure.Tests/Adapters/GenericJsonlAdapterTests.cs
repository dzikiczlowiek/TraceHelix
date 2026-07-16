using TraceHelix.Infrastructure.Adapters;
using Xunit;
namespace TraceHelix.Infrastructure.Tests.Adapters;

public sealed class GenericJsonlAdapterTests
{
    [Fact] public async Task Parses_lines_with_provenance_and_diagnostic() { var p = Path.GetTempFileName(); await File.WriteAllTextAsync(p, "{\"timestamp\":\"2026-01-01T00:00:00Z\",\"kind\":\"ToolCall\",\"actor\":\"agent\",\"summary\":\"read file\",\"payload\":{\"tool\":\"read\"}}\r\n\r\nnot-json\n", TestContext.Current.CancellationToken); var result = await new GenericJsonlAdapter().ReadAsync(p, Guid.NewGuid(), TestContext.Current.CancellationToken); Assert.Single(result.Events); Assert.Single(result.Diagnostics); Assert.Equal(1, result.Events[0].Source.Line); Assert.Equal(0, result.Events[0].Source.ByteOffset); Assert.Equal("generic-jsonl", result.Events[0].Source.Adapter); Assert.Equal(64, result.InputSha256.Length); File.Delete(p); }

    [Fact]
    public async Task Hash_is_of_the_exact_parsed_bytes()
    {
        var p = Path.GetTempFileName();
        var bytes = System.Text.Encoding.UTF8.GetBytes("{\"actor\":\"a\"}\r\n");
        await File.WriteAllBytesAsync(p, bytes, TestContext.Current.CancellationToken);
        var result = await new GenericJsonlAdapter().ReadAsync(p, Guid.NewGuid(), TestContext.Current.CancellationToken);
        var expected = Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(bytes));
        Assert.Equal(expected, result.InputSha256);
        Assert.Equal(expected, Assert.Single(result.Events).Source.InputSha256);
        File.Delete(p);
    }

    [Theory]
    [InlineData(10, 100, 100, "TOTAL_BYTES_LIMIT")]
    [InlineData(100, 3, 100, "RECORD_BYTES_LIMIT")]
    [InlineData(100, 100, 0, "EVENT_COUNT_LIMIT")]
    public async Task Enforces_resource_limits(long total, int record, int events, string code)
    {
        var p = Path.GetTempFileName();
        await File.WriteAllTextAsync(p, "{\"actor\":\"agent\"}\n", TestContext.Current.CancellationToken);
        var error = await Assert.ThrowsAsync<ImportLimitException>(() => new GenericJsonlAdapter(new(total, record, events)).ReadAsync(p, Guid.NewGuid(), TestContext.Current.CancellationToken));
        Assert.Equal(code, error.Code);
        File.Delete(p);
    }

    [Fact]
    public async Task Every_nonblank_record_consumes_the_record_budget()
    {
        var p = Path.GetTempFileName();
        await File.WriteAllTextAsync(p, "x\ny\nz\n", TestContext.Current.CancellationToken);
        var adapter = new GenericJsonlAdapter(new(MaxTotalBytes: 100, MaxRecordBytes: 10, MaxEvents: 10, MaxRecords: 2));
        var error = await Assert.ThrowsAsync<ImportLimitException>(() => adapter.ReadAsync(p, Guid.NewGuid(), TestContext.Current.CancellationToken));
        Assert.Equal("RECORD_COUNT_LIMIT", error.Code);
        File.Delete(p);
    }

    [Fact]
    public async Task Record_limit_allows_exact_boundary_including_malformed_records()
    {
        var p = Path.GetTempFileName();
        await File.WriteAllTextAsync(p, "x\ny\n\n", TestContext.Current.CancellationToken);
        var result = await new GenericJsonlAdapter(new(MaxTotalBytes: 100, MaxRecordBytes: 10, MaxEvents: 10, MaxRecords: 2)).ReadAsync(p, Guid.NewGuid(), TestContext.Current.CancellationToken);
        Assert.Equal(2, result.Diagnostics.Count);
        File.Delete(p);
    }

    [Fact]
    public async Task Invalid_property_types_become_diagnostics()
    {
        var p = Path.GetTempFileName();
        await File.WriteAllTextAsync(p, "{\"kind\":42,\"actor\":false}\n", TestContext.Current.CancellationToken);
        var result = await new GenericJsonlAdapter().ReadAsync(p, Guid.NewGuid(), TestContext.Current.CancellationToken);
        Assert.Empty(result.Events);
        Assert.Single(result.Diagnostics);
        Assert.Equal("INVALID_RECORD", result.Diagnostics[0].Code);
        File.Delete(p);
    }
}
