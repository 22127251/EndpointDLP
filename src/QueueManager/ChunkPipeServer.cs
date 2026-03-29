using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Text.Json;

namespace QueueManager;

/// <summary>
/// Named pipe server that receives chunks from ClipboardInterceptor and addon.py.
/// Responds immediately after enqueueing - analysis is handled asynchronously by InteractiveAnalyzer.
/// </summary>
public class ChunkPipeServer : IDisposable
{
    private const string PipeName = "dlp_upload";
    private readonly CancellationTokenSource _cts = new();
    private readonly ChunkQueueManager _queueManager;
    private readonly Task _serverTask;
    
    // Serialize pipe connections to avoid race conditions
    private readonly SemaphoreSlim _connectionSemaphore = new(1, 1);

    public ChunkPipeServer(ChunkQueueManager queueManager)
    {
        _queueManager = queueManager;
        _serverTask = Task.Run(() => RunServerAsync(_cts.Token));
    }

    private async Task RunServerAsync(CancellationToken ct)
    {
        Console.WriteLine($"[PipeServer] Starting on pipe: {PipeName}");

        while (!ct.IsCancellationRequested)
        {
            try
            {
                // Wait for semaphore to ensure only one connection at a time
                if (!await _connectionSemaphore.WaitAsync(TimeSpan.FromSeconds(30), ct))
                {
                    continue;
                }

                try
                {
                    using var pipeServer = new NamedPipeServerStream(
                        PipeName,
                        PipeDirection.InOut,
                        1, // max instances: 1 (serialize connections)
                        PipeTransmissionMode.Message,
                        PipeOptions.Asynchronous);

                    // Wait for connection with cancellation support
                    await pipeServer.WaitForConnectionAsync(ct);

                    // Handle client synchronously to ensure proper cleanup
                    await HandleClientAsync(pipeServer, ct);
                }
                finally
                {
                    _connectionSemaphore.Release();
                }
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[PipeServer] Error: {ex.Message}");
                if (!ct.IsCancellationRequested)
                {
                    _connectionSemaphore.Release();
                    await Task.Delay(100, ct);
                }
            }
        }
    }

    private async Task HandleClientAsync(NamedPipeServerStream pipe, CancellationToken ct)
    {
        try
        {
            var buffer = new byte[64 * 1024];
            var bytesRead = await pipe.ReadAsync(buffer, 0, buffer.Length, ct);

            if (bytesRead == 0)
            {
                return;
            }

            var json = Encoding.UTF8.GetString(buffer, 0, bytesRead);
            var response = await ProcessPayloadAsync(json, ct);

            // Send response (client may have already disconnected - that's OK)
            try
            {
                var responseBytes = Encoding.UTF8.GetBytes(response);
                await pipe.WriteAsync(responseBytes, 0, responseBytes.Length, ct);
                await pipe.FlushAsync(ct);
            }
            catch (IOException)
            {
                // Client disconnected before we could respond - expected for fire-and-forget
            }
        }
        catch (OperationCanceledException)
        {
            // Client disconnected or cancellation requested
        }
        catch (IOException ioEx) when (ioEx.Message.Contains("closed") || ioEx.Message.Contains("pipe"))
        {
            // Client disconnected before we could respond - expected behavior
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[PipeServer] Handler error: {ex.Message}");
        }
        finally
        {
            try { pipe.Dispose(); }
            catch { }
        }
    }

    private async Task<string> ProcessPayloadAsync(string json, CancellationToken ct)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;

            // Check for clear priority queue signal
            if (root.TryGetProperty("action", out var actionProp) &&
                actionProp.GetString() == "clear_priority_queue")
            {
                var messageId = root.GetProperty("message_id").GetString() ?? "";
                _queueManager.ClearPriorityQueue(messageId);
                return "ALLOW";
            }

            // Check for chunk payload
            if (root.TryGetProperty("channel", out var channelProp) &&
                root.TryGetProperty("chunk_id", out var chunkIdProp))
            {
                var chunk = new Chunk
                {
                    Channel = root.GetProperty("channel").GetString() ?? "",
                    Priority = root.TryGetProperty("priority", out var p) && p.GetBoolean(),
                    MessageId = root.GetProperty("message_id").GetString() ?? "",
                    ChunkId = root.GetProperty("chunk_id").GetInt32(),
                    TotalChunks = root.GetProperty("total_chunks").GetInt32(),
                    Content = root.GetProperty("content").GetString() ?? "",
                    WordCount = root.TryGetProperty("word_count", out var wc) ? wc.GetInt32() : 0,
                    SourceUrl = root.TryGetProperty("source_url", out var url) ? url.GetString() : null,
                    Filename = root.TryGetProperty("filename", out var fn) ? fn.GetString() : null,
                    Timestamp = root.TryGetProperty("timestamp", out var ts) ? ts.GetString() ?? "" : ""
                };

                // Initialize message tracking on first chunk
                if (chunk.ChunkId == 0)
                {
                    _queueManager.InitializeMessageTracking(chunk.MessageId, chunk.TotalChunks, chunk.Channel);
                }

                // Check for immediate decision (streaming - if already blocked)
                var immediateDecision = _queueManager.GetImmediateDecision(chunk.MessageId);
                if (immediateDecision != null)
                {
                    _queueManager.RecordChunkDecision(chunk.MessageId, chunk.ChunkId, immediateDecision, chunk.Content);
                    Console.WriteLine($"[PipeServer] Chunk {chunk.ChunkId + 1}/{chunk.TotalChunks}: {immediateDecision} (cached)");
                    return immediateDecision;
                }

                // Analyze chunk interactively (ask user a/b)
                var decision = await AnalyzeChunkInteractivelyAsync(chunk, ct);
                
                // Record decision
                var (_, overallDecision, _) = _queueManager.RecordChunkDecision(
                    chunk.MessageId, chunk.ChunkId, decision, chunk.Content);
                
                Console.WriteLine($"[PipeServer] Chunk {chunk.ChunkId + 1}/{chunk.TotalChunks}: {decision}");
                
                if (overallDecision != null)
                {
                    Console.WriteLine($"[PipeServer] Message {chunk.MessageId} complete: {overallDecision}");
                }
                
                return decision;
            }

            // Legacy payload
            return "ALLOW";
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[PipeServer] Parse error: {ex.Message}");
            return "ALLOW";
        }
    }

    private async Task<string> AnalyzeChunkInteractivelyAsync(Chunk chunk, CancellationToken ct)
    {
        // Show chunk preview
        var preview = chunk.Content.Length > 150 
            ? chunk.Content[..150] + "..." 
            : chunk.Content;
        
        Console.WriteLine();
        Console.WriteLine($"????????????????????????????????????????????????????????????");
        Console.WriteLine($"?  Chunk {chunk.ChunkId + 1,2}/{chunk.TotalChunks} | Message: {chunk.MessageId[..Math.Min(20, chunk.MessageId.Length)]}...");
        Console.WriteLine($"?  Channel: {chunk.Channel,-10} | Priority: {chunk.Priority,-5} | Words: {chunk.WordCount}");
        Console.WriteLine($"????????????????????????????????????????????????????????????");
        Console.WriteLine($"?  {preview.Replace("\n", " ").Replace("\r", " ")}");
        Console.WriteLine($"????????????????????????????????????????????????????????????");
        Console.WriteLine();
        Console.Write("[a]llow | [b]lock | [q]uit > ");

        // Read user input
        var decision = await ReadUserDecisionAsync(ct);
        
        if (decision == null)
        {
            Console.WriteLine("[PipeServer] Analysis cancelled, defaulting to ALLOW");
            return "ALLOW";
        }

        var decisionStr = decision == 'a' ? "ALLOW" : "BLOCK";
        
        if (decisionStr == "ALLOW" && chunk.ChunkId == chunk.TotalChunks - 1)
        {
            // Last chunk allowed - show reconstructed text
            Console.WriteLine("[PipeServer] All chunks ALLOWED");
        }
        else if (decisionStr == "BLOCK")
        {
            Console.WriteLine("[PipeServer] Chunk BLOCKED - message will be blocked");
        }

        return decisionStr;
    }

    private Task<char?> ReadUserDecisionAsync(CancellationToken ct)
    {
        return Task.Run(() =>
        {
            while (!ct.IsCancellationRequested)
            {
                if (Console.KeyAvailable)
                {
                    var key = Console.ReadKey(intercept: true).KeyChar.ToString().ToLower();
                    if (key == "a") return (char?)'a';
                    if (key == "b") return (char?)'b';
                    if (key == "q") return null;
                }
                Thread.Sleep(50);
            }
            return null;
        }, ct);
    }

    public void Dispose()
    {
        _cts.Cancel();
        try { _serverTask.Wait(TimeSpan.FromSeconds(2)); }
        catch { }
        _cts.Dispose();
        _connectionSemaphore.Dispose();
    }
}
