from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ..models import Agent, Project
from .base import RunTestCase


HTMX = {"HX-Request": "true"}


class LoginRequiredTests(TestCase):
    def test_anonymous_is_redirected_to_login(self):
        for name in ["home", "orchestrator:project_list", "orchestrator:agent_create", "orchestrator:run_list"]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
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
        # Volta para a lista com o modal do projeto novo aberto, onde está a chave a cadastrar no repositório
        project = Project.objects.get()
        self.assertRedirects(response, f"{reverse('orchestrator:project_list')}?editar={project.pk}")
        self.assertContains(response, "git@github.com:org/site.git")
        self.assertContains(response, "criado")
        self.assertContains(response, f'hx-get="{project.get_absolute_url()}" hx-trigger="load"')

    def test_create_requires_repo(self):
        response = self.client.post(reverse("orchestrator:project_create"), {"name": "Site", "default_branch": "main"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Project.objects.exists())
        self.assertContains(response, "Este campo é obrigatório")

    def test_forms_live_in_a_modal_on_the_list(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        url = reverse("orchestrator:project_list")
        page = self.client.get(url)
        self.assertContains(page, 'role="dialog"')
        self.assertContains(page, 'id="project-modal-body"')
        self.assertNotContains(page, 'hx-trigger="load"')
        # Os endereços dos formulários, abertos direto, levam para a lista com o modal aberto
        self.assertRedirects(self.client.get(reverse("orchestrator:project_create")), url + "?novo=1")
        self.assertRedirects(self.client.get(project.get_absolute_url()), f"{url}?editar={project.pk}")
        self.assertContains(self.client.get(url, {"novo": "1"}), f'hx-get="{reverse("orchestrator:project_create")}" hx-trigger="load"')
        self.assertContains(self.client.get(url, {"editar": project.pk}), f'hx-get="{project.get_absolute_url()}" hx-trigger="load"')
        self.assertNotContains(self.client.get(url, {"editar": "x"}), 'hx-trigger="load"')

    def test_modal_content_is_fetched_by_htmx(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        new = self.client.get(reverse("orchestrator:project_create"), headers=HTMX)
        self.assertTemplateUsed(new, "projects/_modal_create.html")
        self.assertTemplateNotUsed(new, "layouts/app.html")
        self.assertContains(new, "Novo projeto")
        edit = self.client.get(project.get_absolute_url(), headers=HTMX)
        self.assertTemplateUsed(edit, "projects/_modal_edit.html")
        self.assertTemplateNotUsed(edit, "layouts/app.html")
        self.assertContains(edit, 'value="https://example.com/a.git"')
        self.assertContains(edit, "Acesso ao repositório")
        self.assertContains(edit, "Excluir")

    def test_modal_create_redirects_the_browser_to_the_new_project(self):
        response = self.client.post(
            reverse("orchestrator:project_create"),
            {"name": "Site", "repo_url": "git@github.com:org/site.git", "default_branch": "main"},
            headers=HTMX,
        )
        project = Project.objects.get(name="Site")
        self.assertEqual(response["HX-Redirect"], f"{reverse('orchestrator:project_list')}?editar={project.pk}")

    def test_modal_errors_come_back_as_just_the_modal_content(self):
        response = self.client.post(
            reverse("orchestrator:project_create"), {"name": "Site", "default_branch": "main"}, headers=HTMX
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "projects/_modal_create.html")
        self.assertTemplateNotUsed(response, "layouts/app.html")
        self.assertContains(response, "Este campo é obrigatório")
        self.assertContains(response, 'value="Site"')
        self.assertFalse(Project.objects.exists())

    def test_modal_update(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        invalid = self.client.post(project.get_absolute_url(), {"name": "", "repo_url": "x"}, headers=HTMX)
        self.assertTemplateUsed(invalid, "projects/_modal_edit.html")
        self.assertContains(invalid, "Este campo é obrigatório")
        self.assertContains(invalid, "Acesso ao repositório")
        response = self.client.post(
            project.get_absolute_url(),
            {"name": "Site", "repo_url": "https://example.com/b.git", "default_branch": "develop", "description": "x"},
            headers=HTMX,
        )
        self.assertEqual(response["HX-Redirect"], reverse("orchestrator:project_list"))
        project.refresh_from_db()
        self.assertEqual((project.repo_url, project.default_branch), ("https://example.com/b.git", "develop"))
        self.assertContains(self.client.get(reverse("orchestrator:project_list")), "atualizado")

    def test_update_without_javascript_still_works(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        invalid = self.client.post(project.get_absolute_url(), {"name": ""})
        self.assertTemplateUsed(invalid, "projects/form.html")
        self.assertContains(invalid, "Este campo é obrigatório")
        response = self.client.post(
            project.get_absolute_url(), {"name": "Novo nome", "repo_url": "https://example.com/a.git", "default_branch": "main"}
        )
        self.assertRedirects(response, reverse("orchestrator:project_list"))
        self.assertEqual(Project.objects.get().name, "Novo nome")

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


class AgentToolsFormTests(RunTestCase):
    def test_tools_are_saved_from_the_form(self):
        response = self.client.post(
            self.agent.get_absolute_url(),
            {"name": "Revisor", "provider": "anthropic", "model": "m", "tools": ["read_file", "run_command"]},
        )
        self.assertEqual(response.status_code, 302)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.tools, ["read_file", "run_command"])
        page = self.client.get(self.agent.get_absolute_url())
        checked = [option.data["value"] for option in page.context["form"]["tools"] if option.data["selected"]]
        self.assertEqual(checked, ["read_file", "run_command"])
        self.assertContains(page, "checked", count=2)  # as duas ferramentas; "ativo" ficou desmarcado

    def test_new_agent_form_suggests_read_only_tools(self):
        page = self.client.get(reverse("orchestrator:agent_create"))
        self.assertEqual(page.context["form"]["tools"].value(), ["list_files", "read_file", "search_files"])

    def test_unknown_tool_is_rejected(self):
        response = self.client.post(
            self.agent.get_absolute_url(), {"name": "Revisor", "provider": "anthropic", "model": "m", "tools": ["rm"]}
        )
        self.assertEqual(response.status_code, 200)


class AgentDelegatesFormTests(RunTestCase):
    def test_delegates_are_saved_and_agent_cannot_pick_itself(self):
        other = Agent.objects.create(name="Programador", provider="openai", model="gpt-x")
        page = self.client.get(self.agent.get_absolute_url())
        self.assertEqual(list(page.context["form"].fields["delegates"].queryset), [other])
        data = {"name": "Revisor", "provider": "anthropic", "model": "m"}
        self.client.post(self.agent.get_absolute_url(), data | {"delegates": [other.pk]})
        self.assertEqual(list(self.agent.delegates.all()), [other])
        self.assertEqual(list(other.delegates.all()), [])  # a relação não é automática nos dois sentidos
        response = self.client.post(self.agent.get_absolute_url(), data | {"delegates": [self.agent.pk]})
        self.assertEqual(response.status_code, 200)
        page = self.client.get(self.agent.get_absolute_url())
        self.assertTrue(all(option.data["selected"] for option in page.context["form"]["delegates"]))

    def test_field_is_hidden_when_there_is_no_other_agent(self):
        page = self.client.get(self.agent.get_absolute_url())
        self.assertNotIn("delegates", page.context["form"].fields)
