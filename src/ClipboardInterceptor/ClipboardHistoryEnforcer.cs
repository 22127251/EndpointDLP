using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace ClipboardInterceptor;

/// <summary>
/// Enforces clipboard history disabled state by watching the registry key
/// HKCU\Software\Microsoft\Clipboard for changes using RegNotifyChangeKeyValue.
/// On any value change, immediately re-sets EnableClipboardHistory to 0.
/// Must be disposed to stop monitoring and restore the original value.
/// </summary>
public sealed class ClipboardHistoryEnforcer : IDisposable
{
    private const string ClipboardHistoryKeyPath = @"Software\Microsoft\Clipboard";
    private const string ClipboardHistoryValueName = "EnableClipboardHistory";
    private const string ClipboardHistoryFullKeyPath = @"HKEY_CURRENT_USER\Software\Microsoft\Clipboard";

    private const uint KEY_NOTIFY = 0x0010;
    private const uint KEY_SET_VALUE = 0x0002;
    private const uint KEY_READ = 0x20019;
    private const uint REG_NOTIFY_CHANGE_LAST_SET = 0x00000004;
    private const uint REG_NOTIFY_CHANGE_THREAD_AGNOSTIC = 0x10000000;
    private const uint WAIT_OBJECT_0 = 0;
    private const uint WAIT_TIMEOUT = 258;
    private const uint REG_DWORD = 4;
    private static readonly IntPtr HKEY_CURRENT_USER = new(unchecked((int)0x80000001));

    #region P/Invoke - advapi32.dll

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int RegOpenKeyExW(
        IntPtr hKey, string lpSubKey, uint ulOptions, uint samDesired, out IntPtr phkResult);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern int RegNotifyChangeKeyValue(
        IntPtr hKey, bool bWatchSubtree, uint dwNotifyFilter, IntPtr hEvent, bool fAsynchronous);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern int RegCloseKey(IntPtr hKey);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int RegSetValueExW(
        IntPtr hKey, string lpValueName, int Reserved, uint dwType, byte[] lpData, uint cbData);

    #endregion

    #region P/Invoke - kernel32.dll

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateEventW(
        IntPtr lpEventAttributes, bool bManualReset, bool bInitialState, string? lpName);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ResetEvent(IntPtr hEvent);

    #endregion

    private IntPtr _hKey;
    private IntPtr _hEvent;
    private Thread? _watchThread;
    private readonly ManualResetEventSlim _started = new(false);
    private readonly CancellationTokenSource _cts = new();
    private readonly object? _previousValue;
    private bool _disposed;

    public ClipboardHistoryEnforcer()
    {
        // Snapshot current value
        _previousValue = Registry.GetValue(ClipboardHistoryFullKeyPath, ClipboardHistoryValueName, null);

        // Disable clipboard history
        Registry.SetValue(ClipboardHistoryFullKeyPath, ClipboardHistoryValueName, 0, RegistryValueKind.DWord);
        Console.WriteLine("[DLP] Clipboard history disabled.");

        // Open registry key with KEY_NOTIFY | KEY_SET_VALUE for notification + write access
        int error = RegOpenKeyExW(HKEY_CURRENT_USER, ClipboardHistoryKeyPath, 0,
            KEY_NOTIFY | KEY_SET_VALUE, out _hKey);
        if (error != 0)
            throw new InvalidOperationException($"RegOpenKeyExW failed with error {error}");

        // Create manual-reset event (initially non-signaled)
        _hEvent = CreateEventW(IntPtr.Zero, true, false, null);
        if (_hEvent == IntPtr.Zero)
            throw new InvalidOperationException($"CreateEventW failed with error {Marshal.GetLastWin32Error()}");

        // Start background watch thread
        _watchThread = new Thread(WatchLoop)
        {
            IsBackground = true,
            Name = "ClipboardHistoryEnforcer"
        };
        _watchThread.Start();

        // Wait for the first RegNotifyChangeKeyValue registration to complete
        _started.Wait();
    }

    /// <summary>
    /// Returns the current value of EnableClipboardHistory from the registry.
    /// Exposed for testing.
    /// </summary>
    public static uint GetCurrentClipboardHistoryValue()
    {
        object? val = Registry.GetValue(ClipboardHistoryFullKeyPath, ClipboardHistoryValueName, null);
        return val is int i ? (uint)i : 1;
    }

    private void WatchLoop()
    {
        try
        {
            uint notifyFlags = REG_NOTIFY_CHANGE_LAST_SET | REG_NOTIFY_CHANGE_THREAD_AGNOSTIC;

            // Register first notification
            int error = RegNotifyChangeKeyValue(_hKey, false, notifyFlags, _hEvent, true);
            if (error != 0)
            {
                Console.WriteLine($"[DLP] RegNotifyChangeKeyValue failed: {error}");
                _started.Set();
                return;
            }
            _started.Set();

            while (!_cts.IsCancellationRequested)
            {
                uint waitResult = WaitForSingleObject(_hEvent, 500);
                if (_cts.IsCancellationRequested) break;

                if (waitResult == WAIT_OBJECT_0)
                {
                    // Value changed — re-disable
                    SetClipboardHistoryValue(0);
                    Console.WriteLine("[DLP] Clipboard history re-disabled (external change detected).");

                    // Reset the manual-reset event before re-registering
                    ResetEvent(_hEvent);

                    // Re-register for next notification
                    error = RegNotifyChangeKeyValue(_hKey, false, notifyFlags, _hEvent, true);
                    if (error != 0)
                    {
                        Console.WriteLine($"[DLP] Re-registration failed: {error}");
                        break;
                    }
                }
                // WAIT_TIMEOUT — loop back and check cancellation
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"[DLP] Enforcer watch loop error: {ex.Message}");
        }
    }

    private void SetClipboardHistoryValue(uint value)
    {
        byte[] data = BitConverter.GetBytes(value);
        RegSetValueExW(_hKey, ClipboardHistoryValueName, 0, REG_DWORD, data, (uint)data.Length);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        _cts.Cancel();
        _watchThread?.Join(2000);

        if (_hEvent != IntPtr.Zero) CloseHandle(_hEvent);
        if (_hKey != IntPtr.Zero) RegCloseKey(_hKey);

        _cts.Dispose();
        _started.Dispose();

        // Restore previous value
        uint restoreValue = _previousValue is int i ? (uint)i : 1;
        Registry.SetValue(ClipboardHistoryFullKeyPath, ClipboardHistoryValueName,
            (int)restoreValue, RegistryValueKind.DWord);
        Console.WriteLine("[DLP] Clipboard history restored.");
    }
}
