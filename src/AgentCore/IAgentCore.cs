namespace AgentCore;

public interface IAgentCore
{
    Task<AnalysisOutcome> AnalyseAsync(string content, CancellationToken cancellationToken = default);
}
