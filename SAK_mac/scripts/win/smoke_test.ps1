# Smoke test for the Windows build, run on a real Windows x64 machine (CI or by hand):
#
#   powershell -ExecutionPolicy Bypass -File smoke_test.ps1 -Sak C:\path\to\SAK [-Tests C:\path\to\tests]
#
# Needs no keys or game files. Checks: every bundled program starts, the Python packages
# load, squirrel runs through the embeddable interpreter, the archive bit is set, and the
# GUI started by SAK.exe answers with platform "windows". -Tests runs the unit tests with
# the bundled interpreter too.
param(
    [Parameter(Mandatory = $true)][string]$Sak,
    [string]$Tests = ""
)
$ErrorActionPreference = "Stop"
$Sak = (Resolve-Path $Sak).Path
$py = Join-Path $Sak "python\python.exe"
$failures = 0

function Check($name, [scriptblock]$body) {
    try {
        & $body
        Write-Host "[pass] $name"
    } catch {
        $script:failures++
        Write-Host "[FAIL] $name : $($_.Exception.Message)"
    }
}

function Run-Capture($exe, [string[]]$argv) {
    # merged stdout/stderr as one string; native exit codes are not treated as errors here
    $out = & $exe @argv 2>&1 | Out-String
    return $out
}

Check "doctor finds every tool" {
    $out = Run-Capture (Join-Path $Sak "sak.cmd") @("doctor")
    foreach ($t in "hactool", "hacbrewpack", "savex", "guiformat", "squirrel") {
        if ($out -notmatch "\[ok\]\s+$t") { throw "doctor: $t not ok`n$out" }
    }
}

Check "hactool.exe runs" {
    $out = Run-Capture (Join-Path $Sak "tools\bin\hactool.exe") @()
    if ($out -notmatch "Usage") { throw $out }
}

Check "hacbrewpack.exe runs" {
    $out = Run-Capture (Join-Path $Sak "tools\bin\hacbrewpack.exe") @("--help")
    if ($out -notmatch "(?i)usage|hacbrewpack") { throw $out }
}

Check "savex.exe runs" {
    $out = Run-Capture (Join-Path $Sak "tools\bin\savex.exe") @()
    if ($out -notmatch "(?i)usage|savex") { throw $out }
}

Check "Python packages load (native wheels)" {
    $out = Run-Capture $py @("-c", "import Crypto.Cipher.AES, zstandard, PIL.Image, bs4, googletrans, pykakasi, tqdm, requests, chardet; print('imports ok')")
    if ($out -notmatch "imports ok") { throw $out }
}

Check "squirrel runs under the embeddable interpreter" {
    $squirrel = Join-Path $Sak "tools\nscb\py\ztools\squirrel.py"
    $out = Run-Capture $py @("-W", "ignore::SyntaxWarning", "-m", "sak.squirrel_run", $squirrel, "-h")
    if ($out -notmatch "usage: squirrel.py") { throw $out }
}

Check "squirrel imports its Fs package (sibling modules)" {
    $ztools = Join-Path $Sak "tools\nscb\py\ztools"
    $probe = Join-Path $ztools "_smoke_probe.py"
    Set-Content -Path $probe -Value "import os, sys`nos.chdir(os.path.dirname(os.path.abspath(__file__)))`nsys.path.insert(0, 'lib')`nimport Fs, win32api, win32con`nprint('fs ok')"
    try {
        $out = Run-Capture $py @("-m", "sak.squirrel_run", $probe)
        if ($out -notmatch "fs ok|keys.txt missing") { throw $out }
    } finally { Remove-Item $probe -ErrorAction SilentlyContinue }
}

Check "archive bit on a split folder" {
    $dir = Join-Path $env:TEMP ("sak_smoke_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $dir | Out-Null
    try {
        & attrib -A $dir
        $out = Run-Capture $py @("-c", "import sys; from pathlib import Path; from sak import split; print('set', split.set_archive_bit(Path(sys.argv[1])))", $dir)
        if ($out -notmatch "set True") { throw $out }
        if (-not ((Get-Item $dir).Attributes -band [IO.FileAttributes]::Archive)) { throw "archive attribute not set" }
    } finally { Remove-Item -Recurse -Force $dir }
}

Check "split + merge round trip (CLI)" {
    $dir = Join-Path $env:TEMP ("sak_split_" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $dir | Out-Null
    try {
        $code = "import os, sys; from pathlib import Path; import sak.split as s, sak.common as c`n" +
                "s.SPLIT_BYTES = 4 * 32768; s.CHUNKS_PER_PART = 4`n" +
                "d = Path(sys.argv[1]); src = d / 'Game.xci'; src.write_bytes(os.urandom(4 * 32768 * 2 + 1234))`n" +
                "j = c.Job(); parts = s.split_xci(src, d / 'out', j); merged = s.merge_xci(parts[0], d / 'm', j)`n" +
                "m = merged if isinstance(merged, Path) else merged[0]`n" +
                "print('parts', len(parts), 'equal', m.read_bytes() == src.read_bytes())"
        $out = Run-Capture $py @("-c", $code, $dir)
        if ($out -notmatch "parts 3 equal True") { throw $out }
    } finally { Remove-Item -Recurse -Force $dir }
}

Check "removable drive listing" {
    $out = Run-Capture $py @("-c", "from sak import drives; print('drives', drives.external_disks())")
    if ($out -notmatch "^drives \[") { throw $out }
}

Check "SAK.exe starts the GUI (platform windows)" {
    $port = Get-Random -Minimum 20000 -Maximum 40000
    $before = @(Get-Process pythonw -ErrorAction SilentlyContinue | ForEach-Object Id)
    Start-Process -FilePath (Join-Path $Sak "SAK.exe") -ArgumentList @("--no-browser", "--port", "$port")
    try {
        $html = $null
        for ($i = 0; $i -lt 60 -and -not $html; $i++) {
            Start-Sleep -Milliseconds 500
            try { $html = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:$port/").Content } catch {}
        }
        if (-not $html) { throw "GUI did not answer on port $port; log: $(Get-Content (Join-Path $Sak 'logs\sak.log') -Raw -ErrorAction SilentlyContinue)" }
        $token = [regex]::Match($html, '[A-Za-z0-9_-]{30,}').Value
        $state = Invoke-RestMethod -UseBasicParsing "http://127.0.0.1:$port/api/state" -Headers @{ "X-SAK-Token" = $token }
        if ($state.platform -ne "windows") { throw "platform = $($state.platform)" }
        foreach ($t in "hactool", "hacbrewpack", "savex", "guiformat", "squirrel") {
            if (-not $state.tools.$t) { throw "GUI reports $t missing" }
        }
    } finally {
        Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | Stop-Process -Force
    }
}

if ($Tests) {
    Check "unit tests with the bundled interpreter" {
        $dest = Join-Path $Sak "tests"
        Copy-Item -Recurse -Force $Tests $dest
        try {
            Push-Location $Sak
            $out = Run-Capture $py @("-m", "unittest", "-v",
                "tests.test_sak.SplitMergeTests", "tests.test_sak.NacpTests", "tests.test_sak.GuiTests",
                "tests.test_sak.WindowsLogicTests", "tests.test_sak.ExtractTests.test_read_container_pfs0",
                "tests.test_sak.ExtractTests.test_parse_cnmt", "tests.test_sak.ExtractTests.test_ticket_titlekey",
                "tests.test_sak.ExtractTests.test_sanitized_keys_drop_malformed_and_cleanup")
            Write-Host $out
            if ($out -notmatch "(?m)^OK") { throw "unit tests failed" }
        } finally { Pop-Location; Remove-Item -Recurse -Force $dest }
    }
}

if ($failures) { Write-Host "$failures check(s) failed"; exit 1 }
Write-Host "all checks passed"
