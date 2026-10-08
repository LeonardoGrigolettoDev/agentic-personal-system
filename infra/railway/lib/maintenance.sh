# shellcheck shell=sh
# Maintenance mode for the Railway start scripts (sourced, POSIX sh; copied to /opt/aios/railway/ in the litellm
# and hermes images). With AIOS_MAINTENANCE=1 the container comes up with its volume, `railway ssh` and
# `railway volume files`, but the application never starts: nothing writes the cloud databases and no Hermes
# gateway runs. RUNBOOK §9 uses it before the cutover copy and for the rollback export. The Railway health-check
# path answers 200, so the deployment goes Active and replaces the previous one instead of failing its check.
#
#   aios_maintenance_hold <python> <health-path> <port> <component>   # returns only when AIOS_MAINTENANCE != 1
aios_maintenance_hold() {
  [ "${AIOS_MAINTENANCE:-0}" = 1 ] || return 0
  printf '{"level":"warn","component":"%s","msg":"AIOS_MAINTENANCE=1: application not started; answering %s on :%s"}\n' \
    "$4" "$2" "$3" >&2
  exec "$1" -c '
import http.server
import sys

path, port = sys.argv[1], int(sys.argv[2])


class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path.split("?", 1)[0] == path
        body = b"maintenance\n" if ok else b"not found\n"
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("0.0.0.0", port), Health).serve_forever()
' "$2" "$3"
}
