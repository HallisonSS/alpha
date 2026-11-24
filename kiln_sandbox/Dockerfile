FROM python:3.12-slim-bookworm

WORKDIR /sandbox

COPY kiln_smoke.py .

CMD ["python", "kiln_smoke.py"]

# Construir usando a raiz como contexto:
# sudo docker build \
#        -f kiln_sandbox/Dockerfile \
#        -t kiln-sandbox:latest \
#        .
