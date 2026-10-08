# syntax=docker/dockerfile:1
# bench CLI image: Python + ssh client. Tasks/fixtures are baked in and normally overridden by read-only mounts.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH BENCH_HOME=/opt/aios/bench
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv==0.12.23 \
 && useradd --uid 1000 --create-home bench
WORKDIR /opt/aios/bench
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev
COPY tasks ./tasks
COPY fixtures ./fixtures
USER bench
ENTRYPOINT ["bench"]
CMD ["--help"]
