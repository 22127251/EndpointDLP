using System.Drawing;
using System.Windows.Forms;

namespace TransferAgent;

internal sealed class TransferForm : Form
{
    // ── layout constants ─────────────────────────────────────────────────────
    private const int Pad    = 12;
    private const int BtnH   = 32;
    private const int BtnW   = 130;

    // ── state ────────────────────────────────────────────────────────────────
    private readonly string[]         _sources;
    private readonly string           _destFolder;
    private TransferResult[]?         _results;
    private CancellationTokenSource   _cts = new();

    // ── controls (created once, shown/hidden per stage) ──────────────────────
    private readonly Label       _lblStatus;
    private readonly ProgressBar _progressBar;
    private readonly ListView    _listView;
    private readonly Button      _btnTransfer;
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

        // ── buttons ───────────────────────────────────────────────────────
        int btnY = ClientSize.Height - Pad - BtnH;

        _btnTransfer = new Button
        {
            Text    = "Transfer Allowed Files",
            Bounds  = new Rectangle(ClientSize.Width - Pad * 2 - BtnW * 2, btnY, BtnW, BtnH),
            Enabled = false,
            Visible = false,
        };
        _btnTransfer.Click += OnTransferClick;

        _btnCancel = new Button
        {
            Text   = "Cancel",
            Bounds = new Rectangle(ClientSize.Width - Pad - BtnW, btnY, BtnW, BtnH),
        };
        _btnCancel.Click += OnCancelClick;

        Controls.AddRange(new Control[]
        {
            _lblStatus, _progressBar, _listView, _btnTransfer, _btnCancel,
        });
    }

    // ── lifecycle ─────────────────────────────────────────────────────────────

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        await RunAnalysisStageAsync();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        _cts.Cancel();
        _cts.Dispose();
        base.OnFormClosed(e);
    }

    // ── stage 1: analysis ─────────────────────────────────────────────────────

    private async Task RunAnalysisStageAsync()
    {
        _lblStatus.Text = $"Analyzing {_sources.Length} file(s) for policy compliance…";
        _progressBar.Visible = true;
        _listView.Visible    = false;
        _btnTransfer.Visible = false;

        var tasks = _sources.Select(src =>
        {
            var fi  = new System.IO.FileInfo(src);
            var req = new TransferRequest(
                src,
                System.IO.Path.Combine(_destFolder, fi.Name),
                fi.Name,
                fi.Exists ? fi.Length : 0L);
            return OrchestratorClient.AnalyzeAsync(req, _cts.Token);
        });

        try
        {
            _results = await Task.WhenAll(tasks);
        }
        catch (OperationCanceledException)
        {
            Close();
            return;
        }

        ShowResultsStage();
    }

    // ── stage 2: results ──────────────────────────────────────────────────────

    private void ShowResultsStage()
    {
        _progressBar.Visible = false;
        _listView.Visible    = true;
        _btnTransfer.Visible = true;

        _listView.Items.Clear();
        int allowCount = 0;

        foreach (var r in _results!)
        {
            var fi   = new System.IO.FileInfo(r.FilePath);
            var item = new ListViewItem(fi.Name);
            item.SubItems.Add(r.Allowed ? "ALLOW" : "BLOCK");
            item.SubItems.Add(fi.Exists ? FormatSize(fi.Length) : "?");
            item.SubItems.Add(r.ErrorMessage ?? "");
            item.ForeColor = r.Allowed ? Color.DarkGreen : Color.DarkRed;
            item.Tag       = r;
            _listView.Items.Add(item);
            if (r.Allowed) allowCount++;
        }

        int blockCount = _results.Length - allowCount;
        _lblStatus.Text = $"{allowCount} allowed, {blockCount} blocked — ready to transfer.";
        _btnTransfer.Enabled = allowCount > 0;
        _btnCancel.Text      = "Close";
    }

    // ── stage 3: copying ──────────────────────────────────────────────────────

    private async void OnTransferClick(object? sender, EventArgs e)
    {
        _btnTransfer.Enabled = false;
        _btnCancel.Enabled   = false;

        var allowed = _results!.Where(r => r.Allowed).ToArray();
        _progressBar.Style   = ProgressBarStyle.Blocks;
        _progressBar.Minimum = 0;
        _progressBar.Maximum = allowed.Length;
        _progressBar.Value   = 0;
        _progressBar.Visible = true;
        _listView.Visible    = false;

        int copied  = 0;
        int skipped = 0;

        foreach (var r in allowed)
        {
            var fi   = new System.IO.FileInfo(r.FilePath);
            _lblStatus.Text = $"Copying {fi.Name} ({copied + 1}/{allowed.Length})…";
            Application.DoEvents();

            try
            {
                string destFile = System.IO.Path.Combine(_destFolder, fi.Name);
                System.IO.File.Copy(r.FilePath, destFile, overwrite: false);
                NativeMethods.NotifyFileCreated(destFile);
                copied++;
            }
            catch (IOException)
            {
                // destination exists — skip rather than overwrite without asking
                skipped++;
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    $"Failed to copy {fi.Name}:\n{ex.Message}",
                    "Transfer Error",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning);
                skipped++;
            }

            _progressBar.Value++;
            await Task.Yield();
        }

        ShowDoneStage(copied, skipped, _results!.Length - allowed.Length);
    }

    // ── stage 4: done ─────────────────────────────────────────────────────────

    private void ShowDoneStage(int copied, int skipped, int blocked)
    {
        _progressBar.Visible  = false;
        _btnTransfer.Visible  = false;
        _btnCancel.Text       = "Close";
        _btnCancel.Enabled    = true;

        var parts = new List<string>();
        if (copied  > 0) parts.Add($"{copied} transferred");
        if (skipped > 0) parts.Add($"{skipped} skipped (already exists)");
        if (blocked > 0) parts.Add($"{blocked} blocked by policy");
        _lblStatus.Text = "Transfer complete: " + string.Join(", ", parts) + ".";
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    private void OnCancelClick(object? sender, EventArgs e)
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
