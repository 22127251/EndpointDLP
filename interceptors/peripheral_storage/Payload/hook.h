#pragma once

/// Called from DllMain DLL_PROCESS_ATTACH.
/// Opens the shared memory segment, reads fail_closed flag, attaches Detours hook,
/// then spawns the watcher thread that waits on Global\UsbDlpAlive.
void HookInit();

/// Detaches the Detours hook and cleans up resources.
/// Thread-safe: safe to call from the watcher thread or DllMain.
/// Idempotent: additional calls after the first are no-ops.
void HookUninit();
