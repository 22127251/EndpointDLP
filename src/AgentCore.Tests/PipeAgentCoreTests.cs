using System.Diagnostics;
using System.IO.Pipes;
using AgentCore;
using Xunit;

namespace AgentCore.Tests;

public class PipeAgentCoreTests
{
    /// <summary>
    /// Spawns a NamedPipeServerStream that accepts the client, drains the
    /// request, then never writes a response. Returns immediately; the server
    /// task continues until the test disposes the CancellationTokenSource.
    /// </summary>
    private static async Task<(Task serverTask, CancellationTokenSource cts)>
        StartHangingServerAsync(string pipeName)
    {
        var serverReady = new TaskCompletionSource();
        var cts = new CancellationTokenSource();

        var serverTask = Task.Run(async () =>
        {
            using var server = new NamedPipeServerStream(
                pipeName,
                PipeDirection.InOut,
                1,
                PipeTransmissionMode.Message,
                PipeOptions.Asynchronous);

            serverReady.SetResult();

            try
            {
                await server.WaitForConnectionAsync(cts.Token);
                byte[] buf = new byte[4096];
                await server.ReadAsync(buf, cts.Token);
                // Never write. Hold the pipe until the test cancels us.
                await Task.Delay(Timeout.Infinite, cts.Token);
            }
            catch (OperationCanceledException)
            {
                // expected on teardown
            }
        });

        await serverReady.Task;
        return (serverTask, cts);
    }

    [Fact]
    public async Task AnalyseAsync_HangingServer_BlocksWithinDeadline()
    {
        var pipeName = $"dlp_test_hang_{Guid.NewGuid():N}";
        var (serverTask, serverCts) = await StartHangingServerAsync(pipeName);

        try
        {
            var client = new PipeAgentCore(pipeName, timeoutMs: 1500);

            var sw = Stopwatch.StartNew();
            var decision = await client.AnalyseAsync("anything", CancellationToken.None);
            sw.Stop();

            Assert.Equal(AnalysisDecision.Block, decision);
            // Internal deadline is 1500 ms; allow generous margin for scheduling.
            Assert.True(sw.ElapsedMilliseconds < 3000,
                $"AnalyseAsync did not honor the internal deadline (took {sw.ElapsedMilliseconds} ms)");
        }
        finally
        {
            serverCts.Cancel();
            await serverTask;
        }
    }

    [Fact]
    public async Task AnalyseAsync_UserCancellation_PropagatesOpCanceled()
    {
        var pipeName = $"dlp_test_cancel_{Guid.NewGuid():N}";
        var (serverTask, serverCts) = await StartHangingServerAsync(pipeName);

        try
        {
            var client = new PipeAgentCore(pipeName, timeoutMs: 10_000);

            using var userCts = new CancellationTokenSource();
            userCts.CancelAfter(200);

            await Assert.ThrowsAnyAsync<OperationCanceledException>(
                () => client.AnalyseAsync("anything", userCts.Token));
        }
        finally
        {
            serverCts.Cancel();
            await serverTask;
        }
    }
}
