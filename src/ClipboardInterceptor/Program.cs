using AgentCore;
using ClipboardInterceptor;

// --- Enforce clipboard history disabled (watches registry for re-enable attempts) ---
using var enforcer = new ClipboardHistoryEnforcer();

// --- Wire up components ---
var agentCore = new PipeAgentCore();
using var monitor = new ClipboardMonitor();
var service = new ClipboardInterceptorService(agentCore);
monitor.ClipboardChanged += service.OnClipboardChanged;

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
// enforcer.Dispose() is called implicitly by 'using' — restores clipboard history
