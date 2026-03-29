using AgentCore;
using QueueManager;

Console.WriteLine("?????????????????????????????????????????????????????????????");
Console.WriteLine("?         EndpointDLP - QueueManager (Demo Mode)            ?");
Console.WriteLine("?????????????????????????????????????????????????????????????");
Console.WriteLine();

// Create components
var queueManager = new ChunkQueueManager();

// Start pipe server (receives chunks from ClipboardInterceptor and addon.py)
using var pipeServer = new ChunkPipeServer(queueManager);

// Start interactive analyzer (asks user a/b for each chunk)
using var analyzer = new InteractiveAnalyzer(queueManager);

Console.WriteLine("[*] Components started:");
Console.WriteLine("    - Pipe Server: \\\\.\\pipe\\dlp_upload");
Console.WriteLine("    - Interactive Analyzer: waiting for chunks...");
Console.WriteLine();
Console.WriteLine("[*] Priority queue: clipboard chunks (analyzed first)");
Console.WriteLine("[*] Non-priority queue: browser file upload chunks");
Console.WriteLine();
Console.WriteLine("[*] Policy: ALL chunks must be ALLOW for overall ALLOW");
Console.WriteLine("          ANY chunk BLOCK = overall BLOCK");
Console.WriteLine();
Console.WriteLine("Press Ctrl+C to exit.\n");

// Keep alive until Ctrl+C
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

Console.WriteLine("\n[*] Shutting down...");

// Report final stats
var (priority, nonPriority) = queueManager.GetQueueStats();
Console.WriteLine($"[*] Final queue stats: priority={priority}, non-priority={nonPriority}");
