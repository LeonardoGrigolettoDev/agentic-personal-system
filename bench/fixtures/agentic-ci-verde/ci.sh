#!/usr/bin/env bash
# Pipeline de CI do serviço de cupons (o mesmo que roda no GitHub Actions).
set -euo pipefail
cd "$(dirname "$0")"
naoformatados="$(gofmt -l .)"
if [ -n "$naoformatados" ]; then
  echo "gofmt: arquivos não formatados:"; echo "$naoformatados"; exit 1
fi
go vet ./...
go test -count=1 ./...
echo "CI verde"
