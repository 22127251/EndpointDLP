using DlpShared;
using Xunit;

namespace AgentCore.Tests;

/// <summary>
/// Phase B IT-B3 — five specified cases for ConfigLocator.FindConfigYaml.
/// Tests use the anchorOverride seam to point the walk-up at a temp tree
/// instead of AppContext.BaseDirectory.
/// </summary>
public sealed class ConfigLocatorTests : IDisposable
{
    private const string EnvVarName = "DLP_CONFIG_PATH";
    private const string Sentinel = @"data_pipe: ""\\\\.\\pipe\\dlp_agent""" + "\nctl_pipe: \"x\"\n";

    private readonly string _testRoot;
    private readonly string? _savedEnv;

    public ConfigLocatorTests()
    {
        _testRoot = Path.Combine(Path.GetTempPath(), $"dlp_locator_{Guid.NewGuid():N}");
        Directory.CreateDirectory(_testRoot);
        _savedEnv = Environment.GetEnvironmentVariable(EnvVarName);
        Environment.SetEnvironmentVariable(EnvVarName, null);
    }

    public void Dispose()
    {
        Environment.SetEnvironmentVariable(EnvVarName, _savedEnv);
        try { Directory.Delete(_testRoot, recursive: true); } catch { /* best-effort */ }
    }

    [Fact]
    public void EnvVarPointsAtValidFile_ReturnsThatPath()
    {
        var path = Path.Combine(_testRoot, "envcfg.yaml");
        File.WriteAllText(path, Sentinel);
        Environment.SetEnvironmentVariable(EnvVarName, path);

        // Anchor unused on this path (env var wins) — but pass it for hermeticity.
        var result = ConfigLocator.FindConfigYaml(anchorOverride: _testRoot);

        Assert.Equal(path, result);
    }

    [Fact]
    public void EnvVarPointsAtFileMissingSentinel_FallsThroughToWalkUp()
    {
        var bogus = Path.Combine(_testRoot, "bogus.yaml");
        File.WriteAllText(bogus, "something_else: 42\n");
        Environment.SetEnvironmentVariable(EnvVarName, bogus);

        // Real config 2 levels above the anchor.
        var anchor = Path.Combine(_testRoot, "a", "b");
        Directory.CreateDirectory(anchor);
        var realPath = Path.Combine(_testRoot, "config.yaml");
        File.WriteAllText(realPath, Sentinel);

        var result = ConfigLocator.FindConfigYaml(anchorOverride: anchor);

        Assert.Equal(realPath, result);
    }

    [Fact]
    public void WalkUpFindsValidConfigAtDepth4()
    {
        var anchor = Path.Combine(_testRoot, "a", "b", "c", "d");
        Directory.CreateDirectory(anchor);
        var configPath = Path.Combine(_testRoot, "config.yaml");
        File.WriteAllText(configPath, Sentinel);

        var result = ConfigLocator.FindConfigYaml(anchorOverride: anchor);

        Assert.Equal(configPath, result);
    }

    [Fact]
    public void WalkUpSkipsMisleadingConfigWithoutSentinel()
    {
        // Anchor 5 levels deep; misleading config (no data_pipe) at depth 1
        // above anchor; real config at depth 5.
        var anchor = Path.Combine(_testRoot, "a", "b", "c", "d", "e");
        Directory.CreateDirectory(anchor);

        var misleading = Path.Combine(_testRoot, "a", "b", "c", "d", "config.yaml");
        File.WriteAllText(misleading, "other_tool: \"unrelated\"\n");

        var realPath = Path.Combine(_testRoot, "config.yaml");
        File.WriteAllText(realPath, Sentinel);

        var result = ConfigLocator.FindConfigYaml(anchorOverride: anchor);

        Assert.Equal(realPath, result);
    }

    [Fact]
    public void NoEnvVarAndNoFileFound_ThrowsWithDiagnostics()
    {
        var anchor = Path.Combine(_testRoot, "deep", "empty", "tree");
        Directory.CreateDirectory(anchor);
        // _testRoot itself contains no config.yaml; the walk-up bounded at N=8
        // means the worst case is hitting %TEMP% / AppData, which won't have one.

        var ex = Assert.Throws<FileNotFoundException>(
            () => ConfigLocator.FindConfigYaml(anchorOverride: anchor));

        Assert.Contains(EnvVarName, ex.Message);
        Assert.Contains("config.yaml", ex.Message);
        // The diagnostic must enumerate at least the first walk-up candidate.
        Assert.Contains(anchor, ex.Message);
    }
}
