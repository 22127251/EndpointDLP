using System.Text;
using System.Text.Json;
using Controller.Config;
using DlpShared;
using YamlDotNet.Serialization;
using YamlDotNet.Serialization.NamingConventions;

namespace Controller;

internal static class Program
{
    static async Task Main(string[] args)
    {
        // Phase B: read the central config.yaml. The legacy
        // Controller/Config/config.yaml has been removed; this file is the
        // single source of truth for non-policy config across all components.
        string yamlPath;
        AppConfig config;
        string ctlPipeName;
        try
        {
            yamlPath = ConfigLocator.FindConfigYaml();
            var (_, ctlPipe) = ConfigLocator.LoadTopLevel(yamlPath);
            ctlPipeName = ctlPipe;
            // Phase 7: the Controller's settings moved under peripheral_storage.controller.
            config = ConfigLocator.LoadSection<PeripheralStorageSection>(yamlPath, "peripheral_storage").Controller;
        }
        catch (Exception ex)
        {
            Log.WriteError($"[Controller] Failed to load central config: {ex.Message}");
            return;
        }

        Log.Write($"[Controller] Loaded central config: {yamlPath}");
        Log.Write($"[Controller] Targets: {string.Join(", ", config.TargetProcesses)}");
        Log.Write($"[Controller] Failure mode: {config.FailureMode}");

        // Phase E: enable SeDebugPrivilege up front so injection works when the
        // Controller runs in Session 0 (LocalSystem service) and must reach a
        // user-session explorer.exe. No-op-ish for same-session (Phase C) runs.
        Privileges.EnableSeDebug();

        using var cts = new CancellationTokenSource();
        Console.CancelKeyPress += (_, e) =>
        {
            e.Cancel = true;
            cts.Cancel();
        };

        using var shmWriter = new SharedMemoryWriter();
        shmWriter.Initialize(config.SharedMemoryName, config.FailClosed);
        Log.Write($"[Controller] Shared memory ready: Global\\{config.SharedMemoryName}");

        using var aliveMutex = new AliveMutex();
        aliveMutex.Create();

        // Resolve relative DLL path against the exe's own directory so it works
        // regardless of the working directory the user launches from.
        var resolvedDllPath = Path.IsPathRooted(config.PayloadDllPath)
            ? config.PayloadDllPath
            : Path.Combine(AppContext.BaseDirectory, config.PayloadDllPath);
        var injector = new Injector(resolvedDllPath);

        using var driveMonitor = new DriveMonitor(shmWriter);
        driveMonitor.Start();

        using var processMonitor = new ProcessMonitor(config.TargetProcesses, injector);
        processMonitor.Start();

        // ── Hot-reload via ctl-pipe push ──────────────────────────────────────
        // Tracks what is actually running (may differ from the central config
        // after a rejected change like shared_memory_name).
        AppConfig currentConfig = config;
        var reloadGate = new object();

        void ExportRunningConfig()
        {
            try
            {
                var outPath = Path.Combine(AppContext.BaseDirectory, "running-config.yaml");
                var sb = new StringBuilder();
                sb.AppendLine("# AUTO-GENERATED — Do not edit directly.");
                sb.AppendLine("# Reflects the configuration currently active in the running Controller.");
                sb.AppendLine("# The central config.yaml's peripheral_storage section may differ from this");
                sb.AppendLine("# if a ctl-pipe push was rejected (e.g., shared_memory_name change).");
                sb.AppendLine($"# Last written: {DateTime.Now:yyyy-MM-dd HH:mm:ss}");
                sb.AppendLine();
                sb.Append(new SerializerBuilder()
                    .WithNamingConvention(UnderscoredNamingConvention.Instance)
                    .Build()
                    .Serialize(currentConfig));
                File.WriteAllText(outPath, sb.ToString());
                Log.Write($"[Controller] Running config written to: {outPath}");
            }
            catch (Exception ex)
            {
                Log.WriteError($"[Controller] Failed to write running-config.yaml: {ex.Message}");
            }
        }

        void TryReload(AppConfig newConfig)
        {
            lock (reloadGate)
            {
                if (newConfig.TargetProcesses == null || newConfig.TargetProcesses.Count == 0)
                {
                    Log.WriteError(
                        "[Controller] New config has empty target_processes — keeping current config");
                    ExportRunningConfig();
                    return;
                }

                // shared_memory_name cannot change at runtime — warn and revert.
                if (!string.Equals(newConfig.SharedMemoryName, currentConfig.SharedMemoryName,
                        StringComparison.OrdinalIgnoreCase))
                {
                    Log.WriteError(
                        $"[Controller] shared_memory_name cannot change at runtime " +
                        $"(was: {currentConfig.SharedMemoryName}, new: {newConfig.SharedMemoryName}) " +
                        "— keeping old value, applying remaining changes");
                    newConfig.SharedMemoryName = currentConfig.SharedMemoryName;
                }

                if (!newConfig.TargetProcesses.SequenceEqual(
                        currentConfig.TargetProcesses, StringComparer.OrdinalIgnoreCase))
                {
                    processMonitor.UpdateTargets(newConfig.TargetProcesses);
                }

                if (newConfig.FailClosed != currentConfig.FailClosed)
                {
                    shmWriter.UpdateFailClosed(newConfig.FailClosed);
                    Log.Write($"[Controller] failure_mode updated: {newConfig.FailureMode}");
                }

                if (!string.Equals(newConfig.PayloadDllPath, currentConfig.PayloadDllPath,
                        StringComparison.OrdinalIgnoreCase))
                {
                    var resolvedNew = Path.IsPathRooted(newConfig.PayloadDllPath)
                        ? newConfig.PayloadDllPath
                        : Path.Combine(AppContext.BaseDirectory, newConfig.PayloadDllPath);
                    injector.UpdateDllPath(resolvedNew);
                }

                currentConfig = newConfig;
                Log.Write("[Controller] Config reloaded successfully.");
                ExportRunningConfig();
            }
        }

        // ctl-pipe subscriber. Each config_snapshot / config_update contains the
        // top-level data_pipe / ctl_pipe plus the peripheral_storage subtree.
        // We pluck peripheral_storage and deserialize into AppConfig.
        var jsonOpts = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        };
        var subscriber = new CtlPipeSubscriber(ctlPipeName, "controller", configPayload =>
        {
            if (!configPayload.TryGetProperty("peripheral_storage", out var peripheral)
                || !peripheral.TryGetProperty("controller", out var controller))
            {
                Log.WriteError("[Controller] ctl push missing peripheral_storage.controller section — ignoring");
                return;
            }
            AppConfig? newConfig;
            try
            {
                newConfig = controller.Deserialize<AppConfig>(jsonOpts);
            }
            catch (Exception ex)
            {
                Log.WriteError($"[Controller] ctl payload deserialize failed: {ex.Message}");
                return;
            }
            if (newConfig is null) return;
            TryReload(newConfig);
        })
        {
            OnLog = msg => Log.Write($"[Controller] {msg}"),
        };
        var subscriberTask = Task.Run(() => subscriber.StartAsync(cts.Token), cts.Token);

        ExportRunningConfig();   // write initial running-config.yaml
        // ── End hot-reload ────────────────────────────────────────────────────

        Log.Write("[Controller] Running. Press Ctrl+C to stop.");

        try { await Task.Delay(Timeout.Infinite, cts.Token); }
        catch (OperationCanceledException) { }

        // Release the mutex NOW so injected DLLs deactivate their hooks immediately.
        // Without this, the using-block disposal order runs processMonitor.Dispose()
        // first; ManagementEventWatcher.Stop() can block 15–30 s, keeping the mutex
        // held and the hook active the entire time. AliveMutex.Dispose() is idempotent,
        // so the using-block call below is a no-op.
        Log.Write("[Controller] Releasing mutex — hooks deactivating...");
        aliveMutex.Dispose();

        // Best-effort drain of the subscriber task.
        try { await subscriberTask.WaitAsync(TimeSpan.FromSeconds(2)); }
        catch (TimeoutException) { }
        catch (OperationCanceledException) { }

        Log.Write("[Controller] Shutting down...");
    }
}
