using System.IO.Pipes;
using System.Security.Cryptography;
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
    string  FilePath,
    bool    Allowed,
    string? ErrorMessage = null,
    string? FileHash     = null);   // SHA-256 hex of snapshot (audit trail)

internal static class OrchestratorClient
{
    private const string PipeName         = "dlp_agent";
    private const int    ConnectTimeoutMs = 5_000;
    private const int    AnalysisTimeoutS = 10;

    private static readonly JsonSerializerOptions s_jsonOpts = new()
    {
        PropertyNamingPolicy   = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    // snapshotStream: the caller-owned, exclusively-locked snapshot stream.
    // This method creates an orchestrator temp from it (orchestrator deletes the temp after analysis).
    internal static async Task<TransferResult> AnalyzeAsync(
        TransferRequest   req,
        Stream            snapshotStream,
        string            fileHash,
        CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromSeconds(AnalysisTimeoutS));

        string ext      = Path.GetExtension(req.FileName);
        string tempPath = Path.Combine(
            Path.GetTempPath(),
            $"dlp_{Guid.NewGuid():N}{ext}");

        try
        {
            snapshotStream.Position = 0;
            using (var tempStream = new FileStream(
                tempPath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920, true))
                await snapshotStream.CopyToAsync(tempStream, cts.Token);
            snapshotStream.Position = 0;

            using var pipe = new NamedPipeClientStream(
                ".", PipeName, PipeDirection.InOut, PipeOptions.Asynchronous);

            await pipe.ConnectAsync(ConnectTimeoutMs, cts.Token);
            pipe.ReadMode = PipeTransmissionMode.Message;

            var payload = new
            {
                channel   = "peripheral_storage",
                kind      = "file",
                file_path = tempPath,   // orchestrator analyzes and deletes this copy
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

            byte[] buf  = new byte[256];
            int    read = await pipe.ReadAsync(buf, cts.Token);
            string response = Encoding.UTF8.GetString(buf, 0, read).Trim();

            bool allowed = string.Equals(response, "ALLOW", StringComparison.OrdinalIgnoreCase);
            return new TransferResult(req.FilePath, allowed, null, fileHash);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            TryDeleteTemp(tempPath);
            return new TransferResult(req.FilePath, false,
                "Analysis timed out — transfer blocked.", fileHash);
        }
        catch (OperationCanceledException)
        {
            TryDeleteTemp(tempPath);
            throw;   // user-initiated cancel — let TransferForm handle it
        }
        catch (Exception ex)
        {
            TryDeleteTemp(tempPath);
            return new TransferResult(req.FilePath, false,
                $"Orchestrator error: {ex.Message}", fileHash);
        }
    }

    private static void TryDeleteTemp(string path)
    {
        try { File.Delete(path); } catch { }
    }
}
