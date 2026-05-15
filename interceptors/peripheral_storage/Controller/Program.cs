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
        Log.Write("[Controller] Shutting down...");
    }
}
