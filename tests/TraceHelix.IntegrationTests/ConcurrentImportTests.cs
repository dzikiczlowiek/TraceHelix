using TraceHelix.Application;
using TraceHelix.Infrastructure.Adapters;
using TraceHelix.Infrastructure.Persistence;
using Xunit;

namespace TraceHelix.IntegrationTests;

public sealed class ConcurrentImportTests
{
    [Fact]
    public async Task Concurrent_duplicate_imports_are_atomic_and_stable()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        var input = Path.Combine(dir, "input.jsonl");
        var database = Path.Combine(dir, "tracehelix.db");
        await File.WriteAllTextAsync(input, "{\"actor\":\"agent\",\"summary\":\"hello\"}\n", TestContext.Current.CancellationToken);
        _ = new SqliteStore(database); // Complete schema initialization before releasing workers.
        var gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var tasks = Enumerable.Range(0, 8).Select(async _ =>
        {
            await gate.Task;
            return await new ImportTrace(new GenericJsonlAdapter(), new SqliteStore(database)).ExecuteAsync(input, TestContext.Current.CancellationToken);
        }).ToArray();
        gate.SetResult();
        var results = await Task.WhenAll(tasks);
        var imported = Assert.Single(results, x => x.Outcome == ImportOutcome.Imported);
        Assert.Equal(7, results.Count(x => x.Outcome == ImportOutcome.Duplicate));
        Assert.All(results, result => Assert.Equal(imported.Run.Id, result.Run.Id));
        Assert.Single(await new SqliteStore(database).ListAsync(TestContext.Current.CancellationToken));
        Directory.Delete(dir, true);
    }
}
