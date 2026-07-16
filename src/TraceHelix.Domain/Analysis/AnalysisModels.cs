namespace TraceHelix.Domain.Analysis;

public enum StepLabel { Explore, Plan, Execute, Verify, Recover, Unknown }
public enum AlertSeverity { Info, Warning, Critical }
public enum AnalysisStatus { Pending, Completed, Failed }
public sealed record StepClassification
{
    public StepClassification(Guid eventId, StepLabel label, float confidence, string classifierId, string classifierVersion, IReadOnlyList<Guid> evidenceEventIds, IReadOnlyDictionary<string, float>? scores) { if (eventId == Guid.Empty) throw new ArgumentException("Event ID required.", nameof(eventId)); if (confidence is < 0 or > 1) throw new ArgumentOutOfRangeException(nameof(confidence)); if (string.IsNullOrWhiteSpace(classifierId) || string.IsNullOrWhiteSpace(classifierVersion)) throw new ArgumentException("Classifier identity and version required."); if (evidenceEventIds is null || !evidenceEventIds.Contains(eventId)) throw new ArgumentException("Classification must cite its event.", nameof(evidenceEventIds)); EventId = eventId; Label = label; Confidence = confidence; ClassifierId = classifierId; ClassifierVersion = classifierVersion; EvidenceEventIds = evidenceEventIds; Scores = scores; }
    public Guid EventId { get; init; }
    public StepLabel Label { get; init; }
    public float Confidence { get; init; }
    public string ClassifierId { get; init; }
    public string ClassifierVersion { get; init; }
    public IReadOnlyList<Guid> EvidenceEventIds { get; init; }
    public IReadOnlyDictionary<string, float>? Scores { get; init; }
}
public sealed record PatternAlert
{
    public PatternAlert(string code, AlertSeverity severity, long startSequence, long endSequence, string detectorVersion, IReadOnlyDictionary<string, string> parameters, IReadOnlyList<Guid> evidenceEventIds, string explanation) { if (string.IsNullOrWhiteSpace(code) || string.IsNullOrWhiteSpace(detectorVersion)) throw new ArgumentException("Detector code and version required."); if (startSequence < 0 || endSequence < startSequence) throw new ArgumentOutOfRangeException(nameof(startSequence)); if (evidenceEventIds is null || evidenceEventIds.Count == 0) throw new ArgumentException("Alert evidence required.", nameof(evidenceEventIds)); Code = code; Severity = severity; StartSequence = startSequence; EndSequence = endSequence; DetectorVersion = detectorVersion; Parameters = parameters; EvidenceEventIds = evidenceEventIds; Explanation = explanation; }
    public string Code { get; init; }
    public AlertSeverity Severity { get; init; }
    public long StartSequence { get; init; }
    public long EndSequence { get; init; }
    public string DetectorVersion { get; init; }
    public IReadOnlyDictionary<string, string> Parameters { get; init; }
    public IReadOnlyList<Guid> EvidenceEventIds { get; init; }
    public string Explanation { get; init; }
}
public sealed record AnalysisRevision(Guid Id, Guid RunId, AnalysisStatus Status, DateTimeOffset CreatedAt, string ClassifierId, string ClassifierVersion, IReadOnlyList<StepClassification> Classifications, IReadOnlyList<PatternAlert> Alerts, string? ContentSha256, string? Failure)
{
    public static AnalysisRevision Completed(Guid id, Guid runId, string classifierId, string classifierVersion, IReadOnlyList<StepClassification> classifications, IReadOnlyList<PatternAlert> alerts, string contentSha256) { if (string.IsNullOrWhiteSpace(contentSha256)) throw new ArgumentException("Hash required.", nameof(contentSha256)); return new(id, runId, AnalysisStatus.Completed, DateTimeOffset.UtcNow, classifierId, classifierVersion, classifications, alerts, contentSha256, null); }
}
