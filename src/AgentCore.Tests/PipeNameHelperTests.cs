using DlpShared;
using Xunit;

namespace AgentCore.Tests;

/// <summary>
/// Post-implementation fix #1: regression coverage for the pipe-name prefix
/// strip that ClipboardInterceptor, Controller, and TransferAgent all depend
/// on when constructing NamedPipeClientStream from a yaml-sourced pipe name.
/// </summary>
public sealed class PipeNameHelperTests
{
    [Fact]
    public void ToBareName_WithFullPrefix_StripsIt()
    {
        Assert.Equal("dlp_agent", PipeNameHelper.ToBareName(@"\\.\pipe\dlp_agent"));
        Assert.Equal("dlp_agent_ctl", PipeNameHelper.ToBareName(@"\\.\pipe\dlp_agent_ctl"));
    }

    [Fact]
    public void ToBareName_WithBareName_PassesThrough()
    {
        // Idempotency: a name already in bare form is returned unchanged.
        Assert.Equal("dlp_agent", PipeNameHelper.ToBareName("dlp_agent"));
        Assert.Equal("dlp_agent", PipeNameHelper.ToBareName(PipeNameHelper.ToBareName(@"\\.\pipe\dlp_agent")));
    }

    [Fact]
    public void ToBareName_EmptyOrNull_ReturnsAsIs()
    {
        Assert.Equal("", PipeNameHelper.ToBareName(""));
        Assert.Null(PipeNameHelper.ToBareName(null!));
    }
}
