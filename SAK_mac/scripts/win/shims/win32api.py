"""Minimal stand-in for pywin32's win32api (ctypes): only what NSC_Builder's squirrel calls.

squirrel uses win32api.SetFileAttributes to mark split-NSP folders with the archive bit.
Shipping these two small modules avoids bundling pywin32, whose DLL bootstrap depends on
.pth processing and post-install steps.
"""

import ctypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
_k32.SetFileAttributesW.restype = ctypes.c_int
_k32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
_k32.GetFileAttributesW.restype = ctypes.c_uint32


def SetFileAttributes(path, attributes):
    if not _k32.SetFileAttributesW(str(path), int(attributes)):
        raise ctypes.WinError(ctypes.get_last_error())


def GetFileAttributes(path):
    attrs = _k32.GetFileAttributesW(str(path))
    if attrs == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    return attrs
