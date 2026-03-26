using AgentCore;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace ClipboardInterceptor;

/// <summary>
/// Intercepts user clipboard copies, routes them through the agent core for analysis,
/// and applies the decision (Allow = restore original, Block = replace with notification).
///
/// Self-write detection uses string comparison:
///   - Placeholder and block strings are matched exactly.
///   - The most recently allowed text is held in _allowRestoreText and matched on restore.
///
/// Concurrent copies cancel the in-flight analysis via CancellationToken. Only the
/// decision whose ID matches _currentAnalysisId is applied; stale decisions are discarded.
/// </summary>
public sealed class ClipboardInterceptorService
{
    private const string Placeholder = "[DLP: Analyzing...]";
    private const string BlockNotification = "[DLP: Content Blocked]";

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

        _ = ProcessAsync(content, id, _currentCts.Token);
    }

    // Starts on STA thread; resumes on STA thread after await via WinForms SynchronizationContext
    private async Task ProcessAsync(string content, string id, CancellationToken ct)
    {
        try
        {
            SetOwnClipboardText(Placeholder);

            AnalysisDecision decision = await _agentCore.AnalyseAsync(content, ct);

            // Discard stale decisions from superseded analyses
            if (_currentAnalysisId != id) return;

            if (decision == AnalysisDecision.Allow)
            {
                _allowRestoreText = content;
                SetOwnClipboardText(content);
                Console.WriteLine("[DLP] Decision: ALLOW — original content restored.");
            }
            else
            {
                SetOwnClipboardText(BlockNotification);
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
}
