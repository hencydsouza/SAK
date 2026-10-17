# Python tool layer (squirrel / nsz)

This replaces `squirrel.exe` (a PyInstaller build of NSC_BUILDER's `py/ztools/squirrel.py`) and adds `nsz` as a second way to compress and decompress. To install, run `scripts/setup_python_tools.sh`. It is idempotent, so you can re-run it at any time.

## What gets installed

| Item | Location | Pin |
|---|---|---|
| venv (Python >= 3.10; prefers 3.12, override with `SAK_PYTHON=`) | `SAK_mac/.venv` | built with Python 3.12.13 |
| NSC_BUILDER | `SAK_mac/tools/nscb` | `e9083e83383281bdd9e167d3141163dcc56b6710` (master HEAD, 2021-11-15, last upstream commit) |
| macOS patch | `scripts/patches/nscb-macos.patch` (applied with `git apply`) | n/a |
| pip deps | `.venv` | pycryptodome 4.0.0, zstandard 0.25.0, tqdm 4.70.1, pykakasi 2.3.0, urllib3 2.8.0, requests 2.34.2, chardet 7.6.0, beautifulsoup4 4.15.0, pillow 13.0.0, googletrans 4.0.2 |
| nsz | `.venv/bin/nsz` | 5.0.0 |

Not installed: pywin32, windows-curses, comtypes, and the eel/flask/google-drive packages. None of them is imported on the squirrel code paths that SAK uses. Every `win32api` import is behind a `sys.platform == 'win32'` check.

## Patch (`nscb-macos.patch`, 4 files, about 30 lines)

1. `squirrel.py`: before squirrel `chdir()`s into its own folder, it saves the caller's cwd in `SQUIRREL_ORIG_CWD`. Without this, SAK's convention (cwd = the folder that holds keys.txt) cannot work.
2. `lib/sq_settings.py`: adds `find_keys_file()`, one OS-neutral key lookup used by both key loaders.
3. `lib/Keys.py`: the hard-coded candidates (`'ztools\\keys.txt'`, a Windows backslash path) are replaced with `find_keys_file()`.
4. `lib/NXKeys.py`: same lookup. It also fixes an upstream bug: when no keys file was found, it crashed at import with `UnboundLocalError: f`. It now loads an empty key set, and the first key access raises a clear `KeyError: Missing key ... in keys.txt`.

## keys.txt placement

Supply your own keys. The first match wins:

1. `$NSCB_KEYS_FILE` (any path)
2. `SAK_mac/tools/nscb/py/ztools/keys.txt` (the folder holding squirrel.py; this is upstream behaviour)
3. `<cwd of the squirrel process>/keys.txt` (the SAK convention)
4. `~/.switch/prod.keys`

Every squirrel command except `-h` needs keys, because `Fs/pyNCA3.py` reads keys in a class body at import time. A keys.txt that is present but incomplete fails at import with `KeyError: 'aes_kek_generation_source'`.

nsz takes keys from `--keys <file|dir>`, or from `~/.switch/prod.keys` / `keys.txt`. It does not read squirrel's locations, so pass `--keys`.

## Invocations (macOS)

```
SQ="SAK_mac/.venv/bin/python -W ignore::SyntaxWarning SAK_mac/tools/nscb/py/ztools/squirrel.py"
```

`-W ignore::SyntaxWarning` hides the invalid-escape warnings that upstream code triggers on Python 3.12 or later. **All paths must be absolute, both on the command line and inside the `-tfile` list.** Squirrel chdirs to `py/ztools`, so a relative path resolves against that folder, not against your cwd.

| Op | Command |
|---|---|
| merge base+upd+DLC | `$SQ -b 65536 -pv false -kp false --RSVcap 268435656 -fat exfat -fx files -ND true -t xci\|nsp -o OUT -tfile LIST -roma TRUE -dmul calculate` |
| NSP->XCI | `$SQ -b 65536 -pv false -kp false --RSVcap 268435656 -fat exfat -fx files -ND true -o OUT -t xci -dc FILE.nsp` |
| patch -> XCI | `$SQ -b 65536 -pv true -kp 0 --RSVcap 0 -fat exfat -fx files -ND true -o OUT -t xci -dc FILE` |
| XCI->NSP (replaces 4NXCI) | `$SQ -b 65536 -pv false -kp false --RSVcap 268435656 -fat exfat -fx files -ND true -o OUT -t nsp -dc FILE.xci` |
| patch NSP (replaces reNXpack) | `$SQ -b 65536 -pv true -kp 0 --RSVcap 0 -fat exfat -fx files -ND true -o OUT -t nsp -dc FILE.nsp` |
| compress | `$SQ -b 65536 -o OUT -tfile LIST --compress 1\|10\|17\|22 --threads 0 --nodelta "-ND true" --fexport xcz` |
| decompress | `$SQ -b 65536 -o OUT -tfile LIST --decompress auto` |
| nsz alternative | `.venv/bin/nsz --keys KEYS -C -l 18 -o OUT FILE.nsp` / `.venv/bin/nsz --keys KEYS -D -o OUT FILE.nsz` |

How squirrel reads these options:

- `-dc` with `-t nsp` routes NSP or XCI input to `c_nsp_direct`, and with `-t xci` to `c_xci_direct`. Both functions take the `-pv`/`--RSVcap`/`-kp` patch arguments, so XCI->NSP and patch-NSP are valid uses of `-dc`.
- `-kp false` means "no keygen patch".
- `--compress` and `--decompress` with `-tfile` handle **only the first line** of the list, so use one file per call. Compress accepts only lowercase `.nsp`/`.xci`, and decompress only `.nsz`/`.xcz`; anything else silently does nothing. With `-tfile`, the `auto` value is ignored.
- `-dmul` reads every line of the list.

## Known limitations

- **Not tested on real files.** No real keys or game files were available. Verified so far:
  - `squirrel.py -h` and `nsz --help` work.
  - Without keys, every command fails cleanly with the missing-key `KeyError`.
  - With a test double that returns zero bytes for keys (no key file written), every command above gets through argument parsing, reaches the correct handler, and stops at `FileNotFoundError` on the missing input.

  The actual crypto, merge and compression output has not been checked.
- The exit code is unreliable: many handlers catch the exception, print `Exception: ...`, and exit 0. For example, `-dmul` with missing inputs still writes a stub `UNKNOWN.xci`/`UNKNOWN.nsp` to OUT and returns 0. Check inputs before calling squirrel, and check the output file afterwards.
- `-pv false` always prints a harmless `Exception: invalid literal for int() ... 'false'`, from a standalone `--patchversion` block. Upstream behaves the same way on Windows.
- Windows-only code outside these paths is left unpatched: `--archive` (calls `win32api.SetFileAttributes` unguarded), `--xml_gen` (`Fs/Nca.py` builds `ofolder+'\\'`), the Interface/MTP/Google Drive modules, and `python_pick` (curses).
- squirrel writes its DB folder under `tools/nscb/py/zconfig/DB`.
