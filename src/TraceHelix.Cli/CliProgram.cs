using System.ComponentModel;
using System.Diagnostics;
using System.Text.Json;
using TraceHelix.Application;
using TraceHelix.Application.Abstractions;
using TraceHelix.Infrastructure.Adapters;
using TraceHelix.Infrastructure.Classification;
using TraceHelix.Infrastructure.Detection;
using TraceHelix.Infrastructure.Persistence;
using TraceHelix.Infrastructure.Reporting;
using TraceHelix.Infrastructure.Serialization;
namespace TraceHelix.Cli;

public static class CliProgram
{
    public static async Task<int> RunAsync(string[] args, TextWriter stdout, TextWriter stderr, CancellationToken cancellationToken)
    {
        if (args.Length == 0) { await stderr.WriteLineAsync(Usage()); return 2; }
        try { cancellationToken.ThrowIfCancellationRequested(); var parsed = Arguments.Parse(args); return args[0] switch { "import" => await Import(parsed, stdout, cancellationToken), "analyze" => await Analyze(parsed, stdout, cancellationToken), "list" => await List(parsed, stdout, cancellationToken), "show" => await Show(parsed, stdout, cancellationToken), "compare" => await Compare(parsed, stdout, cancellationToken), "report" => await Report(parsed, stdout, cancellationToken), "dataset" => await Dataset(parsed, stdout, stderr, cancellationToken), _ => throw new UsageException("Unknown command.") }; }
        catch (OperationCanceledException) { await stderr.WriteLineAsync("Operation canceled."); return 130; }
        catch (ImportLimitException ex) { await stderr.WriteLineAsync($"Import error [{ex.Code}]: {ex.Message}"); return 3; }
        catch (UsageException ex) { await stderr.WriteLineAsync(ex.Message); await stderr.WriteLineAsync(Usage()); return 2; }
        catch (JsonException ex) { await stderr.WriteLineAsync($"Import error: {ex.Message}"); return 3; }
        catch (Microsoft.Data.Sqlite.SqliteException ex) { await stderr.WriteLineAsync($"Storage error: {ex.Message}"); return 5; }
        catch (IOException ex) { await stderr.WriteLineAsync($"I/O error: {ex.Message}"); return 5; }
        catch (UnauthorizedAccessException ex) { await stderr.WriteLineAsync($"I/O error: {ex.Message}"); return 5; }
    }
    private static async Task<int> Import(Arguments a, TextWriter output, CancellationToken ct) { var path = a.Position(0); var db = a.Required("db"); if (!File.Exists(path)) throw new UsageException($"Input does not exist: {path}"); if (!string.Equals(a.Required("adapter"), "generic-jsonl", StringComparison.Ordinal)) throw new UsageException("Only adapter 'generic-jsonl' is available in this slice."); var store = new SqliteStore(db); var result = await new ImportTrace(new GenericJsonlAdapter(), store).ExecuteAsync(path, ct); await Write(output, new { outcome = result.Outcome, runId = result.Run.Id, inputSha256 = result.Run.InputSha256, eventCount = result.Run.Events.Count, diagnostics = result.Run.Diagnostics }, ct); return result.Outcome == ImportOutcome.Partial ? 3 : 0; }
    private static async Task<int> Analyze(Arguments a, TextWriter output, CancellationToken ct) { var id = a.GuidPosition(0); var db = a.Required("db"); if (!string.Equals(a.Value("classifier") ?? "rules", "rules", StringComparison.Ordinal)) throw new UsageException("Only classifier 'rules' is available."); var store = new SqliteStore(db); var result = await new AnalyzeRun(store, store, new RuleBasedStepClassifier(), DefaultDetectors.Create()).ExecuteAsync(id, ct); if (result.Outcome == AnalyzeOutcome.NotFound) { await output.WriteLineAsync("{\"error\":\"run-not-found\"}"); return 4; } if (result.Outcome == AnalyzeOutcome.NoEvents) { await output.WriteLineAsync("{\"error\":\"run-has-no-events\"}"); return 4; } await Write(output, new { outcome = result.Outcome, revisionId = result.Revision!.Id, status = result.Revision.Status, classificationCount = result.Revision.Classifications.Count, alertCount = result.Revision.Alerts.Count, contentSha256 = result.Revision.ContentSha256 }, ct); return 0; }
    private static async Task<int> List(Arguments a, TextWriter output, CancellationToken ct) { var store = new SqliteStore(a.Required("db")); var runs = await store.ListAsync(ct); await Write(output, runs.Select(r => new { runId = r.Id, name = r.Name, inputSha256 = r.InputSha256, adapter = r.Adapter, adapterVersion = r.AdapterVersion, importedAt = r.ImportedAt, eventCount = r.Events.Count, diagnosticCount = r.Diagnostics.Count }), ct); return 0; }
    private static async Task<int> Show(Arguments a, TextWriter output, CancellationToken ct) { var id = a.GuidPosition(0); var store = new SqliteStore(a.Required("db")); var run = await store.GetAsync(id, ct); if (run is null) { await output.WriteLineAsync("{\"error\":\"run-not-found\"}"); return 5; } var analysis = await store.GetLatestAsync(id, ct); await Write(output, new { runId = run.Id, run.Name, run.InputSha256, run.Adapter, run.AdapterVersion, run.ImportedAt, eventCount = run.Events.Count, diagnostics = run.Diagnostics, events = a.Flag("events") ? run.Events : null, analysis = analysis is null ? null : new { analysis.Id, analysis.Status, analysis.ClassifierId, analysis.ClassifierVersion, classificationCount = analysis.Classifications.Count, alertCount = analysis.Alerts.Count, analysis.ContentSha256 }, alerts = a.Flag("alerts") ? analysis?.Alerts : null }, ct); return 0; }
    private static async Task<int> Compare(Arguments a, TextWriter output, CancellationToken ct) { var left = a.GuidPosition(0); var right = a.GuidPosition(1); var store = new SqliteStore(a.Required("db")); var comparison = await new CompareRuns(store, store).ExecuteAsync(left, right, ct); if (comparison is null) { await output.WriteLineAsync("{\"error\":\"run-not-found\"}"); return 5; } await Write(output, comparison, ct); return 0; }
    private static async Task<int> Report(Arguments a, TextWriter output, CancellationToken ct)
    {
        var id = a.GuidPosition(0);
        var databasePath = NormalizePath(a.Required("db"));
        var outputPath = NormalizePath(a.Required("out"));
        if (string.Equals(databasePath, outputPath, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal))
            throw new UsageException("Report output and database resolve to the same path.");
        var store = new SqliteStore(databasePath);
        var format = a.Required("format");
        IReportWriter writer = format switch { "json" => new JsonReportWriter(), "html" => new HtmlReportWriter(), _ => throw new UsageException("Report format must be json or html.") };
        var report = await new ExportReport(store, store).CreateAsync(id, ct);
        if (report is null) { await output.WriteLineAsync("{\"error\":\"analysis-not-found\"}"); return 4; }
        await writer.WriteAsync(report, outputPath, ct);
        var artifactSha256 = await TraceHelix.Infrastructure.Hashing.Sha256ContentHasher.HashFileAsync(outputPath, ct);
        await Write(output, new { runId = id, format, outputPath, artifactSha256, contentIdentitySha256 = report.ContentIdentitySha256 }, ct);
        return 0;
    }
    private static async Task<int> Dataset(Arguments a, TextWriter stdout, TextWriter stderr, CancellationToken ct)
    {
        if (!string.Equals(a.Position(0), "export", StringComparison.Ordinal) || a.PositionCount != 1) throw new UsageException("Dataset subcommand must be export with no extra arguments.");
        a.AllowOnly("db", "out", "source-category", "license-or-consent", "mode", "context-before", "context-after");
        var project = FindTrainingProject();
        var start = new ProcessStartInfo { FileName = "uv", WorkingDirectory = project, UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true };
        foreach (var value in new[] { "run", "--offline", "--frozen", "--no-sync", "--project", project, "python", "-m", "tracehelix_training.extract", "--db", a.Required("db"), "--out", a.Required("out"), "--source-category", a.Required("source-category"), "--license-or-consent", a.Required("license-or-consent"), "--mode", a.Required("mode"), "--context-before", a.Required("context-before"), "--context-after", a.Required("context-after") }) start.ArgumentList.Add(value);
        Process process;
        try { process = Process.Start(start) ?? throw new IOException("Dataset exporter is unavailable."); }
        catch (Win32Exception) { throw new IOException("Dataset exporter is unavailable."); }
        catch (InvalidOperationException) { throw new IOException("Dataset exporter is unavailable."); }
        using (process)
        using (ct.Register(() => { try { if (!process.HasExited) process.Kill(true); } catch (InvalidOperationException) { } }))
        {
            var outputTask = process.StandardOutput.ReadToEndAsync(ct);
            var errorTask = process.StandardError.ReadToEndAsync(ct);
            await process.WaitForExitAsync(ct);
            var childOutput = await outputTask;
            _ = await errorTask; // Drain but never relay untrusted child diagnostics.
            if (process.ExitCode != 0)
            {
                await stderr.WriteLineAsync("Dataset export error: candidate export failed.");
                return process.ExitCode;
            }
            var candidateCount = ParseCandidateCount(childOutput);
            await Write(stdout, new { candidateCount }, ct);
            return 0;
        }
    }
    internal static long ParseCandidateCount(string output)
    {
        var json = output.EndsWith("\r\n", StringComparison.Ordinal) ? output[..^2] : output.EndsWith('\n') ? output[..^1] : output;
        if (json.Length == 0 || json.Contains('\r') || json.Contains('\n')) throw new IOException("Dataset exporter returned an invalid result.");
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || root.EnumerateObject().Count() != 1 || !root.TryGetProperty("candidateCount", out var count) || count.ValueKind != JsonValueKind.Number || count.GetRawText().Any(character => character is < '0' or > '9') || !count.TryGetInt64(out var value) || value < 0)
                throw new IOException("Dataset exporter returned an invalid result.");
            return value;
        }
        catch (JsonException) { throw new IOException("Dataset exporter returned an invalid result."); }
    }
    private static string FindTrainingProject()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        {
            var candidate = Path.Combine(directory.FullName, "training");
            if (File.Exists(Path.Combine(candidate, "pyproject.toml"))) return candidate;
        }
        throw new IOException("Training exporter was not found.");
    }
    private static string NormalizePath(string path)
    {
        var fullPath = Path.GetFullPath(path);
        return File.Exists(fullPath) ? new FileInfo(fullPath).ResolveLinkTarget(true)?.FullName ?? fullPath : fullPath;
    }
    private static async Task Write(TextWriter output, object value, CancellationToken ct) { var json = JsonSerializer.Serialize(value, JsonDefaults.Options); await output.WriteLineAsync(json.AsMemory(), ct); }
    private static string Usage() => "Usage: tracehelix import <path> --adapter generic-jsonl --db <path> --json | analyze <run-id> --db <path> --classifier rules --json | list --db <path> --json | show <run-id> --db <path> --events --alerts --json | compare <run-a> <run-b> --db <path> --json | report <run-id> --db <path> --format json|html --out <path> | dataset export --db <path> --out <path> --source-category fake|fixture|live --license-or-consent <text> --mode online|offline-analysis --context-before <0..64> --context-after <0..64>";
    private sealed class UsageException(string message) : Exception(message);
    private sealed class Arguments
    {
        private static readonly HashSet<string> Flags = ["json", "events", "alerts"]; private static readonly HashSet<string> Values = ["adapter", "db", "classifier", "detectors", "format", "out", "source-category", "license-or-consent", "mode", "context-before", "context-after"]; private readonly List<string> positions = []; private readonly Dictionary<string, string?> options = new(StringComparer.Ordinal);
        public static Arguments Parse(string[] args) { var result = new Arguments(); for (var i = 1; i < args.Length; i++) { if (!args[i].StartsWith("--", StringComparison.Ordinal)) { result.positions.Add(args[i]); continue; } var key = args[i][2..]; if (!Flags.Contains(key) && !Values.Contains(key)) throw new UsageException("Unknown option."); if (result.options.ContainsKey(key)) throw new UsageException($"Duplicate option: --{key}"); if (Flags.Contains(key)) { result.options[key] = null; continue; } if (++i >= args.Length || args[i].StartsWith("--", StringComparison.Ordinal)) throw new UsageException($"Missing value for --{key}"); result.options[key] = args[i]; } return result; }
        public int PositionCount => positions.Count;
        public void AllowOnly(params string[] keys) { var allowed = keys.ToHashSet(StringComparer.Ordinal); if (options.Keys.Any(key => !allowed.Contains(key))) throw new UsageException("Option is not valid for this command."); }
        public string Position(int index) => index < positions.Count ? positions[index] : throw new UsageException("Missing positional argument."); public Guid GuidPosition(int index) => Guid.TryParse(Position(index), out var id) ? id : throw new UsageException("Run ID must be a GUID."); public string? Value(string key) => options.GetValueOrDefault(key); public string Required(string key) => Value(key) ?? throw new UsageException($"Missing required option --{key}."); public bool Flag(string key) => options.ContainsKey(key);
    }
}
