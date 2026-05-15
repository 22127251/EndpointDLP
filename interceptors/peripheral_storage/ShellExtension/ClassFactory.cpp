#include "ClassFactory.h"
#include "DlpContextMenu.h"

// Tracks how many objects and locks are alive so DllCanUnloadNow is correct.
extern LONG g_objectCount;
extern LONG g_lockCount;

// ── IUnknown ─────────────────────────────────────────────────────────────────

STDMETHODIMP ClassFactory::QueryInterface(REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    if (riid == IID_IUnknown || riid == IID_IClassFactory)
        { *ppv = this; AddRef(); return S_OK; }
    *ppv = nullptr;
    return E_NOINTERFACE;
}

STDMETHODIMP_(ULONG) ClassFactory::AddRef()
{
    return InterlockedIncrement(&m_refCount);
}

STDMETHODIMP_(ULONG) ClassFactory::Release()
{
    LONG ref = InterlockedDecrement(&m_refCount);
    if (ref == 0) delete this;
    return static_cast<ULONG>(ref);
}

// ── IClassFactory ─────────────────────────────────────────────────────────────

STDMETHODIMP ClassFactory::CreateInstance(IUnknown* pUnkOuter, REFIID riid, void** ppv)
{
    if (!ppv) return E_POINTER;
    *ppv = nullptr;
    if (pUnkOuter) return CLASS_E_NOAGGREGATION;

    auto* obj = new (std::nothrow) DlpContextMenu();
    if (!obj) return E_OUTOFMEMORY;
    InterlockedIncrement(&g_objectCount);

    HRESULT hr = obj->QueryInterface(riid, ppv);
    obj->Release();
    return hr;
}

STDMETHODIMP ClassFactory::LockServer(BOOL fLock)
{
    if (fLock) InterlockedIncrement(&g_lockCount);
    else        InterlockedDecrement(&g_lockCount);
    return S_OK;
}
