#include <windows.h>
#include <winternl.h>
#include <intrin.h>
#include <stdio.h>
#include <new>
#include <detours/detours.h>
#include "hook.h"
#include "../Common/shared_layout.h"

// ============================================================
//  NT type declarations (resolved at runtime via GetProcAddress)
// ============================================================

typedef NTSTATUS (NTAPI *NtCreateFile_t)(
    _Out_     PHANDLE            FileHandle,
    _In_      ACCESS_MASK        DesiredAccess,
    _In_      POBJECT_ATTRIBUTES ObjectAttributes,
    _Out_     PIO_STATUS_BLOCK   IoStatusBlock,
    _In_opt_  PLARGE_INTEGER     AllocationSize,
    _In_      ULONG              FileAttributes,
    _In_      ULONG              ShareAccess,
    _In_      ULONG              CreateDisposition,
    _In_      ULONG              CreateOptions,
    _In_opt_  PVOID              EaBuffer,
    _In_      ULONG              EaLength
);

typedef NTSTATUS (NTAPI *NtQueryObject_t)(
    _In_opt_  HANDLE                   Handle,
    _In_      OBJECT_INFORMATION_CLASS ObjectInformationClass,
    _Out_opt_ PVOID                    ObjectInformation,
    _In_      ULONG                    ObjectInformationLength,
    _Out_opt_ PULONG                   ReturnLength
);

typedef struct _OBJECT_NAME_INFORMATION {
    UNICODE_STRING Name;
} OBJECT_NAME_INFORMATION, * POBJECT_NAME_INFORMATION;

// ============================================================
//  Module globals
// ============================================================

static NtCreateFile_t  g_pfnNtCreateFile  = nullptr;
static NtQueryObject_t g_pfnNtQueryObject = nullptr;
static HANDLE          g_hMapping         = nullptr;
static const void*     g_pView            = nullptr;
static DWORD           g_tlsGuard         = TLS_OUT_OF_INDEXES;
static DWORD           g_tlsCache         = TLS_OUT_OF_INDEXES;
static bool            g_failClosed       = false;
static HANDLE          g_hLogFile         = INVALID_HANDLE_VALUE;

// Atomic flag: 0 = not yet hooked / already unhooked, 1 = hooks active.
static LONG   g_hookActive        = 0;
static HANDLE g_hReactivateEvent  = NULL;
static HANDLE g_hSuppressEvent    = NULL;   // Global\UsbDlpSuppress_<pid> — signaled by Controller to soft-bypass this process on demand

// ============================================================
//  Debug logging  (writes to %TEMP%\UsbDlpPayload.log)
// ============================================================

static void DlpLogInit()
{
    wchar_t tmpDir[MAX_PATH];
    if (GetTempPathW(MAX_PATH, tmpDir) == 0) return;
    wchar_t logPath[MAX_PATH];
    _snwprintf_s(logPath, _countof(logPath), _TRUNCATE,
                 L"%sUsbDlpPayload.log", tmpDir);
    g_hLogFile = CreateFileW(logPath, GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE,
                             NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (g_hLogFile == INVALID_HANDLE_VALUE) return;
    SetFilePointer(g_hLogFile, 0, NULL, FILE_END);  // append mode
}

static void DlpLog(const wchar_t* fmt, ...)
{
    if (g_hLogFile == INVALID_HANDLE_VALUE) return;

    SYSTEMTIME st;
    GetLocalTime(&st);

    wchar_t wbuf[768];
    int hlen = _snwprintf_s(wbuf, _countof(wbuf), _TRUNCATE,
        L"[%02u:%02u:%02u.%03u P%-5lu T%-5lu] ",
        st.wHour, st.wMinute, st.wSecond, st.wMilliseconds,
        GetCurrentProcessId(), GetCurrentThreadId());
    if (hlen < 0) return;

    va_list ap;
    va_start(ap, fmt);
    int blen = _vsnwprintf_s(wbuf + hlen, (int)_countof(wbuf) - hlen - 2,
                             _TRUNCATE, fmt, ap);
    va_end(ap);
    if (blen < 0) return;

    int total = hlen + blen;
    wbuf[total]   = L'\n';
    wbuf[total+1] = L'\0';
    total++;

    // Write as UTF-8 so the file is readable in any text editor.
    char utf8[1536];
    int utf8len = WideCharToMultiByte(CP_UTF8, 0, wbuf, total,
                                      utf8, (int)sizeof(utf8) - 1, NULL, NULL);
    if (utf8len <= 0) return;

    DWORD w;
    WriteFile(g_hLogFile, utf8, (DWORD)utf8len, &w, NULL);
}

// ============================================================
//  Write-intent mask
// ============================================================

constexpr ACCESS_MASK WRITE_MASK =
    GENERIC_WRITE         |   // 0x40000000
    FILE_WRITE_DATA       |   // 0x00000002
    FILE_APPEND_DATA      |   // 0x00000004
    FILE_WRITE_ATTRIBUTES |   // 0x00000100
    FILE_WRITE_EA;            // 0x00000010

#ifndef STATUS_ACCESS_DENIED
#define STATUS_ACCESS_DENIED ((NTSTATUS)0xC0000022L)
#endif

// NtCreateFile CreateOptions bit: the target is a directory.
// Not exposed in user-mode <windows.h>/<winternl.h>; value is stable since NT 3.1.
#ifndef FILE_DIRECTORY_FILE
#define FILE_DIRECTORY_FILE 0x00000001UL
#endif

// NtCreateFile CreateDisposition value 1 = FILE_OPEN: open existing only, never create.
// We allow this disposition through so Explorer can still enumerate the USB root
// and navigate into directories that already exist on the drive.
static constexpr ULONG kNtDispOpen = 1UL;

// ============================================================
//  Per-thread handle→path cache (single-entry)
// ============================================================

struct TlsCache {
    HANDLE handle;
    WCHAR  path[MAX_PATH];
    UINT32 path_len;
};

// ============================================================
//  Seqlock snapshot reader
// ============================================================

struct EntrySnapshot {
    UINT32      count;
    SharedEntry entries[SHM_MAX_ENTRIES];
};

static bool ReadSnapshot(EntrySnapshot& snap)
{
    if (!g_pView) return false;
    const auto* hdr = static_cast<const SharedHeader*>(g_pView);
    if (hdr->magic != SHM_MAGIC || hdr->version != SHM_VERSION) return false;

    for (int spin = 0; spin < 20; ++spin)
    {
        UINT32 seq1 = hdr->seq_counter;
        if (seq1 & 1) { _mm_pause(); continue; }  // write in progress
        _ReadBarrier();

        UINT32 n = hdr->entry_count;
        if (n > SHM_MAX_ENTRIES) return false;

        memcpy(snap.entries, ShmEntries(g_pView), n * sizeof(SharedEntry));
        _ReadBarrier();

        UINT32 seq2 = hdr->seq_counter;
        if (seq1 == seq2) { snap.count = n; return true; }
    }
    return false;   // spin timeout — caller uses fail-open/closed policy
}

// ============================================================
//  NT path prefix matching (case-insensitive)
// ============================================================

static bool IsRemovablePath(const WCHAR* path, UINT32 len, const EntrySnapshot& snap)
{
    for (UINT32 i = 0; i < snap.count; ++i)
    {
        const SharedEntry& e = snap.entries[i];
        if (len < e.prefix_len) continue;
        if (_wcsnicmp(path, e.nt_prefix, e.prefix_len) != 0) continue;
        // Ensure the prefix is followed by a path separator or end-of-string.
        if (len == e.prefix_len || path[e.prefix_len] == L'\\')
            return true;
    }
    return false;
}

// ============================================================
//  Relative-path resolution via NtQueryObject with TLS cache
// ============================================================

static bool ResolveHandlePath(HANDLE h, WCHAR* outBuf, UINT32& outLen)
{
    // Cache hit: same handle as last time on this thread
    auto* cache = static_cast<TlsCache*>(TlsGetValue(g_tlsCache));
    if (cache && cache->handle == h && cache->path_len > 0)
    {
        memcpy(outBuf, cache->path, cache->path_len * sizeof(WCHAR));
        outBuf[cache->path_len] = L'\0';
        outLen = cache->path_len;
        return true;
    }

    // Cache miss: ask the kernel for the object name
    BYTE    buf[2048];
    ULONG   retLen = 0;
    // ObjectNameInformation = 1
    NTSTATUS st = g_pfnNtQueryObject(
        h, static_cast<OBJECT_INFORMATION_CLASS>(1),
        buf, sizeof(buf), &retLen);
    if (!NT_SUCCESS(st)) return false;

    // The buffer starts with a UNICODE_STRING whose Buffer pointer is an
    // offset into the same allocation (in-place after the struct).
    auto* nameInfo = reinterpret_cast<OBJECT_NAME_INFORMATION*>(buf);
    UINT32 wlen = nameInfo->Name.Length / sizeof(WCHAR);
    if (wlen == 0 || wlen >= MAX_PATH) return false;

    // Populate or create the per-thread cache
    if (!cache)
    {
        cache = new (std::nothrow) TlsCache{};
        if (!cache) return false;
        TlsSetValue(g_tlsCache, cache);
    }
    cache->handle   = h;
    cache->path_len = wlen;
    memcpy(cache->path, nameInfo->Name.Buffer, wlen * sizeof(WCHAR));
    cache->path[wlen] = L'\0';

    memcpy(outBuf, cache->path, wlen * sizeof(WCHAR));
    outBuf[wlen] = L'\0';
    outLen = wlen;
    return true;
}

// ============================================================
//  Core decision logic (called from the hook, guard already set)
// ============================================================

static bool ShouldBlock(POBJECT_ATTRIBUTES objAttrs, ACCESS_MASK desiredAccess,
                         ULONG createDisposition, ULONG createOptions)
{
    // Fast path: allow if the caller has no write intent AND is not creating a directory.
    // CreateDirectoryW calls NtCreateFile with DesiredAccess = SYNCHRONIZE|FILE_LIST_DIRECTORY
    // (no WRITE_MASK bits), so the old single-flag check let directory creation pass through,
    // producing empty folders on the USB that were then held open by Explorer's shell machinery.
    // We still permit FILE_OPEN (disposition 1) so Explorer can enumerate the USB root and
    // navigate into directories that already exist on the drive.
    bool wantWrite = (desiredAccess & WRITE_MASK) != 0;
    bool createDir = (createOptions & FILE_DIRECTORY_FILE) != 0
                  && createDisposition != kNtDispOpen;
    if (!wantWrite && !createDir) return false;

    // Fast path: no shared memory view
    if (!g_pView) return g_failClosed;

    const auto* hdr = static_cast<const SharedHeader*>(g_pView);
    if (hdr->magic != SHM_MAGIC) return g_failClosed;

    // SHM is valid — read fail_closed live so Controller hot-reload takes effect immediately.
    // g_failClosed remains the fallback only when g_pView is null or magic is bad.
    bool liveFail = (hdr->fail_closed != 0);

    // Fast path: no removable drives in the map
    if (hdr->entry_count == 0) return false;

    if (!objAttrs || !objAttrs->ObjectName || !objAttrs->ObjectName->Buffer)
        return false;

    // Build the full NT path
    WCHAR  fullPath[1024];
    UINT32 fullLen = 0;

    if (objAttrs->RootDirectory == nullptr)
    {
        // Absolute path — copy directly
        UINT32 wlen = objAttrs->ObjectName->Length / sizeof(WCHAR);
        if (wlen == 0 || wlen >= _countof(fullPath)) return false;
        memcpy(fullPath, objAttrs->ObjectName->Buffer, wlen * sizeof(WCHAR));
        fullPath[wlen] = L'\0';
        fullLen = wlen;
    }
    else
    {
        // Relative path — resolve the directory handle (with TLS cache)
        WCHAR  basePath[MAX_PATH];
        UINT32 baseLen = 0;
        if (!ResolveHandlePath(objAttrs->RootDirectory, basePath, baseLen))
            return false;   // fail-open: can't resolve → don't block

        UINT32 relLen  = objAttrs->ObjectName->Length / sizeof(WCHAR);
        UINT32 needed  = baseLen + 1 + relLen;   // base + '\' + relative
        if (needed >= _countof(fullPath)) return false;

        memcpy(fullPath, basePath, baseLen * sizeof(WCHAR));
        fullPath[baseLen] = L'\\';
        memcpy(fullPath + baseLen + 1, objAttrs->ObjectName->Buffer,
               relLen * sizeof(WCHAR));
        fullPath[needed] = L'\0';
        fullLen = needed;
    }

    // Seqlock read + prefix check
    EntrySnapshot snap;
    if (!ReadSnapshot(snap)) return liveFail;

    return IsRemovablePath(fullPath, fullLen, snap);
}

// ============================================================
//  Hook function
// ============================================================

static NTSTATUS NTAPI Hook_NtCreateFile(
    PHANDLE            FileHandle,
    ACCESS_MASK        DesiredAccess,
    POBJECT_ATTRIBUTES ObjectAttributes,
    PIO_STATUS_BLOCK   IoStatusBlock,
    PLARGE_INTEGER     AllocationSize,
    ULONG              FileAttributes,
    ULONG              ShareAccess,
    ULONG              CreateDisposition,
    ULONG              CreateOptions,
    PVOID              EaBuffer,
    ULONG              EaLength)
{
    // Soft-bypass: hook deactivated — skip all logic, no TLS touch needed.
    if (!g_hookActive)
        goto callOriginal;

    // Re-entrancy guard — prevents recursive invocations on the same thread
    if (TlsGetValue(g_tlsGuard))
        goto callOriginal;

    TlsSetValue(g_tlsGuard, reinterpret_cast<PVOID>(1));
    {
        bool block = ShouldBlock(ObjectAttributes, DesiredAccess, CreateDisposition, CreateOptions);
        TlsSetValue(g_tlsGuard, nullptr);
        if (block)
        {
            // Log the blocked path (guard is cleared, no recursion risk)
            if (ObjectAttributes && ObjectAttributes->ObjectName
                && ObjectAttributes->ObjectName->Buffer)
            {
                UINT32 plen = ObjectAttributes->ObjectName->Length / sizeof(WCHAR);
                plen = (plen < 260) ? plen : 260;
                wchar_t pBuf[261];
                memcpy(pBuf, ObjectAttributes->ObjectName->Buffer, plen * sizeof(WCHAR));
                pBuf[plen] = L'\0';
                const wchar_t* tag = (CreateOptions & FILE_DIRECTORY_FILE)
                                     ? L"BLOCK(DIR): " : L"BLOCK: ";
                DlpLog(L"%ls%ls", tag, pBuf);
            }
            return STATUS_ACCESS_DENIED;
        }
    }

callOriginal:
    return g_pfnNtCreateFile(
        FileHandle, DesiredAccess, ObjectAttributes,
        IoStatusBlock, AllocationSize, FileAttributes,
        ShareAccess, CreateDisposition, CreateOptions,
        EaBuffer, EaLength);
}

// ============================================================
//  Watcher thread — waits on Global\UsbDlpAlive
// ============================================================

static DWORD WINAPI WatcherThread(LPVOID)
{
    DlpLog(L"WatcherThread: started");

    for (;;)
    {
        HANDLE hMutex = OpenMutexW(SYNCHRONIZE, FALSE, L"Global\\UsbDlpAlive");
        if (!hMutex)
        {
            DlpLog(L"WatcherThread: OpenMutex failed err=%lu, hook stays active", GetLastError());
            return 0;
        }

        DlpLog(L"WatcherThread: mutex opened, waiting for alive-mutex or suppress event...");
        HANDLE waitHandles[2] = { hMutex, g_hSuppressEvent };
        DWORD  handleCount    = (g_hSuppressEvent != NULL) ? 2 : 1;
        DWORD  waitResult     = WaitForMultipleObjects(handleCount, waitHandles, FALSE, INFINITE);
        DlpLog(L"WatcherThread: wait returned 0x%08lX, calling HookUninit", waitResult);
        // Only release the mutex if we acquired ownership (first handle signaled).
        // WAIT_OBJECT_0+1 = suppress event fired; we never acquired the mutex.
        if (waitResult == WAIT_OBJECT_0 || waitResult == WAIT_ABANDONED_0)
            ReleaseMutex(hMutex);
        CloseHandle(hMutex);

        HookUninit();
        DlpLog(L"WatcherThread: soft-bypass active, waiting for reactivate signal");

        if (!g_hReactivateEvent)
        {
            DlpLog(L"WatcherThread: no reactivate event handle, thread exiting");
            return 0;
        }

        // Inner loop: consume stale signals (Controller died right after signalling).
        for (;;)
        {
            WaitForSingleObject(g_hReactivateEvent, INFINITE);

            HANDLE hVerify = OpenMutexW(SYNCHRONIZE, FALSE, L"Global\\UsbDlpAlive");
            if (hVerify)
            {
                CloseHandle(hVerify);
                break;
            }
            DlpLog(L"WatcherThread: stale reactivate signal — mutex absent, waiting again");
        }

        InterlockedExchange(&g_hookActive, 1);
        DlpLog(L"WatcherThread: hook re-activated, looping to watch new controller");
    }
}

// ============================================================
//  Init / Uninit
// ============================================================

void HookInit()
{
    DlpLogInit();
    DlpLog(L"HookInit: started in PID=%lu", GetCurrentProcessId());

    g_tlsGuard = TlsAlloc();
    g_tlsCache = TlsAlloc();
    DlpLog(L"HookInit: TLS guard=%lu cache=%lu", g_tlsGuard, g_tlsCache);

    // Pre-map shared memory BEFORE attaching the hook to avoid any chance of
    // OpenFileMappingW or MapViewOfFile re-entering our hook.
    g_hMapping = OpenFileMappingW(FILE_MAP_READ, FALSE, L"Global\\UsbDlpDriveMap");
    if (g_hMapping)
    {
        DlpLog(L"HookInit: shared memory handle opened");
        g_pView = MapViewOfFile(g_hMapping, FILE_MAP_READ, 0, 0, SHM_SIZE);
        if (g_pView)
        {
            const auto* hdr = static_cast<const SharedHeader*>(g_pView);
            if (hdr->magic == SHM_MAGIC)
            {
                g_failClosed = (hdr->fail_closed != 0);
                DlpLog(L"HookInit: shm mapped OK — entry_count=%lu fail_closed=%d",
                       hdr->entry_count, (int)g_failClosed);
            }
            else
                DlpLog(L"HookInit: shm magic mismatch 0x%08lX (expected 0x%08lX)",
                       hdr->magic, SHM_MAGIC);
        }
        else
            DlpLog(L"HookInit: MapViewOfFile failed err=%lu", GetLastError());
    }
    else
        DlpLog(L"HookInit: OpenFileMapping failed err=%lu", GetLastError());

    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    g_pfnNtCreateFile  = reinterpret_cast<NtCreateFile_t>(
                            GetProcAddress(ntdll, "NtCreateFile"));
    g_pfnNtQueryObject = reinterpret_cast<NtQueryObject_t>(
                            GetProcAddress(ntdll, "NtQueryObject"));

    DlpLog(L"HookInit: NtCreateFile=%p  NtQueryObject=%p",
           (void*)g_pfnNtCreateFile, (void*)g_pfnNtQueryObject);

    if (!g_pfnNtCreateFile || !g_pfnNtQueryObject)
    {
        DlpLog(L"HookInit: ABORT — could not resolve NT functions");
        return;
    }

    // Attach hook
    InterlockedExchange(&g_hookActive, 1);
    DetourTransactionBegin();
    DetourUpdateThread(GetCurrentThread());
    DetourAttach(reinterpret_cast<PVOID*>(&g_pfnNtCreateFile), Hook_NtCreateFile);
    LONG attachErr = DetourTransactionCommit();
    DlpLog(L"HookInit: hook attached, DetourTransactionCommit=%ld (0=OK)", attachErr);

    // Create the per-process reactivate event before spawning WatcherThread so it
    // is available as soon as the thread enters its bypass-wait phase.
    WCHAR evtName[64];
    _snwprintf_s(evtName, _countof(evtName), _TRUNCATE,
                 L"Global\\UsbDlpReactivate_%lu", GetCurrentProcessId());
    g_hReactivateEvent = CreateEventW(nullptr, FALSE, FALSE, evtName);
    if (g_hReactivateEvent)
        DlpLog(L"HookInit: reactivate event '%s'", evtName);
    else
        DlpLog(L"HookInit: reactivate CreateEventW failed err=%lu", GetLastError());

    WCHAR suppressName[64];
    _snwprintf_s(suppressName, _countof(suppressName), _TRUNCATE,
                 L"Global\\UsbDlpSuppress_%lu", GetCurrentProcessId());
    g_hSuppressEvent = CreateEventW(nullptr, FALSE, FALSE, suppressName);
    if (g_hSuppressEvent)
        DlpLog(L"HookInit: suppress event '%s'", suppressName);
    else
        DlpLog(L"HookInit: suppress CreateEventW failed err=%lu", GetLastError());

    // Spawn the watcher thread (detached; manages its own lifetime)
    DWORD watcherTid = 0;
    HANDLE hThread = CreateThread(nullptr, 0, WatcherThread, nullptr, 0, &watcherTid);
    if (hThread)
    {
        DlpLog(L"HookInit: watcher thread spawned TID=%lu", watcherTid);
        CloseHandle(hThread);
    }
    else
        DlpLog(L"HookInit: CreateThread failed err=%lu", GetLastError());

    DlpLog(L"HookInit: done");
}

void HookUninit()
{
    DlpLog(L"HookUninit: entered (hookActive=%ld)", g_hookActive);

    // Idempotent: only one caller wins the CAS from 1 → 0
    if (!InterlockedCompareExchange(&g_hookActive, 0, 1))
    {
        DlpLog(L"HookUninit: CAS failed — already 0, returning");
        return;
    }

    // g_hookActive is now 0.  Hook_NtCreateFile takes the early-bypass path on
    // every subsequent call (one volatile load → callOriginal).  We deliberately
    // do NOT call DetourDetach / DetourTransactionCommit: doing so suspends all
    // process threads via SuspendThread, which corrupts WinUI 3 DispatcherQueue
    // and COM apartment state even when the commit returns 0.
    //
    // The Detours trampoline stays installed but is inert.  All resources (TLS
    // slots, shared-memory view, trampoline pages) remain valid for the DLL's
    // lifetime and are reclaimed by the OS on process exit.
    //
    // Refs:
    //   https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-suspendthread
    //   https://github.com/microsoft/Detours/issues/70
    //   https://github.com/microsoft/Detours/issues/78
    DlpLog(L"HookUninit: CAS won, soft bypass active — no DetourDetach");
    DlpLog(L"HookUninit: done");
}
