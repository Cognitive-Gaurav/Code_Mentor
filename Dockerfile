FROM python:3.11-slim-bookworm

# Python dependencies are standard-library only. Install only the native tools
# used by code_executor.py and prlimit for its existing per-process limits.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        default-jdk-headless \
        g++ \
        gcc \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 codementor \
    && useradd --system --uid 10001 --gid codementor --no-create-home --shell /usr/sbin/nologin codementor

WORKDIR /app

# Allowlist runtime files; never copy .env, local databases, tests or backups.
COPY --chown=codementor:codementor server.py database.py analyzer.py code_executor.py tutor_engine.py feature5.py ./
COPY --chown=codementor:codementor static/ ./static/
COPY docker-entrypoint.sh /usr/local/bin/codementor-entrypoint
RUN chmod 0755 /usr/local/bin/codementor-entrypoint \
    && mkdir -p /tmp/codementor-sandboxes \
    && chown codementor:codementor /tmp/codementor-sandboxes

ENV HOST=0.0.0.0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEMENTOR_SANDBOX_DIR=/tmp/codementor-sandboxes

EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/codementor-entrypoint"]
CMD ["python", "server.py"]
