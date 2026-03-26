namespace AgentCore;

public interface IAgentCore
{
    Task<AnalysisDecision> AnalyseAsync(string content, CancellationToken cancellationToken = default);
}
