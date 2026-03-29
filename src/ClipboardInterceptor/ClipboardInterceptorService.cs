using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows.Forms;

namespace ClipboardInterceptor;

/// <summary>
/// Intercepts user clipboard copies, chunks text into 500-word segments,
/// and sends chunks to QueueManager via named pipe for analysis.
///
/// This interceptor does NOT make decisions - it only sends chunks and 
/// restores original content. All decisions are made in QueueManager.
///
/// Self-write detection uses string comparison:
///   - Placeholder and block strings are matched exactly.
///   - The most recently allowed text is held in _allowRestoreText and matched on restore.
///
/// Concurrent copies cancel the in-flight analysis via CancellationToken.
/// </summary>
public sealed class ClipboardInterceptorService : IDisposable
{
    private const string Placeholder = "[DLP: Analyzing...]";
    private const string BlockNotification = "[DLP: Content Blocked]";
    private const string PipeName = "dlp_upload";
    private const int ChunkSizeWords = 500;
    private const int ChunkOverlapWords = 50;

    private string? _allowRestoreText;
    private CancellationTokenSource? _currentCts;
    private string _currentAnalysisId = string.Empty;
    private string? _currentMessageId;

    public ClipboardInterceptorService()
    {
    }

    public void Dispose()
    {
        _currentCts?.Dispose();
    }

    // Called on STA thread via ClipboardMonitor
    public void OnClipboardChanged(object? sender, EventArgs e)
    {
        if (!Clipboard.ContainsText()) return;
        string content = Clipboard.GetText();

        // Ignore our own writes
        if (content == Placeholder || content == BlockNotification) return;
        if (_allowRestoreText != null && content == _allowRestoreText) return;
        _allowRestoreText = null; // new user content — clear the allow restore guard

        // Cancel any in-flight analysis and start fresh for the new content
        _currentCts?.Cancel();
        _currentCts?.Dispose();
        _currentCts = new CancellationTokenSource();

        var id = Guid.NewGuid().ToString("N");
        _currentAnalysisId = id;
        _currentMessageId = $"clipboard_{id}";

        _ = ProcessAsync(content, id, _currentCts.Token);
    }

    // Starts on STA thread; resumes on STA thread after await via WinForms SynchronizationContext
    private async Task ProcessAsync(string content, string id, CancellationToken ct)
    {
        try
        {
            SetOwnClipboardText(Placeholder);

            // Chunk the text
            var chunks = ChunkText(content, ChunkSizeWords, ChunkOverlapWords);
            if (chunks.Count == 0)
            {
                Console.WriteLine("[DLP] No chunks to analyze");
                SetOwnClipboardText(content);
                return;
            }

            Console.WriteLine($"[DLP] Sending {chunks.Count} chunks to QueueManager...");
            Console.WriteLine($"[DLP] Message ID: {_currentMessageId}");

            // Send clear priority queue signal first
            await SendClearPriorityQueueAsync(_currentMessageId!, ct);

            // Send all chunks to QueueManager
            var timestamp = DateTime.UtcNow.ToString("o");
            for (int i = 0; i < chunks.Count; i++)
            {
                await SendChunkAsync(chunks[i], i, chunks.Count, _currentMessageId!, timestamp, ct);
            }

            // Discard stale decisions from superseded analyses
            if (_currentAnalysisId != id)
            {
                Console.WriteLine("[DLP] Analysis cancelled — newer copy detected");
                return;
            }

            // Always restore original content (decision is handled by QueueManager only)
            _allowRestoreText = content;
            SetOwnClipboardText(content);
            Console.WriteLine("[DLP] Content restored. Check QueueManager for analysis.");
        }
        catch (OperationCanceledException)
        {
            Console.WriteLine("[DLP] Analysis cancelled — newer copy detected.");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DLP] Error during interception: {ex.Message}");
            // On error, restore original content (fail-open for clipboard)
            if (_currentAnalysisId == id)
                SetOwnClipboardText(content);
        }
    }

    private static List<string> ChunkText(string text, int chunkSizeWords, int overlapWords)
    {
        var chunks = new List<string>();
        if (string.IsNullOrWhiteSpace(text))
            return chunks;

        var words = text.Split(new[] { ' ', '\t', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries);
        if (words.Length <= chunkSizeWords)
        {
            chunks.Add(string.Join(" ", words));
            return chunks;
        }

        int start = 0;
        while (start < words.Length)
        {
            int end = Math.Min(start + chunkSizeWords, words.Length);
            var chunkWords = words.Skip(start).Take(end - start).ToArray();
            chunks.Add(string.Join(" ", chunkWords));

            if (end >= words.Length)
                break;

            start = end - overlapWords;
            if (start >= words.Length)
                break;
        }

        return chunks;
    }

    private async Task SendChunkAsync(
        string content, 
        int chunkId, 
        int totalChunks, 
        string messageId, 
        string timestamp,
        CancellationToken ct)
    {
        try
        {
            var payload = new
            {
                channel = "clipboard",
                priority = true,
                message_id = messageId,
                chunk_id = chunkId,
                total_chunks = totalChunks,
                content = content,
                word_count = content.Split(new[] { ' ', '\t', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries).Length,
                timestamp = timestamp
            };

            await SendToPipeAsync(payload, ct);
            Console.WriteLine($"[DLP] Sent chunk {chunkId + 1}/{totalChunks}");
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DLP] Failed to send chunk {chunkId + 1}: {ex.Message}");
        }
    }

    private async Task SendClearPriorityQueueAsync(string messageId, CancellationToken ct)
    {
        try
        {
            var payload = new
            {
                channel = "clipboard",
                priority = true,
                message_id = messageId,
                action = "clear_priority_queue"
            };

            await SendToPipeAsync(payload, ct);
            Console.WriteLine($"[DLP] Sent clear priority queue signal for {messageId}");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DLP] Failed to send clear queue signal: {ex.Message}");
        }
    }

    private static async Task SendToPipeAsync(object payload, CancellationToken ct)
    {
        await Task.Run(() =>
        {
            try
            {
                using var pipeClient = new NamedPipeClientStream(
                    ".", 
                    PipeName, 
                    PipeDirection.InOut, 
                    PipeOptions.Asynchronous);
                
                pipeClient.Connect(5000); // 5 second timeout
                
                if (!pipeClient.IsConnected)
                    throw new IOException("Failed to connect to pipe");

                var json = JsonSerializer.Serialize(payload);
                var bytes = Encoding.UTF8.GetBytes(json);

                // Send request
                pipeClient.Write(bytes, 0, bytes.Length);
                pipeClient.Flush();

                // Read and discard response (we don't use it)
                var responseBuffer = new byte[64 * 1024];
                _ = pipeClient.Read(responseBuffer, 0, responseBuffer.Length);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[DLP] Pipe error: {ex.Message}");
            }
        }, ct);
    }

    private static void SetOwnClipboardText(string text)
    {
        for (int i = 0; i < 5; i++)
        {
            try
            {
                Clipboard.SetText(text);
                return;
            }
            catch (ExternalException)
            {
                Thread.Sleep(50);
            }
        }
    }
}
