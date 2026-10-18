#!/usr/bin/env bash
# Build the Windows x64 release of the SAK port, on macOS (or Linux), without a Windows machine.
#
#   SAK_mac/scripts/build_windows.sh            # -> SAK_mac/dist/SAK-windows-x64-<version>.zip
#   FORCE=1 SAK_mac/scripts/build_windows.sh    # rebuild the native tools too
#
# What goes into the zip (folder "SAK"):
#   SAK.exe                 GUI launcher (scripts/win/launcher.c, cross-compiled with zig)
#   sak.cmd                 command line: sak.cmd <command> ...
#   python\                 official Windows embeddable CPython + the packages squirrel needs
#   sak\                    this port's Python package (same code as the macOS build)
#   tools\bin\              hactool.exe, hacbrewpack.exe (pinned sources, zig), savex.exe (dotnet),
#                           guiformat.exe (the FAT32 formatter the original SAK shipped)
#   tools\nscb\py\          NSC_Builder ztools + zconfig at the pinned commit, with nscb-macos.patch
#                           (platform-neutral fixes) and ctypes stand-ins for pywin32
#   keys\                   put prod.keys here
#
# Pins (all verified below):
#   CPython embeddable 3.12.10 amd64   sha256 4acbed6d... (python.org md5 fe8ef205..., 11133606 bytes)
#   zig 0.14.1                         PyPI "ziglang" wheel
#   hactool     SciresM/hactool        1d64a83450e025622f3468c28fc4164dad2c5ef6
#   hacbrewpack rlaphoenix/hacBrewPack 745b16ecfc9ce055743067d200572204cb2aac6c (= The-4n v3.05)
#   Python packages                    WIN_PACKAGES below (same versions as the macOS venv)
#
# Needs: git, make, python3.12 (or the macOS .venv), dotnet SDK 8+ (savex), network access.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"              # SAK_mac
REPO="$(cd "$ROOT/.." && pwd)"                    # SAK repo (holds SAK_64bit/bin/guiformat.exe)
WORK="$ROOT/build/win"
FORCE="${FORCE:-0}"

PY_VER="3.12.10"
PY_ZIP="python-$PY_VER-embed-amd64.zip"
PY_URL="https://www.python.org/ftp/python/$PY_VER/$PY_ZIP"
PY_SHA256="4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"
ZIG_VERSION="0.14.1"
HACTOOL_URL="https://github.com/SciresM/hactool.git"
HACTOOL_SHA="1d64a83450e025622f3468c28fc4164dad2c5ef6"
HBP_URL="https://github.com/rlaphoenix/hacBrewPack.git"
HBP_SHA="745b16ecfc9ce055743067d200572204cb2aac6c"
GUIFORMAT_SHA256="$(shasum -a 256 "$REPO/SAK_64bit/bin/guiformat.exe" 2>/dev/null | cut -d' ' -f1 || true)"

# Everything squirrel imports on Windows, pinned to the macOS venv's versions. Installed with
# --no-deps: pip evaluates environment markers for the *build* host, so Windows-only
# dependencies (tqdm -> colorama) are listed explicitly. check_requirements() below re-checks
# every Requires-Dist with Windows markers.
WIN_PACKAGES=(
  pycryptodome==4.0.0 zstandard==0.25.0 tqdm==4.70.1 colorama==0.4.6
  pykakasi==2.3.0 jaconv==0.5.0 Deprecated==3.0.0 wrapt==2.5.1
  urllib3==2.8.0 requests==2.34.2 certifi==2026.7.22 charset-normalizer==3.5.2 idna==3.20 chardet==7.6.0
  beautifulsoup4==4.15.0 soupsieve==3.0.3 typing_extensions==4.16.0
  pillow==13.0.0
  googletrans==4.0.2 httpx==0.28.1 httpcore==1.0.9 h11==0.16.0 h2==4.4.1 hpack==4.2.0 hyperframe==6.1.0 anyio==4.15.1
)

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

host_python() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then echo "$ROOT/.venv/bin/python"; return; fi
  for p in python3.12 python3.13 python3; do command -v "$p" >/dev/null && { command -v "$p"; return; }; done
  die "python3 not found"
}

setup_zig() {
  local venv="$ROOT/build/winbuild-venv"
  if [[ ! -x "$venv/bin/python" ]]; then
    log "Creating build venv"
    "$(host_python)" -m venv "$venv"
  fi
  if [[ "$("$venv/bin/python" -m ziglang version 2>/dev/null || true)" != "$ZIG_VERSION" ]]; then
    log "Installing zig $ZIG_VERSION (PyPI ziglang)"
    "$venv/bin/python" -m pip install -q --disable-pip-version-check "ziglang==$ZIG_VERSION"
  fi
  mkdir -p "$WORK"
  # zig cc for x86_64-windows-gnu. mbedTLS's make rule compiles with -c but without -o, and zig
  # names Windows objects *.obj by default, so give such calls an explicit "<src>.o".
  cat > "$WORK/zcc" <<EOF
#!/bin/sh
has_c=0; has_o=0; src=""
for a in "\$@"; do case "\$a" in -c) has_c=1;; -o) has_o=1;; *.c) src="\$a";; esac; done
if [ \$has_c = 1 ] && [ \$has_o = 0 ] && [ -n "\$src" ]; then set -- "\$@" -o "\$(basename "\${src%.c}").o"; fi
exec "$venv/bin/python" -m ziglang cc -target x86_64-windows-gnu "\$@"
EOF
  printf '#!/bin/sh\nexec "%s/bin/python" -m ziglang ar "$@"\n' "$venv" > "$WORK/zar"
  printf '#!/bin/sh\nexec "%s/bin/python" -m ziglang ranlib "$@"\n' "$venv" > "$WORK/zranlib"
  chmod +x "$WORK/zcc" "$WORK/zar" "$WORK/zranlib"
}

# build_c_tool <name> <url> <sha> <binary-produced-by-make>
build_c_tool() {
  local name="$1" url="$2" sha="$3" produced="$4" dir="$WORK/src/$1" out="$WORK/bin/$1.exe"
  if [[ "$FORCE" != 1 && -f "$out" && "$(cat "$out.sha" 2>/dev/null)" == "$sha" ]]; then
    log "$name.exe up to date ($sha)"; return
  fi
  mkdir -p "$WORK/src" "$WORK/bin"
  [[ -d "$dir/.git" ]] || git clone --quiet "$url" "$dir"
  git -C "$dir" cat-file -e "${sha}^{commit}" 2>/dev/null || git -C "$dir" fetch --quiet origin
  git -C "$dir" checkout --quiet --force --detach "$sha"
  git -C "$dir" clean --quiet -fdx
  [[ "$(git -C "$dir" rev-parse HEAD)" == "$sha" ]] || die "checkout of $sha failed"
  # Same flags as upstream's config.mk.template; -Wno-date-time because zig makes __DATE__ an error.
  sed -e "s#^CC = .*#CC = $WORK/zcc#" -e 's/^CFLAGS = .*/& -Wno-date-time/' "$dir/config.mk.template" > "$dir/config.mk"
  echo 'EXEEXT = .exe' >> "$dir/config.mk"
  log "Building $name.exe"
  make -C "$dir" -j"$(sysctl -n hw.ncpu 2>/dev/null || nproc)" AR="$WORK/zar" RANLIB="$WORK/zranlib" \
    > "$WORK/$name.build.log" 2>&1 || { tail -20 "$WORK/$name.build.log"; die "$name build failed"; }
  grep -q -- '-D_FILE_OFFSET_BITS=64' "$WORK/$name.build.log" || die "$name: large-file flags missing"
  file "$dir/$produced" | grep -q 'PE32+ executable (console) x86-64' || die "$name: not a Windows x64 exe"
  cp "$dir/$produced" "$out"
  echo "$sha" > "$out.sha"
}

build_savex() {
  local out="$WORK/bin/savex.exe"
  if [[ "$FORCE" != 1 && -f "$out" && "$out" -nt "$ROOT/savex/Program.cs" ]]; then log "savex.exe up to date"; return; fi
  command -v dotnet >/dev/null || die "dotnet SDK not found (needed for savex.exe)"
  log "Publishing savex.exe (win-x64, self-contained single file)"
  dotnet publish "$ROOT/savex/savex.csproj" -c Release -r win-x64 --self-contained true \
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:EnableCompressionInSingleFile=true \
    -p:DebugType=none -o "$WORK/savex-publish" > "$WORK/savex.build.log" 2>&1 \
    || { tail -20 "$WORK/savex.build.log"; die "savex publish failed"; }
  mkdir -p "$WORK/bin"; cp "$WORK/savex-publish/savex.exe" "$out"
}

build_launcher() {
  log "Building SAK.exe launcher"
  "$WORK/zcc" -O2 -municode -Wall -Wextra -Werror -Wl,--subsystem,windows \
    "$SCRIPT_DIR/win/launcher.c" -lshell32 -o "$WORK/bin/SAK.exe"
  file "$WORK/bin/SAK.exe" | grep -q 'PE32+ executable (GUI) x86-64' || die "SAK.exe is not a Windows GUI exe"
  rm -f "$WORK/bin/SAK.pdb"
}

fetch_python() {
  mkdir -p "$WORK/dl"
  local zip="$WORK/dl/$PY_ZIP"
  if [[ ! -f "$zip" || "$(sha256 "$zip")" != "$PY_SHA256" ]]; then
    log "Downloading $PY_ZIP"
    "$(host_python)" -I -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$PY_URL" "$zip"
  fi
  [[ "$(sha256 "$zip")" == "$PY_SHA256" ]] || die "$PY_ZIP sha256 mismatch"
}

check_requirements() {
  # Every Requires-Dist of the bundled packages, evaluated for Windows, must be bundled too.
  "$ROOT/build/winbuild-venv/bin/python" -m pip install -q --disable-pip-version-check "packaging>=24" 2>/dev/null
  "$ROOT/build/winbuild-venv/bin/python" -I - "$1" <<'EOF'
import sys
from importlib.metadata import distributions
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

site = sys.argv[1]
env = {**default_environment(), "sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
       "platform_machine": "AMD64", "python_version": "3.12", "python_full_version": "3.12.10",
       "implementation_name": "cpython", "platform_python_implementation": "CPython", "extra": ""}
dists = {canonicalize_name(d.metadata["Name"]): d for d in distributions(path=[site])}
missing = []
for name, d in sorted(dists.items()):
    for raw in d.requires or []:
        req = Requirement(raw)
        if req.marker and not req.marker.evaluate(env):
            continue
        have = dists.get(canonicalize_name(req.name))
        if have is None or not req.specifier.contains(have.version, prereleases=True):
            missing.append(f"{name} needs {raw}")
if missing:
    sys.exit("missing Windows dependencies:\n  " + "\n  ".join(missing))
print(f"   {len(dists)} packages, all Windows requirements satisfied")
EOF
}

assemble() {
  local version stage app
  version="$("$(host_python)" -I -c "import sys; sys.path.insert(0, sys.argv[1]); import sak; print(sak.__version__)" "$ROOT")"
  stage="$WORK/stage"; app="$stage/SAK"
  rm -rf "$stage"; mkdir -p "$app"

  log "Python $PY_VER (embeddable) + packages"
  mkdir -p "$app/python"
  (cd "$app/python" && unzip -q "$WORK/dl/$PY_ZIP")
  local pth; pth="$(ls "$app/python"/python3*._pth)"
  # ._pth = the whole sys.path (isolated mode). '..' makes the bundle root importable (sak).
  printf 'python312.zip\n.\nLib\\site-packages\n..\n' > "$pth"
  "$(host_python)" -m pip install -q --disable-pip-version-check --no-deps --only-binary=:all: \
    --platform win_amd64 --python-version 3.12 --implementation cp --abi cp312 --abi abi3 --abi none \
    --target "$app/python/Lib/site-packages" "${WIN_PACKAGES[@]}"
  check_requirements "$app/python/Lib/site-packages"
  find "$app/python/Lib/site-packages" -name '__pycache__' -type d -prune -exec rm -rf {} +
  rm -rf "$app/python/Lib/site-packages/bin"

  log "SAK package"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT/sak" "$app/"

  log "Tools"
  mkdir -p "$app/tools/bin"
  cp "$WORK/bin/hactool.exe" "$WORK/bin/hacbrewpack.exe" "$WORK/bin/savex.exe" "$app/tools/bin/"
  cp "$REPO/SAK_64bit/bin/guiformat.exe" "$app/tools/bin/"
  [[ -d "$ROOT/tools/nscb/py/ztools" ]] || "$SCRIPT_DIR/setup_python_tools.sh"
  mkdir -p "$app/tools/nscb/py"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude 'keys.txt' --exclude '.git' \
    "$ROOT/tools/nscb/py/ztools" "$ROOT/tools/nscb/py/zconfig" "$app/tools/nscb/py/"
  cp "$SCRIPT_DIR/win/shims/win32api.py" "$SCRIPT_DIR/win/shims/win32con.py" "$app/tools/nscb/py/ztools/"

  log "Launchers and docs"
  cp "$WORK/bin/SAK.exe" "$app/"
  printf '@echo off\r\n"%%~dp0python\\python.exe" -m sak %%*\r\n' > "$app/sak.cmd"
  mkdir -p "$app/keys"
  printf 'Put your prod.keys (or keys.dat) in this folder.\r\n' > "$app/keys/PUT_PROD_KEYS_HERE.txt"
  sed 's/$/\r/' "$SCRIPT_DIR/win/README-windows.txt" > "$app/README.txt"
  { echo "SAK Windows x64 $version"; echo "built $(date -u +%Y-%m-%dT%H:%MZ)";
    echo "python $PY_VER sha256 $PY_SHA256"; echo "zig $ZIG_VERSION";
    echo "hactool $HACTOOL_SHA"; echo "hacbrewpack $HBP_SHA"; echo "guiformat.exe sha256 $GUIFORMAT_SHA256";
    echo "nscb $(git -C "$ROOT/tools/nscb" rev-parse HEAD 2>/dev/null || echo unknown)";
    echo; echo "sha256 of shipped executables:";
    (cd "$app" && find . -name '*.exe' | sort | while read -r f; do echo "$(sha256 "$f")  ${f#./}"; done)
  } | sed 's/$/\r/' > "$app/BUILD-INFO.txt"

  find "$app" -name '*.exe' -exec chmod 755 {} +   # zip keeps Unix modes; harmless on Windows

  # No key material may end up in a release (NSC_Builder ships an empty lib/title.keys placeholder).
  local leaked
  leaked="$(find "$app" -type f -size +0 \( -iname 'prod.keys' -o -iname 'keys.dat' -o -iname 'title.keys' \
    -o -iname 'keys.txt' -o -iname '*.keys' \))"
  [[ -z "$leaked" ]] || die "key files ended up in the bundle: $leaked"

  mkdir -p "$ROOT/dist"
  local zip="$ROOT/dist/SAK-windows-x64-$version.zip"
  rm -f "$zip"
  log "Zipping $(basename "$zip")"
  (cd "$stage" && zip -qr -X -9 "$zip" SAK)
  ls -la "$zip"
}

[[ -f "$REPO/SAK_64bit/bin/guiformat.exe" ]] || die "SAK_64bit/bin/guiformat.exe missing"
setup_zig
build_c_tool hactool "$HACTOOL_URL" "$HACTOOL_SHA" hactool.exe
build_c_tool hacbrewpack "$HBP_URL" "$HBP_SHA" hacbrewpack   # its Makefile has no $(EXEEXT)
build_savex
build_launcher
fetch_python
assemble
