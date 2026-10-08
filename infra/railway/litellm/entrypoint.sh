#!/bin/sh
# Render /opt/aios/config/litellm/config.yaml, then hand over to the upstream entrypoint with the CMD args.
# render-litellm-config.py keeps a deployment only when its os.environ/<KEY> is non-empty in <root>/.env, so the
# file written here holds "NAME=1" markers for the keys present in the environment, never the secrets.
# AIOS_MAINTENANCE=1 stops before any of this: LiteLLM's startup runs its Prisma migrations and writes the
# master-key hash, so before the cutover copy (RUNBOOK §9.2) it must not touch the cloud `litellm` database.
set -eu
# shellcheck source-path=SCRIPTDIR source=../lib/maintenance.sh
. "${AIOS_RAILWAY_LIB:-/opt/aios/railway}/maintenance.sh"
aios_maintenance_hold python3 /health/liveliness "${PORT:-4000}" litellm-entrypoint
AIOS_ROOT="${AIOS_ROOT:-/opt/aios}" python3 - <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(os.environ["AIOS_ROOT"])
template = (root / "config/litellm/config.template.yaml").read_text(encoding="utf-8")
names = set(re.findall(r"os\.environ/([A-Z0-9_]+)", template)) | {"LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"}
marker = root / ".env"
marker.write_text("".join(f"{n}=1\n" for n in sorted(names) if os.environ.get(n, "").strip()), encoding="utf-8")
try:
    subprocess.run([sys.executable, str(root / "infra/scripts/render-litellm-config.py")], check=True, timeout=60)
finally:
    marker.unlink(missing_ok=True)
PY
exec "${LITELLM_ENTRYPOINT:-/app/docker/prod_entrypoint.sh}" "$@"
