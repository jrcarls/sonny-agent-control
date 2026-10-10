from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Agent, Project


class LoginRequiredTests(TestCase):
    def test_anonymous_is_redirected_to_login(self):
        for name in ["project_list", "project_create", "agent_list", "agent_create"]:
            with self.subTest(name=name):
                response = self.client.get(reverse(f"orchestrator:{name}"))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])


class ProjectViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("ana", password="x"))

    def test_empty_list(self):
        response = self.client.get(reverse("orchestrator:project_list"))
        self.assertContains(response, "Nenhum projeto ainda")

    def test_create(self):
        response = self.client.post(
            reverse("orchestrator:project_create"),
            {"name": "Site", "repo_url": "git@github.com:org/site.git", "default_branch": "main", "description": ""},
            follow=True,
        )
        self.assertRedirects(response, reverse("orchestrator:project_list"))
        self.assertContains(response, "git@github.com:org/site.git")
        self.assertContains(response, "criado")

    def test_create_requires_repo(self):
        response = self.client.post(reverse("orchestrator:project_create"), {"name": "Site", "default_branch": "main"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Project.objects.exists())
        self.assertContains(response, "Este campo é obrigatório")

    def test_update(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        response = self.client.get(project.get_absolute_url())
        self.assertContains(response, 'value="https://example.com/a.git"')
        self.client.post(
            project.get_absolute_url(),
            {"name": "Site", "repo_url": "https://example.com/b.git", "default_branch": "develop", "description": "x"},
        )
        project.refresh_from_db()
        self.assertEqual((project.repo_url, project.default_branch), ("https://example.com/b.git", "develop"))

    def test_delete(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        url = reverse("orchestrator:project_delete", args=[project.pk])
        self.assertContains(self.client.get(url), "Excluir “Site”?")
        self.assertRedirects(self.client.post(url), reverse("orchestrator:project_list"))
        self.assertFalse(Project.objects.exists())


class AgentViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("ana", password="x"))

    def test_create_and_list(self):
        response = self.client.post(
            reverse("orchestrator:agent_create"),
            {"name": "Revisor", "description": "Revisa PRs", "provider": "anthropic", "model": "claude-sonnet-5-5",
             "system_prompt": "Seja direto.", "is_active": "on"},
            follow=True,
        )
        self.assertRedirects(response, reverse("orchestrator:agent_list"))
        self.assertContains(response, "Revisor")
        self.assertContains(response, "Anthropic")
        self.assertContains(response, "Ativo")

    def test_update_keeps_selected_provider_and_can_deactivate(self):
        agent = Agent.objects.create(name="Revisor", provider="openai", model="gpt-x")
        response = self.client.get(agent.get_absolute_url())
        self.assertContains(response, '<option value="openai" selected>OpenAI</option>', html=True)
        self.client.post(agent.get_absolute_url(), {"name": "Revisor", "provider": "openai", "model": "gpt-x"})
        agent.refresh_from_db()
        self.assertFalse(agent.is_active)

    def test_rejects_unknown_provider(self):
        response = self.client.post(
            reverse("orchestrator:agent_create"), {"name": "X", "provider": "nope", "model": "m"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Agent.objects.exists())

    def test_delete(self):
        agent = Agent.objects.create(name="Revisor", provider="openai", model="gpt-x")
        self.client.post(reverse("orchestrator:agent_delete", args=[agent.pk]))
        self.assertFalse(Agent.objects.exists())
