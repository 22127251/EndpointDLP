using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace AgentCore;

public class PipeAgentCore : IAgentCore
{
    private const int MaxContentBytes = 1048576;
    private readonly string _pipeName;
    private readonly int _timeoutMs;

    public PipeAgentCore(string pipeName = "dlp_agent", int timeoutMs = 6000)
    {
        _pipeName = pipeName;
        _timeoutMs = timeoutMs;
    }

    public async Task<AnalysisDecision> AnalyseAsync(string content, CancellationToken ct = default)
    {
        if (Encoding.UTF8.GetByteCount(content) > MaxContentBytes)
            return AnalysisDecision.Block;

        // Overall deadline that covers connect + write + read. Without this,
        // ReadAsync after a successful connect could block indefinitely if the
        // orchestrator never writes a response.
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(_timeoutMs);

        try
        {
            using var pipe = new NamedPipeClientStream(".", _pipeName,
                PipeDirection.InOut, PipeOptions.Asynchronous);

            await pipe.ConnectAsync(_timeoutMs, cts.Token);
            pipe.ReadMode = PipeTransmissionMode.Message;

            var request = new
            {
                channel = "clipboard",
                kind = "text",
                text = content,
                metadata = new { timestamp = DateTime.UtcNow.ToString("O") }
            };
            byte[] requestBytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(request));
            await pipe.WriteAsync(requestBytes, cts.Token);
            await pipe.FlushAsync(cts.Token);

            byte[] buffer = new byte[16];
            int bytesRead = await pipe.ReadAsync(buffer, cts.Token);
            string response = Encoding.UTF8.GetString(buffer, 0, bytesRead).Trim();

            return response.Equals("ALLOW", StringComparison.OrdinalIgnoreCase)
                ? AnalysisDecision.Allow
                : AnalysisDecision.Block;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Caller-driven cancellation (e.g. user copied newer content):
            // propagate so the supersession path in ClipboardInterceptorService runs.
            throw;
        }
        catch (OperationCanceledException)
        {
            // Our internal deadline (CancelAfter) fired — fail closed.
            return AnalysisDecision.Block;
        }
        catch (Exception)
        {
            return AnalysisDecision.Block;
        }
    }
}
