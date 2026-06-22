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

    /// <summary>"fail_closed" (default) or "fail_open" (Phase 7; was fail_mode open/closed).</summary>
    public string FailureMode { get; set; } = "fail_closed";

    public string SharedMemoryName { get; set; } = "UsbDlpDriveMap";

    public string PayloadDllPath { get; set; } = "Payload.dll";

    // Anything that is not explicitly "fail_open" blocks (fail closed) — the safe
    // default when the value is missing/unknown.
    [JsonIgnore]
    public bool FailClosed => !string.Equals(FailureMode, "fail_open", StringComparison.OrdinalIgnoreCase);
}

/// <summary>
/// Wrapper for the <c>peripheral_storage</c> section so the Controller can load its
/// nested <c>controller:</c> subtree (Phase 7) via
/// <see cref="DlpShared.ConfigLocator.LoadSection{T}"/>. The sibling
/// <c>transfer_agent</c> sub-object is ignored (unmatched keys are tolerated).
/// </summary>
internal sealed class PeripheralStorageSection
{
    public AppConfig Controller { get; set; } = new();
}
