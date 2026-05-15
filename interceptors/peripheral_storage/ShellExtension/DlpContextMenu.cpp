#include "DlpContextMenu.h"
#include <shlwapi.h>
#include <strsafe.h>

extern LONG g_objectCount;

#pragma comment(lib, "shlwapi.lib")
#pragma comment(lib, "shell32.lib")

// ── Registry key where the installer records the agent path ──────────────────
static constexpr wchar_t kAgentRegKey[]  = L"SOFTWARE\\DLPAgent";
static constexpr wchar_t kAgentRegValue[] = L"TransferAgentPath";

// ── Menu item ID offset (relative to idCmdFirst) ─────────────────────────────
static constexpr UINT IDM_TRANSFER = 0;

// ─────────────────────────────────────────────────────────────────────────────

DlpContextMenu::DlpContextMenu() = default;

// ── IUnknown ─────────────────────────────────────────────────────────────────

STDMETHODIMP DlpContextMenu::QueryInterface(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IContextMenu)
        { *ppv = static_cast<IContextMenu*>(this); }
    else if (riid == IID_IShellExtInit)
        { *ppv = static_cast<IShellExtInit*>(this); }
    else
        { *ppv = nullptr; return E_NOINTERFACE; }
    AddRef();
    return S_OK;
}

STDMETHODIMP_(ULONG) DlpContextMenu::AddRef()
{
    return InterlockedIncrement(&m_refCount);
}

STDMETHODIMP_(ULONG) DlpContextMenu::Release()
{
    LONG ref = InterlockedDecrement(&m_refCount);
    if (ref == 0)
    {
        InterlockedDecrement(&g_objectCount);
        delete this;
    }
    return static_cast<ULONG>(ref);
}

// ── IShellExtInit ─────────────────────────────────────────────────────────────

STDMETHODIMP DlpContextMenu::Initialize(
    PCIDLIST_ABSOLUTE /*pidlFolder*/,
    IDataObject*       pdobj,
    HKEY               /*hkeyProgID*/)
{
    if (!pdobj) return E_INVALIDARG;

    FORMATETC fe = { CF_HDROP, nullptr, DVASPECT_CONTENT, -1, TYMED_HGLOBAL };
    STGMEDIUM stm{};
    HRESULT hr = pdobj->GetData(&fe, &stm);
    if (FAILED(hr)) return hr;

    m_files.clear();
    HDROP hDrop = reinterpret_cast<HDROP>(GlobalLock(stm.hGlobal));
    if (hDrop)
    {
        UINT count = DragQueryFileW(hDrop, 0xFFFFFFFF, nullptr, 0);
        for (UINT i = 0; i < count; ++i)
        {
            WCHAR path[MAX_PATH]{};
            if (DragQueryFileW(hDrop, i, path, MAX_PATH))
                m_files.emplace_back(path);
        }
        GlobalUnlock(stm.hGlobal);
    }
    ReleaseStgMedium(&stm);

    return m_files.empty() ? E_FAIL : S_OK;
}

// ── IContextMenu ──────────────────────────────────────────────────────────────

STDMETHODIMP DlpContextMenu::QueryContextMenu(
    HMENU hmenu, UINT indexMenu,
    UINT  idCmdFirst, UINT /*idCmdLast*/,
    UINT  uFlags)
{
    if (uFlags & CMF_DEFAULTONLY) return MAKE_HRESULT(SEVERITY_SUCCESS, 0, 0);

    InsertMenuW(hmenu, indexMenu, MF_BYPOSITION | MF_STRING,
                idCmdFirst + IDM_TRANSFER,
                L"Transfer to USB (DLP Protected)");

    return MAKE_HRESULT(SEVERITY_SUCCESS, 0, IDM_TRANSFER + 1);
}

STDMETHODIMP DlpContextMenu::InvokeCommand(CMINVOKECOMMANDINFO* pici)
{
    // Accept both string-verb and offset-based invocations.
    if (HIWORD(pici->lpVerb) != 0)
    {
        // String verb
        if (lstrcmpiA(pici->lpVerb, "dlp_transfer") != 0)
            return E_FAIL;
    }
    else if (LOWORD(pici->lpVerb) != IDM_TRANSFER)
    {
        return E_FAIL;
    }

    HWND hwndOwner = pici->hwnd;

    // ── 1. Let user pick destination folder ───────────────────────────────
    BROWSEINFOW bi{};
    bi.hwndOwner = hwndOwner;
    bi.lpszTitle = L"Select destination folder on removable drive:";
    bi.ulFlags   = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_USENEWUI;

    PIDLIST_ABSOLUTE pidl = SHBrowseForFolderW(&bi);
    if (!pidl) return S_OK;  // user cancelled

    WCHAR destPath[MAX_PATH]{};
    BOOL  ok = SHGetPathFromIDListW(pidl, destPath);
    CoTaskMemFree(pidl);
    if (!ok) return E_FAIL;

    // ── 2. Verify destination is on a removable drive ─────────────────────
    WCHAR root[4]{};
    root[0] = destPath[0]; root[1] = L':'; root[2] = L'\\'; root[3] = L'\0';

    if (GetDriveTypeW(root) != DRIVE_REMOVABLE)
    {
        ShowNotRemovableError(hwndOwner);
        return S_OK;
    }

    // ── 3. Locate the transfer agent executable ───────────────────────────
    std::wstring agentPath;
    if (!GetAgentPath(agentPath))
    {
        MessageBoxW(hwndOwner,
                    L"DLP Transfer Agent not found.\n"
                    L"Please verify the DLP agent is installed correctly.",
                    L"DLP File Transfer",
                    MB_OK | MB_ICONERROR);
        return E_FAIL;
    }

    // ── 4. Build command line ─────────────────────────────────────────────
    // CommandLineToArgvW (used by the .NET runtime to parse Main args) treats
    // backslashes immediately before a closing " as escape characters.  A path
    // like E:\ produces "E:\" where \" is an escaped quote, not end-of-arg —
    // swallowing subsequent arguments into dest and leaving sources empty.
    // Fix: double any trailing backslashes in each path before the closing ".
    std::wstring cmdLine;

    auto appendQuoted = [&](const std::wstring& s) {
        cmdLine += L'"';
        cmdLine += s;
        size_t n = 0;
        for (auto it = s.rbegin(); it != s.rend() && *it == L'\\'; ++it)
            n++;
        for (size_t i = 0; i < n; i++)
            cmdLine += L'\\';
        cmdLine += L'"';
    };

    appendQuoted(agentPath);
    cmdLine += L" --dest ";
    appendQuoted(destPath);

    for (const auto& src : m_files)
    {
        cmdLine += L' ';
        appendQuoted(src);
    }

    // ── 5. Launch agent (detached, no window) ─────────────────────────────
    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_SHOWNORMAL;

    PROCESS_INFORMATION pi{};
    BOOL launched = CreateProcessW(
        nullptr,
        cmdLine.data(),   // lpCommandLine must be mutable
        nullptr, nullptr,
        FALSE,
        0,
        nullptr, nullptr,
        &si, &pi);

    if (launched)
    {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
    else
    {
        DWORD err = GetLastError();
        WCHAR msg[256]{};
        StringCchPrintfW(msg, 256,
                         L"Failed to launch DLP Transfer Agent (error %lu).\n"
                         L"Path: %s", err, agentPath.c_str());
        MessageBoxW(hwndOwner, msg, L"DLP File Transfer", MB_OK | MB_ICONERROR);
    }

    return S_OK;
}

STDMETHODIMP DlpContextMenu::GetCommandString(
    UINT_PTR idCmd, UINT uType,
    UINT* /*pReserved*/, CHAR* pszName, UINT cchMax)
{
    if (idCmd != IDM_TRANSFER) return E_INVALIDARG;

    if (uType == GCS_VERBW)
    {
        StringCchCopyW(reinterpret_cast<WCHAR*>(pszName), cchMax, L"dlp_transfer");
        return S_OK;
    }
    if (uType == GCS_HELPTEXTW)
    {
        StringCchCopyW(reinterpret_cast<WCHAR*>(pszName), cchMax,
                       L"Transfer selected file(s) to a removable drive via DLP policy check");
        return S_OK;
    }
    return E_NOTIMPL;
}

// ── Private helpers ───────────────────────────────────────────────────────────

static bool ReadAgentPathFromHive(HKEY hive, const wchar_t* key,
                                   const wchar_t* value, std::wstring& out)
{
    HKEY hKey{};
    if (RegOpenKeyExW(hive, key, 0, KEY_READ, &hKey) != ERROR_SUCCESS) return false;
    WCHAR buf[MAX_PATH]{};
    DWORD type = REG_SZ, size = sizeof(buf);
    LSTATUS st = RegQueryValueExW(hKey, value, nullptr, &type,
                                  reinterpret_cast<BYTE*>(buf), &size);
    RegCloseKey(hKey);
    if (st != ERROR_SUCCESS || type != REG_SZ) return false;
    out = buf;
    return !out.empty();
}

bool DlpContextMenu::GetAgentPath(std::wstring& outPath) const
{
    // Production: written to HKLM by the admin installer.
    if (ReadAgentPathFromHive(HKEY_LOCAL_MACHINE, kAgentRegKey, kAgentRegValue, outPath))
        return true;
    // Dev/verification: written to HKCU by verify-install.ps1 (no admin required).
    return ReadAgentPathFromHive(HKEY_CURRENT_USER, kAgentRegKey, kAgentRegValue, outPath);
}

void DlpContextMenu::ShowNotRemovableError(HWND hwndOwner) const
{
    MessageBoxW(hwndOwner,
                L"The selected folder is not on a removable drive.\n\n"
                L"DLP-protected transfers are only allowed to removable storage devices\n"
                L"(USB drives, SD cards, etc.).",
                L"DLP File Transfer",
                MB_OK | MB_ICONWARNING);
}
