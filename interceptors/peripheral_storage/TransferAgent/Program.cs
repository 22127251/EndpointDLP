using System.Windows.Forms;
using TransferAgent;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);

        string? dest    = null;
        var     sources = new List<string>();

        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--dest" && i + 1 < args.Length)
                dest = args[++i];
            else
                sources.Add(args[i]);
        }

        if (dest == null || sources.Count == 0)
        {
            MessageBox.Show(
                "Usage: DlpTransferAgent.exe --dest <destination_folder> <file1> [file2 ...]",
                "DLP Transfer Agent", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        if (!System.IO.Directory.Exists(dest))
        {
            MessageBox.Show(
                $"Destination folder does not exist:\n{dest}",
                "DLP Transfer Agent", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        // Phase B: read the central config once before the form runs.
        // Fail-stop on config errors — no fallback to baked-in defaults; the
        // ShellExtension's invocation contract assumes the orchestrator is set up.
        try
        {
            OrchestratorClient.LoadConfig();
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                $"DLP Transfer Agent could not load its configuration.\n\n{ex.Message}",
                "DLP Transfer Agent", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        Application.Run(new TransferForm(sources.ToArray(), dest));
    }
}
