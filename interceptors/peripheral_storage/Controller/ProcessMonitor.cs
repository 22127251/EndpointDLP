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
    private readonly ConcurrentDictionary<int, string> _injectedPids = new();
    private ManagementEventWatcher? _watcher;
    // Volatile reference: updated atomically by UpdateTargets; read lock-free by the WMI handler.
    // The HashSet itself is immutable after assignment — only .Contains() is called on it.
    private volatile HashSet<string> _activeTargetSet = new();

    public ProcessMonitor(IReadOnlyList<string> targetProcesses, Injector injector)
    {
        _targetProcesses = targetProcesses;
        _injector = injector;
    }

    public void Start()
    {
        // Initialise the active target set before starting the WMI watcher.
        _activeTargetSet = _targetProcesses
            .Select(p => p.ToLowerInvariant())
            .ToHashSet();

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

        _watcher = new ManagementEventWatcher("SELECT * FROM Win32_ProcessStartTrace");
        _watcher.EventArrived += (_, e) =>
        {
            var name = e.NewEvent["ProcessName"]?.ToString();
            if (name is null) return;
            // Read the volatile reference once; the set is immutable after construction.
            if (!_activeTargetSet.Contains(name.ToLowerInvariant())) return;

            if (!int.TryParse(e.NewEvent["ProcessID"]?.ToString(), out var pid)) return;
            TryInject(pid, name);
        };
        _watcher.Start();
    }

    /// <summary>
    /// Hot-reloads the target process list. Newly added processes are injected immediately
    /// (if already running). Removed processes keep their existing injected DLL until they exit.
    /// </summary>
    public void UpdateTargets(IReadOnlyList<string> newTargets)
    {
        var newSet = newTargets.Select(p => p.ToLowerInvariant()).ToHashSet();
        var currentSet = _activeTargetSet;
        var added = newSet.Except(currentSet).ToList();

        // Atomically replace the target set so the WMI handler picks it up on next event.
        _activeTargetSet = newSet;

        // Soft-bypass already-injected processes whose names were removed from the list.
        var removed = currentSet.Except(newSet).ToHashSet();
        if (removed.Count > 0)
        {
            foreach (var kvp in _injectedPids)
            {
                if (!removed.Contains(kvp.Value)) continue;
                SignalSuppressEvent(kvp.Key);
                // Remove PID so it can be re-tracked and reactivated if re-added later.
                _injectedPids.TryRemove(kvp.Key, out _);
            }
        }

        // Inject into already-running instances of newly added targets.
        foreach (var target in added)
        {
            var baseName = Path.GetFileNameWithoutExtension(target);
            foreach (var proc in Process.GetProcessesByName(baseName))
            {
                TryInject(proc.Id, proc.ProcessName + ".exe");
                proc.Dispose();
            }
        }

        Log.Write($"[ProcessMonitor] Target list updated: {string.Join(", ", newTargets)}");
    }

    private static void SignalSuppressEvent(int pid)
    {
        var hEvent = NativeMethods.OpenEvent(
            NativeMethods.EVENT_MODIFY_STATE, false, $"Global\\UsbDlpSuppress_{pid}");
        if (hEvent == IntPtr.Zero) return;
        NativeMethods.SetEvent(hEvent);
        NativeMethods.CloseHandle(hEvent);
        Log.Write($"[ProcessMonitor] Signaled suppress for PID {pid}");
    }

    private void TryInject(int pid, string processName)
    {
        if (!_injectedPids.TryAdd(pid, processName.ToLowerInvariant())) return;  // already injected or in-flight

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
