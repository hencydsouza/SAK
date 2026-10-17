#!/usr/bin/env bash
# Build the savex save-data extractor (LibHac-based replacement for hactoolnet -t save)
# and install it as SAK_mac/tools/bin/savex.
#
# Default: framework-dependent publish into tools/lib/savex/ plus a small launcher
#          script at tools/bin/savex (needs a .NET 10 runtime at run time).
# SAVEX_SELF_CONTAINED=1: self-contained single-file apphost copied directly to
#          tools/bin/savex (no runtime needed, ~70 MB).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJ="$ROOT/savex/savex.csproj"
BIN_DIR="$ROOT/tools/bin"
LIB_DIR="$ROOT/tools/lib/savex"
case "$(uname -m)" in arm64) RID="osx-arm64" ;; *) RID="osx-x64" ;; esac

find_dotnet() {
  if [[ -n "${DOTNET:-}" && -x "${DOTNET}" ]]; then echo "$DOTNET"; return; fi
  if command -v dotnet >/dev/null 2>&1; then command -v dotnet; return; fi
  for c in /opt/homebrew/bin/dotnet /usr/local/share/dotnet/dotnet /usr/local/bin/dotnet "$HOME/.dotnet/dotnet"; do
    [[ -x "$c" ]] && { echo "$c"; return; }
  done
  echo "error: dotnet SDK not found (set DOTNET=/path/to/dotnet)" >&2
  exit 1
}
DOTNET_BIN="$(find_dotnet)"
echo "Using dotnet: $DOTNET_BIN ($("$DOTNET_BIN" --version))"

mkdir -p "$BIN_DIR"

if [[ "${SAVEX_SELF_CONTAINED:-0}" == "1" ]]; then
  OUT="$ROOT/savex/bin/publish-sc"
  rm -rf "$OUT"
  "$DOTNET_BIN" publish "$PROJ" -c Release -r "$RID" --self-contained true \
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
    -o "$OUT" --nologo
  rm -f "$BIN_DIR/savex"
  cp "$OUT/savex" "$BIN_DIR/savex"
  chmod +x "$BIN_DIR/savex"
else
  rm -rf "$LIB_DIR"
  mkdir -p "$LIB_DIR"
  "$DOTNET_BIN" publish "$PROJ" -c Release -r "$RID" --self-contained false \
    -o "$LIB_DIR" --nologo
  rm -f "$BIN_DIR/savex"
  cat > "$BIN_DIR/savex" <<'LAUNCHER'
#!/usr/bin/env bash
# Launcher for savex (framework-dependent .NET build in ../lib/savex).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DLL="$HERE/../lib/savex/savex.dll"
if [[ ! -f "$DLL" ]]; then
  echo "savex: error: $DLL missing; run SAK_mac/scripts/build_savex.sh" >&2
  exit 127
fi
DOTNET_BIN="${DOTNET:-}"
if [[ -z "$DOTNET_BIN" ]]; then
  if command -v dotnet >/dev/null 2>&1; then DOTNET_BIN="$(command -v dotnet)"
  else
    for c in /opt/homebrew/bin/dotnet /usr/local/share/dotnet/dotnet /usr/local/bin/dotnet "$HOME/.dotnet/dotnet"; do
      [[ -x "$c" ]] && { DOTNET_BIN="$c"; break; }
    done
  fi
fi
if [[ -z "$DOTNET_BIN" ]]; then
  echo "savex: error: .NET runtime not found (install .NET 10 or set DOTNET=/path/to/dotnet)" >&2
  exit 127
fi
exec "$DOTNET_BIN" "$DLL" "$@"
LAUNCHER
  chmod +x "$BIN_DIR/savex"
fi

echo "Installed: $BIN_DIR/savex"
"$BIN_DIR/savex" --help >/dev/null && echo "Smoke test (--help): OK"
