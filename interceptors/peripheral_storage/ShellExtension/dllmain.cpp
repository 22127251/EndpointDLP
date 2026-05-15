#include "DlpContextMenu.h"
#include "ClassFactory.h"
#include <strsafe.h>
#include <new>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "advapi32.lib")

// ── Module globals ────────────────────────────────────────────────────────────
static HINSTANCE g_hInst       = nullptr;
LONG             g_objectCount  = 0;
LONG             g_lockCount    = 0;

// ── CLSID string (must match DlpContextMenu.h) ───────────────────────────────
static constexpr wchar_t kClsidStr[] =
    L"{B3A1C2D4-E5F6-7890-ABCD-EF1234567890}";

// Friendly name written to the registry.
static constexpr wchar_t kFriendlyName[] = L"DLP File Transfer";

// ── DllMain ──────────────────────────────────────────────────────────────────

BOOL WINAPI DllMain(HINSTANCE hInst, DWORD reason, LPVOID /*reserved*/)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_hInst = hInst;
        DisableThreadLibraryCalls(hInst);
    }
    return TRUE;
}

// ── COM exports ───────────────────────────────────────────────────────────────

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (rclsid != CLSID_DlpContextMenu) return CLASS_E_CLASSNOTAVAILABLE;

    auto* cf = new (std::nothrow) ClassFactory();
    if (!cf) return E_OUTOFMEMORY;

    HRESULT hr = cf->QueryInterface(riid, ppv);
    cf->Release();
    return hr;
}

STDAPI DllCanUnloadNow()
{
    return (g_objectCount == 0 && g_lockCount == 0) ? S_OK : S_FALSE;
}

// ── Self-registration helpers ─────────────────────────────────────────────────

static LSTATUS SetRegSZ(HKEY root, const wchar_t* subKey,
                        const wchar_t* valueName, const wchar_t* data)
{
    HKEY hKey{};
    LSTATUS st = RegCreateKeyExW(root, subKey, 0, nullptr,
                                 REG_OPTION_NON_VOLATILE, KEY_SET_VALUE,
                                 nullptr, &hKey, nullptr);
    if (st != ERROR_SUCCESS) return st;
    st = RegSetValueExW(hKey, valueName, 0, REG_SZ,
                        reinterpret_cast<const BYTE*>(data),
                        static_cast<DWORD>((wcslen(data) + 1) * sizeof(wchar_t)));
    RegCloseKey(hKey);
    return st;
}

STDAPI DllRegisterServer()
{
    // Get our own DLL path.
    WCHAR dllPath[MAX_PATH]{};
    if (!GetModuleFileNameW(g_hInst, dllPath, MAX_PATH))
        return HRESULT_FROM_WIN32(GetLastError());

    // HKCR\CLSID\{...}
    {
        WCHAR key[128]{};
        StringCchPrintfW(key, 128, L"CLSID\\%s", kClsidStr);
        if (SetRegSZ(HKEY_CLASSES_ROOT, key, nullptr, kFriendlyName) != ERROR_SUCCESS)
            return E_ACCESSDENIED;
    }
    // HKCR\CLSID\{...}\InProcServer32 — DLL path
    {
        WCHAR key[160]{};
        StringCchPrintfW(key, 160, L"CLSID\\%s\\InProcServer32", kClsidStr);
        LSTATUS st = SetRegSZ(HKEY_CLASSES_ROOT, key, nullptr, dllPath);
        if (st != ERROR_SUCCESS) return HRESULT_FROM_WIN32(st);
        st = SetRegSZ(HKEY_CLASSES_ROOT, key, L"ThreadingModel", L"Apartment");
        if (st != ERROR_SUCCESS) return HRESULT_FROM_WIN32(st);
    }
    // HKCR\*\shellex\ContextMenuHandlers\DLPTransfer
    {
        WCHAR key[128]{};
        StringCchPrintfW(key, 128,
                         L"*\\shellex\\ContextMenuHandlers\\DLPTransfer");
        SetRegSZ(HKEY_CLASSES_ROOT, key, nullptr, kClsidStr);
    }
    // Approved shell extensions (required to load in a hardened explorer.exe)
    {
        constexpr wchar_t kApproved[] =
            L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\"
            L"Shell Extensions\\Approved";
        SetRegSZ(HKEY_LOCAL_MACHINE, kApproved, kClsidStr, kFriendlyName);
    }

    // Notify the shell that a new extension was registered.
    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, nullptr, nullptr);
    return S_OK;
}

STDAPI DllUnregisterServer()
{
    // Remove in reverse order; ignore errors (best effort).
    {
        constexpr wchar_t kApproved[] =
            L"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\"
            L"Shell Extensions\\Approved";
        HKEY hKey{};
        if (RegOpenKeyExW(HKEY_LOCAL_MACHINE, kApproved, 0,
                          KEY_SET_VALUE, &hKey) == ERROR_SUCCESS)
        {
            RegDeleteValueW(hKey, kClsidStr);
            RegCloseKey(hKey);
        }
    }
    {
        WCHAR key[128]{};
        StringCchPrintfW(key, 128,
                         L"*\\shellex\\ContextMenuHandlers\\DLPTransfer");
        RegDeleteKeyW(HKEY_CLASSES_ROOT, key);
    }
    {
        WCHAR key[160]{};
        StringCchPrintfW(key, 160, L"CLSID\\%s\\InProcServer32", kClsidStr);
        RegDeleteKeyW(HKEY_CLASSES_ROOT, key);
    }
    {
        WCHAR key[128]{};
        StringCchPrintfW(key, 128, L"CLSID\\%s", kClsidStr);
        RegDeleteKeyW(HKEY_CLASSES_ROOT, key);
    }

    SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, nullptr, nullptr);
    return S_OK;
}
