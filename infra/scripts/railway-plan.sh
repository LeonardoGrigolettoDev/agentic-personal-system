#!/usr/bin/env bash
# Offline view of the Railway control plane defined in infra/railway/ (ARCHITECTURE §20, §25). Never calls Railway.
#   railway-plan.sh                 services, build/deploy settings, variables, shared variables (.env status only)
#   railway-plan.sh --check         validate the manifest (exit 1 on errors)
#   railway-plan.sh --iac [--repo owner/name] [--branch main]   print .railway/railway.ts (Railway IaC)
set -euo pipefail
cd "$(dirname "$0")/../.."
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

cmd=plan
args=()
while (($#)); do
  case "$1" in
    --check) cmd=check ;;
    --iac) cmd=iac ;;
    --repo | --branch | --env-file) args+=("$1" "${2:?$1 needs a value}"); shift ;;
    -h | --help) sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
exec python3 infra/railway/lib/manifest.py "$cmd" ${args[@]+"${args[@]}"}
