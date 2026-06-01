namespace DlpShared;

/// <summary>
/// Pipe-name normalization for .NET callers.
///
/// The central <c>config.yaml</c> stores Windows pipe names in the canonical
/// form <c>\\.\pipe\&lt;name&gt;</c>. pywin32's <c>CreateFile</c> accepts that
/// form directly, but .NET's <see cref="System.IO.Pipes.NamedPipeClientStream"/>
/// expects the bare <c>&lt;name&gt;</c> (it prepends <c>\\&lt;server&gt;\pipe\</c>
/// internally). Passing the full path through results in a malformed path like
/// <c>\\.\pipe\\\.\pipe\&lt;name&gt;</c> and silent connect timeouts.
///
/// Every C# call site that constructs a <see cref="System.IO.Pipes.NamedPipeClientStream"/>
/// from a yaml-sourced pipe name must run the value through
/// <see cref="ToBareName"/> first.
/// </summary>
public static class PipeNameHelper
{
    private const string FullPrefix = @"\\.\pipe\";

    /// <summary>
    /// Strips the canonical Windows-pipe prefix (<c>\\.\pipe\</c>) if present
    /// and returns the bare pipe name. Idempotent: a name that is already bare
    /// passes through unchanged. Null or empty input is returned as-is.
    /// </summary>
    public static string ToBareName(string name)
    {
        if (string.IsNullOrEmpty(name)) return name;
        return name.StartsWith(FullPrefix, StringComparison.OrdinalIgnoreCase)
            ? name.Substring(FullPrefix.Length)
            : name;
    }
}
