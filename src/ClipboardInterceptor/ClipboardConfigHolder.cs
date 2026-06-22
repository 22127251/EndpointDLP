namespace ClipboardInterceptor;

/// <summary>
/// Thread-safe holder for the (pipe_name, timeout_ms, max_input_bytes, fail_open)
/// tuple that PipeAgentCore's provider closes over. ctl-pipe pushes update the
/// hot-reloadable fields in place; the data pipe name is non-hot-reloadable
/// (decision #7) so it is the only immutable field.
/// </summary>
internal sealed class ClipboardConfigHolder
{
    private readonly object _lock = new();
    private readonly string _pipeName;
    private int _timeoutMs;
    private int _maxInputBytes;
    private bool _failOpen;

    public ClipboardConfigHolder(string pipeName, int timeoutMs, int maxInputBytes, bool failOpen)
    {
        _pipeName = pipeName;
        _timeoutMs = timeoutMs;
        _maxInputBytes = maxInputBytes;
        _failOpen = failOpen;
    }

    public (string PipeName, int TimeoutMs, int MaxContentBytes, bool FailOpen) Get()
    {
        lock (_lock) return (_pipeName, _timeoutMs, _maxInputBytes, _failOpen);
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

    public int MaxInputBytes
    {
        get { lock (_lock) return _maxInputBytes; }
    }

    public void SetMaxInputBytes(int newMaxInputBytes)
    {
        lock (_lock) _maxInputBytes = newMaxInputBytes;
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
    // Cap on copied-text size sent for analysis (UTF-8 bytes). Default matches the
    // orchestrator (config.py) so a missing key behaves identically on both ends.
    public int MaxInputBytes { get; set; } = 8388608;   // 8 MB
    // fail_closed → BLOCK (default) | fail_open → ALLOW on any pipe/connect failure.
    public string FailureMode { get; set; } = "fail_closed";
}
