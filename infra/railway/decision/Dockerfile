# syntax=docker/dockerfile:1
# Decision Service for Railway. Same build as decision/Dockerfile (keep the Go and base images in step), but the
# context is the repository root so the policy files compose bind-mounts are baked in (Railway has no bind mounts).
FROM golang:1.27-alpine AS build
WORKDIR /src
COPY decision/go.mod decision/go.sum ./
RUN go mod download
COPY decision/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /out/decision .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/decision /decision
COPY config/decision/rules.yaml /etc/decision/rules.yaml
COPY config/routing.yaml /etc/aios/routing.yaml
COPY agents/ /etc/aios/agents/
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/decision"]
