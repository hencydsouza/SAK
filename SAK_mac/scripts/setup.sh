#!/usr/bin/env bash
# One-shot setup: build/install every external tool SAK for macOS drives, then run `sak doctor`.
#   hactool + hacbrewpack  -> scripts/build_native_tools.sh   (needs Xcode Command Line Tools)
#   NSC_Builder + nsz      -> scripts/setup_python_tools.sh   (needs python3 >= 3.10)
#   savex (LibHac)         -> scripts/build_savex.sh          (needs .NET SDK: brew install --cask dotnet-sdk)
# Set SKIP_SAVEX=1 to skip the .NET part (only "Extract savegames" needs it).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

"$SCRIPT_DIR/build_native_tools.sh"
"$SCRIPT_DIR/setup_python_tools.sh"
if [[ "${SKIP_SAVEX:-0}" != 1 ]]; then
  if command -v dotnet >/dev/null 2>&1 || [[ -x /opt/homebrew/bin/dotnet ]]; then
    "$SCRIPT_DIR/build_savex.sh"
  else
    echo "!! .NET SDK not found - skipping savex (save extraction). Install: brew install --cask dotnet-sdk" >&2
  fi
fi
mkdir -p "$ROOT/keys"
chmod +x "$ROOT/sak.command"
"$ROOT/sak.command" doctor || true
echo
echo "Put your own prod.keys into $ROOT/keys/ (or ~/.switch/), then run: $ROOT/sak.command"
