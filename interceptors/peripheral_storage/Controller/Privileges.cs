using System.Runtime.InteropServices;
using static Controller.NativeMethods;

namespace Controller;

/// <summary>
/// Token-privilege helpers. Phase E adds SeDebugPrivilege enabling so a Controller
/// running in Session 0 (LocalSystem service context) can OpenProcess a
/// user-session explorer.exe for cross-session injection (Option A). When the
/// Controller runs in the user's own session this is a harmless no-op-ish call —
/// the privilege is simply enabled on the process token if held.
/// </summary>
internal static class Privileges
{
    /// <summary>
    /// Enables SeDebugPrivilege on the current process token. Returns true on
    /// success. Logs (does not throw) on failure so startup is never blocked —
    /// same-session injection works without it, and the spike/Option-A smoke
    /// surfaces any genuine cross-session denial.
    /// </summary>
    public static bool EnableSeDebug()
    {
        if (!OpenProcessToken(GetCurrentProcess(),
                TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, out var hToken))
        {
            Log.WriteError(
                $"[Privileges] OpenProcessToken failed (err={Marshal.GetLastWin32Error()})");
            return false;
        }
        try
        {
            if (!LookupPrivilegeValueW(null, SE_DEBUG_NAME, out var luid))
            {
                Log.WriteError(
                    $"[Privileges] LookupPrivilegeValue(SeDebugPrivilege) failed " +
                    $"(err={Marshal.GetLastWin32Error()})");
                return false;
            }

            var tp = new TOKEN_PRIVILEGES
            {
                PrivilegeCount = 1,
                Luid = luid,
                Attributes = SE_PRIVILEGE_ENABLED,
            };

            if (!AdjustTokenPrivileges(hToken, false, ref tp, 0, IntPtr.Zero, IntPtr.Zero))
            {
                Log.WriteError(
                    $"[Privileges] AdjustTokenPrivileges failed (err={Marshal.GetLastWin32Error()})");
                return false;
            }

            // AdjustTokenPrivileges returns true even when the privilege is not held
            // by the account; that case surfaces as ERROR_NOT_ALL_ASSIGNED.
            if (Marshal.GetLastWin32Error() == ERROR_NOT_ALL_ASSIGNED)
            {
                Log.Write(
                    "[Privileges] SeDebugPrivilege not held by this account " +
                    "(ERROR_NOT_ALL_ASSIGNED) — cross-session injection unavailable.");
                return false;
            }

            Log.Write("[Privileges] SeDebugPrivilege enabled.");
            return true;
        }
        finally { CloseHandle(hToken); }
    }
}
