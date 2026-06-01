using System.IO.Pipes;
using System.Text.Json;

namespace DlpShared;

/// <summary>
/// Long-lived ctl-pipe subscriber for ClipboardInterceptor and Controller.
/// Subscribes once and stays connected, invoking <paramref name="onConfigChanged"/>
/// for each config_snapshot / config_update push.
/// </summary>
public sealed class CtlPipeSubscriber
{
    private static readonly TimeSpan BackoffInitial = TimeSpan.FromMilliseconds(250);
    private static readonly TimeSpan BackoffMax = TimeSpan.FromSeconds(4);
    private const int ConnectTimeoutMs = 5000;

    private readonly string _ctlPipeName;
    private readonly string _componentName;
    private readonly Action<JsonElement> _onConfigChanged;

    public CtlPipeSubscriber(string ctlPipeName, string componentName, Action<JsonElement> onConfigChanged)
    {
        _ctlPipeName = ctlPipeName;
        _componentName = componentName;
        _onConfigChanged = onConfigChanged;
    }

    /// <summary>Optional structured-log sink. Defaults to Console.Error.</summary>
    public Action<string>? OnLog { get; set; }

    private void Log(string message) => (OnLog ?? Console.Error.WriteLine)(message);

    /// <summary>
    /// Long-lived loop: connect → subscribe → read pushes → on disconnect, back off + reconnect.
    /// Exits cleanly when <paramref name="ct"/> is cancelled or the orchestrator
    /// rejects with a non-retryable error code.
    /// </summary>
    public async Task StartAsync(CancellationToken ct)
    {
        var backoff = BackoffInitial;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await RunOnceAsync(ct);
                backoff = BackoffInitial;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                return;
            }
            catch (CtlAlreadySubscribedException ex)
            {
                Log($"ctl: already_subscribed (component={_componentName}); backing off — {ex.Message}");
                if (await DelayOrCancel(backoff, ct)) return;
                backoff = NextBackoff(backoff);
            }
            catch (CtlFatalErrorException ex)
            {
                Log($"ctl: fatal error code={ex.Code} — exiting subscriber. {ex.Message}");
                return;
            }
            catch (Exception ex)
            {
                Log($"ctl: subscriber error: {ex.Message}; reconnecting in {backoff.TotalMilliseconds:F0}ms");
                if (await DelayOrCancel(backoff, ct)) return;
                backoff = NextBackoff(backoff);
            }
        }
    }

    private async Task RunOnceAsync(CancellationToken ct)
    {
        using var pipe = new NamedPipeClientStream(
            ".", PipeNameHelper.ToBareName(_ctlPipeName), PipeDirection.InOut, PipeOptions.Asynchronous);

        await pipe.ConnectAsync(ConnectTimeoutMs, ct);
        pipe.ReadMode = PipeTransmissionMode.Message;

        var subscribeMsg = new
        {
            type = "subscribe",
            component = _componentName,
            pid = Environment.ProcessId,
            snapshot_request = true,
        };
        byte[] subscribeBytes = JsonSerializer.SerializeToUtf8Bytes(subscribeMsg);
        await pipe.WriteAsync(subscribeBytes, ct);
        await pipe.FlushAsync(ct);

        Log($"ctl: subscribed component={_componentName}");

        var buf = new byte[64 * 1024];
        while (!ct.IsCancellationRequested)
        {
            int n = await pipe.ReadAsync(buf, ct);
            if (n == 0)
            {
                throw new IOException("ctl pipe closed by server");
            }
            DispatchMessage(buf.AsMemory(0, n));
        }
    }

    private void DispatchMessage(ReadOnlyMemory<byte> message)
    {
        using var doc = JsonDocument.Parse(message);
        var root = doc.RootElement;
        if (!root.TryGetProperty("type", out var typeEl))
        {
            Log("ctl: message missing 'type' — ignoring");
            return;
        }
        var type = typeEl.GetString();
        switch (type)
        {
            case "config_snapshot":
            case "config_update":
                long version = root.TryGetProperty("version", out var v) && v.ValueKind == JsonValueKind.Number
                    ? v.GetInt64() : 0;
                Log($"ctl: {type} received (version={version})");
                try
                {
                    _onConfigChanged(root.GetProperty("config"));
                }
                catch (Exception ex)
                {
                    Log($"ctl: onConfigChanged callback threw: {ex.Message}");
                }
                break;
            case "error":
                var code = root.TryGetProperty("code", out var c) ? c.GetString() ?? "" : "";
                var msg = root.TryGetProperty("message", out var m) ? m.GetString() ?? "" : "";
                if (code == "already_subscribed")
                {
                    throw new CtlAlreadySubscribedException(msg);
                }
                throw new CtlFatalErrorException(code, msg);
            default:
                Log($"ctl: ignoring unknown message type={type}");
                break;
        }
    }

    private static TimeSpan NextBackoff(TimeSpan current)
    {
        var next = TimeSpan.FromMilliseconds(current.TotalMilliseconds * 2);
        return next > BackoffMax ? BackoffMax : next;
    }

    /// <returns>true if cancelled (caller should return), false on normal sleep completion.</returns>
    private static async Task<bool> DelayOrCancel(TimeSpan delay, CancellationToken ct)
    {
        try
        {
            await Task.Delay(delay, ct);
            return false;
        }
        catch (OperationCanceledException)
        {
            return true;
        }
    }
}

internal sealed class CtlAlreadySubscribedException : Exception
{
    public CtlAlreadySubscribedException(string message) : base(message) { }
}

internal sealed class CtlFatalErrorException : Exception
{
    public string Code { get; }
    public CtlFatalErrorException(string code, string message) : base(message)
    {
        Code = code;
    }
}
