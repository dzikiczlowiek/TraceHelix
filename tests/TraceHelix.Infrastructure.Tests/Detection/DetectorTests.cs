using TraceHelix.Domain.Analysis;
using TraceHelix.Infrastructure.Detection;
using Xunit;
namespace TraceHelix.Infrastructure.Tests.Detection;

public sealed class DetectorTests
{
    private static StepClassification C(StepLabel l) { var id = Guid.NewGuid(); return new(id, l, .8f, "rules", "1.0", [id], null); }
    [Fact] public void Detector_catalog_contains_all_codes() { var codes = DefaultDetectors.Create().Select(x => x.Code).ToArray(); Assert.Equal(["THX001_NO_PROGRESS_LOOP", "THX002_PLAN_LOOP", "THX003_VERIFICATION_GAP", "THX004_PREMATURE_SUCCESS", "THX005_RECOVERY_STORM", "THX006_TOOL_ERROR_CASCADE"], codes); }
    [Fact] public async Task Plan_loop_emits_evidence() { var cs = new[] { C(StepLabel.Plan), C(StepLabel.Plan), C(StepLabel.Plan) }; var alerts = await DefaultDetectors.Create()[1].DetectAsync([], cs, TestContext.Current.CancellationToken); Assert.Single(alerts); Assert.Equal(3, alerts[0].EvidenceEventIds.Count); Assert.Equal("3", alerts[0].Parameters["threshold"]); }
    [Fact] public async Task Every_detector_honors_pre_cancellation() { using var cancellation = new CancellationTokenSource(); cancellation.Cancel(); foreach (var detector in DefaultDetectors.Create()) await Assert.ThrowsAnyAsync<OperationCanceledException>(() => detector.DetectAsync([], [], cancellation.Token)); }
}
