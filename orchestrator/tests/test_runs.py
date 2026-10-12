from unittest import mock

from django.db import connection
from django.urls import reverse
from langchain_core.messages import AIMessage

from .. import tasks
from ..models import Project, Run, RunEvent
from .base import RunTestCase, real_open_checkpointer


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


class ExecuteRunTests(RunTestCase):
    def test_success_stores_result_and_events(self):
        run = self.make_run()
        model = self.execute(run, AIMessage(content="É um orquestrador."))
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "É um orquestrador."))
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(
            list(run.events.values_list("kind", flat=True)),
            [RunEvent.Kind.STATUS, RunEvent.Kind.STATUS, RunEvent.Kind.MESSAGE, RunEvent.Kind.STATUS],
        )
        self.assertIsNone(model.bound_tools)  # agente sem ferramentas só conversa

    def test_agent_receives_instructions_project_and_task(self):
        run = self.make_run()
        model = self.execute(run, AIMessage(content="ok"))
        system, human = model.calls[0]
        self.assertIn("Seja direto.", system.content)
        self.assertIn(str(self.remote), system.content)
        self.assertIn(f"sonny/run-{run.pk}", system.content)
        self.assertEqual(human.content, "Resuma o README")

    def test_failure_is_recorded(self):
        run = self.make_run()
        with mock.patch(
            "orchestrator.graph.build_chat_model", side_effect=RuntimeError("sem chave")
        ):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.events.last().content, "RuntimeError: sem chave")

    def test_unreachable_repository_fails_the_run(self):
        self.project.repo_url = str(self.tmp / "nao-existe")
        self.project.save()
        run = self.make_run()
        self.execute(run)
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIn("git fetch falhou", run.events.last().content)

    def test_run_that_already_started_is_not_executed_again(self):
        run = self.make_run(status=Run.Status.RUNNING)
        with mock.patch("orchestrator.graph.build_chat_model") as build:
            tasks.execute_run(run.pk)
        build.assert_not_called()
        self.assertFalse(run.events.exists())


class CheckpointerTests(RunTestCase):
    def test_graph_state_is_saved_in_postgres(self):
        run = self.make_run()
        with mock.patch.object(tasks, "open_checkpointer", real_open_checkpointer):
            self.execute(run, AIMessage(content="ok"))
        self.assertEqual(run.status, Run.Status.SUCCEEDED, run.events.last())
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from checkpoints where thread_id = %s", [str(run.pk)])
            self.assertGreater(cursor.fetchone()[0], 0)


class RunViewTests(RunTestCase):
    def test_list_filters_by_situation(self):
        self.make_run(prompt="tarefa ativa")
        self.make_run(prompt="tarefa esperando", status=Run.Status.WAITING)
        self.make_run(prompt="tarefa pronta", status=Run.Status.SUCCEEDED)
        url = reverse("orchestrator:run_list")

        def listed(**params):
            # Pela lista da tela, não pelo HTML: a sidebar também mostra as execuções recentes
            return {run.prompt for run in self.client.get(url, params).context["runs"]}

        self.assertEqual(listed(), {"tarefa ativa", "tarefa esperando", "tarefa pronta"})
        self.assertEqual(listed(situacao="ativas"), {"tarefa ativa", "tarefa esperando"})
        self.assertEqual(listed(situacao="finalizadas"), {"tarefa pronta"})

    def test_list_is_paginated_and_keeps_the_filter(self):
        Run.objects.bulk_create(
            Run(project=self.project, agent=self.agent, prompt=f"tarefa {number}", created_by=self.user)
            for number in range(30)
        )
        page = self.client.get(reverse("orchestrator:run_list"), {"situacao": "ativas"})
        self.assertEqual(len(page.context["runs"]), 25)
        self.assertContains(page, "Página 1 de 2")
        self.assertContains(page, 'href="?situacao=ativas&page=2"')
        self.assertNotContains(page, "Anterior")
        second = self.client.get(reverse("orchestrator:run_list"), {"situacao": "ativas", "page": 2})
        self.assertEqual(len(second.context["runs"]), 5)
        self.assertContains(second, 'href="?situacao=ativas&page=1"')

    def test_detail_polls_only_while_working(self):
        run = self.make_run()
        self.assertContains(self.client.get(run.get_absolute_url()), 'hx-trigger="every 2s"')
        run.status = Run.Status.SUCCEEDED
        run.result = "Pronto."
        run.save()
        response = self.client.get(run.get_absolute_url())
        self.assertNotContains(response, "every 2s")
        self.assertContains(response, "Pronto.")

    def test_htmx_request_gets_only_the_progress_fragment(self):
        run = self.make_run()
        run.events.create(kind=RunEvent.Kind.STATUS, content="Execução colocada na fila.")
        response = self.client.get(run.get_absolute_url(), headers={"HX-Request": "true"})
        self.assertTemplateUsed(response, "runs/_poll.html")
        self.assertTemplateNotUsed(response, "runs/detail.html")
        self.assertContains(response, "Execução colocada na fila.")

    def test_project_with_runs_cannot_be_deleted(self):
        self.make_run()
        response = self.client.post(reverse("orchestrator:project_delete", args=[self.project.pk]), follow=True)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertContains(response, "não pode ser excluído")
