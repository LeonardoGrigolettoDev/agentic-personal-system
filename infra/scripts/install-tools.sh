#!/usr/bin/env bash
# User-space toolchain for the host-side commands (make test, kb CLI, decision build): uv and Go under
# ~/.local, no sudo. Pinned versions, sha256-verified; idempotent. Same on Linux and in WSL2 (Ubuntu).
set -euo pipefail

UV_VERSION=0.12.23
UV_SHA256=9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6
GO_VERSION=1.27.1
GO_SHA256=63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445

[[ "$(uname -m)" == x86_64 ]] || { echo "only x86_64 checksums are pinned"; exit 1; }
[[ $EUID -ne 0 ]] || { echo "run as your normal user (installs into ~/.local)"; exit 1; }
BIN="$HOME/.local/bin"
OPT="$HOME/.local/opt"
mkdir -p "$BIN" "$OPT"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fetch() { # url sha256 dest
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --quiet - || { echo "checksum mismatch: $1"; exit 1; }
}

if [[ "$("$BIN/uv" --version 2>/dev/null)" == "uv $UV_VERSION"* ]]; then
  echo "uv $UV_VERSION: ok"
else
  fetch "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" \
    "$UV_SHA256" "$tmp/uv.tgz"
  tar -xzf "$tmp/uv.tgz" -C "$tmp"
  install -m 0755 "$tmp/uv-x86_64-unknown-linux-gnu/uv" "$tmp/uv-x86_64-unknown-linux-gnu/uvx" "$BIN/"
  echo "uv $UV_VERSION: installed"
fi

if [[ "$("$BIN/go" version 2>/dev/null)" == "go version go$GO_VERSION "* ]]; then
  echo "go $GO_VERSION: ok"
else
  fetch "https://go.dev/dl/go$GO_VERSION.linux-amd64.tar.gz" "$GO_SHA256" "$tmp/go.tgz"
  rm -rf "$OPT/go.new"
  mkdir -p "$OPT/go.new"
  tar -xzf "$tmp/go.tgz" -C "$OPT/go.new" --strip-components=1
  rm -rf "$OPT/go"
  mv "$OPT/go.new" "$OPT/go"
  ln -sfn "$OPT/go/bin/go" "$BIN/go"
  ln -sfn "$OPT/go/bin/gofmt" "$BIN/gofmt"
  echo "go $GO_VERSION: installed"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "add ~/.local/bin to PATH (Ubuntu's ~/.profile does it on the next login)" ;;
esac
