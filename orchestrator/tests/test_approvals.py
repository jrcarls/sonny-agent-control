from unittest import mock

from django.urls import reverse
from langchain_core.messages import AIMessage

from .. import tasks
from ..models import Approval, Run
from ..workspace import workspace_path
from .base import FakeModel, RunTestCase, real_open_checkpointer, tool_call, use_model


class ApprovalFlowTests(RunTestCase):
    agent_tools = ["run_command"]

    def start(self, *responses):
        run = self.make_run()
        self.model = FakeModel(*responses)
        with use_model(self.model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        return run

    def decide(self, approval, decision):
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("orchestrator:approval_decide", args=[approval.pk]), {"decision": decision}, follow=True
            )
        return response, delay

    def resume(self, run):
        with use_model(self.model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()

    def test_sensitive_tool_waits_for_approval_before_running(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        self.assertEqual(run.status, Run.Status.WAITING)
        self.assertIsNone(run.finished_at)
        approval = run.approvals.get()
        self.assertEqual((approval.status, approval.tool_args), (Approval.Status.PENDING, {"command": "touch feito.txt"}))
        self.assertFalse((workspace_path(run) / "feito.txt").exists())

        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "touch feito.txt")
        self.assertNotContains(page, "hx-trigger")

    def test_approving_runs_the_tool_and_finishes(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        response, delay = self.decide(run.approvals.get(), "approve")
        delay.assert_called_once_with(run.pk)
        self.assertRedirects(response, run.get_absolute_url())
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.QUEUED)
        approval = run.approvals.get()
        self.assertEqual((approval.status, approval.decided_by), (Approval.Status.APPROVED, self.user))

        self.resume(run)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Pronto."))
        self.assertTrue((workspace_path(run) / "feito.txt").exists())
        self.assertEqual(len(self.model.calls), 2)  # o modelo não é chamado de novo ao retomar a aprovação

    def test_rejecting_skips_the_tool_and_tells_the_agent(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Entendido."))
        self.decide(run.approvals.get(), "reject")
        self.resume(run)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Entendido."))
        self.assertFalse((workspace_path(run) / "feito.txt").exists())
        self.assertEqual(self.model.calls[1][-1].content, "Ação negada pelo usuário.")
        self.assertEqual(run.approvals.get().status, Approval.Status.REJECTED)

    def test_run_resumes_only_after_every_pending_request_is_decided(self):
        both = AIMessage(
            content="",
            tool_calls=[
                {"name": "run_command", "args": {"command": "touch a.txt"}, "id": "call_a"},
                {"name": "run_command", "args": {"command": "touch b.txt"}, "id": "call_b"},
            ],
        )
        run = self.start(both, AIMessage(content="Feito."))
        first, second = run.approvals.order_by("tool_call_id")
        _, delay = self.decide(first, "approve")
        delay.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.WAITING)
        _, delay = self.decide(second, "reject")
        delay.assert_called_once_with(run.pk)

        self.resume(run)
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        self.assertTrue((workspace_path(run) / "a.txt").exists())
        self.assertFalse((workspace_path(run) / "b.txt").exists())

    def test_request_cannot_be_decided_twice(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"))
        approval = run.approvals.get()
        self.decide(approval, "reject")
        response, delay = self.decide(approval, "approve")
        delay.assert_not_called()
        self.assertContains(response, "já tinha sido decidido")
        approval.refresh_from_db()
        self.assertEqual(approval.status, Approval.Status.REJECTED)

    def test_approval_list_separates_pending_from_history(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"))
        url = reverse("orchestrator:approval_list")
        self.assertContains(self.client.get(url), "touch feito.txt")
        self.assertContains(self.client.get(url, {"situacao": "historico"}), "Nenhuma decisão registrada")
        self.decide(run.approvals.get(), "approve")
        self.assertContains(self.client.get(url), "Nenhum pedido pendente")
        self.assertContains(self.client.get(url, {"situacao": "historico"}), "Aprovada")


class CheckpointerApprovalTests(RunTestCase):
    agent_tools = ["run_command"]

    def test_run_resumes_from_state_saved_in_postgres(self):
        """Cada chamada da tarefa abre uma conexão nova: a pausa e a retomada passam pelo banco."""
        run = self.make_run()
        model = FakeModel(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        with mock.patch.object(tasks, "open_checkpointer", real_open_checkpointer), use_model(model):
            tasks.execute_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, Run.Status.WAITING, run.events.last())
            with mock.patch.object(tasks.execute_run, "delay"):
                self.client.post(
                    reverse("orchestrator:approval_decide", args=[run.approvals.get().pk]), {"decision": "approve"}
                )
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Pronto."), run.events.last())
        self.assertTrue((workspace_path(run) / "feito.txt").exists())
