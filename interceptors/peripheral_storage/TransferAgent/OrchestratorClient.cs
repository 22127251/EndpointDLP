using System.IO.Pipes;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace TransferAgent;

internal sealed record TransferRequest(
    string FilePath,
    string Destination,
    string FileName,
    long   SizeBytes);

internal sealed record TransferResult(
    string FilePath,
    bool   Allowed,
    string? ErrorMessage = null);

internal static class OrchestratorClient
{
    private const string PipeName        = "dlp_agent";
    private const int    ConnectTimeoutMs = 5_000;
    private const int    AnalysisTimeoutS = 10;

    private static readonly JsonSerializerOptions s_jsonOpts = new()
    {
        PropertyNamingPolicy        = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition      = JsonIgnoreCondition.WhenWritingNull,
    };

    internal static async Task<TransferResult> AnalyzeAsync(
        TransferRequest req, CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromSeconds(AnalysisTimeoutS));

        // Copy the source file to a temp path.  The orchestrator always deletes
        // the file it receives (designed for temp copies); sending the original
        // path would delete the user's file before it can be transferred.
        string shortId  = Guid.NewGuid().ToString("N")[..8];
        string tempPath = Path.Combine(
            Path.GetTempPath(),
            $"dlp_{shortId}_{Path.GetFileName(req.FilePath)}");
        File.Copy(req.FilePath, tempPath, overwrite: false);

        try
        {
            using var pipe = new NamedPipeClientStream(
                ".", PipeName, PipeDirection.InOut, PipeOptions.Asynchronous);

            await pipe.ConnectAsync(ConnectTimeoutMs, cts.Token);
            pipe.ReadMode = PipeTransmissionMode.Message;

            var payload = new
            {
                channel   = "peripheral_storage",
                kind      = "file",
                file_path = tempPath,   // orchestrator analyzes and deletes the copy
                metadata  = new
                {
                    filename        = req.FileName,
                    size_bytes      = req.SizeBytes,
                    destination     = req.Destination,
                    timestamp       = DateTime.UtcNow.ToString("o"),
                },
            };

            byte[] requestBytes = JsonSerializer.SerializeToUtf8Bytes(payload, s_jsonOpts);
            await pipe.WriteAsync(requestBytes, cts.Token);

            byte[] buf = new byte[256];
            int    read = await pipe.ReadAsync(buf, cts.Token);
            string response = Encoding.UTF8.GetString(buf, 0, read).Trim();

            bool allowed = string.Equals(response, "ALLOW", StringComparison.OrdinalIgnoreCase);
            return new TransferResult(req.FilePath, allowed);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            TryDeleteTemp(tempPath);
            return new TransferResult(req.FilePath, false, "Analysis timed out — transfer blocked.");
        }
        catch (Exception ex)
        {
            TryDeleteTemp(tempPath);
            return new TransferResult(req.FilePath, false, $"Orchestrator error: {ex.Message}");
        }
    }

    private static void TryDeleteTemp(string path)
    {
        try { File.Delete(path); } catch { }
    }
}
