using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using DlpShared;

namespace AgentCore;

public class PipeAgentCore : IAgentCore
{
    private const int DefaultMaxContentBytes = 1048576;
    // Either _provider is null (literal-value ctor was used) or it is set
    // (provider ctor used). Provider is read once per AnalyseAsync so that
    // connect, write, and read of a single request all see the same snapshot —
    // a mid-flight ctl-push that changes the timeout/cap doesn't split the call.
    private readonly Func<(string PipeName, int TimeoutMs, int MaxContentBytes, bool FailOpen)>? _provider;
    private readonly string _constantPipeName;
    private readonly int _constantTimeoutMs;
    private readonly int _constantMaxContentBytes;
    private readonly bool _constantFailOpen;

    public PipeAgentCore(string pipeName = "dlp_agent", int timeoutMs = 6000)
    {
        _constantPipeName = pipeName;
        _constantTimeoutMs = timeoutMs;
        _constantMaxContentBytes = DefaultMaxContentBytes;
        _constantFailOpen = false;   // fail closed by default (BLOCK on pipe error)
        _provider = null;
    }

    /// <summary>
    /// Provider-form ctor: ClipboardInterceptor passes a closure over a
    /// thread-safe ConfigHolder so hot-reloaded timeout / max_input_bytes /
    /// failure_mode take effect on the next AnalyseAsync call without
    /// re-instantiating PipeAgentCore.
    /// </summary>
    public PipeAgentCore(Func<(string PipeName, int TimeoutMs, int MaxContentBytes, bool FailOpen)> provider)
    {
        _provider = provider;
        _constantPipeName = "";
        _constantTimeoutMs = 0;
        _constantMaxContentBytes = DefaultMaxContentBytes;
        _constantFailOpen = false;
    }

    public async Task<AnalysisOutcome> AnalyseAsync(string content, CancellationToken ct = default)
    {
        var (pipeName, timeoutMs, maxContentBytes, failOpen) =
            _provider?.Invoke()
            ?? (_constantPipeName, _constantTimeoutMs, _constantMaxContentBytes, _constantFailOpen);

        // Unified per-channel failure verdict (clipboard.failure_mode): fail_open →
        // ALLOW, fail_closed → BLOCK. Applied to oversize content and any pipe failure.
        var failVerdict = failOpen ? AnalysisDecision.Allow : AnalysisDecision.Block;

        if (Encoding.UTF8.GetByteCount(content) > maxContentBytes)
            return new AnalysisOutcome(failVerdict, null);

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

            // 512 bytes covers "ALLOW" / "BLOCK" / "BLOCK|<reason>" (message-mode
            // pipe → the whole response arrives in one read; the orchestrator caps
            // the reason length).
            byte[] buffer = new byte[512];
            int bytesRead = await pipe.ReadAsync(buffer, cts.Token);
            string response = Encoding.UTF8.GetString(buffer, 0, bytesRead).Trim();

            if (response.Equals("ALLOW", StringComparison.OrdinalIgnoreCase))
                return new AnalysisOutcome(AnalysisDecision.Allow, null);

            // "BLOCK" or "BLOCK|<reason>": surface the end-user reason if present.
            int bar = response.IndexOf('|');
            string? reason = (bar >= 0 && bar + 1 < response.Length)
                ? response[(bar + 1)..].Trim()
                : null;
            return new AnalysisOutcome(
                AnalysisDecision.Block,
                string.IsNullOrEmpty(reason) ? null : reason);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // Caller-driven cancellation (e.g. user copied newer content):
            // propagate so the supersession path in ClipboardInterceptorService runs.
            throw;
        }
        catch (OperationCanceledException)
        {
            // Our internal deadline (CancelAfter) fired — apply the failure mode.
            return new AnalysisOutcome(failVerdict, null);
        }
        catch (Exception)
        {
            return new AnalysisOutcome(failVerdict, null);
        }
    }
}
