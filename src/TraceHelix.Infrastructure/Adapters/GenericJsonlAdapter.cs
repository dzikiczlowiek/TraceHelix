using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Runs;
using TraceHelix.Domain.Traces;
using TraceHelix.Infrastructure.Hashing;

namespace TraceHelix.Infrastructure.Adapters;

public sealed record ImportLimits(
    long MaxTotalBytes = GenericJsonlAdapter.DefaultMaxTotalBytes,
    int MaxRecordBytes = GenericJsonlAdapter.DefaultMaxRecordBytes,
    int MaxEvents = GenericJsonlAdapter.DefaultMaxEvents,
    int MaxRecords = GenericJsonlAdapter.DefaultMaxRecords);

public sealed class ImportLimitException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}

public sealed class GenericJsonlAdapter(ImportLimits? limits = null) : ITraceAdapter
{
    public const long DefaultMaxTotalBytes = 256L * 1024 * 1024;
    public const int DefaultMaxRecordBytes = 1024 * 1024;
    public const int DefaultMaxEvents = 100_000;
    // Blank lines are ignored; every other JSONL record, valid or malformed, consumes this budget.
    public const int DefaultMaxRecords = 100_000;
    private readonly ImportLimits limits = limits ?? new();
    public string Id => "generic-jsonl";
    public string Version => "1.0.0";

    public async Task<AdapterReadResult> ReadAsync(string path, Guid runId, CancellationToken cancellationToken)
    {
        var stagedPath = Path.GetTempFileName();
        try
        {
            string inputHash;
            await using (var input = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 81920, FileOptions.Asynchronous | FileOptions.SequentialScan))
            await using (var staged = new FileStream(stagedPath, FileMode.Truncate, FileAccess.Write, FileShare.None, 81920, FileOptions.Asynchronous | FileOptions.SequentialScan))
            using (var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256))
            {
                var buffer = new byte[81920];
                long total = 0;
                int read;
                while ((read = await input.ReadAsync(buffer, cancellationToken)) != 0)
                {
                    total += read;
                    if (total > limits.MaxTotalBytes) throw new ImportLimitException("TOTAL_BYTES_LIMIT", $"Input exceeds the {limits.MaxTotalBytes} byte total limit.");
                    hash.AppendData(buffer, 0, read);
                    await staged.WriteAsync(buffer.AsMemory(0, read), cancellationToken);
                }
                inputHash = Convert.ToHexStringLower(hash.GetHashAndReset());
            }

            return await ParseStagedAsync(stagedPath, Path.GetFileName(path), inputHash, runId, cancellationToken);
        }
        finally { File.Delete(stagedPath); }
    }

    private async Task<AdapterReadResult> ParseStagedAsync(string path, string relativePath, string inputHash, Guid runId, CancellationToken ct)
    {
        var events = new List<TraceEvent>(Math.Min(limits.MaxEvents, 4096));
        var diagnostics = new List<ImportDiagnostic>();
        await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.Asynchronous | FileOptions.SequentialScan);
        var lineBytes = new List<byte>(Math.Min(limits.MaxRecordBytes, 4096));
        long offset = 0, lineStart = 0;
        var line = 1;
        var records = 0;
        var buffer = new byte[4096];
        int read;
        while ((read = await stream.ReadAsync(buffer, ct)) != 0)
        {
            for (var i = 0; i < read; i++)
            {
                ct.ThrowIfCancellationRequested();
                var b = buffer[i]; offset++;
                if (b == 10)
                {
                    if (lineBytes.Count > 0 && lineBytes[^1] == 13) lineBytes.RemoveAt(lineBytes.Count - 1);
                    ParseLine(lineBytes.ToArray(), line, lineStart, inputHash, runId, events, diagnostics, relativePath, ref records);
                    lineBytes.Clear(); line++; lineStart = offset;
                }
                else
                {
                    if (lineBytes.Count >= limits.MaxRecordBytes) throw new ImportLimitException("RECORD_BYTES_LIMIT", $"Record at line {line} exceeds the {limits.MaxRecordBytes} byte limit.");
                    lineBytes.Add(b);
                }
            }
        }
        if (lineBytes.Count > 0) ParseLine(lineBytes.ToArray(), line, lineStart, inputHash, runId, events, diagnostics, relativePath, ref records);
        return new(inputHash, events, diagnostics);
    }

    private void ParseLine(byte[] bytes, int line, long offset, string inputHash, Guid runId, List<TraceEvent> events, List<ImportDiagnostic> diagnostics, string relativePath, ref int records)
    {
        if (bytes.All(b => char.IsWhiteSpace((char)b))) return;
        if (records >= limits.MaxRecords) throw new ImportLimitException("RECORD_COUNT_LIMIT", $"Input exceeds the {limits.MaxRecords} nonblank record limit.");
        records++;
        if (events.Count >= limits.MaxEvents) throw new ImportLimitException("EVENT_COUNT_LIMIT", $"Input exceeds the {limits.MaxEvents} event limit.");
        try
        {
            using var doc = JsonDocument.Parse(bytes); var root = doc.RootElement; if (root.ValueKind != JsonValueKind.Object) throw new JsonException("JSONL record must be an object.");
            var timestamp = root.TryGetProperty("timestamp", out var ts) && ts.TryGetDateTimeOffset(out var parsed) ? parsed : DateTimeOffset.UnixEpoch;
            var kind = root.TryGetProperty("kind", out var k) && Enum.TryParse<TraceEventKind>(k.GetString(), true, out var parsedKind) ? parsedKind : TraceEventKind.Unknown;
            var actor = root.TryGetProperty("actor", out var a) && !string.IsNullOrWhiteSpace(a.GetString()) ? a.GetString()! : "unknown";
            var summary = root.TryGetProperty("summary", out var s) && s.ValueKind == JsonValueKind.String ? s.GetString() : null;
            var payload = root.TryGetProperty("payload", out var p) ? p.Clone() : root.Clone(); var sequence = events.Count; var contentHash = Sha256ContentHasher.Hash(bytes); var identity = SHA256.HashData(Encoding.UTF8.GetBytes($"{runId:N}:{sequence}:{contentHash}")); var eventId = new Guid(identity.AsSpan(0, 16));
            events.Add(new(eventId, runId, sequence, timestamp, kind, actor, summary, payload, new SourceReference("generic-jsonl", inputHash, relativePath, offset, line, "/"), [], contentHash));
        }
        catch (JsonException ex) { diagnostics.Add(new(line, offset, "INVALID_JSON", ex.Message)); }
        catch (InvalidOperationException ex) { diagnostics.Add(new(line, offset, "INVALID_RECORD", ex.Message)); }
    }
}
