"""Chaves de API dos serviços externos: a cadastrada na tela de Integrações vale antes da variável de ambiente."""

import os
from dataclasses import dataclass

import httpx
from django.conf import settings

from .crypto import DecryptionError
from .models import Credential

Service = Credential.Service


MODELS = "Provedores de modelo"
CODE = "Código"


@dataclass(frozen=True)
class Integration:
    # Seção da tela de Integrações em que o serviço aparece
    group: str
    description: str
    # Variáveis de ambiente aceitas quando não há chave cadastrada na tela; a primeira é a citada nos avisos
    env_vars: tuple
    # Onde a chave é criada: endereço e nome da página do serviço
    key_url: str
    key_site: str
    placeholder: str
    # Ícone do Tabler; sem ele nem logo a tela mostra a inicial do serviço
    icon: str = ""
    # Logo da marca que o Tabler não tem: arquivo em templates/brands/, sem a extensão
    logo: str = ""
    # Orientação extra mostrada junto do campo da chave
    help: str = ""


INTEGRATIONS = {
    Service.ANTHROPIC: Integration(
        MODELS,
        "Modelos Claude para os agentes.",
        ("ANTHROPIC_API_KEY",),
        "https://console.anthropic.com/settings/keys",
        "Console da Anthropic",
        "sk-ant-…",
        logo="anthropic",
    ),
    Service.OPENAI: Integration(
        MODELS,
        "Modelos GPT para os agentes.",
        ("OPENAI_API_KEY",),
        "https://platform.openai.com/api-keys",
        "Plataforma da OpenAI",
        "sk-…",
        icon="brand-openai",
    ),
    Service.GOOGLE: Integration(
        MODELS,
        "Modelos Gemini para os agentes.",
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        "https://aistudio.google.com/apikey",
        "Google AI Studio",
        "AIza…",
        icon="brand-google",
    ),
    Service.DEEPSEEK: Integration(
        MODELS,
        "Modelos DeepSeek para os agentes.",
        ("DEEPSEEK_API_KEY",),
        "https://platform.deepseek.com/api_keys",
        "Plataforma da DeepSeek",
        "sk-…",
        logo="deepseek",
    ),
    Service.GITHUB: Integration(
        CODE,
        "Abre os pull requests dos agentes. Sem o token, o agente envia a branch e entrega o link para você abrir.",
        ("GITHUB_TOKEN",),
        "https://github.com/settings/tokens",
        "Tokens do GitHub",
        "ghp_… ou github_pat_…",
        icon="brand-github",
        help="Permissão necessária: escrita em pull requests (ou o escopo repo, em um token clássico).",
    ),
}


class IntegrationError(Exception):
    """O teste de uma integração falhou; a mensagem é mostrada na tela."""


def stored_secrets():
    """Serviço -> chave cadastrada na tela. As que não abrem com a SECRET_KEY atual ficam de fora."""
    secrets = {}
    for credential in Credential.objects.all():
        try:
            secrets[credential.service] = credential.get_secret()
        except DecryptionError:
            pass
    return secrets


def environment_variable(service):
    """Nome da variável de ambiente que está fornecendo a chave do serviço, ou "" se nenhuma está definida."""
    if service == Service.GITHUB:
        return "GITHUB_TOKEN" if settings.GITHUB_TOKEN else ""
    return next((name for name in INTEGRATIONS[service].env_vars if os.environ.get(name)), "")


def get_secret(service, stored=None):
    """Chave em uso para o serviço: a da tela e, sem ela, a do ambiente. Vazio se não há nenhuma.

    stored é o resultado de stored_secrets(), para quem consulta vários serviços de uma vez.
    """
    secret = (stored_secrets() if stored is None else stored).get(service)
    if secret:
        return secret
    if service == Service.GITHUB:
        return settings.GITHUB_TOKEN
    return os.environ.get(environment_variable(service), "")


def mask(secret):
    """O final da chave, para a pessoa reconhecer qual está salva sem a tela mostrar o valor."""
    return f"••••{secret[-4:]}" if len(secret) >= 12 else "••••"


def github_login():
    """Confere o token do GitHub em uso e devolve a conta a que ele pertence."""
    token = get_secret(Service.GITHUB)
    if not token:
        raise IntegrationError("Nenhum token configurado.")
    try:
        response = httpx.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=settings.GITHUB_API_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise IntegrationError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    if response.status_code != 200:
        raise IntegrationError(f"O GitHub recusou o token ({response.status_code}): {response.text[:200]}")
    return response.json()["login"]
