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

    private record TransferItem(
        string SourcePath,    // absolute source file path
        string DestPath,      // absolute destination file path (may be nested inside a folder)
        string DisplayName,   // relative name shown in ListView, e.g. "Folder\sub\file.txt"
        long   SizeBytes);

    // ── state ────────────────────────────────────────────────────────────────
    private readonly string[]               _sources;
    private readonly string                 _destFolder;
    private          List<TransferItem>     _items = new();
    private          CancellationTokenSource _cts  = new();

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
        FormBorderStyle = FormBorderStyle.Sizable;
        MaximizeBox     = true;
        MinimizeBox     = false;
        MinimumSize     = new Size(420, 260);
        StartPosition   = FormStartPosition.CenterScreen;
        ClientSize      = new Size(520, 260);
        Font            = new Font("Segoe UI", 9f);

        // ── status label ──────────────────────────────────────────────────
        _lblStatus = new Label
        {
            AutoSize  = false,
            TextAlign = ContentAlignment.MiddleLeft,
            Bounds    = new Rectangle(Pad, Pad, ClientSize.Width - Pad * 2, 24),
            Anchor    = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
        };

        // ── indeterminate progress bar ────────────────────────────────────
        _progressBar = new ProgressBar
        {
            Style  = ProgressBarStyle.Marquee,
            Bounds = new Rectangle(Pad, Pad + 28, ClientSize.Width - Pad * 2, 20),
            Anchor = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right,
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
            Anchor        = AnchorStyles.Top | AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom,
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
            Anchor = AnchorStyles.Bottom | AnchorStyles.Right,
        };
        _btnCancel.Click += OnCloseClick;

        Controls.AddRange(new Control[] { _lblStatus, _progressBar, _listView, _btnCancel });
    }

    // ── lifecycle ─────────────────────────────────────────────────────────────

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);

        _items = ExpandSources(_sources, _destFolder);

        if (_items.Count == 0)
        {
            MessageBox.Show("No files found to transfer.", "DLP File Transfer",
                            MessageBoxButtons.OK, MessageBoxIcon.Information);
            Close();
            return;
        }

        var conflicts = FindFolderConflicts(_sources, _destFolder);
        if (conflicts.Count > 0)
        {
            string names = string.Join("\n  ", conflicts.Select(c => $"\"{c}\""));
            var dr = MessageBox.Show(
                $"The following folder(s) already exist at the destination and will be merged:\n\n  {names}\n\nClick OK to override (merge), or Cancel to abort the transfer.",
                "Folder Already Exists",
                MessageBoxButtons.OKCancel,
                MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2);
            if (dr != DialogResult.OK)
            {
                Close();
                return;
            }
        }

        await RunAtomicScanTransferAsync();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        _cts.Cancel();
        _cts.Dispose();
        base.OnFormClosed(e);
    }

    // ── source expansion ──────────────────────────────────────────────────────

    private static List<TransferItem> ExpandSources(string[] sources, string destFolder)
    {
        var items = new List<TransferItem>();
        foreach (string src in sources)
        {
            if (System.IO.Directory.Exists(src))
            {
                string folderName = System.IO.Path.GetFileName(src.TrimEnd('\\', '/'));
                foreach (string file in System.IO.Directory.EnumerateFiles(
                             src, "*", System.IO.SearchOption.AllDirectories))
                {
                    string rel      = System.IO.Path.GetRelativePath(src, file);
                    string display  = System.IO.Path.Combine(folderName, rel);
                    string destPath = System.IO.Path.Combine(destFolder, folderName, rel);
                    long   size     = 0;
                    try { size = new System.IO.FileInfo(file).Length; } catch { }
                    items.Add(new TransferItem(file, destPath, display, size));
                }
            }
            else
            {
                string fileName = System.IO.Path.GetFileName(src);
                string destPath = System.IO.Path.Combine(destFolder, fileName);
                long   size     = 0;
                try { size = new System.IO.FileInfo(src).Length; } catch { }
                items.Add(new TransferItem(src, destPath, fileName, size));
            }
        }
        return items;
    }

    private static List<string> FindFolderConflicts(string[] sources, string destFolder)
    {
        var conflicts = new List<string>();
        foreach (string src in sources)
        {
            if (!System.IO.Directory.Exists(src)) continue;
            string folderName = System.IO.Path.GetFileName(src.TrimEnd('\\', '/'));
            if (System.IO.Directory.Exists(System.IO.Path.Combine(destFolder, folderName)))
                conflicts.Add(folderName);
        }
        return conflicts;
    }

    // ── atomic scan + transfer ────────────────────────────────────────────────

    private async Task RunAtomicScanTransferAsync()
    {
        _lblStatus.Text      = $"Scanning and transferring {_items.Count} file(s)…";
        _progressBar.Style   = ProgressBarStyle.Marquee;
        _progressBar.Visible = true;
        _listView.Visible    = false;

        var tasks = _items.Select(async item =>
        {
            var req = new TransferRequest(
                item.SourcePath,
                item.DestPath,
                System.IO.Path.GetFileName(item.SourcePath),
                item.SizeBytes);

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

                using (var srcStream = System.IO.File.OpenRead(item.SourcePath))
                    await srcStream.CopyToAsync(snapshotStream, _cts.Token);
                snapshotStream.Position = 0;

                byte[] hashBytes = await SHA256.HashDataAsync(snapshotStream, _cts.Token);
                string fileHash  = Convert.ToHexString(hashBytes).ToLowerInvariant();
                snapshotStream.Position = 0;

                var result = await OrchestratorClient.AnalyzeAsync(
                    req, snapshotStream, fileHash, _cts.Token);

                if (result.Allowed)
                {
                    try
                    {
                        // Create parent dirs before writing — handles nested folder structures.
                        string? parentDir = System.IO.Path.GetDirectoryName(item.DestPath);
                        if (parentDir is not null)
                            System.IO.Directory.CreateDirectory(parentDir);

                        snapshotStream.Position = 0;
                        using var destStream = new System.IO.FileStream(
                            item.DestPath,
                            System.IO.FileMode.CreateNew,
                            System.IO.FileAccess.Write,
                            System.IO.FileShare.None,
                            bufferSize: 81920,
                            useAsync: true);
                        await snapshotStream.CopyToAsync(destStream, _cts.Token);
                        NativeMethods.NotifyFileCreated(item.DestPath);
                        return (result, TransferStatus.Copied, item.DisplayName, item.SizeBytes);
                    }
                    catch (System.IO.IOException)
                    {
                        return (result with { ErrorMessage = "Destination exists — skipped." },
                                TransferStatus.Skipped, item.DisplayName, item.SizeBytes);
                    }
                    catch (Exception ex)
                    {
                        return (result with { ErrorMessage = $"Copy failed: {ex.Message}" },
                                TransferStatus.CopyFailed, item.DisplayName, item.SizeBytes);
                    }
                }

                return (result, TransferStatus.Blocked, item.DisplayName, item.SizeBytes);
            }
            catch (OperationCanceledException) { throw; }
            catch (System.IO.FileNotFoundException)
            {
                return (new TransferResult(item.SourcePath, false, "Source file removed before transfer."),
                        TransferStatus.Blocked, item.DisplayName, item.SizeBytes);
            }
            catch (Exception ex)
            {
                return (new TransferResult(item.SourcePath, false, $"Snapshot error: {ex.Message}"),
                        TransferStatus.Blocked, item.DisplayName, item.SizeBytes);
            }
            finally
            {
                try { System.IO.File.Delete(snapshotPath); } catch { }
            }
        });

        (TransferResult result, TransferStatus status, string displayName, long sizeBytes)[] outcomes;
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

    private void ShowDoneStage(
        (TransferResult result, TransferStatus status, string displayName, long sizeBytes)[] outcomes)
    {
        _progressBar.Visible = false;
        _listView.Visible    = true;
        _btnCancel.Text      = "Close";

        _listView.Items.Clear();
        int copied = 0, skipped = 0, blocked = 0;

        foreach (var (result, status, displayName, sizeBytes) in outcomes)
        {
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

            var item = new ListViewItem(displayName);
            item.SubItems.Add(statusText);
            item.SubItems.Add(FormatSize(sizeBytes));
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
