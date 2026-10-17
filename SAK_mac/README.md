# Switch Army Knife for macOS

A macOS port of kempa's **Switch Army Knife (SAK) v0.7.14**, a Windows tool for managing Nintendo Switch XCI/NSP files. It works on Apple Silicon and Intel Macs. Use it only with games and saves you own and with keys dumped from your own console. No keys are included, and the port never downloads any.

## How the port was made

The original `SAK.exe` is a compiled AutoIt v3 script. The program itself is just a GUI. It calls a set of Windows command-line tools in `bin\` and does the split/merge byte copying itself.

- The script was extracted from the `SCRIPT` RCDATA resource of `SAK_64bit/SAK.exe` with `autoit-ripper`. The app's own code is about 4,300 lines after the bundled AutoIt libraries.
- From that source we recorded every tool command line, output naming rule, folder layout and edge case, and used them as the spec for this port.
- Two things were rebuilt from the decompiled logic instead of calling a tool:
  - Split/merge is done natively.
  - `hptnacp.exe` is replaced by `sak/nacp.py`. It writes the same values SAK's generated `control.xml` asked for, at the offsets checked against NSC_Builder's NACP parser.

| SAK feature | Windows tool SAK used | macOS port uses |
|---|---|---|
| Split / merge XCI & NSP | built-in AutoIt code | `sak/split.py` (native, same naming and the same 0xFFFF0000 part size) |
| Update XCI/NSP with update + DLC | squirrel.exe (NSC_Builder) | NSC_Builder `squirrel.py` run from source |
| NSP → XCI, patch XCI | squirrel.exe | `squirrel.py` (same arguments) |
| XCI → NSP | 4nxci.exe (repo deleted) | `squirrel.py -t nsp -dc` |
| Patch NSP to lower firmware | renxpack.exe (repo deleted) | `squirrel.py -t nsp -dc -pv true -kp 0` |
| Compress / decompress NSZ / XCZ | squirrel.exe | `squirrel.py` |
| Extract firmware from XCI | hactool.exe | `hactool`, built from SciresM/hactool |
| Extract savegames | hactoolnet.exe (LibHac repo deleted) | `savex`, a small .NET tool built on the LibHac 0.19.0 NuGet package |
| NRO / RetroArch forwarder | hptnacp.exe + hacbrewpack.exe | `sak/nacp.py` + `hacbrewpack` v3.05, built from source |
| Format SD card FAT32 | guiformat.exe | `diskutil eraseDisk FAT32`, plus optional `newfs_msdos` with 64 KiB clusters |
| Self-updater (psxtools.de) | update.exe | dropped (the project is closed) |

## Install

```bash
cd SAK_mac
./scripts/setup.sh          # builds hactool + hacbrewpack, sets up NSC_Builder/nsz, builds savex
cp /path/to/your/prod.keys keys/
./sak.command doctor        # every line should say [ok]
```

You need the Xcode Command Line Tools (`xcode-select --install`) and `python3` 3.10 or newer. Saves extraction also needs the .NET SDK (`brew install --cask dotnet-sdk`); set `SKIP_SAVEX=1` to skip it.

Keys are looked up in this order: `--keys` → `$SAK_KEYS` → `SAK_mac/keys/prod.keys` → `SAK_mac/keys/keys.dat` → `~/.switch/prod.keys`.

NSC_Builder runs from source at a pinned commit, with a small macOS patch in `scripts/patches/nscb-macos.patch`. The patch fixes key lookup, which upstream hard-coded to a Windows path, and an upstream crash at import. The port hands squirrel the keys through `NSCB_KEYS_FILE`, so no copy of your keys is written anywhere. See `tools/README_TOOLS_PY.md` for details.

## Use

**GUI:** double-click `sak.command`, or run `./sak.command` with no arguments. It opens a local web page with the same menu as SAK, and output goes to `~/SAK`. The server listens only on 127.0.0.1, and every API call needs a random token created at launch. File pickers use the native macOS dialogs.

**CLI:** `./sak.command [-o OUTDIR] [--keys FILE] <command> ...`

```bash
./sak.command split-xci "Game.xci" --trim
./sak.command merge-xci Game_splitted.xc0
./sak.command split-nsp Game.nsp
./sak.command xci2nsp Game.xci --split
./sak.command update --base Game.xci --format xci Update.nsp DLC1.nsp
./sak.command compress Game.nsp --level hard
./sak.command extract-fw Game.xci
./sak.command extract-saves ./save
./sak.command forwarder --name "My App" --nro /switch/myapp/myapp.nro --icon icon.jpg
./sak.command forwarder --name "SNES - Mario" --nro /retroarch/cores/snes9x_libretro_libnx.nro \
              --rom "/roms/snes/mario.sfc" --icon mario.jpg
./sak.command drives
sudo ./sak.command fat32 disk4 --confirm disk4 --cluster-64k   # ERASES disk4
```

## Behaviour kept from SAK

- Parts are 0xFFFF0000 bytes and copied in 32 KiB blocks. A file must be at least 0xFFFF0000 bytes to be split, and there must be free space equal to the file size plus 100 MB.
- Spaces in names become `_`.
  - XCI parts are named `Name_splitted.xc0`, `.xc1`, and so on.
  - Split NSPs go in a `Name_splitted.nsp/` folder with parts `00`, `01`, and so on.
  - Merged files are named `*_merged.xci` or `*_merged.nsp`.
- Merge accepts `.xc0`, `.xci.00` and folder-style `00` first parts.
- With "Trim", any 32 KiB block in the second or later part that starts with 10 bytes of `0xFF` is dropped. This is exactly SAK's `StringInStr(binary, "0xFFFFFFFFFFFFFFFFFFFF")` check.
- The forwarder builds the same tree as SAK:
  - `control.nacp` with 12 title languages, 11 supported languages (no Portuguese), the same ratings and IDs, and the screenshot and video-capture flags.
  - The icon, plus the bundled nx-hbloader `main` and `main.npdm`.
  - The logo files, or your own from a `custom/` folder (`NintendoLogo.png`, `StartupMovie.gif`).
  - `romfs/nextNroPath` and `nextArgv`. For RetroArch, `nextArgv` is `sdmc:<core> "sdmc:<rom>"`.
- A generated title ID is `01` + 11 random hex digits + `000`.
- Firmware folders are named `Firmware <version>`, using `fw_nca_mapping.csv`.
- Saves are extracted to `extracted_save_<date>/<UserID>/<TitleID>/<Timestamp> <SaveID>/`.

## Deliberate differences

- No empty trailing part when a file's size is an exact multiple of the part size. SAK created one.
- Folder-style `00` merges look for later parts next to the first part, not in the working directory. That was a SAK bug.
- The forwarder checks that the title ID is hex and that the icon is a 256×256 JPEG before calling hacbrewpack.
- FAT32 formatting:
  - It works only on external physical disks.
  - You must repeat the disk identifier to confirm.
  - The GUI also asks for confirmation.
- Split NSP folders need the FAT "archive bit" for the Switch to read them as one file.
  - The port tries `chflags noarch`, which is how macOS msdosfs exposes `ATTR_ARCHIVE`. This only matters when you split directly onto the SD card.
  - If you split on APFS and copy the folder over, fix the bit on the console with Hekate → Tools → Arch bit • RCM • Touch → Fix Archive Bit.

## What has been verified

- **Unit and integration tests** (`python3 -m unittest discover tests`, 24 tests): split/merge round trips, trim, naming rules, cancel cleanup, the NACP encoding, the forwarder tree, the firmware map and save metadata parsing. squirrel and hacbrewpack were replaced by fake scripts in these tests. They check the argument lists, the keys hand-off, lower-casing of input extensions and how output files are collected.
- **Real tools with a fake keys file:** squirrel, hactool, hacbrewpack and savex each received the port's arguments and keys and stopped at key loading or input validation with a clean error. No temporary files were left behind.
- **Live on macOS arm64:**
  - A 4 GiB-plus XCI was split into a part of exactly 0xFFFF0000 bytes plus the rest and merged back byte-identical.
  - `hactool`, `hacbrewpack` and `savex` build and run.
  - The GUI API was tested end-to-end.
  - The FAT32 command refuses the internal `disk0`.
- **Not verified:** any operation that needs real keys and real game or save files. That is conversion, patching, update merging, compression, firmware extraction, save extraction and forwarder NSP building. No keys or game data were available for testing.

## Layout

```
sak/            python package (cli.py, gui/, split.py, nacp.py, forwarder.py, ops.py, drives.py, tools.py)
sak/resources/  nro_fw_main(.npdm), logo png/gif, fw_nca_mapping.csv  (copied from SAK_64bit/bin)
savex/          LibHac save extractor (C#)
scripts/        setup.sh, build_native_tools.sh, setup_python_tools.sh, build_savex.sh
tools/          built tools (git-ignored)
tests/          unittest suite
```

Credits: kempa (SAK), SciresM (hactool), The-4n (hacBrewPack, 4NXCI, reNXpack), julesontheroad (NSC_Builder), nicoboss (nsz), Thealexbarney (LibHac), and the switchbrew team (nx-hbloader).
