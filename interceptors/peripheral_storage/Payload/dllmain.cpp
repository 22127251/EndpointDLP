#include <windows.h>
#include "hook.h"

BOOL WINAPI DllMain(HINSTANCE hInst, DWORD reason, LPVOID lpReserved)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        DisableThreadLibraryCalls(hInst);
        HookInit();
    }
    else if (reason == DLL_PROCESS_DETACH)
    {
        // lpReserved != NULL means the process is terminating — unsafe to call
        // complex code during teardown.  lpReserved == NULL means FreeLibrary.
        if (lpReserved == nullptr)
            HookUninit();
    }
    return TRUE;
}