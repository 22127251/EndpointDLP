using System.Collections.Concurrent;
using AgentCore;

namespace QueueManager;

/// <summary>
/// Represents a text chunk to be analyzed.
/// </summary>
public class Chunk
{
    public string Channel { get; set; } = "";       // "clipboard" or "browser"
    public bool Priority { get; set; }              // true for clipboard
    public string MessageId { get; set; } = "";     // Unique ID for the source message
    public int ChunkId { get; set; }                // Index of this chunk within the message
    public int TotalChunks { get; set; }            // Total number of chunks in this message
    public string Content { get; set; } = "";       // The text content
    public int WordCount { get; set; }              // Number of words
    public string? SourceUrl { get; set; }          // For browser channel
    public string? Filename { get; set; }           // For file uploads
    public string Timestamp { get; set; } = "";     // ISO timestamp
}

/// <summary>
/// Signal to clear the priority queue.
/// </summary>
public class ClearPriorityQueueSignal
{
    public string Action { get; set; } = "clear_priority_queue";
    public string Channel { get; set; } = "clipboard";
    public string MessageId { get; set; } = "";
}

/// <summary>
/// Tracks chunks for a message and reconstructs original text.
/// </summary>
public class MessageTracker
{
    public string MessageId { get; set; } = "";
    public string Channel { get; set; } = "";
    public int TotalChunks { get; set; }
    public ConcurrentDictionary<int, string> Chunks { get; } = new();
    public bool IsBlocked { get; set; } = false;
    public bool AnalysisComplete { get; set; } = false;
    public string? ReconstructedText { get; set; }
    
    /// <summary>
    /// Reconstruct original text from chunks by removing overlap.
    /// Chunks overlap by 50 words, so we need to deduplicate.
    /// </summary>
    public string ReconstructText(int overlapWords = 50)
    {
        var sortedChunks = Chunks.OrderBy(kvp => kvp.Key).ToList();
        if (!sortedChunks.Any())
            return "";
        
        if (sortedChunks.Count == 1)
            return sortedChunks[0].Value;
        
        var result = new System.Text.StringBuilder();
        result.Append(sortedChunks[0].Value);
        
        for (int i = 1; i < sortedChunks.Count; i++)
        {
            var prevChunk = sortedChunks[i - 1].Value;
            var currChunk = sortedChunks[i].Value;
            
            // Find overlap: last N words of prev should match first N words of curr
            var prevWords = prevChunk.Split(new[] { ' ', '\t', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries);
            var currWords = currChunk.Split(new[] { ' ', '\t', '\n', '\r' }, StringSplitOptions.RemoveEmptyEntries);
            
            // Find how many words to skip from curr (the overlap)
            int overlapToSkip = 0;
            for (int check = Math.Min(overlapWords, currWords.Length); check > 0; check--)
            {
                if (prevWords.Length < check)
                    continue;
                    
                var prevEnd = prevWords.Skip(prevWords.Length - check).ToArray();
                var currStart = currWords.Take(check).ToArray();
                
                if (prevEnd.SequenceEqual(currStart))
                {
                    overlapToSkip = check;
                    break;
                }
            }
            
            // Append non-overlapping part
            if (overlapToSkip < currWords.Length)
            {
                var toAppend = string.Join(" ", currWords.Skip(overlapToSkip));
                if (!string.IsNullOrEmpty(toAppend))
                {
                    result.Append(" ");
                    result.Append(toAppend);
                }
            }
        }
        
        return result.ToString().Trim();
    }
}

/// <summary>
/// Thread-safe dual queue system for chunk analysis.
/// </summary>
public class ChunkQueueManager
{
    private readonly ConcurrentQueue<Chunk> _priorityQueue = new();
    private readonly ConcurrentQueue<Chunk> _nonPriorityQueue = new();
    private readonly object _lock = new();

    // Track current clipboard message ID - chunks from old messages are stale
    private string _currentClipboardMessageId = "";

    // Track messages: message_id -> MessageTracker
    private readonly Dictionary<string, MessageTracker> _messages = new();

    // Track blocked messages with expiry
    private readonly Dictionary<string, float> _blockedMessages = new(); // message_id -> expiry_time (monotonic)

    private const float BLOCK_TTL_SECONDS = 60f;

    /// <summary>
    /// Clear priority queue when new clipboard message arrives.
    /// </summary>
    public void ClearPriorityQueue(string newMessageId)
    {
        lock (_lock)
        {
            // Clear the priority queue
            while (!_priorityQueue.IsEmpty)
            {
                _priorityQueue.TryDequeue(out _);
            }

            _currentClipboardMessageId = newMessageId;
            Console.WriteLine($"[QueueManager] Priority queue cleared for new clipboard message: {newMessageId}");
        }
    }

    /// <summary>
    /// Enqueue a chunk to appropriate queue based on priority.
    /// </summary>
    public void Enqueue(Chunk chunk)
    {
        if (chunk.Priority)
        {
            // Clear old chunks from different clipboard message
            if (chunk.MessageId != _currentClipboardMessageId)
            {
                ClearPriorityQueue(chunk.MessageId);
            }

            _priorityQueue.Enqueue(chunk);
            Console.WriteLine($"[QueueManager] Enqueued priority chunk {chunk.ChunkId + 1}/{chunk.TotalChunks} for message {chunk.MessageId}");
        }
        else
        {
            _nonPriorityQueue.Enqueue(chunk);
            Console.WriteLine($"[QueueManager] Enqueued non-priority chunk {chunk.ChunkId + 1}/{chunk.TotalChunks} for message {chunk.MessageId}");
        }
    }

    /// <summary>
    /// Try dequeue next chunk. Priority queue is processed first.
    /// Returns false if no chunks available.
    /// </summary>
    public bool TryDequeue(out Chunk? chunk)
    {
        // Try priority queue first
        if (_priorityQueue.TryDequeue(out chunk))
        {
            // Check if this chunk is from current clipboard message
            if (chunk.MessageId == _currentClipboardMessageId)
            {
                return true;
            }
            // Stale chunk, discard and try again
            chunk = null;
        }

        // Try non-priority queue
        if (_nonPriorityQueue.TryDequeue(out chunk))
        {
            return true;
        }

        return false;
    }

    /// <summary>
    /// Check if we can make an immediate decision for a chunk (streaming).
    /// Returns null if we need to analyze this chunk.
    /// </summary>
    public string? GetImmediateDecision(string messageId)
    {
        lock (_lock)
        {
            // If message is already blocked, all chunks are BLOCK
            if (_blockedMessages.ContainsKey(messageId))
            {
                var now = (float)DateTimeOffset.Now.ToUnixTimeSeconds();
                var expiry = _blockedMessages[messageId];
                if (now < expiry)
                {
                    return "BLOCK";
                }
                _blockedMessages.Remove(messageId);
            }

            // Check if message is already blocked
            if (_messages.TryGetValue(messageId, out var tracker) && tracker.IsBlocked)
            {
                return "BLOCK";
            }

            return null;
        }
    }

    /// <summary>
    /// Record a decision for a chunk.
    /// Returns (chunkDecision, overallDecision, reconstructedText) where overallDecision is null if message not complete.
    /// </summary>
    public (string chunkDecision, string? overallDecision, string? reconstructedText) RecordChunkDecision(
        string messageId, int chunkId, string decision, string chunkContent)
    {
        lock (_lock)
        {
            if (!_messages.ContainsKey(messageId))
            {
                _messages[messageId] = new MessageTracker 
                { 
                    MessageId = messageId,
                    TotalChunks = 0,
                    Channel = ""
                };
            }

            var tracker = _messages[messageId];
            tracker.TotalChunks = Math.Max(tracker.TotalChunks, chunkId + 1);
            tracker.Chunks[chunkId] = chunkContent;

            // If any chunk is BLOCK, message is blocked
            if (decision == "BLOCK")
            {
                tracker.IsBlocked = true;
                _blockedMessages[messageId] = (float)DateTimeOffset.Now.ToUnixTimeSeconds() + BLOCK_TTL_SECONDS;
                
                Console.WriteLine($"[QueueManager] Message {messageId} BLOCKED at chunk {chunkId + 1}");
                return (decision, "BLOCK", null);
            }

            // Check if all chunks received
            if (tracker.Chunks.Count >= tracker.TotalChunks && tracker.TotalChunks > 0)
            {
                tracker.AnalysisComplete = true;
                
                // All chunks ALLOW - reconstruct original text
                var reconstructedText = tracker.ReconstructText();
                tracker.ReconstructedText = reconstructedText;
                
                // Clean up tracking
                _messages.Remove(messageId);

                Console.WriteLine($"[QueueManager] Message {messageId} ALLOW - reconstructed {reconstructedText.Length} chars");
                return (decision, "ALLOW", reconstructedText);
            }

            return (decision, null, null);
        }
    }

    /// <summary>
    /// Initialize tracking for a message.
    /// </summary>
    public void InitializeMessageTracking(string messageId, int totalChunks, string channel)
    {
        lock (_lock)
        {
            if (!_messages.ContainsKey(messageId))
            {
                _messages[messageId] = new MessageTracker 
                { 
                    MessageId = messageId,
                    TotalChunks = totalChunks,
                    Channel = channel
                };
            }
        }
    }

    /// <summary>
    /// Check if a message is blocked (with TTL).
    /// </summary>
    public bool IsMessageBlocked(string messageId)
    {
        lock (_lock)
        {
            if (_blockedMessages.TryGetValue(messageId, out var expiry))
            {
                var now = (float)DateTimeOffset.Now.ToUnixTimeSeconds();
                if (now >= expiry)
                {
                    _blockedMessages.Remove(messageId);
                    return false;
                }
                return true;
            }
            
            // Also check tracker
            if (_messages.TryGetValue(messageId, out var tracker) && tracker.IsBlocked)
            {
                return true;
            }
            
            return false;
        }
    }

    /// <summary>
    /// Get queue statistics.
    /// </summary>
    public (int priorityCount, int nonPriorityCount) GetQueueStats()
    {
        return (_priorityQueue.Count, _nonPriorityQueue.Count);
    }
}
