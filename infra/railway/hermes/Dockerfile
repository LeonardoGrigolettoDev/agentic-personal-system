# syntax=docker/dockerfile:1
# Hermes Agent for Railway: the pinned upstream image plus everything compose bind-mounts read-only
# (plugin, policy, SOULs, workflows, setup script, skills). State lives on the /opt/data volume.
# Build context: repository root. Keep the tag in step with HERMES_TAG in .env.example.
FROM nousresearch/hermes-agent:v2026.9.24@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7
COPY tools/hermes-plugin/aios/ /opt/aios/plugins/aios/
COPY config/hermes/ /opt/aios/config/
COPY agents/ /opt/aios/agents/
COPY workflows/ /opt/aios/workflows/
COPY infra/hermes/ /opt/aios/bin/
COPY skills/ /opt/shared-skills/
COPY infra/railway/hermes/rehost.py infra/railway/hermes/hermes_state.py infra/railway/lib/maintenance.sh \
     /opt/aios/railway/
COPY --chmod=0755 infra/railway/hermes/start.sh /opt/aios/railway/start.sh
# compose hostnames -> <service>.railway.internal (this image's copy only); the key dir is written at boot by start.sh
RUN find /opt/aios/plugins -name '__pycache__' -prune -exec rm -rf {} + \
 && /opt/hermes/.venv/bin/python /opt/aios/railway/rehost.py /opt/aios/config/config.yaml /opt/aios/config/profiles.yaml \
 && install -d -m 0700 -o hermes -g hermes /opt/aios/ssh
# The image ENTRYPOINT (s6 /init dispatcher) stays; main-wrapper.sh runs an executable CMD as the hermes user.
CMD ["/opt/aios/railway/start.sh"]
