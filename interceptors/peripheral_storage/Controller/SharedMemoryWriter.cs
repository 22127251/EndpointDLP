using System.Runtime.InteropServices;
using System.Threading;
using static Controller.NativeMethods;

namespace Controller;

/// <summary>
/// Creates the named shared memory segment and writes the seqlock-protected
/// drive-map entries that injected DLLs read from inside NtCreateFile.
/// </summary>
internal sealed class SharedMemoryWriter : IDisposable
{
    // Must match shared_layout.h
    private const int  ShmSize    = 8192;
    private const uint ShmMagic   = 0x55534244u;
    private const uint ShmVersion = 1u;
    private const int  MaxEntries = 26;
    private const int  HeaderSize = 32;   // sizeof(SharedHeader)
    private const int  EntrySize  = 264;  // sizeof(SharedEntry)
    private const int  PrefixWChars = 128;

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    private struct SharedHeader
    {
        public uint Magic;
        public uint Version;
        public uint SeqCounter;    // accessed via Interlocked + MemoryBarrier
        public uint EntryCount;
        public uint FailClosed;    // 0 = fail-open, 1 = fail-closed
        private uint _pad0;
        private uint _pad1;
        private uint _pad2;
    }  // = 32 bytes

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode, Pack = 4)]
    private unsafe struct SharedEntry
    {
        public fixed char NtPrefix[128];   // 256 bytes
        public uint PrefixLen;             //   4 bytes
        public uint Reserved;              //   4 bytes
    }  // = 264 bytes

    private IntPtr _mapHandle = IntPtr.Zero;
    private unsafe byte* _view = null;
    private bool _disposed;

    /// <summary>Allocates the named mapping and initialises the header.</summary>
    public void Initialize(string name, bool failClosed)
    {
        const string sddlTemplate =
            "D:(A;;0x00000004;;;WD)(A;;0x001F001F;;;SY)(A;;0x001F001F;;;BA)";

        if (!ConvertStringSecurityDescriptorToSecurityDescriptor(
                sddlTemplate, 1, out var pSD, out _))
            throw new InvalidOperationException(
                $"SDDL conversion failed: {Marshal.GetLastWin32Error()}");

        try
        {
            var sa = new SECURITY_ATTRIBUTES
            {
                nLength = Marshal.SizeOf<SECURITY_ATTRIBUTES>(),
                lpSecurityDescriptor = pSD,
                bInheritHandle = false
            };

            _mapHandle = CreateFileMappingW(
                INVALID_HANDLE_VALUE, ref sa,
                PAGE_READWRITE, 0, ShmSize,
                $"Global\\{name}");

            if (_mapHandle == IntPtr.Zero)
                throw new InvalidOperationException(
                    $"CreateFileMappingW failed: {Marshal.GetLastWin32Error()}");
        }
        finally
        {
            LocalFree(pSD);
        }

        unsafe
        {
            _view = MapViewOfFile(_mapHandle, FILE_MAP_ALL_ACCESS, 0, 0, ShmSize);
            if (_view == null)
                throw new InvalidOperationException(
                    $"MapViewOfFile failed: {Marshal.GetLastWin32Error()}");

            var hdr = (SharedHeader*)_view;
            hdr->Magic      = ShmMagic;
            hdr->Version    = ShmVersion;
            hdr->SeqCounter = 0;
            hdr->EntryCount = 0;
            hdr->FailClosed = failClosed ? 1u : 0u;
        }
    }

    /// <summary>
    /// Writes a new set of removable-drive NT path prefixes under the seqlock.
    /// Safe to call from any thread; only one writer is expected.
    /// </summary>
    public unsafe void WriteEntries(IReadOnlyList<string> ntPaths)
    {
        if (_view == null) return;

        var hdr     = (SharedHeader*)_view;
        var entries = (SharedEntry*)(_view + HeaderSize);

        int count = Math.Min(ntPaths.Count, MaxEntries);

        // Seqlock: increment to odd (write in progress)
        Interlocked.Increment(ref hdr->SeqCounter);
        Thread.MemoryBarrier();

        hdr->EntryCount = (uint)count;
        for (int i = 0; i < count; i++)
        {
            var path = ntPaths[i];
            int len  = Math.Min(path.Length, PrefixWChars - 1);
            entries[i].PrefixLen = (uint)len;
            entries[i].Reserved  = 0;

            fixed (char* src = path)
            {
                // NtPrefix is a fixed buffer at offset 0 inside an unmanaged struct;
                // access via pointer dereference to get a char*.
                SharedEntry* ep  = &entries[i];
                char*        dst = ep->NtPrefix;
                Buffer.MemoryCopy(src, dst, PrefixWChars * sizeof(char), len * sizeof(char));
                dst[len] = '\0';
            }
        }

        // Seqlock: increment to even (stable)
        Thread.MemoryBarrier();
        Interlocked.Increment(ref hdr->SeqCounter);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        unsafe
        {
            if (_view != null) { UnmapViewOfFile(_view); _view = null; }
        }
        if (_mapHandle != IntPtr.Zero) { CloseHandle(_mapHandle); _mapHandle = IntPtr.Zero; }
    }
}
