#!/usr/bin/env bash
# Build the native helper tools SAK shells out to (hactool, hacbrewpack) for macOS.
#
#   SAK_mac/scripts/build_native_tools.sh          # build (skips a tool if already built at the pinned SHA)
#   FORCE=1 SAK_mac/scripts/build_native_tools.sh  # force a clean rebuild
#
# Sources are cloned into SAK_mac/build/src/<name> and checked out at a pinned full SHA.
# Binaries are installed to SAK_mac/tools/bin/{hactool,hacbrewpack}.
#
# Dependencies: git, make, clang (Xcode Command Line Tools). No Homebrew packages are
# required: both projects vendor mbedTLS 2.6.1 and build it statically from source.
#
# Pins:
#   hactool     SciresM/hactool (official)            1d64a834... = upstream master HEAD
#               (2023-10-10, "kernel .crt0 in .rodata reacts only (support for 17.0.0 pk21)")
#   hacbrewpack rlaphoenix/hacBrewPack (mirror of the deleted The-4n/hacBrewPack)
#               745b16ec... = tag v3.05 = last The-4n commit (2019-10-13, "Updated sample
#               hbmenu loader"). The same SHA (hence byte-identical history/tree) is also the
#               HEAD of dragonflylee/hacBrewPack, an independent mirror.
#
# macOS portability: neither project needs source patches on macOS/arm64 with Apple clang.
# The only change is config.mk, generated from the upstream config.mk.template with
# CC=gcc -> CC=clang (macOS "gcc" is a clang shim anyway; this just makes it explicit).
# The template has no -static flag, so nothing else needs removing. If a future patch is
# ever needed, drop it at scripts/patches/<tool>-*.patch and it is applied with `git apply`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"          # SAK_mac
SRC_DIR="$ROOT/build/src"
BIN_DIR="$ROOT/tools/bin"
PATCH_DIR="$SCRIPT_DIR/patches"
FORCE="${FORCE:-0}"
JOBS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"

HACTOOL_URL="https://github.com/SciresM/hactool.git"
HACTOOL_SHA="1d64a83450e025622f3468c28fc4164dad2c5ef6"
HBP_URL="https://github.com/rlaphoenix/hacBrewPack.git"
HBP_SHA="745b16ecfc9ce055743067d200572204cb2aac6c"

log() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

check_prereqs() {
    [[ "$(uname -s)" == "Darwin" ]] || die "this script targets macOS"
    for t in git make clang; do
        command -v "$t" >/dev/null 2>&1 || die "'$t' not found. Install Xcode Command Line Tools: xcode-select --install"
    done
    # No Homebrew formulae are needed (mbedTLS is vendored). Intentionally do not brew install anything.
}

# fetch_pinned <dir> <url> <sha>: ensure <dir> is a pristine checkout of <sha>.
fetch_pinned() {
    local dir="$1" url="$2" sha="$3"
    if [[ ! -d "$dir/.git" ]]; then
        log "Cloning $url"
        git clone --quiet "$url" "$dir"
    fi
    if ! git -C "$dir" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        log "Fetching $sha"
        git -C "$dir" fetch --quiet origin || die "fetch failed for $url"
        git -C "$dir" cat-file -e "${sha}^{commit}" 2>/dev/null || die "commit $sha not found in $url"
    fi
    # Pristine tree at the pin (removes previous build outputs/config.mk; only touches build/src).
    git -C "$dir" checkout --quiet --force --detach "$sha"
    git -C "$dir" clean --quiet -fdx
    [[ "$(git -C "$dir" rev-parse HEAD)" == "$sha" ]] || die "checkout of $sha failed in $dir"
}

apply_patches() {
    local dir="$1" name="$2" p
    shopt -s nullglob
    for p in "$PATCH_DIR/$name"-*.patch; do
        log "Applying patch $(basename "$p")"
        git -C "$dir" apply --whitespace=nowarn "$p"
    done
    shopt -u nullglob
}

write_config_mk() {
    local dir="$1"
    [[ -f "$dir/config.mk.template" ]] || die "missing $dir/config.mk.template"
    sed -e 's/^CC = gcc$/CC = clang/' "$dir/config.mk.template" > "$dir/config.mk"
    grep -q '^CC = clang$' "$dir/config.mk" || die "failed to set CC=clang in $dir/config.mk"
}

# build_tool <name> <url> <sha> <make-output> <installed-name>
build_tool() {
    local name="$1" url="$2" sha="$3" out="$4" inst="$5"
    local dir="$SRC_DIR/$name" stamp="$BIN_DIR/.$inst.sha"
    if [[ "$FORCE" != "1" && -x "$BIN_DIR/$inst" && -f "$stamp" && "$(cat "$stamp")" == "$sha" ]]; then
        log "$inst already built at $sha (FORCE=1 to rebuild)"
        return
    fi
    fetch_pinned "$dir" "$url" "$sha"
    apply_patches "$dir" "$name"
    write_config_mk "$dir"
    log "Building $name @ $sha"
    make -C "$dir" -j"$JOBS" CC=clang >"$SRC_DIR/$name.build.log" 2>&1 \
        || { tail -n 40 "$SRC_DIR/$name.build.log" >&2; die "$name build failed (log: $SRC_DIR/$name.build.log)"; }
    [[ -x "$dir/$out" ]] || die "$name build produced no $out"
    install -m 0755 "$dir/$out" "$BIN_DIR/$inst"
    printf '%s\n' "$sha" > "$stamp"
    log "Installed $BIN_DIR/$inst"
}

verify() {
    # hactool has no --help; with no file argument it prints usage and exits non-zero.
    # (Capture first: pipefail + a non-zero exit would otherwise fail the check.)
    local out
    out="$("$BIN_DIR/hactool" 2>&1 || true)"
    grep -q 'hactool (c) SciresM' <<<"$out" || die "hactool did not print usage"
    # hacbrewpack --help prints usage and exits. (Do NOT run it with no args: that starts a
    # build and creates hacbrewpack_* directories in the cwd.)
    out="$("$BIN_DIR/hacbrewpack" --help 2>&1 || true)"
    grep -q '^Usage: hacbrewpack' <<<"$out" || die "hacbrewpack did not print usage"
    log "Verified: $(file -b "$BIN_DIR/hactool" | cut -d' ' -f1-3) hactool, hacbrewpack"
}

main() {
    check_prereqs
    mkdir -p "$SRC_DIR" "$BIN_DIR"
    build_tool hactool     "$HACTOOL_URL" "$HACTOOL_SHA" hactool     hactool
    build_tool hacBrewPack "$HBP_URL"     "$HBP_SHA"     hacbrewpack hacbrewpack
    verify
}

main "$@"
