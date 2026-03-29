using ClipboardInterceptor;
using Microsoft.Win32;

const string ClipboardHistoryRegKey = @"HKEY_CURRENT_USER\Software\Microsoft\Clipboard";
const string ClipboardHistoryRegValue = "EnableClipboardHistory";

// --- Disable clipboard history ---
object? previousValue = Registry.GetValue(ClipboardHistoryRegKey, ClipboardHistoryRegValue, null);
Registry.SetValue(ClipboardHistoryRegKey, ClipboardHistoryRegValue, 0, RegistryValueKind.DWord);
Console.WriteLine("[DLP] Clipboard history disabled.");

// --- Wire up components ---
using var monitor = new ClipboardMonitor();
var service = new ClipboardInterceptorService();
monitor.ClipboardChanged += service.OnClipboardChanged;

Console.WriteLine("[DLP] Clipboard DLP running. Press Ctrl+C to exit.");
Console.WriteLine("[DLP] Copy any text to trigger analysis.");
Console.WriteLine("[DLP] Chunks will be sent to QueueManager for analysis.\n");

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

// --- Restore clipboard history ---
Console.WriteLine("\n[DLP] Shutting down...");
service.Dispose();

if (previousValue is int prev)
    Registry.SetValue(ClipboardHistoryRegKey, ClipboardHistoryRegValue, prev, RegistryValueKind.DWord);
else
    Registry.SetValue(ClipboardHistoryRegKey, ClipboardHistoryRegValue, 1, RegistryValueKind.DWord);

Console.WriteLine("[DLP] Clipboard history restored.");
