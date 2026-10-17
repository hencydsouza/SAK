#!/usr/bin/env bash
# Sets up the Python tool layer for the SAK macOS port (idempotent; safe to re-run).
#   - venv:         SAK_mac/.venv            (Python >= 3.10; override with SAK_PYTHON=/path/to/python3)
#   - NSC_BUILDER:  SAK_mac/tools/nscb        (pinned commit, + scripts/patches/nscb-macos.patch)
#   - pip deps:     minimal set squirrel.py imports on macOS (no pywin32 / windows-curses / comtypes)
#   - nsz:          pinned, alternative compress/decompress CLI at SAK_mac/.venv/bin/nsz
# Never downloads or creates Switch keys, games or firmware.
set -euo pipefail

NSCB_REPO="https://github.com/julesontheroad/NSC_BUILDER"
NSCB_SHA="e9083e83383281bdd9e167d3141163dcc56b6710"   # master HEAD, 2021-11-15 (last upstream commit)
NSZ_VERSION="5.0.0"

# Direct runtime deps of squirrel.py and the lib/ + Fs/ modules it imports at startup.
PIP_DEPS=(
  "pycryptodome==4.0.0"    # Crypto.Cipher (AES)
  "zstandard==0.25.0"      # NSZ/XCZ (de)compression
  "tqdm==4.70.1"
  "pykakasi==2.3.0"        # -roma (romanize)
  "urllib3==2.8.0"
  "requests==2.34.2"
  "chardet==7.6.0"
  "beautifulsoup4==4.15.0" # imported by lib/Title.py
  "pillow==13.0.0"         # imported by Fs modules (icons)
  "googletrans==4.0.2"     # imported at module level by Fs/Nca.py, lib/nutdb.py
)

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAK_MAC="$(cd "$HERE/.." && pwd)"
VENV="$SAK_MAC/.venv"
NSCB_DIR="$SAK_MAC/tools/nscb"
PATCH="$HERE/patches/nscb-macos.patch"

log() { printf '[setup_python_tools] %s\n' "$*"; }
die() { printf '[setup_python_tools] ERROR: %s\n' "$*" >&2; exit 1; }

py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

# ---------------------------------------------------------------- 1. Python
pick_python() {
  if [[ -n "${SAK_PYTHON:-}" ]]; then
    py_ok "$SAK_PYTHON" || die "SAK_PYTHON=$SAK_PYTHON is not Python >= 3.10"
    echo "$SAK_PYTHON"; return
  fi
  local c p
  for c in python3.12 python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.13 python3 /opt/homebrew/bin/python3; do
    p="$(command -v "$c" 2>/dev/null || true)"
    [[ -n "$p" ]] && py_ok "$p" && { echo "$p"; return; }
  done
  die "no python3 >= 3.10 found (brew install python@3.12, or set SAK_PYTHON)"
}

if [[ -x "$VENV/bin/python" ]] && py_ok "$VENV/bin/python"; then
  log "venv exists: $VENV ($("$VENV/bin/python" -V))"
else
  [[ -e "$VENV" ]] && { log "removing unusable venv $VENV"; rm -rf "$VENV"; }
  PY="$(pick_python)"
  log "creating venv with $PY ($("$PY" -V))"
  "$PY" -m venv "$VENV"
fi

# ---------------------------------------------------------------- 2. NSC_BUILDER
mkdir -p "$SAK_MAC/tools"
if [[ ! -d "$NSCB_DIR/.git" ]]; then
  [[ -e "$NSCB_DIR" ]] && die "$NSCB_DIR exists but is not a git checkout; remove it and re-run"
  log "fetching NSC_BUILDER @ $NSCB_SHA"
  git init -q "$NSCB_DIR"
  git -C "$NSCB_DIR" remote add origin "$NSCB_REPO"
fi
if [[ "$(git -C "$NSCB_DIR" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$NSCB_SHA" ]]; then
  git -C "$NSCB_DIR" fetch -q --depth 1 origin "$NSCB_SHA"
  git -C "$NSCB_DIR" checkout -q --detach "$NSCB_SHA"
fi
log "NSC_BUILDER at $(git -C "$NSCB_DIR" rev-parse HEAD)"

# ---------------------------------------------------------------- 3. macOS patch
if git -C "$NSCB_DIR" apply --reverse --check "$PATCH" 2>/dev/null; then
  log "patch already applied: $(basename "$PATCH")"
elif git -C "$NSCB_DIR" apply --check "$PATCH" 2>/dev/null; then
  git -C "$NSCB_DIR" apply "$PATCH"
  log "applied $(basename "$PATCH")"
else
  die "cannot apply $PATCH (local edits in $NSCB_DIR?). Inspect with: git -C '$NSCB_DIR' status"
fi

# ---------------------------------------------------------------- 4. pip deps + nsz
log "installing pip deps"
"$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install -q --disable-pip-version-check "${PIP_DEPS[@]}" "nsz==$NSZ_VERSION"

# ---------------------------------------------------------------- 5. smoke test (no keys needed)
"$VENV/bin/python" -W ignore::SyntaxWarning "$NSCB_DIR/py/ztools/squirrel.py" -h >/dev/null \
  || die "squirrel.py -h failed"
"$VENV/bin/nsz" --help >/dev/null || die "nsz --help failed"

KEYS_AT="$NSCB_DIR/py/ztools/keys.txt"
log "OK. squirrel: $VENV/bin/python $NSCB_DIR/py/ztools/squirrel.py"
log "    nsz:      $VENV/bin/nsz"
if [[ ! -f "$KEYS_AT" && ! -f "$HOME/.switch/prod.keys" && -z "${NSCB_KEYS_FILE:-}" ]]; then
  log "NOTE: no keys found. Supply your own keys.txt (see tools/README_TOOLS_PY.md)."
fi
