namespace AgentCore;

public class StubAgentCore : IAgentCore
{
    public async Task<AnalysisDecision> AnalyseAsync(string content, CancellationToken cancellationToken = default)
    {
        // Task.Run itself is not cancellable — cancellation is checked inside the loop
        return await Task.Run(() =>
        {
            var preview = content.Length > 80 ? content[..80] + "..." : content;
            Console.WriteLine($"\n[DLP] Intercepted: \"{preview}\"");

            while (true)
            {
                cancellationToken.ThrowIfCancellationRequested();
                Console.Write("Allow or Block? [a/b]: ");
                var input = Console.ReadLine()?.Trim().ToLowerInvariant();
                // Check after ReadLine: if a new copy arrived while we were waiting,
                // discard this stale input and propagate cancellation.
                cancellationToken.ThrowIfCancellationRequested();

                if (input == "a") return AnalysisDecision.Allow;
                if (input == "b") return AnalysisDecision.Block;
                Console.WriteLine("  Invalid input. Enter 'a' to allow or 'b' to block.");
            }
        }, CancellationToken.None);
    }
}
