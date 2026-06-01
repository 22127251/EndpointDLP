using System.Text.Json.Serialization;

namespace Controller.Config;

/// <summary>
/// Represents the <c>peripheral_storage</c> section of the central
/// <c>config.yaml</c> (Phase B). Loaded via
/// <see cref="DlpShared.ConfigLocator.LoadSection{T}"/> at startup, and
/// re-built from the ctl-pipe's JSON push at every config change.
///
/// The nested <c>transfer_agent</c> sub-object (used by TransferAgent only)
/// is intentionally absent here; both YamlDotNet (IgnoreUnmatchedProperties)
/// and System.Text.Json silently ignore unknown keys.
/// </summary>
internal sealed class AppConfig
{
    public List<string> TargetProcesses { get; set; } = new();

    /// <summary>"open" (default) or "closed"</summary>
    public string FailMode { get; set; } = "open";

    public string SharedMemoryName { get; set; } = "UsbDlpDriveMap";

    public string PayloadDllPath { get; set; } = "Payload.dll";

    [JsonIgnore]
    public bool FailClosed => string.Equals(FailMode, "closed", StringComparison.OrdinalIgnoreCase);
}
