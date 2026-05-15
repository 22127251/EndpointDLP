using System.Runtime.InteropServices;
using static Controller.NativeMethods;

namespace Controller;

/// <summary>
/// Creates and holds the named mutex Global\UsbDlpAlive for the duration of
/// the Controller process.  Injected DLLs wait on this mutex; when it is
/// released (graceful) or abandoned (crash), they detach their hooks.
/// </summary>
internal sealed class AliveMutex : IDisposable
{
    private IntPtr _handle = IntPtr.Zero;
    private bool _disposed;

    // SYNCHRONIZE to Everyone; full control to SYSTEM and Administrators.
    private const string Sddl =
        "D:(A;;0x00100000;;;WD)(A;;0x001F0001;;;SY)(A;;0x001F0001;;;BA)";

    public void Create()
    {
        if (!ConvertStringSecurityDescriptorToSecurityDescriptor(
                Sddl, 1, out var pSD, out _))
            throw new InvalidOperationException(
                $"SDDL conversion failed: {Marshal.GetLastWin32Error()}");

        try
        {
            var sa = new SECURITY_ATTRIBUTES
            {
                nLength = Marshal.SizeOf<SECURITY_ATTRIBUTES>(),
                lpSecurityDescriptor = pSD,
                bInheritHandle = false
            };

            // bInitialOwner = true → we hold the mutex until Dispose()
            _handle = CreateMutexW(ref sa, bInitialOwner: true, "Global\\UsbDlpAlive");
            if (_handle == IntPtr.Zero)
                throw new InvalidOperationException(
                    $"CreateMutexW failed: {Marshal.GetLastWin32Error()}");
        }
        finally
        {
            LocalFree(pSD);
        }

        Log.Write("[Controller] Alive mutex created: Global\\UsbDlpAlive");
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        if (_handle != IntPtr.Zero)
        {
            ReleaseMutex(_handle);   // signals all waiting DLL watcher threads
            CloseHandle(_handle);
            _handle = IntPtr.Zero;
            Log.Write("[Controller] Alive mutex released.");
        }
    }
}
