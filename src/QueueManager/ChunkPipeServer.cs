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
            // Use larger buffer for chunk payloads (up to 10MB)
            var buffer = new byte[10 * 1024 * 1024];
            var totalBytesRead = 0;

            // Read all available data (may come in multiple chunks)
            while (pipe.IsConnected && !ct.IsCancellationRequested)
            {
                var bytesRead = await pipe.ReadAsync(buffer, totalBytesRead, buffer.Length - totalBytesRead, ct);
                if (bytesRead == 0)
                {
                    break; // End of stream
                }
                totalBytesRead += bytesRead;

                // Check if we have a complete JSON message (ends with })
                if (totalBytesRead > 0)
                {
                    var tempStr = Encoding.UTF8.GetString(buffer, 0, totalBytesRead);
                    if (tempStr.TrimEnd().EndsWith("}"))
                    {
                        break; // Complete message received
                    }
                }
            }

            if (totalBytesRead == 0)
            {
                return;
            }

            var json = Encoding.UTF8.GetString(buffer, 0, totalBytesRead);
            Console.WriteLine($"[PipeServer] Received {totalBytesRead} bytes from pipe");
            
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
            Console.WriteLine($"[PipeServer] Stack: {ex.StackTrace}");
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

            // Check for file analysis request (new workflow)
            if (root.TryGetProperty("action", out var actionProp2) &&
                actionProp2.GetString() == "analyze_file")
            {
                return await AnalyzeFileAsync(root, ct);
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

    private async Task<string> AnalyzeFileAsync(JsonElement root, CancellationToken ct)
    {
        var tempPath = root.TryGetProperty("temp_path", out var tp) ? tp.GetString() ?? "" : "";
        var filename = root.TryGetProperty("filename", out var fn) ? fn.GetString() ?? "" : "unknown";
        var url = root.TryGetProperty("url", out var u) ? u.GetString() ?? "" : "";

        Console.WriteLine();
        Console.WriteLine($"????????????????????????????????????????????????????????????");
        Console.WriteLine($"?  FILE ANALYSIS REQUEST                                  ?");
        Console.WriteLine($"?  File: {filename,-50} ?");
        Console.WriteLine($"?  Path: {tempPath,-50} ?");
        Console.WriteLine($"????????????????????????????????????????????????????????????");
        Console.WriteLine();

        if (!File.Exists(tempPath))
        {
            Console.WriteLine($"[FileAnalysis] File not found: {tempPath}");
            return "ALLOW"; // Fail open
        }

        try
        {
            // Extract text from file
            var text = ExtractTextFromFile(tempPath, filename);
            
            if (string.IsNullOrWhiteSpace(text))
            {
                Console.WriteLine($"[FileAnalysis] No text extracted from {filename}");
                return "ALLOW"; // No content to analyze
            }

            // Chunk the text
            var chunks = ChunkText(text, 500, 50); // 500 words per chunk, 50 words overlap
            Console.WriteLine($"[FileAnalysis] Extracted {text.Length} chars, chunked into {chunks.Count} pieces");

            if (chunks.Count == 0)
            {
                return "ALLOW";
            }

            // Generate message ID
            var messageId = $"file_{Path.GetFileName(tempPath)}_{DateTimeOffset.UtcNow.ToUnixTimeSeconds()}";

            // Analyze each chunk
            var overallDecision = "ALLOW";
            for (var i = 0; i < chunks.Count; i++)
            {
                var chunk = new Chunk
                {
                    Channel = "browser",
                    Priority = false,
                    MessageId = messageId,
                    ChunkId = i,
                    TotalChunks = chunks.Count,
                    Content = chunks[i],
                    WordCount = chunks[i].Split(' ', StringSplitOptions.RemoveEmptyEntries).Length,
                    SourceUrl = url,
                    Filename = filename,
                    Timestamp = DateTime.UtcNow.ToString("o") + "Z"
                };

                // Initialize tracking
                if (i == 0)
                {
                    _queueManager.InitializeMessageTracking(messageId, chunks.Count, "browser");
                }

                // Check for cached decision
                var cachedDecision = _queueManager.GetImmediateDecision(messageId);
                if (cachedDecision != null)
                {
                    _queueManager.RecordChunkDecision(messageId, i, cachedDecision, chunk.Content);
                    Console.WriteLine($"[FileAnalysis] Chunk {i + 1}/{chunks.Count}: {cachedDecision} (cached)");
                    overallDecision = cachedDecision;
                    if (cachedDecision == "BLOCK") break;
                    continue;
                }

                // Analyze interactively
                var decision = await AnalyzeChunkInteractivelyAsync(chunk, ct);
                
                if (decision == null)
                {
                    decision = "ALLOW"; // User cancelled
                }

                var (_, chunkOverallDecision, _) = _queueManager.RecordChunkDecision(
                    messageId, i, decision, chunk.Content);

                Console.WriteLine($"[FileAnalysis] Chunk {i + 1}/{chunks.Count}: {decision}");

                if (chunkOverallDecision != null)
                {
                    overallDecision = chunkOverallDecision;
                    Console.WriteLine($"[FileAnalysis] Message {messageId} complete: {overallDecision}");
                    if (overallDecision == "BLOCK") break;
                }

                if (decision == "BLOCK")
                {
                    overallDecision = "BLOCK";
                    break;
                }
            }

            Console.WriteLine();
            Console.WriteLine($"[FileAnalysis] Final decision for {filename}: {overallDecision}");
            
            // Cleanup temp file
            try
            {
                File.Delete(tempPath);
                Console.WriteLine($"[FileAnalysis] Cleaned up temp file: {tempPath}");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[FileAnalysis] Could not delete temp file: {ex.Message}");
            }

            return overallDecision;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[FileAnalysis] Error: {ex.Message}");
            return "ALLOW"; // Fail open
        }
    }

    private string ExtractTextFromFile(string path, string filename)
    {
        var ext = Path.GetExtension(filename).ToLowerInvariant();
        
        try
        {
            // Plain text files - read with UTF-8 encoding
            if (ext is ".txt" or ".csv" or ".md" or ".json" or ".xml" or ".html" or ".htm" or ".log")
            {
                // Try reading with UTF-8 first (with BOM detection)
                return File.ReadAllText(path, System.Text.Encoding.UTF8);
            }
            
            // For Office/PDF/Archives, we need Python interop or external libraries
            // For now, try to read as text with UTF-8
            var content = File.ReadAllText(path, System.Text.Encoding.UTF8);
            return content;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[FileAnalysis] Failed to extract text from {filename}: {ex.Message}");
            return "";
        }
    }

    private List<string> ChunkText(string text, int chunkSizeWords, int overlapWords)
    {
        var chunks = new List<string>();
        var words = text.Split(' ', StringSplitOptions.RemoveEmptyEntries);

        if (words.Length <= chunkSizeWords)
        {
            chunks.Add(text);
            return chunks;
        }

        var start = 0;
        while (start < words.Length)
        {
            var end = Math.Min(start + chunkSizeWords, words.Length);
            var chunkWords = words.Skip(start).Take(end - start).ToArray();
            chunks.Add(string.Join(" ", chunkWords));

            start = end - overlapWords;
            if (start >= words.Length) break;
        }

        return chunks;
    }

    private async Task<string> AnalyzeChunkInteractivelyAsync(Chunk chunk, CancellationToken ct)
    {
        // Show full chunk content
        var content = chunk.Content;

        Console.WriteLine();
        Console.WriteLine($"=================================================================");
        Console.WriteLine($"  Chunk {chunk.ChunkId + 1,2}/{chunk.TotalChunks} | Message: {chunk.MessageId[..Math.Min(20, chunk.MessageId.Length)]}...");
        Console.WriteLine($"  Channel: {chunk.Channel,-10} | Priority: {chunk.Priority,-5} | Words: {chunk.WordCount}");
        Console.WriteLine($"=================================================================");
        Console.WriteLine(content);
        Console.WriteLine($"=================================================================");
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
