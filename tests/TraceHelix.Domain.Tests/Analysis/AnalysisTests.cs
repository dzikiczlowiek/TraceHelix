using TraceHelix.Domain.Analysis;
using Xunit;
namespace TraceHelix.Domain.Tests.Analysis;

public sealed class AnalysisTests
{
    [Fact] public void Classification_requires_own_event_as_evidence() => Assert.Throws<ArgumentException>(() => new StepClassification(Guid.NewGuid(), StepLabel.Explore, .8f, "rules", "1.0", [], null));
    [Fact] public void Alert_requires_evidence() => Assert.Throws<ArgumentException>(() => new PatternAlert("THX001_NO_PROGRESS_LOOP", AlertSeverity.Warning, 0, 2, "1.0", new Dictionary<string, string>(), [], "observed"));
    [Fact] public void Completed_revision_has_hash() { var id = Guid.NewGuid(); var c = new StepClassification(id, StepLabel.Unknown, .2f, "rules", "1.0", [id], null); var r = AnalysisRevision.Completed(Guid.NewGuid(), Guid.NewGuid(), "rules", "1.0", [c], [], "abc"); Assert.Equal(AnalysisStatus.Completed, r.Status); Assert.Equal("abc", r.ContentSha256); }
}
