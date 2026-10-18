Switch Army Knife (SAK) - Windows x64 port
==========================================

A rebuild of kempa's Switch Army Knife with a web interface, the same tools as the macOS
port, and an "Extract exefs / romfs" page with a LASRecomp preset.

Start
-----
1. Extract this zip somewhere writable (not Program Files), e.g. C:\SAK.
2. Put your own prod.keys (or keys.dat) into the "keys" folder.
3. Double-click SAK.exe. Your browser opens the SAK page (it only answers on this PC).
   Output goes to %USERPROFILE%\SAK unless you change it under "Settings & status".

Windows may warn about an unknown publisher (the exe is not code-signed): choose
"More info" -> "Run anyway".

Command line
------------
    sak.cmd doctor                      check keys and tools
    sak.cmd split-xci "D:\Game.xci"
    sak.cmd -o C:\LASRecomp extract --lasrecomp "D:\Link's Awakening [v0].nsp"
    sak.cmd -o D:\TOTK extract --totk --exefs-only "D:\TOTK [0100F2C0115B6000][v0].nsp"
    sak.cmd --help                      all commands

Notes
-----
- FAT32: "Open FAT32 Format tool" starts guiformat (the formatter the original SAK shipped).
  Pick the SD card's drive letter there; 64K allocation units are recommended for the Switch.
- Split NSP folders get the archive bit set. If you copy a split folder to the SD card
  afterwards, fix the archive bit on the console (Hekate: "Fix Archive Bit").
- Logs from the GUI: logs\sak.log.
- Nothing here contains keys or game data; BUILD-INFO.txt lists every tool's source version.
