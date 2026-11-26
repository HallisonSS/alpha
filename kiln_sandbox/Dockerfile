# Imagem DESCARTAVEL de sandbox (um container por comando). Sem segredos, sem docker, sem sudo.
# Build: docker build -t kiln-sandbox:latest kiln-sandbox/
# O comando vem sempre como argv de `docker run`; nao ha shell de login.

FROM node:20-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-pip python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10001 kiln && useradd -u 10001 -g 10001 -m -d /home/kiln -s /usr/sbin/nologin kiln \
 && mkdir -p /work /scratch /opt/kiln && chown 10001:10001 /work /scratch

COPY kiln_smoke.py /opt/kiln/kiln_smoke.py
RUN chmod 0644 /opt/kiln/kiln_smoke.py

ENV HOME=/tmp \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIT_TERMINAL_PROMPT=0

USER 10001:10001
WORKDIR /work

ENTRYPOINT []
CMD ["true"]
