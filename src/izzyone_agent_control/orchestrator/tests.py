from contextlib import contextmanager
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.urls import reverse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from . import tasks
from .models import Agent, Project, Run, RunEvent


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


@contextmanager
def memory_checkpointer():
    yield InMemorySaver()


class RunTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="x")
        self.client.force_login(self.user)
        self.project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        self.agent = Agent.objects.create(
            name="Revisor", provider="anthropic", model="claude-sonnet-5-5", system_prompt="Seja direto."
        )

    def make_run(self, **kwargs):
        defaults = {"project": self.project, "agent": self.agent, "prompt": "Resuma o README", "created_by": self.user}
        return Run.objects.create(**defaults | kwargs)


class HomeViewTests(RunTestCase):
    def test_submitting_prompt_creates_run_and_enqueues_it(self):
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("home"), {"prompt": "Resuma o README", "project": self.project.pk, "agent": self.agent.pk}
            )
        run = Run.objects.get()
        self.assertRedirects(response, run.get_absolute_url())
        self.assertEqual((run.status, run.created_by), (Run.Status.QUEUED, self.user))
        delay.assert_called_once_with(run.pk)

    def test_inactive_agent_is_not_accepted(self):
        self.agent.is_active = False
        self.agent.save()
        with mock.patch.object(tasks.execute_run, "delay") as delay:
            response = self.client.post(
                reverse("home"), {"prompt": "x", "project": self.project.pk, "agent": self.agent.pk}
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Run.objects.exists())
        delay.assert_not_called()

    def test_form_is_disabled_without_projects(self):
        self.project.delete()
        response = self.client.get(reverse("home"))
        self.assertFalse(response.context["can_submit"])
        self.assertContains(response, "adicione um projeto")

    def test_recent_runs_are_listed(self):
        self.make_run(prompt="Atualize as dependências")
        self.assertContains(self.client.get(reverse("home")), "Atualize as dependências")


@mock.patch.object(tasks, "open_checkpointer", memory_checkpointer)
class ExecuteRunTests(RunTestCase):
    def test_success_stores_result_and_events(self):
        run = self.make_run()
        model = GenericFakeChatModel(messages=iter([AIMessage(content="É um orquestrador.")]))
        with mock.patch("izzyone_agent_control.orchestrator.graph.build_chat_model", return_value=model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "É um orquestrador."))
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(
            list(run.events.values_list("kind", flat=True)),
            [RunEvent.Kind.STATUS, RunEvent.Kind.MESSAGE, RunEvent.Kind.STATUS],
        )

    def test_agent_receives_instructions_project_and_task(self):
        run = self.make_run()
        model = mock.Mock()
        model.invoke.return_value = AIMessage(content="ok")
        with mock.patch("izzyone_agent_control.orchestrator.graph.build_chat_model", return_value=model):
            tasks.execute_run(run.pk)
        system, human = model.invoke.call_args.args[0]
        self.assertIn("Seja direto.", system.content)
        self.assertIn("https://example.com/a.git", system.content)
        self.assertEqual(human.content, "Resuma o README")

    def test_failure_is_recorded(self):
        run = self.make_run()
        with mock.patch(
            "izzyone_agent_control.orchestrator.graph.build_chat_model", side_effect=RuntimeError("sem chave")
        ):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.events.last().content, "RuntimeError: sem chave")

    def test_run_that_already_started_is_not_executed_again(self):
        run = self.make_run(status=Run.Status.RUNNING)
        with mock.patch("izzyone_agent_control.orchestrator.graph.build_chat_model") as build:
            tasks.execute_run(run.pk)
        build.assert_not_called()
        self.assertFalse(run.events.exists())


class CheckpointerTests(RunTestCase):
    def test_graph_state_is_saved_in_postgres(self):
        run = self.make_run()
        model = GenericFakeChatModel(messages=iter([AIMessage(content="ok")]))
        with mock.patch("izzyone_agent_control.orchestrator.graph.build_chat_model", return_value=model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.SUCCEEDED, run.events.last())
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from checkpoints where thread_id = %s", [str(run.pk)])
            self.assertGreater(cursor.fetchone()[0], 0)


class RunViewTests(RunTestCase):
    def test_list_filters_by_situation(self):
        self.make_run(prompt="tarefa ativa")
        self.make_run(prompt="tarefa pronta", status=Run.Status.SUCCEEDED)
        url = reverse("orchestrator:run_list")
        self.assertContains(self.client.get(url), "tarefa ativa")
        self.assertContains(self.client.get(url), "tarefa pronta")
        active = self.client.get(url, {"situacao": "ativas"})
        self.assertContains(active, "tarefa ativa")
        self.assertNotContains(active, "tarefa pronta")
        finished = self.client.get(url, {"situacao": "finalizadas"})
        self.assertNotContains(finished, "tarefa ativa")

    def test_detail_polls_only_while_active(self):
        run = self.make_run()
        self.assertContains(self.client.get(run.get_absolute_url()), 'hx-trigger="every 2s"')
        run.status = Run.Status.SUCCEEDED
        run.result = "Pronto."
        run.save()
        response = self.client.get(run.get_absolute_url())
        self.assertNotContains(response, "hx-trigger")
        self.assertContains(response, "Pronto.")

    def test_htmx_request_gets_only_the_progress_fragment(self):
        run = self.make_run()
        run.events.create(kind=RunEvent.Kind.STATUS, content="Execução colocada na fila.")
        response = self.client.get(run.get_absolute_url(), headers={"HX-Request": "true"})
        self.assertTemplateUsed(response, "runs/_progress.html")
        self.assertTemplateNotUsed(response, "runs/detail.html")
        self.assertContains(response, "Execução colocada na fila.")

    def test_project_with_runs_cannot_be_deleted(self):
        self.make_run()
        response = self.client.post(reverse("orchestrator:project_delete", args=[self.project.pk]), follow=True)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertContains(response, "não pode ser excluído")
