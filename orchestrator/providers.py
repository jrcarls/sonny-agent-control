"""O que o sistema sabe sobre cada provedor de LLM: nome no LangChain, chave de API e modelos sugeridos."""

import os
import time

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

# Sugestões do campo "Modelo" no cadastro do agente. É só uma ajuda de digitação: qualquer
# identificador aceito pelo provedor funciona. Revise quando os provedores lançarem modelos novos.
MODEL_SUGGESTIONS = {
    Provider.ANTHROPIC: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"],
    Provider.OPENAI: ["gpt-5", "gpt-5-mini", "gpt-4.1"],
    Provider.GOOGLE: ["gemini-2.5-pro", "gemini-2.5-flash"],
    Provider.OLLAMA: ["llama3.3", "qwen2.5-coder"],
}

CHECK_TIMEOUT = 20


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
    options = {} if provider == Provider.OLLAMA else {"timeout": CHECK_TIMEOUT, "max_retries": 0}
    chat = init_chat_model(model, model_provider=MODEL_PROVIDERS[provider], **options)
    started = time.monotonic()
    response = chat.invoke("Responda apenas com a palavra: ok")
    return str(response.text).strip(), time.monotonic() - started
