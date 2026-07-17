#pragma warning disable xUnit1051 // HttpClient calls are short-lived integration requests.
using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using TraceHelix.Application;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Runs;
using TraceHelix.Infrastructure.Adapters;
using TraceHelix.Infrastructure.Persistence;
using Xunit;

namespace TraceHelix.Api.Tests;

public sealed class ApiIntegrationTests
{
    [Fact]
    public async Task Health_endpoints_are_live_and_ready()
    {
        await using var fixture = await ApiFixture.CreateAsync();
        Assert.Equal("live", (await fixture.Client.GetFromJsonAsync<JsonElement>("/health/live")).GetProperty("status").GetString());
        Assert.Equal("ready", (await fixture.Client.GetFromJsonAsync<JsonElement>("/health/ready")).GetProperty("status").GetString());
    }

    [Fact]
    public async Task Runs_list_and_detail_are_real_http_and_camel_case_json()
    {
        await using var fixture = await ApiFixture.CreateAsync();
        var response = await fixture.Client.GetAsync("/api/v1/runs");
        Assert.Equal("application/json", response.Content.Headers.ContentType?.MediaType);
        var runs = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(3, runs.GetArrayLength());
        Assert.True(runs[0].TryGetProperty("eventCount", out _));
        Assert.False(runs[0].TryGetProperty("EventCount", out _));
        var detail = await fixture.Client.GetFromJsonAsync<JsonElement>($"/api/v1/runs/{fixture.LeftId}");
        Assert.Equal(fixture.LeftId, detail.GetProperty("id").GetGuid());
        Assert.True(detail.GetProperty("inputSha256").GetString()!.Length == 64);
    }

    [Fact]
    public async Task Events_cursor_is_stable_and_validation_is_bounded()
    {
        await using var fixture = await ApiFixture.CreateAsync();
        var first = await fixture.Client.GetFromJsonAsync<JsonElement>($"/api/v1/runs/{fixture.LeftId}/events?limit=1");
        Assert.Equal(0, first.GetProperty("items")[0].GetProperty("sequence").GetInt64());
        Assert.Equal(0, first.GetProperty("nextCursor").GetInt64());
        var second = await fixture.Client.GetFromJsonAsync<JsonElement>($"/api/v1/runs/{fixture.LeftId}/events?limit=1&cursor=0");
        Assert.Equal(1, second.GetProperty("items")[0].GetProperty("sequence").GetInt64());
        Assert.Equal(JsonValueKind.Null, second.GetProperty("nextCursor").ValueKind);
        var negativeCursor = await fixture.Client.GetAsync($"/api/v1/runs/{fixture.LeftId}/events?cursor=-1");
        Assert.Equal(HttpStatusCode.BadRequest, negativeCursor.StatusCode);
        Assert.Equal("application/problem+json", negativeCursor.Content.Headers.ContentType?.MediaType);

        foreach (var url in new[] { "/api/v1/runs/nope/events", $"/api/v1/runs/{fixture.LeftId}/events?cursor=x", $"/api/v1/runs/{fixture.LeftId}/events?limit=0", $"/api/v1/runs/{fixture.LeftId}/events?limit=201" })
            Assert.Equal(HttpStatusCode.BadRequest, (await fixture.Client.GetAsync(url)).StatusCode);
    }

    [Fact]
    public async Task Validation_problem_details_and_documented_not_found_are_consistent()
    {
        await using var fixture = await ApiFixture.CreateAsync();
        var malformed = await fixture.Client.GetAsync("/api/v1/runs/not-a-guid");
        Assert.Equal(HttpStatusCode.BadRequest, malformed.StatusCode);
        Assert.Equal("application/problem+json", malformed.Content.Headers.ContentType?.MediaType);
        var problem = await malformed.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(400, problem.GetProperty("status").GetInt32());
        Assert.Equal("Invalid request", problem.GetProperty("title").GetString());
        Assert.Contains("GUID", problem.GetProperty("detail").GetString(), StringComparison.Ordinal);

        var missing = await fixture.Client.GetAsync($"/api/v1/runs/{Guid.NewGuid()}");
        Assert.Equal(HttpStatusCode.NotFound, missing.StatusCode);
        Assert.Empty(await missing.Content.ReadAsByteArrayAsync());
    }

    [Fact]
    public async Task Empty_run_analysis_is_conflict_problem_details()
    {
        await using var fixture = await ApiFixture.CreateAsync();
        var response = await fixture.Client.PostAsync($"/api/v1/runs/{fixture.EmptyId}/analysis/rules", null);
        Assert.Equal(HttpStatusCode.Conflict, response.StatusCode);
        Assert.Equal("application/problem+json", response.Content.Headers.ContentType?.MediaType);
        var problem = await response.Content.ReadFromJsonAsync<JsonElement>();
        Assert.Equal(409, problem.GetProperty("status").GetInt32());
        Assert.Equal("Analysis unavailable", problem.GetProperty("title").GetString());
        Assert.Equal("Run has no events.", problem.GetProperty("detail").GetString());
    }

    [Fact]
    public async Task Rules_analysis_latest_alerts_and_compare_work()
    {
        await using var fixture = await ApiFixture.CreateAsync();
        Assert.Equal(HttpStatusCode.NotFound, (await fixture.Client.GetAsync($"/api/v1/runs/{fixture.LeftId}/analysis/latest")).StatusCode);
        var posted = await fixture.Client.PostAsync($"/api/v1/runs/{fixture.LeftId}/analysis/rules", null);
        posted.EnsureSuccessStatusCode();
        var latest = await fixture.Client.GetFromJsonAsync<JsonElement>($"/api/v1/runs/{fixture.LeftId}/analysis/latest");
        Assert.Equal(fixture.LeftId, latest.GetProperty("runId").GetGuid());
        var alerts = await fixture.Client.GetFromJsonAsync<JsonElement>($"/api/v1/runs/{fixture.LeftId}/alerts");
        Assert.Equal(fixture.LeftId, alerts.GetProperty("runId").GetGuid());
        var comparison = await fixture.Client.GetFromJsonAsync<JsonElement>($"/api/v1/compare?left={fixture.LeftId}&right={fixture.RightId}");
        Assert.Equal(2, comparison.GetProperty("left").GetProperty("eventCount").GetInt32());
        Assert.Equal(1, comparison.GetProperty("right").GetProperty("eventCount").GetInt32());
        Assert.Equal(HttpStatusCode.BadRequest, (await fixture.Client.GetAsync("/api/v1/compare?left=x&right=y")).StatusCode);
    }

    [Fact]
    public async Task Production_failures_are_sanitized_problem_details()
    {
        await using var factory = new WebApplicationFactory<Program>().WithWebHostBuilder(builder =>
        {
            builder.UseEnvironment("Production").UseSetting("TRACEHELIX_DB", Path.Combine(Path.GetTempPath(), $"tracehelix-{Guid.NewGuid():N}", "db.sqlite"));
            builder.ConfigureServices(services =>
            {
                services.RemoveAll<ITraceRepository>();
                services.AddSingleton<ITraceRepository, ThrowingRepository>();
            });
        });
        using var client = factory.CreateClient();
        var response = await client.GetAsync("/api/v1/runs");
        Assert.Equal(HttpStatusCode.InternalServerError, response.StatusCode);
        Assert.Equal("application/problem+json", response.Content.Headers.ContentType?.MediaType);
        var body = await response.Content.ReadAsStringAsync();
        Assert.DoesNotContain(nameof(InvalidOperationException), body, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("stack", body, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("Data Source", body, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain(Path.GetTempPath(), body, StringComparison.OrdinalIgnoreCase);
    }

    private sealed class ThrowingRepository : ITraceRepository
    {
        public Task<IReadOnlyList<TraceRun>> ListAsync(CancellationToken cancellationToken) => throw new InvalidOperationException("Data Source=/absolute/secret.sqlite");
        public Task<TraceRun?> GetAsync(Guid id, CancellationToken cancellationToken) => throw new InvalidOperationException();
        public Task<TraceRun?> FindByImportAsync(string hash, string adapter, string version, CancellationToken cancellationToken) => throw new InvalidOperationException();
        public Task<bool> TrySaveAsync(TraceRun run, CancellationToken cancellationToken) => throw new InvalidOperationException();
    }
}

internal sealed class ApiFixture : IAsyncDisposable
{
    private readonly string directory;
    private readonly WebApplicationFactory<Program> factory;
    public HttpClient Client { get; }
    public Guid LeftId { get; }
    public Guid RightId { get; }
    public Guid EmptyId { get; }

    private ApiFixture(string directory, WebApplicationFactory<Program> factory, HttpClient client, Guid leftId, Guid rightId, Guid emptyId) =>
        (this.directory, this.factory, Client, LeftId, RightId, EmptyId) = (directory, factory, client, leftId, rightId, emptyId);

    public static async Task<ApiFixture> CreateAsync()
    {
        var directory = Path.Combine(Path.GetTempPath(), $"tracehelix-api-{Guid.NewGuid():N}");
        Directory.CreateDirectory(directory);
        var database = Path.Combine(directory, "real.sqlite");
        var store = new SqliteStore(database);
        var adapter = new GenericJsonlAdapter();
        async Task<Guid> Import(string name, string lines)
        {
            var path = Path.Combine(directory, name);
            await File.WriteAllTextAsync(path, lines);
            return (await new ImportTrace(adapter, store).ExecuteAsync(path, CancellationToken.None)).Run.Id;
        }
        var left = await Import("left.jsonl", "{\"kind\":\"ToolCall\",\"actor\":\"agent\",\"summary\":\"call\"}\n{\"kind\":\"ToolResult\",\"actor\":\"tool\",\"summary\":\"result\"}\n");
        var right = await Import("right.jsonl", "{\"kind\":\"Message\",\"actor\":\"user\",\"summary\":\"hello\"}\n");
        var empty = Guid.NewGuid();
        Assert.True(await store.TrySaveAsync(new TraceRun(empty, "empty", new string('0', 64), "test", "1", DateTimeOffset.UtcNow, [], []), CancellationToken.None));
        var factory = new WebApplicationFactory<Program>().WithWebHostBuilder(builder => builder.UseEnvironment("Production").UseSetting("TRACEHELIX_DB", database));
        return new(directory, factory, factory.CreateClient(), left, right, empty);
    }

    public async ValueTask DisposeAsync()
    {
        Client.Dispose();
        await factory.DisposeAsync();
        Directory.Delete(directory, true);
    }
}
