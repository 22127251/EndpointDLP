using System.Collections.Concurrent;
using System.Diagnostics;
using System.Management;

namespace Controller;

/// <summary>
/// Injects the payload DLL into already-running and newly-spawned instances of
/// the configured target executables.
/// </summary>
internal sealed class ProcessMonitor : IDisposable
{
    private readonly IReadOnlyList<string> _targetProcesses;
    private readonly Injector _injector;
    private readonly ConcurrentDictionary<int, byte> _injectedPids = new();
    private ManagementEventWatcher? _watcher;

    public ProcessMonitor(IReadOnlyList<string> targetProcesses, Injector injector)
    {
        _targetProcesses = targetProcesses;
        _injector = injector;
    }

    public void Start()
    {
        // Inject into processes already running before the Controller started.
        foreach (var target in _targetProcesses)
        {
            var baseName = Path.GetFileNameWithoutExtension(target);
            foreach (var proc in Process.GetProcessesByName(baseName))
            {
                TryInject(proc.Id, proc.ProcessName + ".exe");
                proc.Dispose();
            }
        }

        // Build a lower-case set for fast comparison.
        var targetSet = _targetProcesses
            .Select(p => p.ToLowerInvariant())
            .ToHashSet();

        _watcher = new ManagementEventWatcher("SELECT * FROM Win32_ProcessStartTrace");
        _watcher.EventArrived += (_, e) =>
        {
            var name = e.NewEvent["ProcessName"]?.ToString();
            if (name is null) return;
            if (!targetSet.Contains(name.ToLowerInvariant())) return;

            if (!int.TryParse(e.NewEvent["ProcessID"]?.ToString(), out var pid)) return;
            TryInject(pid, name);
        };
        _watcher.Start();
    }

    private void TryInject(int pid, string processName)
    {
        if (!_injectedPids.TryAdd(pid, 0)) return;  // already injected or in-flight

        Log.Write($"[ProcessMonitor] Targeting {processName} (PID={pid})");
        int errorCode;
        if (_injector.Inject(pid, out errorCode))
        {
            Log.Write($"[Injector] Injected Payload.dll into {processName} (PID={pid})");
        }
        else
        {
            Log.Write($"[Injector] Failed to inject into {processName} (PID={pid}) — error {errorCode}");
            _injectedPids.TryRemove(pid, out _);
        }
    }

    public void Dispose()
    {
        _watcher?.Stop();
        _watcher?.Dispose();
    }
}
