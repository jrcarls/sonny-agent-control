import os
from unittest import mock

from django.contrib.auth import get_user_model
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
        self.assertNotContains(page, "Situação")
        self.client.post(self.create_url, {"name": "Revisor", "provider": "anthropic", "model": "claude-sonnet-5-5"})
        self.assertTrue(Agent.objects.get().is_active)

    def test_existing_agent_can_be_deactivated(self):
        agent = Agent.objects.create(name="Revisor", provider="anthropic", model="m")
        page = self.client.get(agent.get_absolute_url())
        self.assertContains(page, "Situação")
        self.assertIn("is_active", page.context["form"].fields)

    def test_form_is_split_into_sections(self):
        page = self.client.get(self.create_url)
        for title in ["Identidade", "Modelo", "Comportamento", "Permissões"]:
            self.assertContains(page, f">{title}</h2>")

    def test_model_field_offers_suggestions_per_provider(self):
        page = self.client.get(self.create_url)
        self.assertContains(page, '<datalist id="models-anthropic">')
        self.assertContains(page, '<option value="claude-sonnet-5-5"></option>')
        self.assertContains(page, '<datalist id="models-ollama">')
        # A sugestão não restringe: qualquer identificador é aceito
        self.client.post(self.create_url, {"name": "Local", "provider": "ollama", "model": "meu-modelo:latest"})
        self.assertEqual(Agent.objects.get().model, "meu-modelo:latest")

    def test_tools_are_grouped_and_sensitive_ones_are_flagged(self):
        page = self.client.get(self.create_url)
        groups = [(name, [spec.label for _, spec in items]) for name, items in page.context["form"].tool_groups()]
        self.assertEqual(
            groups,
            [
                ("Leitura", ["Listar arquivos", "Ler arquivo", "Buscar no código"]),
                ("Escrita", ["Escrever arquivo"]),
                ("Ações sensíveis", ["Rodar comando", "Abrir pull request"]),
            ],
        )
        self.assertContains(page, "pede aprovação", count=2)
        self.assertContains(page, "Executa comandos de shell no worktree.")

    def test_delegates_show_the_role_of_each_agent(self):
        agent = Agent.objects.create(name="Revisor", provider="anthropic", model="m")
        Agent.objects.create(name="Programador", description="Escreve o código", provider="openai", model="m")
        Agent.objects.create(name="Antigo", provider="openai", model="m", is_active=False)
        page = self.client.get(agent.get_absolute_url())
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
        environment = {"ANTHROPIC_API_KEY": "sk-teste", "OPENAI_API_KEY": "", "GEMINI_API_KEY": "chave"}
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
        init.assert_called_once_with("gemini-2.5-flash", model_provider="google_genai", timeout=20, max_retries=0)
        self.assertEqual(answer, "ok")
        self.assertGreaterEqual(seconds, 0)

    def test_ollama_gets_no_extra_options(self):
        chat = mock.Mock()
        chat.invoke.return_value = AIMessage(content="ok")
        with mock.patch.object(providers, "init_chat_model", return_value=chat) as init:
            providers.check_model("ollama", "llama3.3")
        init.assert_called_once_with("llama3.3", model_provider="ollama")
