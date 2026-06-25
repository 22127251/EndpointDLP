namespace ClipboardInterceptor;

/// <summary>
/// Thread-safe holder for the (pipe_name, timeout_ms, fail_open) tuple that
/// PipeAgentCore's provider closes over. ctl-pipe pushes update the hot-reloadable
/// fields in place; the data pipe name is non-hot-reloadable (decision #7) so it is
/// the only immutable field. (The copied-text size cap was removed — the interceptor
/// sends the full clipboard text and the analyzer governs it via max_extracted_chars.)
/// </summary>
internal sealed class ClipboardConfigHolder
{
    private readonly object _lock = new();
    private readonly string _pipeName;
    private int _timeoutMs;
    private bool _failOpen;

    public ClipboardConfigHolder(string pipeName, int timeoutMs, bool failOpen)
    {
        _pipeName = pipeName;
        _timeoutMs = timeoutMs;
        _failOpen = failOpen;
    }

    public (string PipeName, int TimeoutMs, bool FailOpen) Get()
    {
        lock (_lock) return (_pipeName, _timeoutMs, _failOpen);
    }

    public string PipeName => _pipeName;

    public int TimeoutMs
    {
        get { lock (_lock) return _timeoutMs; }
    }

    public void SetTimeoutMs(int newTimeoutMs)
    {
        lock (_lock) _timeoutMs = newTimeoutMs;
    }

    public bool FailOpen
    {
        get { lock (_lock) return _failOpen; }
    }

    public void SetFailOpen(bool newFailOpen)
    {
        lock (_lock) _failOpen = newFailOpen;
    }
}

internal sealed class ClipboardSection
{
    public int PipeTimeoutMs { get; set; } = 6000;
    // fail_closed → BLOCK (default) | fail_open → ALLOW on any pipe/connect failure.
    public string FailureMode { get; set; } = "fail_closed";
}
