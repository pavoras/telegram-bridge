# Build from the repository root: docker build -f relay/Dockerfile -t telegram-bridge-relay:<version> .
# Pin the base image by digest when deploying: --build-arg PYTHON_IMAGE=python:3.12-slim@sha256:...
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TG_DB_PATH=/data/relay.db \
    TG_LISTEN=0.0.0.0:8080

RUN groupadd --system --gid 10010 relay \
    && useradd --system --uid 10010 --gid relay --no-create-home --shell /usr/sbin/nologin relay \
    && mkdir /data && chown relay:relay /data

WORKDIR /app
COPY relay/ /app/relay/
COPY client/tg.py integrations/claude/SKILL.md integrations/claude/SETUP.md \
     integrations/agents/AGENT-INSTRUCTIONS.md /app/client-files/

USER relay
VOLUME ["/data"]
CMD ["python", "-m", "relay.main"]
