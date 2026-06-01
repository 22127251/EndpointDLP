using System.Text;
using YamlDotNet.Serialization;
using YamlDotNet.Serialization.NamingConventions;

namespace DlpShared;

/// <summary>
/// Path discovery + YAML section loading for the central config.yaml.
///
/// Discovery order (decision #10 + #12 in the Phase B plan):
///   1. Env var DLP_CONFIG_PATH (validated by the sentinel check).
///   2. Walk-up from the discovery anchor (AppContext.BaseDirectory by default),
///      up to N=8 levels, looking for a config.yaml with a top-level "data_pipe"
///      key (the sentinel that prevents picking up unrelated config.yaml files).
///   3. Throw with a clear diagnostic naming the env var and every path tried.
/// </summary>
public static class ConfigLocator
{
    private const string EnvVarName = "DLP_CONFIG_PATH";
    private const string FileName = "config.yaml";
    private const int MaxWalkUpLevels = 8;

    /// <param name="anchorOverride">
    /// Test seam: when set, overrides AppContext.BaseDirectory as the walk-up
    /// anchor. Production callers (Controller, ClipboardInterceptor,
    /// TransferAgent) leave this null.
    /// </param>
    public static string FindConfigYaml(string? anchorOverride = null)
    {
        var tried = new List<(string Path, string Reason)>();

        var envPath = Environment.GetEnvironmentVariable(EnvVarName);
        if (!string.IsNullOrWhiteSpace(envPath))
        {
            if (!File.Exists(envPath))
            {
                tried.Add((envPath, "file does not exist"));
            }
            else if (HasDataPipeSentinel(envPath))
            {
                return envPath;
            }
            else
            {
                tried.Add((envPath, "missing data_pipe sentinel"));
            }
        }

        var anchor = anchorOverride ?? AppContext.BaseDirectory;
        DirectoryInfo? dir = new DirectoryInfo(anchor.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        for (int i = 0; i < MaxWalkUpLevels && dir is not null; i++, dir = dir.Parent)
        {
            var candidate = Path.Combine(dir.FullName, FileName);
            if (!File.Exists(candidate))
            {
                tried.Add((candidate, "not found"));
                continue;
            }
            if (HasDataPipeSentinel(candidate))
            {
                return candidate;
            }
            tried.Add((candidate, "missing data_pipe sentinel"));
        }

        var diag = new StringBuilder();
        diag.AppendLine($"Could not locate {FileName}.");
        diag.AppendLine($"Set {EnvVarName} or place {FileName} in the repo root (must contain a top-level 'data_pipe' key).");
        diag.AppendLine("Paths tried:");
        foreach (var (path, reason) in tried)
        {
            diag.AppendLine($"  - {path}: {reason}");
        }
        throw new FileNotFoundException(diag.ToString());
    }

    /// <summary>
    /// Returns the in-use pipe names from the central config. Always reads
    /// fresh from disk; callers should cache the result at startup.
    /// </summary>
    public static (string DataPipe, string CtlPipe) LoadTopLevel(string yamlPath)
    {
        var root = ParseRoot(yamlPath);
        return (
            GetString(root, "data_pipe"),
            GetString(root, "ctl_pipe")
        );
    }

    /// <summary>
    /// Pluck a per-component section out of the central config and deserialize
    /// it into T using snake_case → PascalCase naming. Returns a default-
    /// initialized T if the section is missing. Extra keys (e.g.,
    /// peripheral_storage.transfer_agent when T is the Controller's AppConfig)
    /// are tolerated.
    /// </summary>
    public static T LoadSection<T>(string yamlPath, string sectionKey) where T : new()
    {
        var root = ParseRoot(yamlPath);
        if (!root.TryGetValue(sectionKey, out var section) || section is null)
        {
            return new T();
        }
        // Re-serialize the section subtree and deserialize as T. Doing it via
        // a YAML round-trip lets us reuse YamlDotNet's nested mapping logic
        // without having to write a custom converter from object graphs to T.
        var serializer = new SerializerBuilder().Build();
        var sectionYaml = serializer.Serialize(section);
        var typedDeserializer = new DeserializerBuilder()
            .WithNamingConvention(UnderscoredNamingConvention.Instance)
            .IgnoreUnmatchedProperties()
            .Build();
        return typedDeserializer.Deserialize<T>(sectionYaml) ?? new T();
    }

    // -- internals ---------------------------------------------------------

    private static bool HasDataPipeSentinel(string yamlPath)
    {
        try
        {
            var root = ParseRoot(yamlPath);
            if (!root.TryGetValue("data_pipe", out var v) || v is null)
            {
                return false;
            }
            var s = v.ToString();
            return !string.IsNullOrWhiteSpace(s);
        }
        catch
        {
            // Malformed YAML or IO error → treat as sentinel failure; the caller
            // logs the path with reason and either falls through to walk-up
            // (env-var case) or skips the candidate (walk-up case).
            return false;
        }
    }

    private static Dictionary<object, object?> ParseRoot(string yamlPath)
    {
        var raw = File.ReadAllText(yamlPath);
        var deserializer = new DeserializerBuilder().Build();
        var root = deserializer.Deserialize<Dictionary<object, object?>>(raw);
        if (root is null)
        {
            throw new InvalidDataException($"YAML root is not a mapping: {yamlPath}");
        }
        return root;
    }

    private static string GetString(Dictionary<object, object?> root, string key)
    {
        return root.TryGetValue(key, out var v) && v is not null ? (v.ToString() ?? "") : "";
    }
}
