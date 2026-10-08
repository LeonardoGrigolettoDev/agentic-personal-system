# syntax=docker/dockerfile:1
# agent-edge media worker. Optional speaker diarization: --build-arg EDGE_EXTRAS=diarize
# (pyannote + CPU-only torch from download.pytorch.org/whl/cpu, about +1.3 GB installed, no CUDA).
FROM python:3.12-slim
ARG EDGE_EXTRAS=""
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_CACHE_DIR=/root/.cache/uv PATH=/opt/venv/bin:$PATH \
    PYANNOTE_METRICS_ENABLED=0
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.12.23 \
 && useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin app \
 && install -d -o 1000 -g 1000 /data/hf-cache
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project ${EDGE_EXTRAS:+--extra $EDGE_EXTRAS}
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev ${EDGE_EXTRAS:+--extra $EDGE_EXTRAS}
# Runtime: the root filesystem may be read-only (compose snippet). The user has no home directory, so generic
# caches (~/.cache, ~/.config) go to /tmp, and the pyannote models go to HF_HOME, a volume that survives
# restarts (an empty named volume inherits this directory's 1000:1000 owner).
ENV HOME=/tmp HF_HOME=/data/hf-cache
# uid 1000 = the host user that owns ./data/storage (bind mount); jobs live in memory -> one process only
USER 1000:1000
EXPOSE 8080
CMD ["uvicorn", "edge.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", \
     "--timeout-graceful-shutdown", "20"]
