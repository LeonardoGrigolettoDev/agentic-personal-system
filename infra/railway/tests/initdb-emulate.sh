#!/usr/bin/env bash
# Runs a /docker-entrypoint-initdb.d directory the way the docker-library postgres entrypoint does
# (docker_process_init_files in 17/trixie/docker-entrypoint.sh): files in glob order; an executable *.sh runs as
# a child with the container environment, a non-executable one is sourced into this shell (so its `unset` reaches
# the later scripts), and *.sql goes through psql with PGHOST/PGHOSTADDR cleared. run.sh drives it with a psql
# stub, integration.sh with a real server that listens on the Unix socket only, like the first-boot server.
set -Eeo pipefail
dir=${1:?usage: initdb-emulate.sh <initdb dir>}
for f in "$dir"/*; do
  case "$f" in
    *.sh)
      if [[ -x "$f" ]]; then
        echo "initdb-emulate: running $f"
        "$f"
      else
        echo "initdb-emulate: sourcing $f"
        # shellcheck disable=SC1090
        . "$f"
      fi
      ;;
    *.sql)
      echo "initdb-emulate: running $f"
      PGHOST='' PGHOSTADDR='' psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER:-postgres}" --no-password --no-psqlrc \
        --dbname "${POSTGRES_DB:-postgres}" -f "$f"
      ;;
    *) echo "initdb-emulate: ignoring $f" ;;
  esac
done
