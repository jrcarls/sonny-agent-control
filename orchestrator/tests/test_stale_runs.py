from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from langchain_core.messages import AIMessage

from .. import tasks
from ..models import Run, RunEvent
from ..workspace import workspace_path
from .base import FakeModel, RunTestCase, tool_call, use_model


def ago(**kwargs):
    return timezone.now() - timedelta(**kwargs)


@override_settings(RUN_STALE_SECONDS=120)
class StaleRunTests(RunTestCase):
    agent_tools = ["write_file"]

    def test_running_run_records_worker_and_heartbeat(self):
        run = self.make_run()
        seen = {}

        def answer():
            current = Run.objects.get(pk=run.pk)
            seen.update(status=current.status, heartbeat=current.heartbeat_at)
            return AIMessage(content="ok")

        self.execute(run, answer)
        self.assertEqual(seen["status"], Run.Status.RUNNING)
        self.assertIsNotNone(seen["heartbeat"])
        self.assertEqual(run.status, Run.Status.SUCCEEDED)

    def test_only_running_runs_without_recent_heartbeat_are_failed(self):
        dead = self.make_run(status=Run.Status.RUNNING, heartbeat_at=ago(minutes=5))
        never_beat = self.make_run(status=Run.Status.RUNNING)
        alive = self.make_run(status=Run.Status.RUNNING, heartbeat_at=ago(seconds=30))
        waiting = self.make_run(status=Run.Status.WAITING, heartbeat_at=ago(hours=3))
        queued = self.make_run(heartbeat_at=None)

        self.assertEqual(tasks.fail_stale_runs(), 2)
        for run in [dead, never_beat]:
            run.refresh_from_db()
            self.assertEqual(run.status, Run.Status.FAILED)
            self.assertIsNotNone(run.finished_at)
            self.assertIn("O worker parou de responder", run.events.get(kind=RunEvent.Kind.ERROR).content)
        for run, status in [(alive, Run.Status.RUNNING), (waiting, Run.Status.WAITING), (queued, Run.Status.QUEUED)]:
            run.refresh_from_db()
            self.assertEqual(run.status, status)
        self.assertEqual(tasks.fail_stale_runs(), 0)

    def test_restarted_worker_fails_its_own_runs_right_away(self):
        mine = self.make_run(status=Run.Status.RUNNING, heartbeat_at=timezone.now(), worker="celery@a")
        other = self.make_run(status=Run.Status.RUNNING, heartbeat_at=timezone.now(), worker="celery@b")
        tasks._fail_runs_of_restarted_worker(sender=SimpleNamespace(hostname="celery@a"))
        mine.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((mine.status, other.status), (Run.Status.FAILED, Run.Status.RUNNING))
        self.assertIn("O worker foi reiniciado", mine.events.last().content)

    def test_worker_killed_mid_run_can_be_continued_from_the_last_step(self):
        run = self.make_run()
        # SystemExit não é um erro comum: simula o processo sendo encerrado no meio da chamada ao modelo
        model = FakeModel(tool_call("write_file", path="a.txt", content="1"), SystemExit(), AIMessage(content="Pronto."))
        with use_model(model), self.assertRaises(SystemExit):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.RUNNING)  # ninguém ficou para marcar a falha

        Run.objects.filter(pk=run.pk).update(heartbeat_at=ago(minutes=10))
        self.assertEqual(tasks.fail_stale_runs(), 1)
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "O worker parou de responder")
        self.assertContains(page, "Continuar de onde parou")

        (workspace_path(run) / "a.txt").write_text("alterado depois")
        with mock.patch.object(tasks.execute_run, "delay"):
            self.client.post(reverse("orchestrator:run_resume", args=[run.pk]))
        with use_model(model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Pronto."))
        self.assertEqual((workspace_path(run) / "a.txt").read_text(), "alterado depois")  # o passo já feito não se repete

    def test_worker_drops_a_run_that_was_failed_while_it_was_still_working(self):
        run = self.make_run()

        def answer_after_being_declared_dead():
            Run.objects.filter(pk=run.pk).update(heartbeat_at=ago(minutes=10))
            tasks.fail_stale_runs()
            return tool_call("write_file", path="a.txt", content="1")

        model = self.execute(run, answer_after_being_declared_dead, AIMessage(content="não devia chegar aqui"))
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertEqual(len(model.calls), 1)
        self.assertFalse((workspace_path(run) / "a.txt").exists())
        self.assertEqual(run.result, "")
