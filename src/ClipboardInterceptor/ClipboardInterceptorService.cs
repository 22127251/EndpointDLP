using AgentCore;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace ClipboardInterceptor;

/// <summary>
/// Intercepts user clipboard copies, routes them through the agent core for analysis,
/// and applies the decision (Allow = restore original, Block = replace with notification).
///
/// Self-write detection:
///   - EVERY clipboard text this service writes (the analyzing placeholder AND the
///     dynamic block notice, which now carries a per-policy/per-failure reason)
///     begins with the DlpMarker prefix, so IsDlpAuthored() excludes them all.
///     This is critical: the block text is no longer a fixed constant, so an
///     exact-string match could not catch it — without the prefix guard the
///     service would re-ingest its own block write, re-analyze it, and loop
///     forever, completely disabling the clipboard.
///   - The most recently allowed text is held in _allowRestoreText (real user
///     text, not marker-prefixed) and matched on restore.
///
/// Concurrent copies cancel the in-flight analysis via CancellationToken. Only the
/// decision whose ID matches _currentAnalysisId is applied; stale decisions are discarded.
/// </summary>
public sealed class ClipboardInterceptorService
{
    // Prefix shared by EVERY clipboard text this service writes (placeholder +
    // block notice). IsDlpAuthored() uses it to exclude our own writes from
    // re-analysis — the guard that keeps the dynamic block text from looping.
    private const string DlpMarker = "[DLP";
    private const string Placeholder = "[DLP: Analyzing...]";
    private const string BlockFallback = "[DLP] Content blocked";

    private readonly IAgentCore _agentCore;
    private string? _allowRestoreText;
    private CancellationTokenSource? _currentCts;
    private string _currentAnalysisId = string.Empty;

    public ClipboardInterceptorService(IAgentCore agentCore)
    {
        _agentCore = agentCore;
    }

    // Called on STA thread via ClipboardMonitor
    public void OnClipboardChanged(object? sender, EventArgs e)
    {
        if (!Clipboard.ContainsText()) return;
        string content = Clipboard.GetText();

        // Ignore our own writes (placeholder + any dynamic block notice). Prefix
        // match, NOT equality — the block text is dynamic, so equality would miss
        // it and the service would analyze its own write in an endless loop.
        if (IsDlpAuthored(content)) return;
        if (_allowRestoreText != null && content == _allowRestoreText) return;
        _allowRestoreText = null; // new user content — clear the allow restore guard

        // Cancel any in-flight analysis and start fresh for the new content
        _currentCts?.Cancel();
        _currentCts?.Dispose();
        _currentCts = new CancellationTokenSource();

        var id = Guid.NewGuid().ToString("N");
        _currentAnalysisId = id;

        _ = ProcessAsync(content, id, _currentCts.Token);
    }

    // Starts on STA thread; resumes on STA thread after await via WinForms SynchronizationContext
    private async Task ProcessAsync(string content, string id, CancellationToken ct)
    {
        try
        {
            SetOwnClipboardText(Placeholder);

            AnalysisOutcome outcome = await _agentCore.AnalyseAsync(content, ct);

            // Discard stale decisions from superseded analyses
            if (_currentAnalysisId != id) return;

            if (outcome.Decision == AnalysisDecision.Allow)
            {
                _allowRestoreText = content;
                SetOwnClipboardText(content);
                Console.WriteLine("[DLP] Decision: ALLOW — original content restored.");
            }
            else
            {
                // The replacement text carries the end-user reason and is
                // marker-prefixed (BlockFallback / BuildBlockText) so IsDlpAuthored
                // excludes it from re-analysis.
                SetOwnClipboardText(BuildBlockText(outcome.Reason));
                Console.WriteLine("[DLP] Decision: BLOCK — content replaced.");
            }
        }
        catch (OperationCanceledException)
        {
            Console.WriteLine("[DLP] Analysis cancelled — newer copy detected.");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DLP] Error during interception: {ex.Message}");
        }
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

    /// <summary>True if <paramref name="content"/> is one of THIS service's own
    /// clipboard writes (placeholder or block notice). Prefix match on DlpMarker —
    /// the loop guard that lets the block text carry a dynamic reason without the
    /// service re-analyzing its own output. Public for unit testing.</summary>
    public static bool IsDlpAuthored(string content) =>
        content.StartsWith(DlpMarker, StringComparison.Ordinal);

    /// <summary>The clipboard replacement text for a BLOCK. Always DlpMarker-prefixed
    /// (so IsDlpAuthored excludes it). Carries the end-user reason when one was
    /// returned; otherwise a generic notice. Public for unit testing.</summary>
    public static string BuildBlockText(string? reason) =>
        string.IsNullOrWhiteSpace(reason) ? BlockFallback : $"[DLP] Blocked: {reason}";
}
