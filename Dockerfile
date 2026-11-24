
# =========================================================
# ESTAGIO 1 - NODE.JS 20
# =========================================================
FROM node:20-bookworm-slim AS node-runtime

# =========================================================
# ESTAGIO 2 - IMAGEM PRINCIPAL PYTHON + NODE
# =========================================================
FROM python:3.12-slim-bookworm

# ---------------------------------------------------------
# VARIAVEIS DE AMBIENTE
# ---------------------------------------------------------
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    HOME=/home/agent \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_CACHE=/tmp/.npm \
    GIT_TERMINAL_PROMPT=0

# ---------------------------------------------------------
# NODE.JS 20
# ---------------------------------------------------------
COPY --from=node-runtime /usr/local/ /usr/local/

# ---------------------------------------------------------
# DEPENDENCIAS DO SISTEMA
# ---------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       git \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------
# USUARIO E DIRETORIOS
# ---------------------------------------------------------
RUN groupadd -g 10001 agent \
    && useradd --system \
       --uid 10001 \
       --gid 10001 \
       --create-home \
       --home-dir /home/agent \
       --shell /usr/sbin/nologin agent \
    && mkdir -p \
       /app \
       /work \
       /scratch \
       /opt/kiln \
    && chown -R 10001:10001 \
       /app \
       /work \
       /scratch \
       /opt/kiln \
       /home/agent

# ---------------------------------------------------------
# PYTHON - DEPENDENCIAS
# ---------------------------------------------------------
WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --no-cache-dir \
    -r requirements.txt

# ---------------------------------------------------------
# PROJETO IA ASSISTANT
# ---------------------------------------------------------
COPY app/ ./app/
COPY worker/ ./worker/
COPY db/ ./db/

# --------------------------------------------------------
# PROJETO KILN
# --------------------------------------------------------
COPY app_kiln/ ./app_kiln/
COPY worker_kiln/ ./worker_kiln/

# ---------------------------------------------------------
# KILN - AMBIENTE DESCARTAVEL
# ---------------------------------------------------------
COPY kiln_smoke.py /opt/kiln/kiln_smoke.py

RUN chmod 0644 /opt/kiln/kiln_smoke.py \
    && chown -R 10001:10001 \
       /app \
       /opt/kiln
       
# ---------------------------------------------------------
# SEGURANCA
# ---------------------------------------------------------
USER 10001:10001

# ---------------------------------------------------------
# DIRETORIO DE EXECUCAO
# ---------------------------------------------------------
WORKDIR /app

# ---------------------------------------------------------
# API
# ---------------------------------------------------------
EXPOSE 8000

# Comando padrao: Agent API
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
