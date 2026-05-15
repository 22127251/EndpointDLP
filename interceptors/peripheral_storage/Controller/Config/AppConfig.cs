namespace Controller.Config;

internal sealed class AppConfig
{
    public List<string> TargetProcesses { get; set; } = new();

    /// <summary>"open" (default) or "closed"</summary>
    public string FailMode { get; set; } = "open";

    public string SharedMemoryName { get; set; } = "UsbDlpDriveMap";

    public string PayloadDllPath { get; set; } = "Payload.dll";

    public bool FailClosed => string.Equals(FailMode, "closed", StringComparison.OrdinalIgnoreCase);
}
