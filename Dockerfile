# One image for the API and the worker (compose runs them with different commands).
FROM python:3.14.10-slim-trixie

# uv: the same version Tomo pins (tomo scripts/uv-pin.json)
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

# uid/gid 1000 (when the API listens on a Unix socket instead of TCP, give the socket's folder to the host user that
# runs the reverse proxy with this id)
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m -d /home/app app

WORKDIR /srv/app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 HF_HOME=/models/hf

# dependencies first (cached while only the code changes), exactly as locked
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY sql ./sql
RUN uv sync --frozen --no-dev
# TORCH=cu128 (compose.gpu.yml): the same PyTorch version built for NVIDIA GPUs instead of the locked CPU-only one
# (the CUDA libraries come with the wheel; the host needs the NVIDIA driver and the container toolkit)
ARG TORCH=cpu
RUN if [ "$TORCH" != "cpu" ]; then \
      v=$(/opt/venv/bin/python -c "import torch; print(torch.__version__.split('+')[0])") && \
      uv pip install --python /opt/venv/bin/python --reinstall-package torch "torch==$v" \
        --index-url "https://download.pytorch.org/whl/$TORCH" --extra-index-url https://pypi.org/simple; \
    fi

# the folders compose mounts named volumes on: a new volume takes this owner, so the app (uid 1000) can write there
RUN mkdir -p /data/files /models/hf && chown -R 1000:1000 /data /models

USER 1000:1000
# the API: one process (the embedding model is loaded once; requests run concurrently inside it). It listens where
# GUNICORN_CMD_ARGS says: TCP port 8000 in the container by default (compose.yml publishes it on 127.0.0.1 only),
# or e.g. "--bind unix:/run/api/api.sock" for a Unix socket. A reverse proxy is the only client, so its
# X-Forwarded-For is trusted (set FORWARDED_ALLOW_IPS to the proxy's address if the port is reachable otherwise).
ENV FORWARDED_ALLOW_IPS=* GUNICORN_CMD_ARGS="--bind 0.0.0.0:8000"
EXPOSE 8000
CMD ["gunicorn", "app.main:app", "-k", "uvicorn_worker.UvicornWorker", "--umask", "0o007", "--workers", "1", \
     "--timeout", "60", "--graceful-timeout", "20", "--access-logfile", "-"]
