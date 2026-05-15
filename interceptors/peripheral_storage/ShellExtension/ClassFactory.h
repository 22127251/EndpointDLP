#pragma once
#define NOMINMAX
#include <windows.h>
#include <objbase.h>   // IClassFactory, IUnknown — omitted by WIN32_LEAN_AND_MEAN

class ClassFactory : public IClassFactory
{
public:
    // IUnknown
    STDMETHODIMP         QueryInterface(REFIID riid, void** ppv) override;
    STDMETHODIMP_(ULONG) AddRef()  override;
    STDMETHODIMP_(ULONG) Release() override;

    // IClassFactory
    STDMETHODIMP CreateInstance(IUnknown* pUnkOuter, REFIID riid, void** ppv) override;
    STDMETHODIMP LockServer(BOOL fLock) override;

private:
    LONG m_refCount{1};
};
