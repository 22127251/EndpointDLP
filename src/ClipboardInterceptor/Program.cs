using System.Text.Json;
using AgentCore;
using ClipboardInterceptor;
using DlpShared;

// --- Load central config ---
string configPath;
try
{
    configPath = ConfigLocator.FindConfigYaml();
}
catch (FileNotFoundException ex)
{
    Console.Error.WriteLine($"[DLP] Could not locate config.yaml:\n{ex.Message}");
    return 1;
}

var (initialDataPipe, initialCtlPipe) = ConfigLocator.LoadTopLevel(configPath);
var clipboardCfg = ConfigLocator.LoadSection<ClipboardSection>(configPath, "clipboard");
Console.WriteLine($"[DLP] Loaded orchestrator config: {configPath}");
Console.WriteLine($"[DLP] data_pipe={initialDataPipe} pipe_timeout_ms={clipboardCfg.PipeTimeoutMs}");

var holder = new ClipboardConfigHolder(initialDataPipe, clipboardCfg.PipeTimeoutMs);

// --- Enforce clipboard history disabled (watches registry for re-enable attempts) ---
using var enforcer = new ClipboardHistoryEnforcer();

// --- Wire up components ---
var agentCore = new PipeAgentCore(() => holder.Get());
using var monitor = new ClipboardMonitor();
var service = new ClipboardInterceptorService(agentCore);
monitor.ClipboardChanged += service.OnClipboardChanged;

// --- ctl-pipe subscriber (long-lived background task) ---
var ctlCts = new CancellationTokenSource();
var subscriber = new CtlPipeSubscriber(initialCtlPipe, "clipboard", json =>
{
    // data_pipe is non-hot-reloadable per decision #7. The orchestrator already
    // overrides the broadcast payload's data_pipe back to the in-use value, so
    // a mismatch here is a defense-in-depth signal that something is off.
    if (json.TryGetProperty("data_pipe", out var dp) && dp.ValueKind == JsonValueKind.String)
    {
        var pushedPipe = dp.GetString() ?? "";
        if (!string.IsNullOrEmpty(pushedPipe) && pushedPipe != holder.PipeName)
        {
            Console.Error.WriteLine(
                $"[DLP] ctl: data_pipe change requires restart; keeping {holder.PipeName} (pushed {pushedPipe})");
        }
    }
    if (json.TryGetProperty("clipboard", out var clip)
        && clip.TryGetProperty("pipe_timeout_ms", out var t)
        && t.ValueKind == JsonValueKind.Number)
    {
        int newTimeoutMs = t.GetInt32();
        if (newTimeoutMs != holder.TimeoutMs)
        {
            Console.WriteLine($"[DLP] ctl: pipe_timeout_ms updated → {newTimeoutMs}");
            holder.SetTimeoutMs(newTimeoutMs);
        }
    }
})
{
    OnLog = msg => Console.WriteLine($"[DLP] {msg}"),
};
var subscriberTask = Task.Run(() => subscriber.StartAsync(ctlCts.Token));

Console.WriteLine("[DLP] Clipboard DLP running. Press Ctrl+C to exit.");
Console.WriteLine("[DLP] Copy any text to trigger analysis.\n");

// --- Keep alive until Ctrl+C ---
var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) =>
{
    e.Cancel = true;
    cts.Cancel();
};

try
{
    await Task.Delay(Timeout.Infinite, cts.Token);
}
catch (OperationCanceledException) { }

Console.WriteLine("\n[DLP] Shutting down...");
ctlCts.Cancel();
try { await subscriberTask.WaitAsync(TimeSpan.FromSeconds(2)); }
catch (TimeoutException) { /* drop, subscriber is a daemon */ }
catch (OperationCanceledException) { }
return 0;
