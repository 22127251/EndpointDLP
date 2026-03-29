using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using AgentCore;

namespace QueueManager;

/// <summary>
/// Simple interactive analyzer that asks user for a/b input.
/// </summary>
public class InteractiveAnalyzer : IDisposable
{
    private readonly ChunkQueueManager _queueManager;
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _analyzerTask;
    
    // Track which chunks are being analyzed to avoid duplicate prompts
    private readonly HashSet<string> _analyzingChunks = new();
    private readonly object _lock = new();

    public InteractiveAnalyzer(ChunkQueueManager queueManager)
    {
        _queueManager = queueManager;
        _analyzerTask = Task.Run(() => RunAnalyzerLoop(_cts.Token));
    }

    private async Task RunAnalyzerLoop(CancellationToken ct)
    {
        Console.WriteLine("[Analyzer] Interactive analyzer started - waiting for chunks...");
        
        while (!ct.IsCancellationRequested)
        {
            try
            {
                if (_queueManager.TryDequeue(out var chunk))
                {
                    if (chunk != null)
                    {
                        await AnalyzeChunkAsync(chunk, ct);
                    }
                }
                else
                {
                    // No chunks available, wait a bit
                    await Task.Delay(100, ct);
                }
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[Analyzer] Error: {ex.Message}");
                await Task.Delay(100, ct);
            }
        }
    }

    private async Task AnalyzeChunkAsync(Chunk chunk, CancellationToken ct)
    {
        try
        {
            // Check for duplicate analysis
            lock (_lock)
            {
                var chunkKey = $"{chunk.MessageId}_{chunk.ChunkId}";
                if (_analyzingChunks.Contains(chunkKey))
                {
                    Console.WriteLine($"[Analyzer] Chunk {chunk.ChunkId + 1}/{chunk.TotalChunks} already being analyzed, skipping");
                    return;
                }
                _analyzingChunks.Add(chunkKey);
            }

            // Check if message is already blocked
            if (_queueManager.IsMessageBlocked(chunk.MessageId))
            {
                Console.WriteLine($"[Analyzer] Message {chunk.MessageId} already blocked, skipping chunk {chunk.ChunkId + 1}");
                return;
            }

            // Show chunk preview
            var preview = chunk.Content.Length > 150 
                ? chunk.Content[..150] + "..." 
                : chunk.Content;
            
            Console.WriteLine();
            Console.WriteLine($"╔══════════════════════════════════════════════════════════╗");
            Console.WriteLine($"║  Chunk {chunk.ChunkId + 1,2}/{chunk.TotalChunks} | Message: {chunk.MessageId[..Math.Min(20, chunk.MessageId.Length)]}...");
            Console.WriteLine($"║  Channel: {chunk.Channel,-10} | Priority: {chunk.Priority,-5} | Words: {chunk.WordCount}");
            Console.WriteLine($"╠══════════════════════════════════════════════════════════╣");
            Console.WriteLine($"║  {preview.Replace("\n", " ").Replace("\r", " ")}");
            Console.WriteLine($"╚══════════════════════════════════════════════════════════╝");
            Console.WriteLine();
            Console.Write("[a]llow | [b]lock | [q]uit > ");

            // Read user input asynchronously
            var decision = await ReadUserDecisionAsync(ct);
            
            if (decision == null)
            {
                Console.WriteLine("[Analyzer] Analysis cancelled");
                return;
            }

            var decisionStr = decision == 'a' ? "ALLOW" : "BLOCK";
            
            // Record decision and get overall result
            var (chunkDecision, overallDecision, reconstructedText) = 
                _queueManager.RecordChunkDecision(chunk.MessageId, chunk.ChunkId, decisionStr, chunk.Content);

            if (overallDecision != null)
            {
                Console.WriteLine();
                var color = overallDecision == "ALLOW" ? ConsoleColor.Green : ConsoleColor.Red;
                var originalColor = Console.ForegroundColor;
                Console.ForegroundColor = color;
                Console.WriteLine($"═══════════════════════════════════════════════════════════");
                Console.WriteLine($"  FINAL DECISION: {overallDecision} for message {chunk.MessageId}");
                
                if (overallDecision == "ALLOW" && !string.IsNullOrEmpty(reconstructedText))
                {
                    Console.WriteLine($"  ═══════════════════════════════════════════════════════");
                    Console.WriteLine($"  RECONSTRUCTED TEXT ({reconstructedText.Length} chars):");
                    Console.WriteLine($"  ┌─────────────────────────────────────────────────────┐");
                    var lines = WordWrap(reconstructedText, 55);
                    foreach (var line in lines)
                    {
                        Console.WriteLine($"  │ {line,-55} │");
                    }
                    Console.WriteLine($"  └─────────────────────────────────────────────────────┘");
                    Console.WriteLine($"  ═══════════════════════════════════════════════════════");
                    Console.WriteLine();
                    Console.WriteLine($"  [INFO] Clipboard content was ALLOWED.");
                    Console.WriteLine($"  [INFO] User can paste the original content.");
                }
                else if (overallDecision == "BLOCK")
                {
                    Console.WriteLine();
                    Console.WriteLine($"  [INFO] Clipboard content was BLOCKED.");
                    Console.WriteLine($"  [INFO] If user pastes, they will see block notification.");
                }
                
                Console.WriteLine($"═══════════════════════════════════════════════════════════");
                Console.ForegroundColor = originalColor;
                Console.WriteLine();
            }
            else
            {
                Console.WriteLine($"[Analyzer] Chunk {chunk.ChunkId + 1}/{chunk.TotalChunks}: {decisionStr} - waiting for remaining chunks...");
            }
        }
        catch (OperationCanceledException)
        {
            Console.WriteLine($"[Analyzer] Analysis cancelled for chunk {chunk.ChunkId + 1}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[Analyzer] Failed to analyze chunk {chunk.ChunkId + 1}: {ex.Message}");
        }
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

    private static List<string> WordWrap(string text, int maxWidth)
    {
        var lines = new List<string>();
        var words = text.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
        var currentLine = "";
        
        foreach (var word in words)
        {
            if (currentLine.Length + word.Length + 1 <= maxWidth)
            {
                currentLine += (currentLine.Length > 0 ? " " : "") + word;
            }
            else
            {
                if (currentLine.Length > 0)
                    lines.Add(currentLine);
                currentLine = word.Length <= maxWidth ? word : word[..Math.Min(maxWidth, word.Length)];
            }
        }
        
        if (currentLine.Length > 0)
            lines.Add(currentLine);
        
        return lines;
    }

    public void Dispose()
    {
        _cts.Cancel();
        _analyzerTask.Wait(TimeSpan.FromSeconds(2));
        _cts.Dispose();
    }
}
