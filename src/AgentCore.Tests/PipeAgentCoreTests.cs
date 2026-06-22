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
            var outcome = await client.AnalyseAsync("anything", CancellationToken.None);
            sw.Stop();

            Assert.Equal(AnalysisDecision.Block, outcome.Decision);
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

    /// <summary>Server that accepts, drains the request, writes a single message
    /// <paramref name="response"/>, then holds until the test cancels it.</summary>
    private static async Task<(Task serverTask, CancellationTokenSource cts)>
        StartReplyingServerAsync(string pipeName, string response)
    {
        var serverReady = new TaskCompletionSource();
        var cts = new CancellationTokenSource();

        var serverTask = Task.Run(async () =>
        {
            using var server = new NamedPipeServerStream(
                pipeName, PipeDirection.InOut, 1,
                PipeTransmissionMode.Message, PipeOptions.Asynchronous);

            serverReady.SetResult();

            try
            {
                await server.WaitForConnectionAsync(cts.Token);
                byte[] buf = new byte[4096];
                await server.ReadAsync(buf, cts.Token);
                byte[] resp = System.Text.Encoding.UTF8.GetBytes(response);
                await server.WriteAsync(resp, cts.Token);
                await server.FlushAsync(cts.Token);
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
    public async Task AnalyseAsync_BlockWithReason_ParsesReason()
    {
        var pipeName = $"dlp_test_reason_{Guid.NewGuid():N}";
        // Vietnamese in the response round-trips as UTF-8 over the pipe AND proves
        // this .cs source is read as UTF-8 (the assert compares to a literal).
        var (serverTask, serverCts) = await StartReplyingServerAsync(pipeName, "BLOCK|Phát hiện số thẻ");

        try
        {
            var client = new PipeAgentCore(pipeName, timeoutMs: 5000);
            var outcome = await client.AnalyseAsync("anything", CancellationToken.None);

            Assert.Equal(AnalysisDecision.Block, outcome.Decision);
            Assert.Equal("Phát hiện số thẻ", outcome.Reason);
        }
        finally
        {
            serverCts.Cancel();
            await serverTask;
        }
    }

    [Fact]
    public async Task AnalyseAsync_AllowResponse_HasNoReason()
    {
        var pipeName = $"dlp_test_allow_{Guid.NewGuid():N}";
        var (serverTask, serverCts) = await StartReplyingServerAsync(pipeName, "ALLOW");

        try
        {
            var client = new PipeAgentCore(pipeName, timeoutMs: 5000);
            var outcome = await client.AnalyseAsync("anything", CancellationToken.None);

            Assert.Equal(AnalysisDecision.Allow, outcome.Decision);
            Assert.Null(outcome.Reason);
        }
        finally
        {
            serverCts.Cancel();
            await serverTask;
        }
    }
}
