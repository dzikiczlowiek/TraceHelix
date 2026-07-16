using System.Net;
using System.Text;
using System.Text.Json;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Reports;
using TraceHelix.Infrastructure.Serialization;
namespace TraceHelix.Infrastructure.Reporting;

public sealed class JsonReportWriter : IReportWriter
{
    public string Format => "json";

    public Task WriteAsync(AnalysisReport report, string outputPath, CancellationToken cancellationToken) =>
        ReportFile.WriteNewAsync(outputPath, JsonSerializer.Serialize(report, JsonDefaults.Options) + Environment.NewLine, cancellationToken);
}

public sealed class HtmlReportWriter : IReportWriter
{
    public string Format => "html";

    public Task WriteAsync(AnalysisReport report, string outputPath, CancellationToken cancellationToken)
    {
        var encoded = WebUtility.HtmlEncode(JsonSerializer.Serialize(report, JsonDefaults.Options));
        var html = $$"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>TraceHelix report {{report.Run.Id}}</title><style>body{font-family:system-ui;max-width:75rem;margin:auto;padding:2rem}pre{white-space:pre-wrap;background:#f4f4f4;padding:1rem}.warning{border-left:.4rem solid #b45f06;padding:1rem}</style></head><body><h1>TraceHelix audit report</h1><p class="warning">{{WebUtility.HtmlEncode(report.Disclaimer)}}</p><p>Input SHA-256: <code>{{report.Run.InputSha256}}</code></p><p>Adapter: {{WebUtility.HtmlEncode(report.Run.Adapter)}} {{WebUtility.HtmlEncode(report.Run.AdapterVersion)}}</p><p>Classifier: {{WebUtility.HtmlEncode(report.Analysis.ClassifierId)}} {{WebUtility.HtmlEncode(report.Analysis.ClassifierVersion)}}</p><h2>Machine-readable evidence</h2><pre>{{encoded}}</pre></body></html>""";
        return ReportFile.WriteNewAsync(outputPath, html, cancellationToken);
    }
}

internal static class ReportFile
{
    private static readonly UTF8Encoding Utf8WithoutBom = new(false);

    public static async Task WriteNewAsync(string path, string content, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var directory = Path.GetDirectoryName(Path.GetFullPath(path));
        if (directory is not null)
            Directory.CreateDirectory(directory);

        await using var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None, 4096, FileOptions.Asynchronous);
        var bytes = Utf8WithoutBom.GetBytes(content);
        await stream.WriteAsync(bytes, cancellationToken);
        await stream.FlushAsync(cancellationToken);
    }
}
