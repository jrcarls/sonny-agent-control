import os
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .. import crypto, graph, integrations, providers
from ..models import Agent, Credential
from ..workspace import prepare_workspace
from .base import RunTestCase

NO_KEYS = {
    "ANTHROPIC_API_KEY": "",
    "OPENAI_API_KEY": "",
    "GOOGLE_API_KEY": "",
    "GEMINI_API_KEY": "",
    "DEEPSEEK_API_KEY": "",
}


def store(service, secret):
    credential = Credential(service=service)
    credential.set_secret(secret)
    credential.save()
    return credential


@override_settings(GITHUB_TOKEN="")
class IntegrationTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="x")
        self.client.force_login(self.user)
        self.enterContext(mock.patch.dict(os.environ, NO_KEYS))
        self.list_url = reverse("orchestrator:integration_list")

    def save(self, service, secret):
        return self.client.post(reverse("orchestrator:integration_save", args=[service]), {"secret": secret}, follow=True)


class SecretResolutionTests(IntegrationTestCase):
    def test_stored_key_is_encrypted_with_its_own_purpose(self):
        credential = store("openai", "sk-segredo-1234")
        self.assertNotIn("segredo", credential.secret)
        self.assertEqual(credential.get_secret(), "sk-segredo-1234")
        # A chave de criptografia das chaves SSH não abre uma chave de API
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt(credential.secret)

    def test_stored_key_wins_over_the_environment(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "do-ambiente"}):
            self.assertEqual(integrations.get_secret("openai"), "do-ambiente")
            store("openai", "da-tela")
            self.assertEqual(integrations.get_secret("openai"), "da-tela")
        self.assertEqual(integrations.get_secret("anthropic"), "")

    def test_google_accepts_either_variable(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "g"}):
            self.assertEqual(integrations.environment_variable("google"), "GEMINI_API_KEY")
            self.assertEqual(integrations.get_secret("google"), "g")

    def test_key_saved_under_another_secret_key_falls_back_to_the_environment(self):
        store("anthropic", "da-tela")
        with override_settings(SECRET_KEY="outra"), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "do-ambiente"}):
            self.assertEqual(integrations.get_secret("anthropic"), "do-ambiente")

    def test_missing_keys_considers_both_places(self):
        store("openai", "da-tela")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a"}):
            self.assertEqual(
                providers.missing_api_keys(), {"google": "GOOGLE_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
            )

    def test_agent_model_gets_the_stored_key(self):
        agent = Agent(name="Revisor", provider="anthropic", model="claude-sonnet-5-5")
        with mock.patch.object(graph, "init_chat_model") as init:
            graph.build_chat_model(agent)
            init.assert_called_once_with("claude-sonnet-5-5", model_provider="anthropic")
            store("anthropic", "sk-ant-da-tela")
            graph.build_chat_model(agent)
            init.assert_called_with("claude-sonnet-5-5", model_provider="anthropic", api_key="sk-ant-da-tela")

    def test_model_list_uses_the_stored_key(self):
        cache.clear()
        self.addCleanup(cache.clear)
        store("anthropic", "sk-ant-da-tela")
        with mock.patch("anthropic.Anthropic") as client:
            client.return_value.models.list.return_value = [mock.Mock(id="claude-opus-5-5")]
            self.assertEqual(providers.list_models("anthropic"), ["claude-opus-5-5"])
        client.assert_called_once_with(api_key="sk-ant-da-tela", timeout=10, max_retries=0)


class IntegrationScreenTests(IntegrationTestCase):
    def test_requires_login(self):
        self.client.logout()
        self.assertIn("/accounts/login/", self.client.get(self.list_url)["Location"])
        self.assertEqual(self.save("openai", "sk-x").redirect_chain[0][1], 302)
        self.assertFalse(Credential.objects.exists())

    def test_lists_every_service_and_where_its_key_comes_from(self):
        store("openai", "sk-proj-abcdefgh9876")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-do-ambiente"}):
            page = self.client.get(self.list_url)
        self.assertEqual(
            [(item["service"].value, item["source"]) for item in page.context["integrations"]],
            [("anthropic", "environment"), ("openai", "stored"), ("google", ""), ("deepseek", ""), ("github", "")],
        )
        self.assertContains(page, "••••9876")
        self.assertContains(page, "ANTHROPIC_API_KEY</code> do servidor")
        self.assertContains(page, "Conectada", count=2)
        self.assertContains(page, "Não configurada", count=3)
        self.assertContains(page, ">Provedores de modelo</h2>")
        self.assertContains(page, ">Código</h2>")
        # Cada serviço aparece com a própria marca: logo do projeto ou ícone do Tabler, nunca só a inicial
        self.assertContains(page, 'viewBox="0 0 24 24" fill="currentColor"', count=2)
        self.assertNotContains(page, '<span class="text-base font-semibold">')
        # Nenhuma chave vai para a página, nem a salva nem a do ambiente
        self.assertNotContains(page, "sk-proj-abcdefgh")
        self.assertNotContains(page, "sk-ant-do-ambiente")

    def test_saving_stores_the_key_encrypted_and_never_shows_it_again(self):
        page = self.save("anthropic", "  sk-ant-api03-segredo-4321  ")
        self.assertContains(page, "Chave de Anthropic salva")
        credential = Credential.objects.get(service="anthropic")
        self.assertEqual(credential.get_secret(), "sk-ant-api03-segredo-4321")
        self.assertEqual(credential.updated_by, self.user)
        self.assertNotIn("segredo", credential.secret)
        self.assertContains(page, "••••4321")
        self.assertContains(page, "por ana")
        # A tela volta com o painel do serviço aberto e confere a chave nova sozinha
        self.assertEqual(page.redirect_chain[0][0], f"{self.list_url}?verificar=anthropic")
        self.assertContains(page, 'hx-trigger="load"', count=1)
        opened = {item["service"].value: item["open"] for item in page.context["integrations"]}
        self.assertEqual(
            opened,
            {"anthropic": True, "openai": False, "google": False, "deepseek": False, "github": False},
        )
        self.assertNotContains(page, "sk-ant-api03")

    def test_saving_again_replaces_the_key_and_drops_the_cached_model_list(self):
        self.addCleanup(cache.clear)
        self.save("openai", "sk-primeira-0001")
        cache.set("provider-models:openai", ["gpt-antigo"])
        self.save("openai", "sk-segunda-0002")
        self.assertEqual(Credential.objects.get().get_secret(), "sk-segunda-0002")
        self.assertIsNone(cache.get("provider-models:openai"))

    def test_empty_key_is_refused(self):
        page = self.save("openai", "   ")
        self.assertContains(page, "A chave de OpenAI não foi salva")
        self.assertNotContains(page, 'hx-trigger="load"')
        self.assertFalse(Credential.objects.exists())

    def test_short_key_shows_no_ending(self):
        self.assertEqual(integrations.mask("curta"), "••••")

    def test_removing_goes_back_to_the_environment(self):
        store("openai", "sk-da-tela-0001")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "do-ambiente"}):
            page = self.client.post(reverse("orchestrator:integration_delete", args=["openai"]), follow=True)
            self.assertEqual(integrations.get_secret("openai"), "do-ambiente")
        self.assertContains(page, "Chave de OpenAI removida")
        self.assertFalse(Credential.objects.exists())

    def test_unreadable_key_asks_to_be_saved_again(self):
        with override_settings(SECRET_KEY="outra"):
            store("google", "AIza-segredo-0001")
        page = self.client.get(self.list_url)
        self.assertContains(page, "Chave ilegível")
        self.assertContains(page, "Cadastre-a de novo")
        self.assertContains(page, "Cadastrar de novo")
        self.assertTrue(page.context["integrations"][2]["open"])

    def test_verification_is_ignored_for_a_service_without_key(self):
        self.assertNotContains(self.client.get(self.list_url, {"verificar": "openai"}), 'hx-trigger="load"')

    def test_shows_how_many_active_agents_depend_on_each_key(self):
        Agent.objects.create(name="Revisor", provider="openai", model="m")
        Agent.objects.create(name="Programador", provider="openai", model="m", tools=["read_file", "open_pull_request"])
        Agent.objects.create(name="Antigo", provider="openai", model="m", is_active=False)
        page = self.client.get(self.list_url)
        self.assertContains(page, "2 agentes usam este provedor e vão falhar sem a chave")
        self.assertContains(page, "1 agente pode abrir pull request")
        store("openai", "sk-proj-abcdefgh9876")
        page = self.client.get(self.list_url)
        self.assertContains(page, "Usada por 2 agentes")
        self.assertNotContains(page, "vão falhar")

    def test_unknown_service_is_not_found(self):
        self.assertEqual(self.save("ollama", "x").status_code, 404)
        self.assertEqual(self.client.post(reverse("orchestrator:integration_test", args=["nope"])).status_code, 404)

    def test_agent_form_warning_links_here(self):
        page = self.client.get(reverse("orchestrator:agent_create"))
        self.assertContains(page, f'href="{self.list_url}"')
        store("anthropic", "a"), store("openai", "o"), store("google", "g"), store("deepseek", "d")
        self.assertNotContains(self.client.get(reverse("orchestrator:agent_create")), "está sem chave")


class IntegrationTestButtonTests(IntegrationTestCase):
    def check(self, service):
        return self.client.post(reverse("orchestrator:integration_test", args=[service]))

    def test_provider_is_checked_by_listing_its_models_without_cache(self):
        with mock.patch("orchestrator.views.fetch_models", return_value=["a", "b", "c"]) as fetch:
            response = self.check("openai")
        fetch.assert_called_once_with("openai")
        self.assertTemplateUsed(response, "integrations/_test_result.html")
        self.assertContains(response, "A chave de OpenAI funciona")
        self.assertContains(response, "3 modelos disponíveis")

    def test_provider_without_key_fails_with_guidance(self):
        response = self.check("anthropic")
        self.assertContains(response, "A conexão com Anthropic falhou")
        self.assertContains(response, "defina ANTHROPIC_API_KEY no servidor")

    def test_github_token_is_checked_against_the_api(self):
        store("github", "ghp_da_tela_0001")
        reply = mock.Mock(status_code=200)
        reply.json.return_value = {"login": "sonny-bot"}
        with mock.patch("orchestrator.integrations.httpx.get", return_value=reply) as get:
            response = self.check("github")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer ghp_da_tela_0001")
        self.assertContains(response, "O token pertence à conta sonny-bot")

    def test_github_refusal_and_network_failure_are_the_result(self):
        self.assertContains(self.check("github"), "Nenhum token configurado")
        store("github", "ghp_da_tela_0001")
        with mock.patch("orchestrator.integrations.httpx.get", return_value=mock.Mock(status_code=401, text="Bad credentials")):
            self.assertContains(self.check("github"), "O GitHub recusou o token (401): Bad credentials")
        with mock.patch("orchestrator.integrations.httpx.get", side_effect=httpx.ConnectError("sem rede")):
            self.assertContains(self.check("github"), "ConnectError: sem rede")


@override_settings(GITHUB_TOKEN="", AGENT_GIT_NAME="Agente", AGENT_GIT_EMAIL="agente@example.com")
class PullRequestTokenTests(RunTestCase):
    agent_tools = ["write_file", "open_pull_request"]

    def test_pull_request_is_opened_with_the_token_saved_on_the_screen(self):
        from ..tools import build_tools

        store("github", "ghp_da_tela_0001")
        run = self.make_run()
        workspace = prepare_workspace(run)
        run.project.repo_url = "git@github.com:org/site.git"
        tools = {item.name: item for item in build_tools(run, workspace, self.agent)}
        tools["write_file"].invoke({"path": "a.txt", "content": "1"})
        response = mock.Mock(status_code=201)
        response.json.return_value = {"html_url": "https://github.com/org/site/pull/7"}
        with mock.patch("orchestrator.tools.httpx.post", return_value=response) as post:
            tools["open_pull_request"].invoke({"title": "Adiciona arquivo"})
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer ghp_da_tela_0001")
