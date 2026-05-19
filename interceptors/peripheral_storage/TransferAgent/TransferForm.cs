using System.Drawing;
using System.Security.Cryptography;
using System.Windows.Forms;

namespace TransferAgent;

internal sealed class TransferForm : Form
{
    // ── layout constants ─────────────────────────────────────────────────────
    private const int Pad  = 12;
    private const int BtnH = 32;
    private const int BtnW = 130;

    private enum TransferStatus { Copied, Skipped, Blocked, CopyFailed }

    // ── state ────────────────────────────────────────────────────────────────
    private readonly string[]              _sources;
    private readonly string                _destFolder;
    private          CancellationTokenSource _cts = new();

    // ── controls ─────────────────────────────────────────────────────────────
    private readonly Label       _lblStatus;
    private readonly ProgressBar _progressBar;
    private readonly ListView    _listView;
    private readonly Button      _btnCancel;

    internal TransferForm(string[] sources, string destFolder)
    {
        _sources    = sources;
        _destFolder = destFolder;

        Text            = "DLP File Transfer";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox     = false;
        MinimizeBox     = false;
        StartPosition   = FormStartPosition.CenterScreen;
        ClientSize      = new Size(520, 260);
        Font            = new Font("Segoe UI", 9f);

        // ── status label ──────────────────────────────────────────────────
        _lblStatus = new Label
        {
            AutoSize  = false,
            TextAlign = ContentAlignment.MiddleLeft,
            Bounds    = new Rectangle(Pad, Pad, ClientSize.Width - Pad * 2, 24),
        };

        // ── indeterminate progress bar ────────────────────────────────────
        _progressBar = new ProgressBar
        {
            Style  = ProgressBarStyle.Marquee,
            Bounds = new Rectangle(Pad, Pad + 28, ClientSize.Width - Pad * 2, 20),
        };

        // ── results list view ─────────────────────────────────────────────
        _listView = new ListView
        {
            View          = View.Details,
            FullRowSelect = true,
            GridLines     = true,
            Bounds        = new Rectangle(Pad, Pad + 28, ClientSize.Width - Pad * 2,
                                          ClientSize.Height - Pad * 3 - BtnH - 28),
            Visible       = false,
        };
        _listView.Columns.Add("File",   260);
        _listView.Columns.Add("Status",  80);
        _listView.Columns.Add("Size",    80);
        _listView.Columns.Add("Note",   -2);

        // ── close / cancel button ─────────────────────────────────────────
        int btnY = ClientSize.Height - Pad - BtnH;
        _btnCancel = new Button
        {
            Text   = "Cancel",
            Bounds = new Rectangle(ClientSize.Width - Pad - BtnW, btnY, BtnW, BtnH),
        };
        _btnCancel.Click += OnCloseClick;

        Controls.AddRange(new Control[] { _lblStatus, _progressBar, _listView, _btnCancel });
    }

    // ── lifecycle ─────────────────────────────────────────────────────────────

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        await RunAtomicScanTransferAsync();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        _cts.Cancel();
        _cts.Dispose();
        base.OnFormClosed(e);
    }

    // ── atomic scan + transfer ────────────────────────────────────────────────

    private async Task RunAtomicScanTransferAsync()
    {
        _lblStatus.Text      = $"Scanning and transferring {_sources.Length} file(s)…";
        _progressBar.Style   = ProgressBarStyle.Marquee;
        _progressBar.Visible = true;
        _listView.Visible    = false;

        var tasks = _sources.Select(async src =>
        {
            var fi  = new System.IO.FileInfo(src);
            var req = new TransferRequest(
                src,
                System.IO.Path.Combine(_destFolder, fi.Name),
                fi.Name,
                fi.Exists ? fi.Length : 0L);

            // Snapshot: a stable, exclusively-locked copy taken at scan time.
            // FileShare.None prevents any other process from opening it for reading or writing.
            // Lifetime: from creation through USB copy completion, then deleted in finally.
            string snapshotPath = System.IO.Path.Combine(
                System.IO.Path.GetTempPath(),
                $"dlpsnap_{Guid.NewGuid():N}.tmp");

            try
            {
                using var snapshotStream = new System.IO.FileStream(
                    snapshotPath,
                    System.IO.FileMode.CreateNew,
                    System.IO.FileAccess.ReadWrite,
                    System.IO.FileShare.None,
                    bufferSize: 81920,
                    useAsync: true);

                // Copy source → snapshot. File.OpenRead uses FileShare.Read,
                // blocking writes to the source during this copy.
                using (var srcStream = System.IO.File.OpenRead(src))
                    await srcStream.CopyToAsync(snapshotStream, _cts.Token);
                snapshotStream.Position = 0;

                // SHA-256 of the locked snapshot for audit trail.
                byte[] hashBytes = await SHA256.HashDataAsync(snapshotStream, _cts.Token);
                string fileHash  = Convert.ToHexString(hashBytes).ToLowerInvariant();
                snapshotStream.Position = 0;

                // Orchestrator analyzes a temp copy derived from the snapshot stream.
                var result = await OrchestratorClient.AnalyzeAsync(
                    req, snapshotStream, fileHash, _cts.Token);

                if (result.Allowed)
                {
                    try
                    {
                        string dest = System.IO.Path.Combine(_destFolder, fi.Name);
                        snapshotStream.Position = 0;
                        using var destStream = new System.IO.FileStream(
                            dest,
                            System.IO.FileMode.CreateNew,
                            System.IO.FileAccess.Write,
                            System.IO.FileShare.None,
                            bufferSize: 81920,
                            useAsync: true);
                        await snapshotStream.CopyToAsync(destStream, _cts.Token);
                        NativeMethods.NotifyFileCreated(dest);
                        return (result, TransferStatus.Copied);
                    }
                    catch (System.IO.IOException)
                    {
                        return (result with { ErrorMessage = "Destination exists — skipped." },
                                TransferStatus.Skipped);
                    }
                    catch (Exception ex)
                    {
                        return (result with { ErrorMessage = $"Copy failed: {ex.Message}" },
                                TransferStatus.CopyFailed);
                    }
                }

                return (result, TransferStatus.Blocked);
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception ex)
            {
                return (new TransferResult(src, false, $"Snapshot error: {ex.Message}"),
                        TransferStatus.Blocked);
            }
            finally
            {
                // snapshotStream 'using' above has already closed the handle by now.
                try { System.IO.File.Delete(snapshotPath); } catch { }
            }
        });

        (TransferResult result, TransferStatus status)[] outcomes;
        try
        {
            outcomes = await Task.WhenAll(tasks);
        }
        catch (OperationCanceledException)
        {
            Close();
            return;
        }

        ShowDoneStage(outcomes);
    }

    // ── done stage ────────────────────────────────────────────────────────────

    private void ShowDoneStage((TransferResult result, TransferStatus status)[] outcomes)
    {
        _progressBar.Visible = false;
        _listView.Visible    = true;
        _btnCancel.Text      = "Close";

        _listView.Items.Clear();
        int copied = 0, skipped = 0, blocked = 0;

        foreach (var (result, status) in outcomes)
        {
            var    fi         = new System.IO.FileInfo(result.FilePath);
            string statusText = status switch
            {
                TransferStatus.Copied     => "TRANSFERRED",
                TransferStatus.Skipped    => "SKIPPED",
                TransferStatus.CopyFailed => "FAILED",
                _                         => "BLOCKED",
            };
            Color color = status is TransferStatus.Copied  ? Color.DarkGreen
                        : status is TransferStatus.Skipped ? Color.DarkOrange
                        : Color.DarkRed;

            string note = result.ErrorMessage
                ?? (result.FileHash is not null ? $"sha256:{result.FileHash[..16]}…" : "");

            var item = new ListViewItem(fi.Name);
            item.SubItems.Add(statusText);
            item.SubItems.Add(fi.Exists ? FormatSize(fi.Length) : "?");
            item.SubItems.Add(note);
            item.ForeColor = color;
            _listView.Items.Add(item);

            switch (status)
            {
                case TransferStatus.Copied:  copied++;  break;
                case TransferStatus.Skipped: skipped++; break;
                default:                     blocked++; break;
            }
        }

        var parts = new List<string>();
        if (copied  > 0) parts.Add($"{copied} transferred");
        if (skipped > 0) parts.Add($"{skipped} skipped");
        if (blocked > 0) parts.Add($"{blocked} blocked");
        _lblStatus.Text = "Done: " +
            (parts.Count > 0 ? string.Join(", ", parts) : "nothing to do") + ".";
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private void OnCloseClick(object? sender, EventArgs e)
    {
        _cts.Cancel();
        Close();
    }

    private static string FormatSize(long bytes) => bytes switch
    {
        >= 1_073_741_824 => $"{bytes / 1_073_741_824.0:F1} GB",
        >= 1_048_576     => $"{bytes / 1_048_576.0:F1} MB",
        >= 1_024         => $"{bytes / 1_024.0:F1} KB",
        _                => $"{bytes} B",
    };
}
