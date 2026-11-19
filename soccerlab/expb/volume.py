"""Volume identity (drive-identity check for the research store and backups)."""

import os
from pathlib import Path


def volume_id(path):
    """Identity of the volume holding `path`: the NTFS/FAT volume serial on
    Windows, st_dev elsewhere. Used to enforce a physically separate target."""
    p = Path(path).resolve()
    while not p.exists():
        p = p.parent
    if os.name == "nt":
        import ctypes
        root = os.path.splitdrive(str(p))[0] + "\\"
        serial = ctypes.c_uint32()
        ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), None, 0, ctypes.byref(serial),
                                                          None, None, None, 0)
        if ok:
            return f"{serial.value:08X}"
    return f"dev:{os.stat(p).st_dev}"
