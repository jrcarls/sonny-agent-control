# syntax=docker/dockerfile:1
# Uma imagem para web, worker e beat. Alvos: "dev" (código por bind mount) e "prod" (código dentro da imagem).

# --- CSS: compila o Tailwind a partir dos templates ---------------------------
FROM node:22-slim AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY static/css/input.css static/css/input.css
COPY templates templates
RUN npm run css:build

# --- Base: sistema, usuário sem privilégios e ferramentas ----------------------
FROM python:3.14-slim AS base
# O ambiente virtual fica fora de /app para o bind mount de desenvolvimento não escondê-lo
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"
# git e ssh: os agentes clonam os repositórios e enviam branches a partir do worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app /opt/venv /data/workspaces \
    && chown -R app:app /app /opt/venv /data
WORKDIR /app
USER app
COPY --chown=app:app pyproject.toml uv.lock ./
ENTRYPOINT ["/app/docker/entrypoint.sh"]

# --- Desenvolvimento: dependências de dev; o código vem do bind mount -----------
FROM base AS dev
RUN uv sync --frozen
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# --- Produção: sem dependências de dev, código e arquivos estáticos na imagem ---
FROM base AS prod
RUN uv sync --frozen --no-dev
COPY --chown=app:app . .
COPY --from=assets --chown=app:app /build/static/css/output.css static/css/output.css
# O collectstatic só precisa do settings carregar; a chave de verdade vem do ambiente ao rodar.
# O input.css é o fonte do Tailwind, não um arquivo servido, e o collectstatic não entende seus @import.
RUN SECRET_KEY=build DEBUG=False python manage.py collectstatic --noinput --ignore input.css
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
