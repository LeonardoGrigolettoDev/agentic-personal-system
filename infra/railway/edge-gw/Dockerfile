# syntax=docker/dockerfile:1
# Tailscale gateway (tag:aios-cloud) for Railway: static tailscale binaries from the pinned official image on
# Alpine with socat for the TCP relays. Build context: repository root.
FROM tailscale/tailscale:v1.102.5@sha256:c507f3a2a6ab1cabd8d809b98edeb41edbd5c3fb6ad9632ffd098b4c7d0b4065 AS tailscale

FROM alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
RUN apk add --no-cache ca-certificates socat
COPY --from=tailscale /usr/local/bin/tailscale /usr/local/bin/tailscaled /usr/local/bin/
COPY --chmod=0755 infra/railway/edge-gw/entrypoint.sh /usr/local/bin/edge-gw
ENTRYPOINT ["/usr/local/bin/edge-gw"]
