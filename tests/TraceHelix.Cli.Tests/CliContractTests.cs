using System.ComponentModel;
using System.Runtime.InteropServices;
using TraceHelix.Cli;
using Xunit;
namespace TraceHelix.Cli.Tests;

public sealed class CliContractTests
{
    [Fact] public async Task Unknown_command_is_usage_error() => Assert.Equal(2, await CliProgram.RunAsync(["unknown"], TextWriter.Null, TextWriter.Null, TestContext.Current.CancellationToken));

    [Fact]
    public async Task Pre_cancelled_real_operation_returns_130()
    {
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        var error = new StringWriter();
        Assert.Equal(130, await CliProgram.RunAsync(["list", "--db", Path.GetTempFileName(), "--json"], TextWriter.Null, error, cancellation.Token));
        Assert.Contains("canceled", error.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task Hard_record_limit_is_typed_and_does_not_persist_partial_run()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        var input = Path.Combine(dir, "malformed.jsonl");
        var db = Path.Combine(dir, "tracehelix.db");
        await File.WriteAllTextAsync(input, string.Concat(Enumerable.Repeat("x\n", 100_001)), TestContext.Current.CancellationToken);
        var error = new StringWriter();
        var exit = await CliProgram.RunAsync(["import", input, "--adapter", "generic-jsonl", "--db", db, "--json"], TextWriter.Null, error, TestContext.Current.CancellationToken);
        Assert.Equal(3, exit);
        Assert.Contains("RECORD_COUNT_LIMIT", error.ToString(), StringComparison.Ordinal);
        var output = new StringWriter();
        Assert.Equal(0, await CliProgram.RunAsync(["list", "--db", db, "--json"], output, TextWriter.Null, TestContext.Current.CancellationToken));
        Assert.Equal("[]", output.ToString().Trim());
        Directory.Delete(dir, true);
    }

    [Fact]
    public async Task Report_cannot_overwrite_database_via_lexical_alias()
    {
        var dir = CreateDirectory();
        var db = Path.Combine(dir, "tracehelix.db");
        await CliProgram.RunAsync(["list", "--db", db, "--json"], TextWriter.Null, TextWriter.Null, TestContext.Current.CancellationToken);
        var before = await File.ReadAllBytesAsync(db, TestContext.Current.CancellationToken);
        var error = new StringWriter();
        var alias = Path.Combine(dir, ".", "tracehelix.db");
        var exit = await CliProgram.RunAsync(["report", Guid.NewGuid().ToString(), "--db", db, "--format", "json", "--out", alias], TextWriter.Null, error, TestContext.Current.CancellationToken);
        Assert.Equal(2, exit);
        Assert.Contains("same path", error.ToString(), StringComparison.OrdinalIgnoreCase);
        Assert.Equal(before, await File.ReadAllBytesAsync(db, TestContext.Current.CancellationToken));
        Directory.Delete(dir, true);
    }

    [Fact]
    public async Task Report_through_symlinked_parent_refuses_database_and_preserves_it()
    {
        var dir = CreateDirectory();
        var real = Path.Combine(dir, "real");
        var link = Path.Combine(dir, "link");
        Directory.CreateDirectory(real);
        try { Directory.CreateSymbolicLink(link, real); }
        catch (Exception ex) when (ex is PlatformNotSupportedException or UnauthorizedAccessException or IOException)
        {
            Assert.Skip($"Directory symlinks unavailable: {ex.Message}");
            return;
        }
        var (runId, db) = await CreateAnalyzedRun(real);
        var before = await File.ReadAllBytesAsync(db, TestContext.Current.CancellationToken);
        var error = new StringWriter();
        var exit = await CliProgram.RunAsync(["report", runId, "--db", db, "--format", "json", "--out", Path.Combine(link, "tracehelix.db")], TextWriter.Null, error, TestContext.Current.CancellationToken);
        Assert.Equal(5, exit);
        Assert.Contains("I/O error", error.ToString(), StringComparison.Ordinal);
        Assert.Equal(before, await File.ReadAllBytesAsync(db, TestContext.Current.CancellationToken));
        Directory.Delete(dir, true);
    }

    [Fact]
    public async Task Report_to_hard_link_refuses_database_and_preserves_it()
    {
        var dir = CreateDirectory();
        var (runId, db) = await CreateAnalyzedRun(dir);
        var alias = Path.Combine(dir, "report.json");
        try { CreateHardLink(alias, db); }
        catch (Exception ex) when (ex is PlatformNotSupportedException or UnauthorizedAccessException or IOException)
        {
            Assert.Skip($"Hard links unavailable: {ex.Message}");
            return;
        }
        var before = await File.ReadAllBytesAsync(db, TestContext.Current.CancellationToken);
        var error = new StringWriter();
        var exit = await CliProgram.RunAsync(["report", runId, "--db", db, "--format", "json", "--out", alias], TextWriter.Null, error, TestContext.Current.CancellationToken);
        Assert.Equal(5, exit);
        Assert.Contains("I/O error", error.ToString(), StringComparison.Ordinal);
        Assert.Equal(before, await File.ReadAllBytesAsync(db, TestContext.Current.CancellationToken));
        Directory.Delete(dir, true);
    }

    private static void CreateHardLink(string linkPath, string targetPath)
    {
        var succeeded = OperatingSystem.IsWindows()
            ? CreateHardLinkWindows(linkPath, targetPath, IntPtr.Zero)
            : LinkUnix(targetPath, linkPath) == 0;
        if (!succeeded)
            throw new IOException(new Win32Exception(Marshal.GetLastPInvokeError()).Message);
    }

    [DllImport("libc", EntryPoint = "link", SetLastError = true)]
    private static extern int LinkUnix(string existingPath, string newPath);

    [DllImport("kernel32.dll", EntryPoint = "CreateHardLinkW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateHardLinkWindows(string newFileName, string existingFileName, IntPtr securityAttributes);

    private static string CreateDirectory()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        return dir;
    }

    private static async Task<(string RunId, string Database)> CreateAnalyzedRun(string dir)
    {
        var input = Path.Combine(dir, "input.jsonl");
        var db = Path.Combine(dir, "tracehelix.db");
        await File.WriteAllTextAsync(input, "{\"actor\":\"agent\",\"summary\":\"hello\"}\n", TestContext.Current.CancellationToken);
        var imported = new StringWriter();
        Assert.Equal(0, await CliProgram.RunAsync(["import", input, "--adapter", "generic-jsonl", "--db", db, "--json"], imported, TextWriter.Null, TestContext.Current.CancellationToken));
        var runId = System.Text.Json.JsonDocument.Parse(imported.ToString()).RootElement.GetProperty("runId").GetGuid().ToString();
        Assert.Equal(0, await CliProgram.RunAsync(["analyze", runId, "--db", db, "--classifier", "rules", "--json"], TextWriter.Null, TextWriter.Null, TestContext.Current.CancellationToken));
        return (runId, db);
    }
}
