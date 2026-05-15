#pragma once
#define NOMINMAX
#include <windows.h>
#include <shellapi.h>  // HDROP, DragQueryFileW — not auto-included in SDK 10.0.19041+
#include <shlobj.h>
#include <string>
#include <vector>

// {B3A1C2D4-E5F6-7890-ABCD-EF1234567890}
static constexpr CLSID CLSID_DlpContextMenu = {
    0xB3A1C2D4, 0xE5F6, 0x7890,
    { 0xAB, 0xCD, 0xEF, 0x12, 0x34, 0x56, 0x78, 0x90 }
};

class DlpContextMenu : public IContextMenu, public IShellExtInit
{
public:
    DlpContextMenu();
    virtual ~DlpContextMenu() = default;

    // IUnknown
    STDMETHODIMP         QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef()  override;
    STDMETHODIMP_(ULONG) Release() override;

    // IShellExtInit
    STDMETHODIMP Initialize(PCIDLIST_ABSOLUTE pidlFolder,
                            IDataObject*      pdobj,
                            HKEY              hkeyProgID) override;

    // IContextMenu
    STDMETHODIMP QueryContextMenu(HMENU hmenu, UINT indexMenu,
                                  UINT  idCmdFirst, UINT idCmdLast,
                                  UINT  uFlags) override;
    STDMETHODIMP InvokeCommand(CMINVOKECOMMANDINFO* pici) override;
    STDMETHODIMP GetCommandString(UINT_PTR idCmd, UINT uType,
                                  UINT*    pReserved, CHAR* pszName,
                                  UINT     cchMax) override;

private:
    LONG                      m_refCount{1};
    std::vector<std::wstring> m_files;

    bool         GetAgentPath(std::wstring& outPath) const;
    void         ShowNotRemovableError(HWND hwndOwner) const;
};
