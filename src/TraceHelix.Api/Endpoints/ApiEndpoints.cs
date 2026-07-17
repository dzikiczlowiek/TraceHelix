using TraceHelix.Api.Contracts;
using TraceHelix.Application;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Runs;

namespace TraceHelix.Api.Endpoints;

public static class ApiEndpoints
{
    private const int MaxPageSize = 200;
    public static void MapTraceHelixApi(this WebApplication app)
    {
        app.MapGet("/health/live", () => Results.Ok(new HealthDto("live"))).Produces<HealthDto>();
        app.MapGet("/health/ready", async (ITraceRepository store, CancellationToken ct) => { await store.ListAsync(ct); return Results.Ok(new HealthDto("ready")); })
            .Produces<HealthDto>().ProducesProblem(500);
        var api = app.MapGroup("/api/v1");
        api.MapGet("/runs", ListRuns).Produces<RunSummaryDto[]>();
        api.MapGet("/runs/{id}", GetRun).Produces<RunDetailDto>().ProducesProblem(400).Produces(404);
        api.MapGet("/runs/{id}/events", GetEvents).Produces<EventPageDto>().ProducesProblem(400).Produces(404);
        api.MapGet("/runs/{id}/analysis/latest", GetAnalysis).Produces<AnalysisDto>().ProducesProblem(400).Produces(404);
        api.MapGet("/runs/{id}/alerts", GetAlerts).Produces<AlertsDto>().ProducesProblem(400).Produces(404);
        api.MapPost("/runs/{id}/analysis/rules", Analyze).Produces<AnalysisDto>().ProducesProblem(400).Produces(404).ProducesProblem(409);
        api.MapGet("/compare", Compare).Produces<ComparisonDto>().ProducesProblem(400).Produces(404);
    }

    private static async Task<IResult> ListRuns(ITraceRepository repository, CancellationToken ct) =>
        Results.Ok((await repository.ListAsync(ct)).Select(ToSummary));

    private static async Task<IResult> GetRun(string id, ITraceRepository repository, CancellationToken ct)
    {
        if (!Guid.TryParse(id, out var guid)) return Bad("Run id must be a GUID.");
        var run = await repository.GetAsync(guid, ct);
        return run is null ? Results.NotFound() : Results.Ok(new RunDetailDto(run.Id, run.Name, run.ImportedAt, run.Adapter, run.AdapterVersion, run.InputSha256, run.Events.Count, run.Diagnostics.Count));
    }

    private static async Task<IResult> GetEvents(string id, string? cursor, int? limit, ITraceRepository repository, CancellationToken ct)
    {
        if (!Guid.TryParse(id, out var guid)) return Bad("Run id must be a GUID.");
        if (limit is <= 0 || limit > MaxPageSize) return Bad($"Limit must be between 1 and {MaxPageSize}.");
        if (cursor is not null && (!long.TryParse(cursor, out var parsed) || parsed < 0)) return Bad("Cursor must be a non-negative sequence.");
        var after = cursor is null ? -1 : long.Parse(cursor, System.Globalization.CultureInfo.InvariantCulture);
        var run = await repository.GetAsync(guid, ct);
        if (run is null) return Results.NotFound();
        var take = limit ?? 50;
        var candidates = run.Events.Where(e => e.Sequence > after).OrderBy(e => e.Sequence).Take(take + 1).ToArray();
        var items = candidates.Take(take).Select(e => new EventDto(e.Id, e.Sequence, e.Timestamp, e.Kind.ToString(), e.Actor, e.Summary, e.Payload, e.ContentSha256)).ToArray();
        long? next = candidates.Length > take ? items[^1].Sequence : null;
        return Results.Ok(new EventPageDto(items, next, take));
    }

    private static async Task<IResult> GetAnalysis(string id, ITraceRepository runs, IAnalysisRepository analyses, CancellationToken ct)
    {
        var found = await Find(id, runs, ct); if (found.Error is not null) return found.Error;
        var revision = await analyses.GetLatestAsync(found.Run!.Id, ct);
        return revision is null ? Results.NotFound() : Results.Ok(ToAnalysis(revision));
    }

    private static async Task<IResult> GetAlerts(string id, ITraceRepository runs, IAnalysisRepository analyses, CancellationToken ct)
    {
        var found = await Find(id, runs, ct); if (found.Error is not null) return found.Error;
        var revision = await analyses.GetLatestAsync(found.Run!.Id, ct);
        return revision is null ? Results.NotFound() : Results.Ok(new AlertsDto(revision.Id, revision.RunId, revision.Alerts.Select(ToAlert).ToArray()));
    }

    private static async Task<IResult> Analyze(string id, ITraceRepository runs, AnalyzeRun analyze, CancellationToken ct)
    {
        if (!Guid.TryParse(id, out var guid)) return Bad("Run id must be a GUID.");
        var result = await analyze.ExecuteAsync(guid, ct);
        return result.Outcome switch { AnalyzeOutcome.NotFound => Results.NotFound(), AnalyzeOutcome.NoEvents => Results.Problem(statusCode: 409, title: "Analysis unavailable", detail: "Run has no events."), _ => Results.Ok(ToAnalysis(result.Revision!)) };
    }

    private static async Task<IResult> Compare(string? left, string? right, CompareRuns compare, CancellationToken ct)
    {
        if (!Guid.TryParse(left, out var l) || !Guid.TryParse(right, out var r)) return Bad("Left and right must be GUIDs.");
        var value = await compare.ExecuteAsync(l, r, ct); if (value is null) return Results.NotFound();
        return Results.Ok(new ComparisonDto(new(value.LeftRunId, value.LeftEventCount, value.LeftLabels, value.LeftAlertCount), new(value.RightRunId, value.RightEventCount, value.RightLabels, value.RightAlertCount), value.Note));
    }

    private static RunSummaryDto ToSummary(TraceRun r) => new(r.Id, r.Name, r.ImportedAt, r.Adapter, r.AdapterVersion, r.Events.Count, r.Diagnostics.Count);
    private static AnalysisDto ToAnalysis(AnalysisRevision r) => new(r.Id, r.RunId, r.Status.ToString(), r.CreatedAt, r.ClassifierId, r.ClassifierVersion, r.Classifications.Select(c => new ClassificationDto(c.EventId, c.Label.ToString(), c.Confidence, c.EvidenceEventIds)).ToArray());
    private static AlertDto ToAlert(PatternAlert a) => new(a.Code, a.Severity.ToString(), a.StartSequence, a.EndSequence, a.EvidenceEventIds, a.Explanation);
    private static IResult Bad(string detail) => Results.Problem(statusCode: 400, title: "Invalid request", detail: detail);
    private static async Task<(TraceRun? Run, IResult? Error)> Find(string id, ITraceRepository runs, CancellationToken ct) { if (!Guid.TryParse(id, out var guid)) return (null, Bad("Run id must be a GUID.")); var run = await runs.GetAsync(guid, ct); return run is null ? (null, Results.NotFound()) : (run, null); }
}
