using TraceHelix.Application;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Runs;
using TraceHelix.Domain.Traces;
using Xunit;
namespace TraceHelix.Application.Tests;

public sealed class UseCaseTests
{
    [Fact] public async Task Analyze_missing_run_is_typed_not_found() { var result = await new AnalyzeRun(new EmptyTraceRepo(), new EmptyAnalysisRepo(), new EmptyClassifier(), []).ExecuteAsync(Guid.NewGuid(), TestContext.Current.CancellationToken); Assert.Equal(AnalyzeOutcome.NotFound, result.Outcome); }
    private sealed class EmptyTraceRepo : ITraceRepository { public Task<TraceRun?> FindByImportAsync(string h, string a, string v, CancellationToken c) => Task.FromResult<TraceRun?>(null); public Task<TraceRun?> GetAsync(Guid id, CancellationToken c) => Task.FromResult<TraceRun?>(null); public Task<IReadOnlyList<TraceRun>> ListAsync(CancellationToken c) => Task.FromResult<IReadOnlyList<TraceRun>>([]); public Task<bool> TrySaveAsync(TraceRun r, CancellationToken c) => Task.FromResult(true); }
    private sealed class EmptyAnalysisRepo : IAnalysisRepository { public Task SaveAsync(AnalysisRevision r, CancellationToken c) => Task.CompletedTask; public Task<AnalysisRevision?> GetLatestAsync(Guid id, CancellationToken c) => Task.FromResult<AnalysisRevision?>(null); }
    private sealed class EmptyClassifier : IStepClassifier { public string Id => "rules"; public string Version => "1.0"; public Task<IReadOnlyList<StepClassification>> ClassifyAsync(IReadOnlyList<TraceEvent> e, CancellationToken c) => Task.FromResult<IReadOnlyList<StepClassification>>([]); }
}
