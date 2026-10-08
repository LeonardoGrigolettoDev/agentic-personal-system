"""Preconditions a task declares in `requires`, probed once per `bench run` from where the agent works (the sandbox,
or this machine with --local). An unmet one skips the task, like missing generated media: a pipeline the agent
cannot reach says nothing about the model, so it must never become a failure in the ledger."""

import logging

from bench.workspace import Workspace, WorkspaceError

log = logging.getLogger("bench.preflight")

PROBE_TIMEOUT = 30
EDGE_PROBE_VALUE = "bench-probe"  # stand-in sent like Hermes sends the real key; the bench never holds EDGE_API_KEY

# The transcript_to_notes skill runs edge_pipeline.sh in the agent's terminal: it needs EDGE_API_KEY to arrive over
# ssh (sandbox sshd `AcceptEnv EDGE_API_KEY`) and the edge worker to answer (exit codes as edge_pipeline.sh).
EDGE_PROBE = (
    f'[ "${{EDGE_API_KEY:-}}" = {EDGE_PROBE_VALUE} ] '
    '|| { echo "EDGE_API_KEY does not reach the terminal (sandbox sshd needs AcceptEnv EDGE_API_KEY)"; exit 3; }; '
    'command -v curl >/dev/null || { echo "curl is not installed"; exit 2; }; '
    'url="${AIOS_EDGE_URL:-http://edge:8080}"; '
    'curl -fsS -m 10 -o /dev/null "$url/healthz" || { echo "edge unreachable at $url/healthz"; exit 5; }'
)
PROBES = {"edge": (EDGE_PROBE, {"EDGE_API_KEY": EDGE_PROBE_VALUE})}


class Preflight:
    def __init__(self, workspace: Workspace | None):
        self.workspace = workspace
        self._results: dict[str, str | None] = {}

    def unmet(self, requirements: tuple[str, ...]) -> str | None:
        """Why the first unmet requirement fails, or None when all hold. Each requirement is probed once."""
        for name in requirements:
            if name not in self._results:
                self._results[name] = self._probe(name)
                if self._results[name]:
                    log.warning("precondition unmet: tasks requiring it are skipped",
                                extra={"requirement": name, "reason": self._results[name]})
            if self._results[name]:
                return f"requires {name}: {self._results[name]}"
        return None

    def _probe(self, name: str) -> str | None:
        if name not in PROBES:
            return "unknown requirement"
        if self.workspace is None:
            return "no workspace (sandbox SSH or --local) to probe from"
        command, env = PROBES[name]
        try:
            res = self.workspace.probe(command, env, PROBE_TIMEOUT)
        except WorkspaceError as exc:
            return f"probe failed: {exc}"
        if res.exit_code != 0:
            return (res.output_tail.strip().splitlines() or [f"probe exited {res.exit_code}"])[-1][:300]
        return None
