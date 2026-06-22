using ClipboardInterceptor;
using Xunit;

namespace AgentCore.Tests;

/// <summary>
/// Guards the clipboard self-write loop fix: the service must recognise EVERY
/// text it writes to the clipboard (the analyzing placeholder AND the dynamic
/// block notice) as its own, or it would re-ingest and re-analyze its own block
/// text forever, disabling the clipboard.
/// </summary>
public class ClipboardSelfWriteTests
{
    [Fact]
    public void IsDlpAuthored_Placeholder_IsExcluded()
    {
        Assert.True(ClipboardInterceptorService.IsDlpAuthored("[DLP: Analyzing...]"));
    }

    [Fact]
    public void IsDlpAuthored_DynamicBlockText_IsExcluded()
    {
        // The exact text the service writes on a block must be recognised as its
        // own — this is the case an exact-constant match would have missed.
        string blockText = ClipboardInterceptorService.BuildBlockText("Phát hiện số thẻ tín dụng (Visa)");
        Assert.True(ClipboardInterceptorService.IsDlpAuthored(blockText));

        Assert.True(ClipboardInterceptorService.IsDlpAuthored(
            ClipboardInterceptorService.BuildBlockText(null)));
    }

    [Fact]
    public void IsDlpAuthored_NormalUserText_IsNotExcluded()
    {
        Assert.False(ClipboardInterceptorService.IsDlpAuthored("credit card 4111 1111 1111 1111"));
        Assert.False(ClipboardInterceptorService.IsDlpAuthored("hello world"));
    }

    [Fact]
    public void BuildBlockText_CarriesReason_AndIsUtf8()
    {
        string text = ClipboardInterceptorService.BuildBlockText("Phát hiện số CCCD/CMND");
        Assert.Contains("Phát hiện số CCCD/CMND", text);
        // 'Đ' (U+0110) appears in the fallback below; assert a code point so a
        // mis-decoded (non-UTF-8) source would fail the build's test run.
        Assert.Contains('Đ', ClipboardInterceptorService.BuildBlockText(null));   // "Đã chặn"
    }

    [Fact]
    public void BuildBlockText_NullOrBlankReason_UsesFallback()
    {
        string fallback = ClipboardInterceptorService.BuildBlockText(null);
        Assert.Equal(fallback, ClipboardInterceptorService.BuildBlockText("   "));
        Assert.DoesNotContain(":", fallback[5..]);   // no "reason" suffix after the marker
    }
}
