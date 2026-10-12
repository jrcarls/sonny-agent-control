import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from langchain_core.messages import AIMessage

from .. import providers
from ..models import Agent


class AgentFormTestCase(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("ana", password="x"))
        self.create_url = reverse("orchestrator:agent_create")


class AgentFormLayoutTests(AgentFormTestCase):
    def test_new_agent_has_no_active_checkbox_and_is_created_active(self):
        page = self.client.get(self.create_url)
        self.assertNotIn("is_active", page.context["form"].fields)
        self.assertNotContains(page, 'name="is_active"')
        self.client.post(self.create_url, {"name": "Revisor", "provider": "anthropic", "model": "claude-sonnet-5-5"})
        self.assertTrue(Agent.objects.get().is_active)

    def test_existing_agent_can_be_deactivated(self):
        agent = Agent.objects.create(name="Revisor", provider="anthropic", model="m")
        page = self.client.get(agent.get_absolute_url())
        self.assertContains(page, 'name="is_active"')
        self.assertIn("is_active", page.context["form"].fields)

    def test_form_is_split_into_sections(self):
        page = self.client.get(self.create_url)
        for title in ["Identidade", "Modelo", "Comportamento", "Permissões"]:
            self.assertContains(page, f">{title}</h2>")
        self.assertNotContains(page, ">Delegação</h2>")  # só aparece quando há outro agente para escolher

    def test_model_field_starts_with_fixed_suggestions_and_asks_the_provider_for_the_real_list(self):
        agent = Agent.objects.create(name="Revisor", provider="anthropic", model="claude-sonnet-5-5")
        page = self.client.get(agent.get_absolute_url())
        self.assertContains(page, '<datalist id="model-options">')
        self.assertContains(page, '<option value="claude-sonnet-5-5"></option>')
        self.assertContains(page, f'hx-get="{reverse("orchestrator:agent_models")}"')
        self.assertContains(page, 'list="model-options"')
        # A sugestão não restringe: qualquer identificador é aceito
        self.client.post(self.create_url, {"name": "Local", "provider": "ollama", "model": "meu-modelo:latest"})
        self.assertEqual(Agent.objects.get(name="Local").model, "meu-modelo:latest")

    def test_tools_are_grouped_and_sensitive_ones_are_flagged(self):
        page = self.client.get(self.create_url)
        groups = [(name, [spec.label for _, spec in items]) for name, items in page.context["form"].tool_groups()]
        self.assertEqual(
            groups,
            [
                (
                    "Leitura",
                    [
                        "Listar arquivos",
                        "Ler arquivo",
                        "Buscar no código",
                        "Procurar arquivos",
                        "Ver situação do git",
                        "Ver diff",
                        "Ver histórico",
                        "Listar pull requests",
                        "Ler pull request",
                    ],
                ),
                ("Escrita", ["Escrever arquivo", "Editar arquivo"]),
                ("Ações sensíveis", ["Rodar comando", "Rodar testes", "Abrir pull request", "Comentar no pull request"]),
            ],
        )
        self.assertContains(page, "pede aprovação", count=3)
        self.assertContains(page, "Executa comandos de shell no worktree.")

    def test_delegates_show_the_role_of_each_agent(self):
        agent = Agent.objects.create(name="Revisor", provider="anthropic", model="m")
        Agent.objects.create(name="Programador", description="Escreve o código", provider="openai", model="m")
        Agent.objects.create(name="Antigo", provider="openai", model="m", is_active=False)
        page = self.client.get(agent.get_absolute_url())
        self.assertContains(page, ">Delegação</h2>")
        self.assertContains(page, "Escreve o código")
        self.assertContains(page, "Sem papel descrito")
        self.assertContains(page, "inativo")

    def test_validation_errors_are_shown_next_to_the_custom_fields(self):
        page = self.client.post(self.create_url, {"name": "Revisor", "provider": "nope", "model": ""})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Este campo é obrigatório")
        self.assertContains(page, "não é uma das escolhas disponíveis")


class MissingKeyWarningTests(AgentFormTestCase):
    def test_warns_only_for_providers_without_a_key(self):
        environment = {
            "ANTHROPIC_API_KEY": "sk-teste",
            "OPENAI_API_KEY": "",
            "GEMINI_API_KEY": "chave",
            "DEEPSEEK_API_KEY": "sk-deep",
        }
        with mock.patch.dict(os.environ, environment):
            os.environ.pop("GOOGLE_API_KEY", None)
            self.assertEqual(providers.missing_api_keys(), {"openai": "OPENAI_API_KEY"})
            page = self.client.get(self.create_url)
        self.assertContains(page, "x-show=\"provider === 'openai'\"")
        self.assertContains(page, "OPENAI_API_KEY")
        self.assertNotContains(page, "ANTHROPIC_API_KEY")
        self.assertNotContains(page, "x-show=\"provider === 'ollama'\"")


class AgentTestButtonTests(AgentFormTestCase):
    def test(self, **data):
        return self.client.post(reverse("orchestrator:agent_test"), data)

    def test_success_shows_answer_and_time(self):
        with mock.patch("orchestrator.views.check_model", return_value=("ok", 1.234)) as check:
            response = self.test(provider="openai", model=" gpt-5 ", name="ainda não salvo")
        check.assert_called_once_with("openai", "gpt-5")
        self.assertTemplateUsed(response, "agents/_test_result.html")
        self.assertContains(response, "gpt-5 respondeu em 1,2s")
        self.assertFalse(Agent.objects.exists())

    def test_provider_error_is_shown_as_the_result(self):
        with mock.patch("orchestrator.views.check_model", side_effect=RuntimeError("chave inválida")):
            response = self.test(provider="anthropic", model="claude-sonnet-5-5")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "O teste falhou")
        self.assertContains(response, "RuntimeError: chave inválida")

    def test_incomplete_form_is_not_sent_to_the_provider(self):
        with mock.patch("orchestrator.views.check_model") as check:
            for data in [{"provider": "anthropic", "model": ""}, {"provider": "nope", "model": "x"}, {}]:
                self.assertContains(self.test(**data), "Escolha o provedor e informe o modelo")
        check.assert_not_called()

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.test(provider="openai", model="gpt-5").status_code, 302)


class CheckModelTests(TestCase):
    def test_uses_a_short_timeout_and_no_retries(self):
        chat = mock.Mock()
        chat.invoke.return_value = AIMessage(content=" ok \n")
        with mock.patch.object(providers, "init_chat_model", return_value=chat) as init:
            answer, seconds = providers.check_model("google", "gemini-2.5-flash")
        init.assert_called_once_with("gemini-2.5-flash", model_provider="google_genai", timeout=60, max_retries=0)
        self.assertEqual(answer, "ok")
        self.assertGreaterEqual(seconds, 0)

    def test_ollama_gets_no_extra_options(self):
        chat = mock.Mock()
        chat.invoke.return_value = AIMessage(content="ok")
        with mock.patch.object(providers, "init_chat_model", return_value=chat) as init:
            providers.check_model("ollama", "llama3.3")
        init.assert_called_once_with("llama3.3", model_provider="ollama")


def fake_models(*names, attribute="id"):
    return [mock.Mock(**{attribute: name}) for name in names]


class ListModelsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.enterContext(mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GOOGLE_API_KEY": "g"}))

    def test_anthropic_keeps_the_order_of_the_api(self):
        with mock.patch("anthropic.Anthropic") as client:
            client.return_value.models.list.return_value = fake_models("claude-opus-5-5", "claude-haiku-5-5")
            self.assertEqual(providers.list_models("anthropic"), ["claude-opus-5-5", "claude-haiku-5-5"])
        client.assert_called_once_with(api_key="a", timeout=10, max_retries=0)

    def test_openai_keeps_only_chat_models(self):
        names = ["gpt-5", "gpt-5-mini", "o4-mini", "gpt-4o-audio-preview", "text-embedding-3-large", "whisper-1", "dall-e-3"]
        with mock.patch("openai.OpenAI") as client:
            client.return_value.models.list.return_value = fake_models(*names)
            self.assertEqual(providers.list_models("openai"), ["o4-mini", "gpt-5-mini", "gpt-5"])

    def test_deepseek_lists_through_the_openai_client_at_its_own_address(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "d"}), mock.patch("openai.OpenAI") as client:
            client.return_value.models.list.return_value = fake_models("deepseek-reasoner", "deepseek-chat")
            self.assertEqual(providers.list_models("deepseek"), ["deepseek-chat", "deepseek-reasoner"])
        client.assert_called_once_with(api_key="d", base_url="https://api.deepseek.com", timeout=10, max_retries=0)

    def test_google_keeps_only_text_models_that_generate_content(self):
        models = [
            mock.Mock(supported_actions=["generateContent"]),
            mock.Mock(supported_actions=["generateContent"]),
            mock.Mock(supported_actions=["generateContent"]),
            mock.Mock(supported_actions=["embedContent"]),
            mock.Mock(supported_actions=["generateContent"]),
            mock.Mock(supported_actions=None),
        ]
        names = ["gemma-4-31b-it", "gemini-3.8-flash", "gemini-3.8-flash-tts", "gemini-embedding-1", "lyria-3.5", "gemini-x"]
        for model, name in zip(models, names):
            model.name = f"models/{name}"
        with mock.patch("google.genai.Client") as client:
            client.return_value.models.list.return_value = models
            self.assertEqual(providers.list_models("google"), ["gemini-3.8-flash", "gemma-4-31b-it"])
        self.assertEqual(client.call_args.kwargs["http_options"].timeout, 10_000)

    def test_ollama_lists_local_models_without_a_key(self):
        with mock.patch("ollama.Client") as client:
            client.return_value.list.return_value.models = fake_models("qwen3", "llama3.3", attribute="model")
            self.assertEqual(providers.list_models("ollama"), ["llama3.3", "qwen3"])

    def test_result_is_cached_and_failures_are_not(self):
        with mock.patch("anthropic.Anthropic") as client:
            client.return_value.models.list.side_effect = [RuntimeError("fora do ar"), fake_models("claude-opus-5-5")]
            with self.assertRaisesMessage(providers.ModelListError, "RuntimeError: fora do ar"):
                providers.list_models("anthropic")
            self.assertEqual(providers.list_models("anthropic"), ["claude-opus-5-5"])
            self.assertEqual(providers.list_models("anthropic"), ["claude-opus-5-5"])
        self.assertEqual(client.return_value.models.list.call_count, 2)

    def test_missing_key_and_empty_list_are_errors(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesMessage(providers.ModelListError, "defina OPENAI_API_KEY no servidor"):
                providers.list_models("openai")
        with mock.patch("ollama.Client") as client:
            client.return_value.list.return_value.models = []
            with self.assertRaisesMessage(providers.ModelListError, "nenhum modelo"):
                providers.list_models("ollama")


class AgentModelsEndpointTests(AgentFormTestCase):
    def get(self, **params):
        return self.client.get(reverse("orchestrator:agent_models"), params)

    def test_live_list_is_returned_as_options(self):
        with mock.patch("orchestrator.views.list_models", return_value=["gemini-3.8-flash", "gemini-2.5-flash"]) as lister:
            response = self.get(provider="google")
        lister.assert_called_once_with("google")
        self.assertTemplateUsed(response, "agents/_model_options.html")
        self.assertContains(response, '<option value="gemini-3.8-flash"></option>')
        self.assertContains(response, "2 modelos disponíveis neste provedor")
        self.assertContains(response, 'hx-swap-oob="true"')

    def test_failure_falls_back_to_fixed_suggestions_and_says_why(self):
        error = providers.ModelListError("sem chave: cadastre em Integrações ou defina OPENAI_API_KEY no servidor")
        with mock.patch("orchestrator.views.list_models", side_effect=error):
            response = self.get(provider="openai")
        self.assertContains(response, '<option value="gpt-5"></option>')
        self.assertContains(response, "Não foi possível consultar")
        self.assertContains(response, "defina OPENAI_API_KEY no servidor")

    def test_unknown_or_empty_provider_returns_no_options(self):
        with mock.patch("orchestrator.views.list_models") as lister:
            for params in [{}, {"provider": ""}, {"provider": "nope"}]:
                response = self.get(**params)
                self.assertNotContains(response, "<option")
                self.assertContains(response, "Escolha o provedor")
        lister.assert_not_called()

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.get(provider="google").status_code, 302)
