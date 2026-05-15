#pragma once
#include <windows.h>
#include <cstddef>

// Single source of truth for the shared memory layout used by the Controller (C#)
// and the Payload DLL (C++).  Keep in sync with SharedMemoryWriter.cs.

constexpr UINT32 SHM_MAGIC          = 0x55534244u;   // 'U','S','B','D'
constexpr UINT32 SHM_VERSION        = 1u;
constexpr SIZE_T SHM_SIZE           = 8192u;
constexpr UINT32 SHM_MAX_ENTRIES    = 26u;
constexpr UINT32 NT_PREFIX_WCHARS   = 128u;

#pragma pack(push, 4)

// Header: 32 bytes.  seq_counter and entry_count are updated under seqlock.
// fail_closed: 0 = fail-open (default), 1 = fail-closed.
struct SharedHeader {
    UINT32          magic;           // offset  0
    UINT32          version;         // offset  4
    volatile UINT32 seq_counter;     // offset  8  — odd = write in progress
    volatile UINT32 entry_count;     // offset 12
    UINT32          fail_closed;     // offset 16  — 0 = fail-open
    UINT32          _pad[3];         // offset 20  — align entries to offset 32
};  // = 32 bytes

// Entry: 264 bytes.  Stores one NT device-path prefix for a removable drive.
// E.g. L"\Device\HarddiskVolume5" with prefix_len = 22.
struct SharedEntry {
    WCHAR  nt_prefix[NT_PREFIX_WCHARS];   // offset   0  — null-terminated, 256 bytes
    UINT32 prefix_len;                     // offset 256  — length in WCHARs, excl. null
    UINT32 _reserved;                      // offset 260  — padding
};  // = 264 bytes

#pragma pack(pop)

static_assert(sizeof(SharedHeader) == 32,  "SharedHeader size mismatch");
static_assert(sizeof(SharedEntry)  == 264, "SharedEntry size mismatch");
static_assert(offsetof(SharedHeader, seq_counter) ==  8, "seq_counter offset");
static_assert(offsetof(SharedHeader, entry_count) == 12, "entry_count offset");
static_assert(offsetof(SharedHeader, fail_closed) == 16, "fail_closed offset");
static_assert(offsetof(SharedEntry,  nt_prefix)   ==  0, "nt_prefix offset");
static_assert(offsetof(SharedEntry,  prefix_len)  == 256, "prefix_len offset");

// Pointer to the entries array that follows the header.
inline SharedEntry* ShmEntries(void* base) {
    return reinterpret_cast<SharedEntry*>(
        static_cast<BYTE*>(base) + sizeof(SharedHeader));
}
inline const SharedEntry* ShmEntries(const void* base) {
    return reinterpret_cast<const SharedEntry*>(
        static_cast<const BYTE*>(base) + sizeof(SharedHeader));
}
