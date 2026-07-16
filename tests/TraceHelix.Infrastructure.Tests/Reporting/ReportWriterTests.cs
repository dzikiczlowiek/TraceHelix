using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Reports;
using TraceHelix.Domain.Runs;
using TraceHelix.Infrastructure.Reporting;
using Xunit;
namespace TraceHelix.Infrastructure.Tests.Reporting;

public sealed class ReportWriterTests
{
    [Fact]
    public async Task Writers_include_disclaimer_and_self_contained_evidence()
    {
        var report = CreateReport();
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var json = Path.Combine(dir, "r.json");
        var html = Path.Combine(dir, "r.html");
        await new JsonReportWriter().WriteAsync(report, json, TestContext.Current.CancellationToken);
        await new HtmlReportWriter().WriteAsync(report, html, TestContext.Current.CancellationToken);
        Assert.StartsWith("{", await File.ReadAllTextAsync(json, TestContext.Current.CancellationToken));
        var body = await File.ReadAllTextAsync(html, TestContext.Current.CancellationToken);
        Assert.Contains("not causal proof", body, StringComparison.Ordinal);
        Assert.Contains("Machine-readable evidence", body, StringComparison.Ordinal);
        Assert.DoesNotContain("<script", body, StringComparison.OrdinalIgnoreCase);
        Directory.Delete(dir, true);
    }

    [Theory]
    [InlineData("json")]
    [InlineData("html")]
    public async Task Writers_refuse_existing_output_and_preserve_its_bytes(string format)
    {
        var path = Path.GetTempFileName();
        var original = new byte[] { 0, 1, 2, 255, 42 };
        await File.WriteAllBytesAsync(path, original, TestContext.Current.CancellationToken);
        IReportWriter writer = format == "json" ? new JsonReportWriter() : new HtmlReportWriter();

        await Assert.ThrowsAsync<IOException>(() => writer.WriteAsync(CreateReport(), path, TestContext.Current.CancellationToken));

        Assert.Equal(original, await File.ReadAllBytesAsync(path, TestContext.Current.CancellationToken));
        File.Delete(path);
    }

    private static AnalysisReport CreateReport()
    {
        var run = new TraceRun(Guid.NewGuid(), "x", "input", "generic-jsonl", "1.0", DateTimeOffset.UnixEpoch, [], []);
        var revision = AnalysisRevision.Completed(Guid.NewGuid(), run.Id, "rules", "1.0", [], [], "analysis");
        return new AnalysisReport("1.0", DateTimeOffset.UnixEpoch, run, revision, "Observed patterns are not causal proof.", "report");
    }
}
