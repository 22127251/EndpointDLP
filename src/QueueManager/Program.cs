using AgentCore;
using QueueManager;

Console.WriteLine("-------------------------------------------------");
Console.WriteLine("?         EndpointDLP - QueueManager (Demo Mode)            ?");
Console.WriteLine("-------------------------------------------------");
Console.WriteLine();

// Create components
var queueManager = new ChunkQueueManager();

// Start pipe server (receives chunks and analyzes interactively)
using var pipeServer = new ChunkPipeServer(queueManager);

Console.WriteLine("[*] Components started:");
Console.WriteLine("    - Pipe Server: \\\\.\\pipe\\dlp_upload");
Console.WriteLine("    - Interactive Analysis: enabled (a/b per chunk)");
Console.WriteLine();
Console.WriteLine("[*] Priority queue: clipboard chunks (analyzed first)");
Console.WriteLine("[*] Non-priority queue: browser file upload chunks");
Console.WriteLine();
Console.WriteLine("[*] Policy: ANY chunk BLOCK = overall BLOCK");
Console.WriteLine("          ALL chunks ALLOW = reconstruct & allow");
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
