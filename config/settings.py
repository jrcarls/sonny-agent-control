import sys
from pathlib import Path

import environ
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, True),
    ALLOWED_HOSTS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env("DEBUG")
# Em produção (DEBUG=False) a chave é obrigatória: sem ela o sistema nem sobe
SECRET_KEY = env("SECRET_KEY", default="django-insecure-troque-em-producao") if DEBUG else env("SECRET_KEY")
# localhost entra sempre, para a verificação de saúde feita de dentro do próprio container
ALLOWED_HOSTS = [*env("ALLOWED_HOSTS"), "localhost", "127.0.0.1"]
# Endereços públicos do sistema com esquema, ex.: https://agentes.exemplo.com (exigido atrás de proxy com HTTPS)
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_cotton",
    "django_htmx",
    "allauth",
    "allauth.account",
    "tabler_icons",
    "orchestrator",
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

# Ferramentas só de desenvolvimento: ficam fora da imagem de produção e dos testes
# (a barra de depuração se recusa a rodar junto com o test runner)
DEV_TOOLS = DEBUG and "test" not in sys.argv
if DEV_TOOLS:
    INSTALLED_APPS += ["debug_toolbar", "django_browser_reload"]
    MIDDLEWARE.insert(2, "debug_toolbar.middleware.DebugToolbarMiddleware")
    MIDDLEWARE.append("django_browser_reload.middleware.BrowserReloadMiddleware")
    # Dentro do Docker o pedido não vem de 127.0.0.1, então a barra aparece sempre que DEBUG estiver ligado
    DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": lambda request: True}
if not DEBUG:
    # Atrás de um proxy que termina o HTTPS e repassa o esquema original neste cabeçalho
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Cookies só por HTTPS; desligue (SECURE_COOKIES=False) apenas para acessar produção por http
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env.bool("SECURE_COOKIES", default=not DEBUG)

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "orchestrator.context_processors.sidebar",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# Em desenvolvimento o Postgres e o Redis sobem com docker compose up -d
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://izzyone:izzyone@localhost:5432/izzyone"),
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/1")
CELERY_TIMEZONE = "America/Sao_Paulo"
CELERY_TASK_TRACK_STARTED = True
# Execuções são longas: cada processo do worker pega uma por vez, sem reservar outras da fila
CELERY_WORKER_CONCURRENCY = env.int("CELERY_WORKER_CONCURRENCY", default=2)
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Rodado pelo "celery beat": limpeza diária dos worktrees antigos
CELERY_BEAT_SCHEDULE = {
    "cleanup-workspaces": {
        "task": "orchestrator.tasks.cleanup_workspaces",
        "schedule": crontab(hour=3, minute=0),
    },
}

# ---------------------------------------------------------------------------
# Orquestrador: limites e prazos de operação do app.
# Cada um pode ser trocado por uma variável de ambiente de mesmo nome.
# ---------------------------------------------------------------------------

# Repositórios
# Clones dos projetos e um worktree por execução; fica fora do controle de versão
WORKSPACES_DIR = Path(env("WORKSPACES_DIR", default=BASE_DIR / ".workspaces"))
# Dias que o worktree de uma execução finalizada fica guardado antes da limpeza
WORKSPACE_RETENTION_DAYS = env.int("WORKSPACE_RETENTION_DAYS", default=7)
# Prazo, em segundos, de cada comando git (clone, fetch, push)
GIT_TIMEOUT = env.int("GIT_TIMEOUT", default=600)
# Autor dos commits feitos pelos agentes e token usado para abrir pull requests
AGENT_GIT_NAME = env("AGENT_GIT_NAME", default="IzzyOne Agent")
AGENT_GIT_EMAIL = env("AGENT_GIT_EMAIL", default="agent@izzyone.local")
GITHUB_TOKEN = env("GITHUB_TOKEN", default="")
GITHUB_API_TIMEOUT = env.int("GITHUB_API_TIMEOUT", default=30)
GOOGLE_API_KEY = env("GOOGLE_API_KEY", default="")

# Execuções
# Teto de tokens (entrada + saída, somando todos os agentes) por execução; 0 desliga
RUN_MAX_TOKENS = env.int("RUN_MAX_TOKENS", default=2_000_000)
# Teto de passos do grafo por execução, para um agente em laço não rodar para sempre
RUN_MAX_STEPS = env.int("RUN_MAX_STEPS", default=100)

# Ferramentas dos agentes
# Tamanho máximo, em caracteres, do que uma ferramenta devolve ao agente (o resto é cortado)
TOOL_MAX_OUTPUT_CHARS = env.int("TOOL_MAX_OUTPUT_CHARS", default=20_000)
# Prazo, em segundos, da ferramenta "Rodar comando" e da busca no código
TOOL_COMMAND_TIMEOUT = env.int("TOOL_COMMAND_TIMEOUT", default=300)
TOOL_SEARCH_TIMEOUT = env.int("TOOL_SEARCH_TIMEOUT", default=60)
# Prazo, em segundos, do botão "Testar agente" e da consulta à lista de modelos de um provedor
AGENT_TEST_TIMEOUT = env.int("AGENT_TEST_TIMEOUT", default=60)
PROVIDER_LIST_TIMEOUT = env.int("PROVIDER_LIST_TIMEOUT", default=10)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

INTERNAL_IPS = ["127.0.0.1"]

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# O manifesto só existe depois do collectstatic, então fica restrito a produção;
# em desenvolvimento e nos testes os arquivos são servidos direto de static/
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_REDIRECT_URL = "/"
# Criação de conta pela tela: aberta em desenvolvimento, fechada em produção, onde qualquer
# conta consegue mandar agentes rodarem comandos no servidor. Crie usuários com createsuperuser.
SIGNUP_OPEN = env.bool("SIGNUP_OPEN", default=DEBUG)
ACCOUNT_ADAPTER = "orchestrator.adapters.AccountAdapter"

# Em desenvolvimento os e-mails vão para o Mailpit (docker compose up -d),
# com a caixa de entrada em http://localhost:8025
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="no-reply@localhost")
