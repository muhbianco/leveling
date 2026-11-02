# ---------- build ----------
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime ----------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=America/Sao_Paulo

COPY --from=builder /opt/venv /opt/venv

# Chromium do Playwright (com janela: sem o headless shell) e as libs dele; Xvfb + x11vnc + noVNC
# para a tela remota do painel; tini como PID 1 (recolhe os filhos do Chromium).
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc tini tzdata \
    && playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# uid 1001: dono de /root/leveling-secrets na hel1 (montado read-only) e do volume /data.
RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --create-home app \
    && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --chown=app:app app ./app

USER app

EXPOSE 8000

# Vivo = o supervisor e o Xvfb estão de pé. Jogo fora do ar não derruba o container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# Um worker só: o estado do robô (fase, backoff, tela remota) vive neste processo.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
