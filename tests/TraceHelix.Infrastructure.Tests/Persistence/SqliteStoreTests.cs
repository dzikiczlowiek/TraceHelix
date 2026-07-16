using System.Text.Json;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Runs;
using TraceHelix.Domain.Traces;
using TraceHelix.Infrastructure.Persistence;
using Xunit;
namespace TraceHelix.Infrastructure.Tests.Persistence;

public sealed class SqliteStoreTests
{
    [Fact] public async Task Persists_real_sqlite_run_and_enforces_import_identity() { var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")); var path = Path.Combine(dir, "tracehelix.db"); var runId = Guid.NewGuid(); var e = new TraceEvent(Guid.NewGuid(), runId, 0, DateTimeOffset.UnixEpoch, TraceEventKind.Message, "agent", "hello", JsonDocument.Parse("{}").RootElement, new SourceReference("generic-jsonl", "input", "x", 0, 1, "/"), [], "content"); var run = new TraceRun(runId, "x", "input", "generic-jsonl", "1.0", DateTimeOffset.UnixEpoch, [e], []); var store = new SqliteStore(path); Assert.True(await store.TrySaveAsync(run, TestContext.Current.CancellationToken)); var loaded = await store.GetAsync(runId, TestContext.Current.CancellationToken); Assert.NotNull(loaded); Assert.Single(loaded.Events); Assert.False(await store.TrySaveAsync(run with { Id = Guid.NewGuid() }, TestContext.Current.CancellationToken)); Directory.Delete(dir, true); }
}
