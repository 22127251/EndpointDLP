namespace ClipboardInterceptor;

/// <summary>
/// Thread-safe holder for the (pipe_name, timeout_ms) pair that PipeAgentCore's
/// provider closes over. ctl-pipe pushes update the timeout in place; the data
/// pipe name is non-hot-reloadable (decision #7) so SetTimeoutMs is the only
/// mutator.
/// </summary>
internal sealed class ClipboardConfigHolder
{
    private readonly object _lock = new();
    private readonly string _pipeName;
    private int _timeoutMs;

    public ClipboardConfigHolder(string pipeName, int timeoutMs)
    {
        _pipeName = pipeName;
        _timeoutMs = timeoutMs;
    }

    public (string PipeName, int TimeoutMs) Get()
    {
        lock (_lock) return (_pipeName, _timeoutMs);
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
}

internal sealed class ClipboardSection
{
    public int PipeTimeoutMs { get; set; } = 6000;
}
