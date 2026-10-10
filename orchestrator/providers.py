"""O que o sistema sabe sobre cada provedor de LLM: nome no LangChain, chave de API e modelos sugeridos."""

import os
import re
import time

from django.conf import settings
from django.core.cache import cache
from langchain.chat_models import init_chat_model

from .models import Agent

Provider = Agent.Provider

# Nome do provedor no cadastro do agente -> nome esperado pelo init_chat_model
MODEL_PROVIDERS = {
    Provider.ANTHROPIC: "anthropic",
    Provider.OPENAI: "openai",
    Provider.GOOGLE: "google_genai",
    Provider.OLLAMA: "ollama",
}

# Variáveis de ambiente que cada biblioteca de provedor aceita como chave (Ollama é local, sem chave)
API_KEY_VARS = {
    Provider.ANTHROPIC: ["ANTHROPIC_API_KEY"],
    Provider.OPENAI: ["OPENAI_API_KEY"],
    Provider.GOOGLE: ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
    Provider.OLLAMA: [],
}

# Sugestões do campo "Modelo" usadas quando não dá para consultar o provedor (sem chave, sem rede).
# É só uma ajuda de digitação: qualquer identificador aceito pelo provedor funciona.
MODEL_SUGGESTIONS = {
    Provider.ANTHROPIC: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    Provider.OPENAI: ["gpt-5", "gpt-5-mini", "gpt-4.1"],
    Provider.GOOGLE: ["gemini-3.8-flash", "gemini-3.1-pro-preview", "gemini-3.5-flash-lite", "gemini-flash-latest"],
    Provider.OLLAMA: ["llama3.3", "qwen2.5-coder"],
}


def missing_api_keys():
    """Provedor -> nome da variável que falta, para os provedores sem chave configurada no servidor."""
    return {
        provider.value: names[0]
        for provider, names in API_KEY_VARS.items()
        if names and not any(os.environ.get(name) for name in names)
    }


def check_model(provider, model):
    """Manda uma mensagem curta ao modelo. Devolve (resposta, segundos); deixa o erro do provedor subir."""
    # Sem repetição e com prazo curto: é um teste rápido feito a partir da tela
    options = {} if provider == Provider.OLLAMA else {"timeout": settings.AGENT_TEST_TIMEOUT, "max_retries": 0}
    chat = init_chat_model(model, model_provider=MODEL_PROVIDERS[provider], **options)
    started = time.monotonic()
    response = chat.invoke("Responda apenas com a palavra: ok")
    return str(response.text).strip(), time.monotonic() - started


# Quanto tempo a lista de modelos de um provedor fica guardada antes de ser consultada de novo
MODEL_LIST_CACHE_SECONDS = 600
# Trechos de nome que indicam modelo que não serve para conversar com ferramentas (voz, imagem etc.)
NOT_CHAT = ("tts", "image", "banana", "transcribe", "embedding", "audio", "realtime", "computer-use", "robotics")


class ModelListError(Exception):
    """Não foi possível obter a lista de modelos; a mensagem é mostrada na tela do agente."""


def _is_chat_model(name):
    return not any(marker in name for marker in NOT_CHAT)


def _api_key(provider):
    for name in API_KEY_VARS[provider]:
        if os.environ.get(name):
            return os.environ[name]
    raise ModelListError(f"a chave {API_KEY_VARS[provider][0]} não está configurada no servidor")


def _anthropic_models(timeout):
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key(Provider.ANTHROPIC), timeout=timeout, max_retries=0)
    return [model.id for model in client.models.list()]  # já vem do mais novo para o mais antigo


def _openai_models(timeout):
    import openai

    client = openai.OpenAI(api_key=_api_key(Provider.OPENAI), timeout=timeout, max_retries=0)
    names = [model.id for model in client.models.list()]
    chat = [name for name in names if re.match(r"(gpt-|chatgpt-|o\d)", name) and _is_chat_model(name)]
    return sorted(chat, reverse=True)


def _google_models(timeout):
    from google import genai
    from google.genai import types

    # O SDK do Google mede o prazo em milissegundos
    options = types.HttpOptions(timeout=timeout * 1000)
    client = genai.Client(api_key=_api_key(Provider.GOOGLE), http_options=options)
    names = [
        model.name.removeprefix("models/")
        for model in client.models.list()
        if "generateContent" in (model.supported_actions or [])
    ]
    chat = [name for name in names if name.startswith(("gemini-", "gemma-")) and _is_chat_model(name)]
    # Versões mais novas primeiro, com a família Gemini antes da Gemma
    return sorted(sorted(chat, reverse=True), key=lambda name: not name.startswith("gemini-"))


def _ollama_models(timeout):
    import ollama

    return sorted(model.model for model in ollama.Client(timeout=timeout).list().models)


_LISTERS = {
    Provider.ANTHROPIC: _anthropic_models,
    Provider.OPENAI: _openai_models,
    Provider.GOOGLE: _google_models,
    Provider.OLLAMA: _ollama_models,
}


def list_models(provider):
    """Modelos de conversa que o provedor oferece agora, consultados na API dele (com cache)."""
    key = f"provider-models:{provider}"
    models = cache.get(key)
    if models is None:
        try:
            models = _LISTERS[Provider(provider)](settings.PROVIDER_LIST_TIMEOUT)
        except ModelListError:
            raise
        except Exception as exc:
            raise ModelListError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
        if not models:
            raise ModelListError("o provedor não devolveu nenhum modelo")
        cache.set(key, models, MODEL_LIST_CACHE_SECONDS)
    return models
