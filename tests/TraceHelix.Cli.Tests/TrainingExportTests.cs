using System.Text.Json;
using TraceHelix.Cli;
using Xunit;

namespace TraceHelix.Cli.Tests;

public sealed class TrainingExportTests
{
    [Fact]
    public async Task Dataset_export_runs_real_python_gate_with_space_safe_arguments()
    {
        var directory = Path.Combine(Path.GetTempPath(), $"trace helix {Guid.NewGuid():N}");
        Directory.CreateDirectory(directory);
        var previousProject = Environment.GetEnvironmentVariable("TRACEHELIX_TRAINING_PROJECT");
        Environment.SetEnvironmentVariable("TRACEHELIX_TRAINING_PROJECT", Path.Combine(directory, "poisoned-project"));
        try
        {
            var input = Path.Combine(directory, "input trace.jsonl");
            var database = Path.Combine(directory, "trace helix.db");
            var output = Path.Combine(directory, "candidate rows.jsonl");
            await File.WriteAllTextAsync(input, "{\"timestamp\":\"2026-01-01T00:00:00Z\",\"kind\":\"Message\",\"actor\":\"agent\",\"summary\":\"person@example.com\",\"payload\":{}}\n", TestContext.Current.CancellationToken);
            Assert.Equal(0, await CliProgram.RunAsync(["import", input, "--adapter", "generic-jsonl", "--db", database, "--json"], TextWriter.Null, TextWriter.Null, TestContext.Current.CancellationToken));
            var stdout = new StringWriter();
            var stderr = new StringWriter();
            var arguments = new[] { "dataset", "export", "--db", database, "--out", output, "--source-category", "fixture", "--license-or-consent", "generated fixture", "--mode", "online", "--context-before", "4", "--context-after", "0" };
            Assert.Equal(0, await CliProgram.RunAsync(arguments, stdout, stderr, TestContext.Current.CancellationToken));
            Assert.Equal(1, JsonDocument.Parse(stdout.ToString()).RootElement.GetProperty("candidateCount").GetInt32());
            var text = await File.ReadAllTextAsync(output, TestContext.Current.CancellationToken);
            Assert.DoesNotContain("person@example.com", text, StringComparison.Ordinal);
            Assert.EndsWith("\n", text, StringComparison.Ordinal);
        }
        finally
        {
            Environment.SetEnvironmentVariable("TRACEHELIX_TRAINING_PROJECT", previousProject);
            Directory.Delete(directory, true);
        }
    }

    [Theory]
    [InlineData("")]
    [InlineData("{}")]
    [InlineData("{\"candidateCount\":-1}")]
    [InlineData("{\"candidateCount\":1.0}")]
    [InlineData("{\"candidateCount\":1,\"extra\":0}")]
    [InlineData("{\"candidateCount\":1,\"candidateCount\":2}")]
    [InlineData("{\"candidateCount\":1}\n{\"candidateCount\":2}")]
    public void Dataset_export_rejects_untrusted_child_output(string output)
    {
        var error = Assert.Throws<IOException>(() => CliProgram.ParseCandidateCount(output));
        Assert.Equal("Dataset exporter returned an invalid result.", error.Message);
    }

    [Theory]
    [InlineData("{\"candidateCount\":0}", 0)]
    [InlineData("{\"candidateCount\":42}\n", 42)]
    [InlineData("{\"candidateCount\":42}\r\n", 42)]
    public void Dataset_export_accepts_one_private_result(string output, long expected) =>
        Assert.Equal(expected, CliProgram.ParseCandidateCount(output));

    [Theory]
    [InlineData("unknown-{0}")]
    [InlineData("dataset unknown-{0}")]
    [InlineData("dataset export --unknown-{0}")]
    public async Task Malformed_dataset_related_invocations_do_not_echo_attacker_input(string template)
    {
        var canary = $"canary-{Guid.NewGuid():N}";
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var arguments = string.Format(System.Globalization.CultureInfo.InvariantCulture, template, canary).Split(' ');

        Assert.Equal(2, await CliProgram.RunAsync(arguments, stdout, stderr, TestContext.Current.CancellationToken));
        Assert.DoesNotContain(canary, stdout.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain(canary, stderr.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Dataset_export_reports_private_typed_failures()
    {
        var error = new StringWriter();
        var exit = await CliProgram.RunAsync(["dataset", "export", "--db", "missing.db", "--out", "out", "--source-category", "fixture", "--license-or-consent", "fixture", "--mode", "online", "--context-before", "4", "--context-after", "1"], TextWriter.Null, error, TestContext.Current.CancellationToken);
        Assert.Equal(6, exit);
        Assert.Contains("Dataset export error", error.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain("Traceback", error.ToString(), StringComparison.Ordinal);
    }
}
