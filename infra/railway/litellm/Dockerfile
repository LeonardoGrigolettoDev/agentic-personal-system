# syntax=docker/dockerfile:1
# LiteLLM for Railway: the image compose pins plus the repo's config template. config.yaml is rendered at boot
# (entrypoint.sh) from the providers whose keys are set as Railway variables, so adding a key is just a redeploy.
# Build context: repository root.
FROM ghcr.io/berriai/litellm:v1.104.0@sha256:625981c83410a3ea68eb0697590a57ec1d764d634514d54fa5db0591077ee839
COPY config/litellm/config.template.yaml /opt/aios/config/litellm/config.template.yaml
COPY infra/scripts/render-litellm-config.py /opt/aios/infra/scripts/render-litellm-config.py
COPY --chmod=0755 infra/railway/litellm/entrypoint.sh /opt/aios/entrypoint.sh
COPY infra/railway/lib/maintenance.sh /opt/aios/railway/maintenance.sh
EXPOSE 4000
ENTRYPOINT ["/opt/aios/entrypoint.sh"]
CMD ["--config=/opt/aios/config/litellm/config.yaml", "--port=4000", "--num_workers=1", "--max_requests_before_restart=10000"]
