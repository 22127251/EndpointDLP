using System.Management;
using static Controller.NativeMethods;

namespace Controller;

/// <summary>
/// Enumerates removable drives on startup and subscribes to WMI
/// Win32_VolumeChangeEvent to keep the shared memory map current.
/// </summary>
internal sealed class DriveMonitor : IDisposable
{
    private readonly SharedMemoryWriter _writer;
    private ManagementEventWatcher? _watcher;

    public DriveMonitor(SharedMemoryWriter writer) => _writer = writer;

    public void Start()
    {
        RefreshMap();

        _watcher = new ManagementEventWatcher("SELECT * FROM Win32_VolumeChangeEvent");
        _watcher.EventArrived += (_, _) => RefreshMap();
        _watcher.Start();
    }

    private void RefreshMap()
    {
        var removable = new List<string>();

        foreach (var drive in DriveInfo.GetDrives())
        {
            var dosRoot = drive.Name.TrimEnd('\\');          // e.g. "E:"
            if (GetDriveTypeW(drive.Name) != DRIVE_REMOVABLE) continue;

            var buf = new char[512];
            var len = QueryDosDeviceW(dosRoot, buf, (uint)buf.Length);
            if (len == 0) continue;

            // QueryDosDevice returns one or more null-terminated strings.
            // The first entry is the canonical NT device path.
            var ntPath = new string(buf, 0, (int)len)
                .Split('\0', StringSplitOptions.RemoveEmptyEntries)[0];

            removable.Add(ntPath);
            removable.Add(@"\??\" + dosRoot);   // \??\E: — matches Win32 app NtCreateFile paths
            Log.Write($"[DriveMonitor] Removable: {ntPath} | \\??\\{dosRoot} → {drive.Name}");
        }

        _writer.WriteEntries(removable);
        Log.Write($"[DriveMonitor] Map updated: {removable.Count} removable drive(s)");
    }

    public void Dispose()
    {
        _watcher?.Stop();
        _watcher?.Dispose();
    }
}
