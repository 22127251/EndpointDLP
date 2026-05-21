using System.Text;
using Controller.Config;
using YamlDotNet.Serialization;
using YamlDotNet.Serialization.NamingConventions;

namespace Controller;

internal static class Program
{
    static async Task Main(string[] args)
    {
        var configPath = args.Length > 0 ? args[0]
                                         : Path.Combine(AppContext.BaseDirectory, "config.yaml");

        AppConfig config;
        try
        {
            var yaml = File.ReadAllText(configPath);
            config = new DeserializerBuilder()
                .WithNamingConvention(UnderscoredNamingConvention.Instance)
                .Build()
                .Deserialize<AppConfig>(yaml);
        }
        catch (Exception ex)
        {
            Log.WriteError($"[Controller] Failed to load config: {ex.Message}");
            return;
        }

        Log.Write($"[Controller] Loaded config: {configPath}");
        Log.Write($"[Controller] Targets: {string.Join(", ", config.TargetProcesses)}");
        Log.Write($"[Controller] Fail mode: {config.FailMode}");

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

        // ── Hot-reload ────────────────────────────────────────────────────────
        // Tracks what is actually running (may differ from config.yaml after a
        // failed reload or a shared_memory_name conflict).
        AppConfig currentConfig = config;

        void ExportRunningConfig()
        {
            try
            {
                var outPath = Path.Combine(AppContext.BaseDirectory, "running-config.yaml");
                var sb = new StringBuilder();
                sb.AppendLine("# AUTO-GENERATED — Do not edit directly.");
                sb.AppendLine("# Reflects the configuration currently active in the running Controller.");
                sb.AppendLine("# The config.yaml file may differ if a reload was attempted but failed");
                sb.AppendLine("# (parse error, invalid values, or an attempt to change shared_memory_name).");
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

        Timer? debounceTimer = null;

        void TryReload()
        {
            Log.Write("[Controller] Config file changed — attempting reload...");
            AppConfig newConfig;
            try
            {
                var yaml = File.ReadAllText(configPath);
                newConfig = new DeserializerBuilder()
                    .WithNamingConvention(UnderscoredNamingConvention.Instance)
                    .Build()
                    .Deserialize<AppConfig>(yaml);
            }
            catch (Exception ex)
            {
                Log.WriteError($"[Controller] Config parse failed: {ex.Message} — keeping current config");
                ExportRunningConfig();
                return;
            }

            if (newConfig.TargetProcesses == null || newConfig.TargetProcesses.Count == 0)
            {
                Log.WriteError("[Controller] New config has empty target_processes — keeping current config");
                ExportRunningConfig();
                return;
            }

            // shared_memory_name cannot change at runtime — warn and revert to old value.
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
                Log.Write($"[Controller] fail_mode updated: {newConfig.FailMode}");
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

        using var configWatcher = new FileSystemWatcher(
            Path.GetDirectoryName(configPath)!,
            Path.GetFileName(configPath))
        {
            NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size,
            EnableRaisingEvents = true
        };
        configWatcher.Changed += (_, _) =>
        {
            debounceTimer?.Dispose();
            debounceTimer = new Timer(_ => TryReload(), null, 300, Timeout.Infinite);
        };

        ExportRunningConfig();   // write initial running-config.yaml
        // ── End hot-reload ────────────────────────────────────────────────────

        Log.Write("[Controller] Running. Press Ctrl+C to stop.");

        try { await Task.Delay(Timeout.Infinite, cts.Token); }
        catch (OperationCanceledException) { }

        debounceTimer?.Dispose();

        // Release the mutex NOW so injected DLLs deactivate their hooks immediately.
        // Without this, the using-block disposal order runs processMonitor.Dispose()
        // first; ManagementEventWatcher.Stop() can block 15–30 s, keeping the mutex
        // held and the hook active the entire time. AliveMutex.Dispose() is idempotent,
        // so the using-block call below is a no-op.
        Log.Write("[Controller] Releasing mutex — hooks deactivating...");
        aliveMutex.Dispose();
        Log.Write("[Controller] Shutting down...");
    }
}
