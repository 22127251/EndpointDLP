using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using DlpShared;

namespace AgentCore;

public class PipeAgentCore : IAgentCore
{
    private const int MaxContentBytes = 1048576;
    // Either _provider is null (literal-value ctor was used) or it is set
    // (provider ctor used). Provider is read once per AnalyseAsync so that
    // connect, write, and read of a single request all see the same pair —
    // a mid-flight ctl-push that changes the timeout doesn't split the call.
    private readonly Func<(string PipeName, int TimeoutMs)>? _provider;
    private readonly string _constantPipeName;
    private readonly int _constantTimeoutMs;

    public PipeAgentCore(string pipeName = "dlp_agent", int timeoutMs = 6000)
    {
        _constantPipeName = pipeName;
        _constantTimeoutMs = timeoutMs;
        _provider = null;
    }

    /// <summary>
    /// Provider-form ctor: ClipboardInterceptor passes a closure over a
    /// thread-safe ConfigHolder so hot-reloaded timeouts take effect on the
    /// next AnalyseAsync call without re-instantiating PipeAgentCore.
    /// </summary>
    public PipeAgentCore(Func<(string PipeName, int TimeoutMs)> provider)
    {
        _provider = provider;
        _constantPipeName = "";
        _constantTimeoutMs = 0;
    }

    public async Task<AnalysisDecision> AnalyseAsync(string content, CancellationToken ct = default)
    {
        if (Encoding.UTF8.GetByteCount(content) > MaxContentBytes)
            return AnalysisDecision.Block;

        var (pipeName, timeoutMs) = _provider?.Invoke() ?? (_constantPipeName, _constantTimeoutMs);

        // Overall deadline that covers connect + write + read. Without this,
        // ReadAsync after a successful connect could block indefinitely if the
        // orchestrator never writes a response.
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeoutMs);

        try
        {
            using var pipe = new NamedPipeClientStream(".", PipeNameHelper.ToBareName(pipeName),
                PipeDirection.InOut, PipeOptions.Asynchronous);

            await pipe.ConnectAsync(timeoutMs, cts.Token);
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
