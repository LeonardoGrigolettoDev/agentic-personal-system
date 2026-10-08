#!/usr/bin/env bash
# edge_pipeline.sh — run the edge media pipeline (audio/video -> transcript -> summary/notes/tasks
# -> knowledge ingest) for one storage:// source, scoped to a knowledge tenant.
#
#   edge_pipeline.sh [options] SOURCE TENANT          SOURCE = storage://media/input/<arquivo>
#   edge_pipeline.sh [options] --upload FILE TENANT   envia FILE via POST /upload e usa o URI devolvido
#   edge_pipeline.sh --job JOB_ID                     retoma a espera de um job já submetido
#
# Options: --domain D  --title T  --language pt  --no-ingest  --sync (one blocking request instead of
#          async job + polling)  --poll SECONDS (default 10)
# Prints the pipeline result JSON (txt_uri, srt_uri, summary_uri, summary, ingest, warnings) on stdout;
# progress goes to stderr.
#
# Env: EDGE_API_KEY (required, secret), AIOS_EDGE_URL (default http://edge:8080),
#      AIOS_EDGE_TIMEOUT total seconds to wait for the result (default 1800),
#      HERMES_TENANT (set by Hermes for Kanban workers): when present, TENANT must equal it.
# Exit codes: 0 ok, 2 usage, 3 missing EDGE_API_KEY, 4 edge returned an error / job failed,
#             5 edge unreachable, 6 timed out waiting (job keeps running: resume with --job),
#             7 TENANT differs from the session tenant (HERMES_TENANT).
set -eu

EDGE_URL=${AIOS_EDGE_URL:-http://edge:8080}
TIMEOUT=${AIOS_EDGE_TIMEOUT:-1800}
POLL=10
DOMAIN="" TITLE="" LANGUAGE="" UPLOAD="" JOB="" INGEST=true SYNC=false

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }
die() { code=$1; shift; echo "edge_pipeline: $*" >&2; exit "$code"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN=${2:-}; shift 2 ;;
    --title) TITLE=${2:-}; shift 2 ;;
    --language) LANGUAGE=${2:-}; shift 2 ;;
    --upload) UPLOAD=${2:-}; shift 2 ;;
    --job) JOB=${2:-}; shift 2 ;;
    --poll) POLL=${2:-}; shift 2 ;;
    --no-ingest) INGEST=false; shift ;;
    --sync) SYNC=true; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; die 2 "opção desconhecida: $1" ;;
    *) break ;;
  esac
done

SOURCE="" TENANT=""
if [ -n "$JOB" ]; then
  [ $# -eq 0 ] || die 2 "--job não aceita SOURCE/TENANT"
  case "$JOB" in *[!A-Za-z0-9_-]*) die 2 "job id inválido: $JOB" ;; esac
elif [ -n "$UPLOAD" ]; then
  [ $# -eq 1 ] || { usage >&2; die 2 "com --upload informe só o TENANT"; }
  TENANT=$1
  [ -f "$UPLOAD" ] || die 2 "arquivo não encontrado: $UPLOAD"
else
  [ $# -eq 2 ] || { usage >&2; die 2 "informe SOURCE e TENANT"; }
  SOURCE=$1 TENANT=$2
  case "$SOURCE" in storage://?*) ;; *) die 2 "SOURCE deve ser storage://... (use --upload para arquivo local): $SOURCE" ;; esac
fi
if [ -z "$JOB" ]; then
  case "$TENANT" in ""|*[!a-z0-9_-]*) die 2 "tenant inválido: '$TENANT' (ex.: nitro, pessoal, shared)" ;; esac
  # The aios tenant guard only sees tool arguments, not shell strings: enforce the session tenant here.
  if [ -n "${HERMES_TENANT:-}" ] && [ "$TENANT" != "$HERMES_TENANT" ]; then
    die 7 "tenant '$TENANT' difere do tenant da sessão '$HERMES_TENANT'; crie a tarefa no tenant certo"
  fi
fi
case "$DOMAIN" in *[!a-z0-9_-]*) die 2 "domínio inválido: '$DOMAIN'" ;; esac
case "$LANGUAGE" in *[!a-zA-Z-]*) die 2 "idioma inválido: '$LANGUAGE'" ;; esac
case "$TIMEOUT$POLL" in *[!0-9]*) die 2 "AIOS_EDGE_TIMEOUT e --poll devem ser inteiros (segundos)" ;; esac
[ "$POLL" -ge 1 ] || POLL=1
[ -n "${EDGE_API_KEY:-}" ] || die 3 "EDGE_API_KEY não está definido no ambiente do terminal"
command -v curl >/dev/null 2>&1 || die 2 "curl não encontrado"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/edge_pipeline.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
umask 077
# The bearer token goes through a 0600 header file, never through argv (visible in ps).
printf 'Authorization: Bearer %s\n' "$EDGE_API_KEY" > "$TMP/auth"

# call PATH MAX_TIME [curl args...] -> response body in $TMP/body, HTTP status on stdout
call() {
  path=$1 max_time=$2; shift 2
  set +e
  status=$(curl -sS -o "$TMP/body" -w '%{http_code}' --connect-timeout 10 --max-time "$max_time" \
    -H @"$TMP/auth" "$@" "$EDGE_URL$path")
  rc=$?
  set -e
  [ $rc -eq 0 ] || die 5 "falha ao chamar $EDGE_URL$path (curl exit $rc)"
  case "$status" in
    2??) printf '%s' "$status" ;;
    *) die 4 "HTTP $status em $path: $(head -c 2000 "$TMP/body")" ;;
  esac
}

# json_field KEY < json -> top-level string value ("" when absent/invalid); python3 when present
json_field() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
try:
    v = json.load(sys.stdin).get(sys.argv[1])
except Exception:
    v = None
print(v if isinstance(v, str) else "")' "$1"
  else
    grep -oE "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n 1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/' || true
  fi
}

print_result() {  # job JSON -> its "result" object (whole body when python3 is missing or JSON is odd)
  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    raw = json.dumps(d.get("result", d) if isinstance(d, dict) else d, ensure_ascii=False)
except ValueError:
    pass
print(raw)'
  else
    cat; echo
  fi
}

json_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\000-\037')"; }

wait_job() {
  job=$1 deadline=$(( $(date +%s) + TIMEOUT ))
  echo "edge_pipeline: job $job submetido; aguardando (até ${TIMEOUT}s)..." >&2
  while :; do
    call "/jobs/$job" 30 >/dev/null
    state=$(json_field status < "$TMP/body")
    case "$state" in
      succeeded) print_result < "$TMP/body"; return 0 ;;
      failed|cancelled) die 4 "job $job terminou como $state: $(head -c 2000 "$TMP/body")" ;;
    esac
    [ "$(date +%s)" -lt "$deadline" ] || die 6 "job $job ainda '$state' após ${TIMEOUT}s; retome com --job $job"
    sleep "$POLL"
  done
}

if [ -n "$JOB" ]; then
  wait_job "$JOB"
  exit 0
fi

if [ -n "$UPLOAD" ]; then
  call /upload 900 -F "file=@$UPLOAD" >/dev/null
  SOURCE=$(json_field uri < "$TMP/body")
  case "$SOURCE" in storage://?*) ;; *) die 4 "/upload não devolveu um storage:// URI: $(head -c 500 "$TMP/body")" ;; esac
  echo "edge_pipeline: enviado como $SOURCE" >&2
fi

body="{\"source\":$(json_str "$SOURCE"),\"tenant\":\"$TENANT\",\"ingest\":$INGEST"
[ -z "$DOMAIN" ] || body="$body,\"domain\":\"$DOMAIN\""
[ -z "$LANGUAGE" ] || body="$body,\"language\":\"$LANGUAGE\""
[ -z "$TITLE" ] || body="$body,\"title\":$(json_str "$TITLE")"

if $SYNC; then
  call /pipeline "$TIMEOUT" -H 'Content-Type: application/json' --data-binary "$body}" >/dev/null
  cat "$TMP/body"; echo
  exit 0
fi
call /pipeline 60 -H 'Content-Type: application/json' --data-binary "$body,\"async\":true}" >/dev/null
JOB=$(json_field job_id < "$TMP/body")
[ -n "$JOB" ] || die 4 "/pipeline não devolveu job_id: $(head -c 500 "$TMP/body")"
wait_job "$JOB"
