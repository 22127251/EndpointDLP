using System.Runtime.InteropServices;

namespace TransferAgent;

internal static class NativeMethods
{
    // SHCNE_CREATE  = 0x00000008 — a file was created
    // SHCNF_PATH    = 0x0001     — dwItem1/dwItem2 are file-system paths
    // SHCNF_FLUSH   = 0x1000     — wait for notification to complete
    internal const int  SHCNE_CREATE   = 0x00000008;
    internal const uint SHCNF_PATH     = 0x0001;
    internal const uint SHCNF_FLUSH    = 0x1000;

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    internal static extern void SHChangeNotify(
        int    wEventId,
        uint   uFlags,
        IntPtr dwItem1,
        IntPtr dwItem2);

    internal static void NotifyFileCreated(string path)
    {
        IntPtr ptr = Marshal.StringToCoTaskMemUni(path);
        try
        {
            SHChangeNotify(SHCNE_CREATE, SHCNF_PATH | SHCNF_FLUSH, ptr, IntPtr.Zero);
        }
        finally
        {
            Marshal.FreeCoTaskMem(ptr);
        }
    }
}
