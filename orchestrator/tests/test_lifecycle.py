from unittest import mock

from django.test import override_settings
from django.urls import reverse
from langchain_core.messages import AIMessage

from .. import tasks
from ..models import Agent, Approval, Run, RunEvent
from ..workspace import workspace_path
from .base import FakeModel, RunTestCase, real_open_checkpointer, tool_call, use_model, use_models


class CancelTests(RunTestCase):
    agent_tools = ["run_command", "write_file"]

    def cancel(self, run):
        response = self.client.post(reverse("orchestrator:run_cancel", args=[run.pk]), follow=True)
        run.refresh_from_db()
        return response

    def test_queued_run_is_canceled_and_never_executes(self):
        run = self.make_run()
        self.cancel(run)
        self.assertEqual(run.status, Run.Status.CANCELED)
        self.assertIsNotNone(run.finished_at)
        model = self.execute(run, AIMessage(content="não devia rodar"))
        self.assertEqual((run.status, model.calls), (Run.Status.CANCELED, []))

    def test_canceling_while_waiting_drops_the_pending_requests(self):
        run = self.make_run()
        self.execute(run, tool_call("run_command", command="touch feito.txt"))
        self.assertEqual(run.status, Run.Status.WAITING)
        self.cancel(run)
        self.assertEqual(run.status, Run.Status.CANCELED)
        approval = run.approvals.get()
        self.assertEqual(approval.status, Approval.Status.CANCELED)
        self.assertContains(self.client.get(reverse("orchestrator:approval_list")), "Nenhum pedido pendente")
        response = self.client.post(
            reverse("orchestrator:approval_decide", args=[approval.pk]), {"decision": "approve"}, follow=True
        )
        self.assertContains(response, "já tinha sido decidido")
        self.assertFalse((workspace_path(run) / "feito.txt").exists())

    def test_running_run_stops_at_the_end_of_the_current_step(self):
        run = self.make_run()

        def answer_while_user_cancels():
            # O pedido chega no meio da chamada ao modelo
            response = self.client.post(reverse("orchestrator:run_cancel", args=[run.pk]), follow=True)
            self.assertContains(response, "Cancelando")
            return tool_call("write_file", path="novo.txt", content="x")

        model = self.execute(run, answer_while_user_cancels, AIMessage(content="não devia chegar aqui"))
        self.assertEqual(run.status, Run.Status.CANCELED)
        self.assertEqual(len(model.calls), 1)
        self.assertFalse((workspace_path(run) / "novo.txt").exists())  # a ferramenta do passo seguinte não rodou
        self.assertEqual(run.events.last().content, "Execução cancelada.")

    def test_cancel_request_during_the_step_that_asks_approval_wins(self):
        run = self.make_run()

        def ask_while_user_cancels():
            Run.objects.filter(pk=run.pk).update(cancel_requested=True)
            return tool_call("run_command", command="touch feito.txt")

        self.execute(run, ask_while_user_cancels)
        self.assertEqual(run.status, Run.Status.CANCELED)
        self.assertFalse(run.approvals.filter(status=Approval.Status.PENDING).exists())

    def test_finished_run_cannot_be_canceled(self):
        run = self.make_run(status=Run.Status.SUCCEEDED)
        response = self.cancel(run)
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        self.assertContains(response, "já terminou")
        self.assertNotContains(response, "Cancelar execução")

    def test_canceled_runs_are_listed_as_finished(self):
        self.make_run(prompt="tarefa cancelada", status=Run.Status.CANCELED)
        url = reverse("orchestrator:run_list")
        # Pela lista da tela, não pelo HTML: a sidebar também mostra as execuções recentes
        self.assertEqual(len(self.client.get(url, {"situacao": "finalizadas"}).context["runs"]), 1)
        self.assertEqual(len(self.client.get(url, {"situacao": "ativas"}).context["runs"]), 0)


class ResumeAndRetryTests(RunTestCase):
    agent_tools = ["write_file"]

    def post(self, name, run):
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse(f"orchestrator:{name}", args=[run.pk]), follow=True)
        return response, delay

    def test_failed_run_continues_from_the_last_saved_step(self):
        run = self.make_run()
        model = FakeModel(
            tool_call("write_file", path="a.txt", content="1"),
            RuntimeError("provedor fora do ar"),
            AIMessage(content="Agora foi."),
        )
        with use_model(model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertContains(self.client.get(run.get_absolute_url()), "Continuar de onde parou")
        (workspace_path(run) / "a.txt").write_text("alterado depois")

        response, delay = self.post("run_resume", run)
        delay.assert_called_once_with(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.finished_at), (Run.Status.QUEUED, None))
        with use_model(model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Agora foi."))
        # Só o passo que falhou foi refeito: a ferramenta do passo anterior não rodou de novo
        self.assertEqual((workspace_path(run) / "a.txt").read_text(), "alterado depois")
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(run.events.filter(content__contains="começou a trabalhar").count(), 1)

    def test_failed_run_continues_with_state_saved_in_postgres(self):
        with mock.patch.object(tasks, "open_checkpointer", real_open_checkpointer):
            self.test_failed_run_continues_from_the_last_saved_step()

    def test_resume_is_refused_when_not_failed_or_worktree_is_gone(self):
        succeeded = self.make_run(status=Run.Status.SUCCEEDED)
        response, delay = self.post("run_resume", succeeded)
        delay.assert_not_called()
        self.assertContains(response, "não pode ser continuada")

        failed_before_clone = self.make_run(status=Run.Status.FAILED)
        response, delay = self.post("run_resume", failed_before_clone)
        delay.assert_not_called()
        self.assertNotContains(response, "Continuar de onde parou")
        self.assertContains(response, "Rodar de novo do zero")

    def test_retry_creates_a_fresh_run_with_the_same_task(self):
        original = self.make_run(status=Run.Status.FAILED, prompt="Corrija o bug")
        response, delay = self.post("run_retry", original)
        new = Run.objects.exclude(pk=original.pk).get()
        delay.assert_called_once_with(new.pk)
        self.assertRedirects(response, new.get_absolute_url())
        self.assertEqual(
            (new.prompt, new.project, new.agent, new.status, new.created_by),
            ("Corrija o bug", self.project, self.agent, Run.Status.QUEUED, self.user),
        )
        original.refresh_from_db()
        self.assertEqual(original.status, Run.Status.FAILED)


class TokenTests(RunTestCase):
    agent_tools = ["read_file"]

    def reply(self, content="", input_tokens=0, output_tokens=0, tool_calls=()):
        usage = {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
        return AIMessage(content=content, tool_calls=list(tool_calls), usage_metadata=usage)

    def test_tokens_from_every_model_call_are_added_up(self):
        other = Agent.objects.create(name="Programador", provider="openai", model="gpt-x")
        self.agent.delegates.set([other])
        run = self.make_run()
        delegation = [{"name": "delegate_task", "args": {"agent": "Programador", "task": "Faça"}, "id": "c1"}]
        models = {
            "Revisor": FakeModel(self.reply(tool_calls=delegation, input_tokens=100, output_tokens=10), self.reply("Fim.", 150, 5)),
            "Programador": FakeModel(self.reply("Feito.", 40, 20)),
        }
        with use_models(models):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.input_tokens, run.output_tokens), (Run.Status.SUCCEEDED, 290, 35))
        self.assertContains(self.client.get(run.get_absolute_url()), "290 tokens de entrada, 35 de saída")

    @override_settings(RUN_MAX_TOKENS=100)
    def test_run_fails_when_it_goes_over_the_limit(self):
        run = self.make_run()
        read = [{"name": "read_file", "args": {"path": "README.md"}, "id": "c1"}]
        model = self.execute(run, self.reply("Vou ler.", 60, 20, read), self.reply("Li.", 70, 10), self.reply("extra"))
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(run.total_tokens, 160)
        self.assertIn("passou do limite de 100 tokens (usou 160)", run.events.last().content)
        self.assertTrue(run.events.filter(kind=RunEvent.Kind.MESSAGE, content="Li.").exists())

    @override_settings(RUN_MAX_TOKENS=0)
    def test_zero_disables_the_limit(self):
        run = self.make_run()
        self.execute(run, self.reply("Pronto.", 10**9, 10**9))
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
