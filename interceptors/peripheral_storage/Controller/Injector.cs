using System.Text;
using static Controller.NativeMethods;

namespace Controller;

/// <summary>
/// Injects a 64-bit DLL into a target process using the classic
/// CreateRemoteThread + LoadLibraryW technique.
/// </summary>
internal sealed class Injector
{
    private string _dllPath;

    public Injector(string dllPath) =>
        _dllPath = Path.GetFullPath(dllPath);

    public void UpdateDllPath(string fullPath)
    {
        _dllPath = fullPath;
        Log.Write($"[Injector] DLL path updated: {_dllPath}");
    }

    public bool Inject(int pid, out int errorCode)
    {
        errorCode = 0;

        // If the DLL was loaded by a prior Controller session it is now in soft-bypass
        // (g_hookActive==0). HookInit created a named reactivate event that persists in
        // the kernel for the lifetime of the target process. Signalling it tells
        // WatcherThread to re-activate the hook and begin monitoring the new mutex,
        // avoiding a no-op LoadLibraryW call.
        var reactivateEventName = $"Global\\UsbDlpReactivate_{pid}";
        var hReactivate = OpenEvent(EVENT_MODIFY_STATE, false, reactivateEventName);
        if (hReactivate != IntPtr.Zero)
        {
            try
            {
                SetEvent(hReactivate);
                Log.Write($"[Injector] Signaled reactivate event for PID {pid} (DLL already loaded)");
                return true;
            }
            finally { CloseHandle(hReactivate); }
        }

        // Encode the absolute DLL path as UTF-16 including null terminator.
        var pathBytes = Encoding.Unicode.GetBytes(_dllPath + "\0");
        Log.Write($"[Debug] Injecting DLL at: {_dllPath}");

        var hProcess = OpenProcess(PROCESS_ALL_ACCESS, false, pid);
        if (hProcess == IntPtr.Zero)
        {
            errorCode = System.Runtime.InteropServices.Marshal.GetLastWin32Error();
            return false;
        }
        try
        {
            var remoteAddr = VirtualAllocEx(
                hProcess, IntPtr.Zero, (nuint)pathBytes.Length,
                MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
            if (remoteAddr == IntPtr.Zero)
            {
                errorCode = System.Runtime.InteropServices.Marshal.GetLastWin32Error();
                return false;
            }
            try
            {
                if (!WriteProcessMemory(hProcess, remoteAddr, pathBytes,
                        (nuint)pathBytes.Length, out _))
                {
                    errorCode = System.Runtime.InteropServices.Marshal.GetLastWin32Error();
                    return false;
                }

                // LoadLibraryW lives at the same virtual address in every 64-bit process.
                var kernel32   = GetModuleHandleW("kernel32.dll");
                var loadLibPtr = GetProcAddress(kernel32, "LoadLibraryW");
                if (loadLibPtr == IntPtr.Zero)
                {
                    errorCode = System.Runtime.InteropServices.Marshal.GetLastWin32Error();
                    return false;
                }

                var hThread = CreateRemoteThread(
                    hProcess, IntPtr.Zero, 0,
                    loadLibPtr, remoteAddr, 0, out _);
                if (hThread == IntPtr.Zero)
                {
                    errorCode = System.Runtime.InteropServices.Marshal.GetLastWin32Error();
                    return false;
                }

                try
                {
                    WaitForSingleObject(hThread, 5_000);
                    GetExitCodeThread(hThread, out var hModule);
                    if (hModule == IntPtr.Zero)
                    {
                        errorCode = 9999;  // custom error code for "remote LoadLibraryW failed"
                        return false;
                    }
                    return true;   // LoadLibraryW returns the HMODULE
                }
                finally { CloseHandle(hThread); }
            }
            finally { VirtualFreeEx(hProcess, remoteAddr, 0, MEM_RELEASE); }
        }
        finally { CloseHandle(hProcess); }
    }
}