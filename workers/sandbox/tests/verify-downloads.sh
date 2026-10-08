#!/usr/bin/env bash
# verify-downloads.sh [--full]
# Checks the artifacts pinned in workers/sandbox/Dockerfile: by default that every URL answers (curl -fsSLI)
# and the base image tag+digest exists; with --full it downloads each file and verifies its sha256, and
# checks that the pinned npm/PyPI versions exist. Exit 0 only when everything verified.
set -euo pipefail

here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
dockerfile=$here/../Dockerfile
full=0
if [[ ${1:-} == --full ]]; then full=1; fi

arg() { sed -n "s/^ARG $1=//p" "$dockerfile" | head -n1; }
fail=0
ok() { printf '  ok    %s\n' "$*"; }
bad() {
  printf '  FAIL  %s\n' "$*"
  fail=1
}

declare -A url sha
url[go]="https://go.dev/dl/go$(arg GO_VERSION).linux-amd64.tar.gz" sha[go]=$(arg GO_SHA256)
url[node]="https://nodejs.org/dist/v$(arg NODE_VERSION)/node-v$(arg NODE_VERSION)-linux-x64.tar.xz" sha[node]=$(arg NODE_SHA256)
url[uv]="https://github.com/astral-sh/uv/releases/download/$(arg UV_VERSION)/uv-x86_64-unknown-linux-gnu.tar.gz" sha[uv]=$(arg UV_SHA256)
url[duckdb]="https://github.com/duckdb/duckdb/releases/download/v$(arg DUCKDB_VERSION)/duckdb_cli-linux-amd64.gz" sha[duckdb]=$(arg DUCKDB_SHA256)

work=$(mktemp -d "${TMPDIR:-/tmp}/aios-sandbox-dl.XXXXXX")
trap 'rm -rf -- "$work"' EXIT

for name in go node uv duckdb; do
  [[ ${sha[$name]} =~ ^[0-9a-f]{64}$ ]] || bad "$name: pinned sha256 is malformed"
  if ((full)); then
    if curl -fsSL --retry 2 --max-time 600 -o "$work/$name" "${url[$name]}"; then
      actual=$(sha256sum "$work/$name" | cut -d' ' -f1)
      if [[ $actual == "${sha[$name]}" ]]; then
        ok "$name sha256 ${actual:0:16}… ${url[$name]}"
      else
        bad "$name sha256 mismatch: got $actual, pinned ${sha[$name]}"
      fi
      rm -f -- "$work/$name"
    else
      bad "$name download failed: ${url[$name]}"
    fi
  elif curl -fsSLI --retry 2 --max-time 60 -o /dev/null "${url[$name]}"; then
    ok "$name ${url[$name]}"
  else
    bad "$name not reachable: ${url[$name]}"
  fi
done

# Base image: tag and digest must both resolve on Docker Hub.
image=$(arg DEBIAN_IMAGE)
ref=${image#debian:}
tag=${ref%@*} digest=${ref#*@}
token=$(curl -fsS --max-time 30 "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/debian:pull" |
  python3 -I -c 'import json,sys; print(json.load(sys.stdin)["token"])') || token=""
accept='application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json'
for r in "$tag" "$digest"; do
  if [[ -n $token ]] && curl -fsSI --max-time 30 -o /dev/null -H "Authorization: Bearer $token" -H "Accept: $accept" \
    "https://registry-1.docker.io/v2/library/debian/manifests/$r"; then
    ok "debian:$r"
  else
    bad "debian:$r not found on Docker Hub"
  fi
done

if ((full)); then
  for pkg in "pnpm@$(arg PNPM_VERSION)" "eslint@$(arg ESLINT_VERSION)" "typescript@$(arg TYPESCRIPT_VERSION)"; do
    if curl -fsS --max-time 30 -o /dev/null "https://registry.npmjs.org/${pkg%@*}/${pkg##*@}"; then
      ok "npm $pkg"
    else
      bad "npm $pkg not published"
    fi
  done
  ruff=$(arg RUFF_VERSION)
  if curl -fsS --max-time 30 -o /dev/null "https://pypi.org/pypi/ruff/$ruff/json"; then
    ok "pypi ruff==$ruff"
  else
    bad "pypi ruff==$ruff not published"
  fi
fi

exit "$fail"
