namespace AgentCore;

public enum AnalysisDecision
{
    Allow,
    Block
}

/// <summary>
/// Outcome of an analysis: the decision plus an optional end-user block reason.
/// Reason is the message the orchestrator may append as "BLOCK|&lt;reason&gt;"
/// (the policy's user_message or a friendly failure message); it is null on
/// ALLOW and on failure-mode blocks where no server reason is available.
/// </summary>
public readonly record struct AnalysisOutcome(AnalysisDecision Decision, string? Reason);
