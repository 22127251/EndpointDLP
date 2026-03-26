using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace ClipboardInterceptor;

/// <summary>
/// Monitors clipboard changes using a hidden message-only window and WM_CLIPBOARDUPDATE.
/// ClipboardChanged events are raised on the STA message thread.
/// Must be disposed to unregister the clipboard listener.
/// </summary>
public sealed class ClipboardMonitor : IDisposable
{
    private const int WM_CLIPBOARDUPDATE = 0x031D;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool AddClipboardFormatListener(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool RemoveClipboardFormatListener(IntPtr hwnd);

    public event EventHandler? ClipboardChanged;

    // Exposed so callers can marshal work back to the STA thread
    public SynchronizationContext StaContext { get; private set; } = null!;

    private readonly Thread _messageThread;
    private MessageWindow? _messageWindow;
    private readonly ManualResetEventSlim _ready = new(false);

    public ClipboardMonitor()
    {
        _messageThread = new Thread(() =>
        {
            Application.SetCompatibleTextRenderingDefault(false);
            _messageWindow = new MessageWindow(this);
            // Force handle creation
            _ = _messageWindow.Handle;
            StaContext = SynchronizationContext.Current!;
            AddClipboardFormatListener(_messageWindow.Handle);
            _ready.Set();
            Application.Run();
        });
        _messageThread.SetApartmentState(ApartmentState.STA);
        _messageThread.IsBackground = true;
        _messageThread.Start();
        _ready.Wait();
    }

    internal void RaiseClipboardChanged()
    {
        ClipboardChanged?.Invoke(this, EventArgs.Empty);
    }

    public void Dispose()
    {
        if (_messageWindow != null)
        {
            RemoveClipboardFormatListener(_messageWindow.Handle);
            _messageWindow.BeginInvoke(Application.ExitThread);
        }
        _ready.Dispose();
    }

    private sealed class MessageWindow : Form
    {
        private readonly ClipboardMonitor _monitor;

        public MessageWindow(ClipboardMonitor monitor)
        {
            _monitor = monitor;
            ShowInTaskbar = false;
            FormBorderStyle = FormBorderStyle.None;
            WindowState = FormWindowState.Minimized;
            Opacity = 0;
        }

        protected override void SetVisibleCore(bool value)
        {
            // Never show the window
            if (!IsHandleCreated) CreateHandle();
            base.SetVisibleCore(false);
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == WM_CLIPBOARDUPDATE)
            {
                _monitor.RaiseClipboardChanged();
            }
            base.WndProc(ref m);
        }
    }
}
