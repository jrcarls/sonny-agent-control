from unittest import mock

from django.urls import reverse
from langchain_core.messages import AIMessage

from .. import tasks
from ..models import Approval, Run
from ..workspace import workspace_path
from .base import FakeModel, RunTestCase, real_open_checkpointer, tool_call, use_model


class ApprovalFlow:
    """Passos comuns aos testes de pedidos que pausam a execução: começar, decidir pela tela e retomar."""

    def start(self, *responses):
        run = self.make_run()
        self.model = FakeModel(*responses)
        with use_model(self.model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        return run

    def decide(self, approval, decision, **data):
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("orchestrator:approval_decide", args=[approval.pk]), {"decision": decision, **data}, follow=True
            )
        return response, delay

    def resume(self, run):
        with use_model(self.model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()


class ApprovalFlowTests(ApprovalFlow, RunTestCase):
    agent_tools = ["run_command"]

    def test_sensitive_tool_waits_for_approval_before_running(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        self.assertEqual(run.status, Run.Status.WAITING)
        self.assertIsNone(run.finished_at)
        approval = run.approvals.get()
        self.assertEqual((approval.status, approval.tool_args), (Approval.Status.PENDING, {"command": "touch feito.txt"}))
        self.assertFalse((workspace_path(run) / "feito.txt").exists())

        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "touch feito.txt")
        self.assertNotContains(page, "every 2s")

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


class AskUserTests(ApprovalFlow, RunTestCase):
    agent_tools = ["ask_user", "run_command"]
    question = "Qual banco devo usar: Postgres ou SQLite?"

    def ask(self):
        return self.start(tool_call("ask_user", question=self.question), AIMessage(content="Usei Postgres."))

    def test_question_pauses_the_run_and_is_shown_with_a_field_to_answer(self):
        run = self.ask()
        self.assertEqual(run.status, Run.Status.WAITING)
        approval = run.approvals.get()
        self.assertEqual((approval.tool_name, approval.tool_args), ("ask_user", {"question": self.question}))
        self.assertTrue(run.events.filter(content="Aguardando a sua resposta para continuar.").exists())
        for url in (run.get_absolute_url(), reverse("orchestrator:approval_list")):
            page = self.client.get(url)
            self.assertContains(page, self.question)
            self.assertContains(page, 'name="answer"')
            self.assertContains(page, ">Responder<")

    def test_answer_goes_back_to_the_agent(self):
        run = self.ask()
        response, delay = self.decide(run.approvals.get(), "approve", answer="  Postgres.  ")
        delay.assert_called_once_with(run.pk)
        approval = run.approvals.get()
        self.assertEqual((approval.status, approval.answer, approval.decided_by), ("approved", "Postgres.", self.user))
        self.assertContains(response, "respondeu à pergunta do agente")

        self.resume(run)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Usei Postgres."))
        self.assertEqual(self.model.calls[1][-1].content, "Resposta do usuário:\nPostgres.")
        self.assertEqual(len(self.model.calls), 2)
        history = self.client.get(reverse("orchestrator:approval_list"), {"situacao": "historico"})
        self.assertContains(history, "Respondida")
        self.assertContains(history, "Postgres.")

    def test_empty_answer_is_refused_and_the_question_stays_pending(self):
        run = self.ask()
        response, delay = self.decide(run.approvals.get(), "approve", answer="   ")
        delay.assert_not_called()
        self.assertContains(response, "Escreva a resposta")
        self.assertEqual(run.approvals.get().status, Approval.Status.PENDING)

    def test_declining_to_answer_lets_the_agent_go_on(self):
        run = self.ask()
        self.decide(run.approvals.get(), "reject", answer="ignorado")
        self.assertEqual(run.approvals.get().answer, "")
        self.resume(run)
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        self.assertIn("preferiu não responder", self.model.calls[1][-1].content)
        self.assertContains(self.client.get(reverse("orchestrator:approval_list"), {"situacao": "historico"}), "Sem resposta")

    def test_question_and_command_in_the_same_step_wait_together(self):
        both = AIMessage(
            content="",
            tool_calls=[
                {"name": "ask_user", "args": {"question": self.question}, "id": "call_a"},
                {"name": "run_command", "args": {"command": "touch b.txt"}, "id": "call_b"},
            ],
        )
        run = self.start(both, AIMessage(content="Feito."))
        self.assertTrue(run.events.filter(content="Aguardando aprovação para continuar.").exists())
        question, command = run.approvals.order_by("tool_call_id")
        self.decide(question, "approve", answer="SQLite")
        _, delay = self.decide(command, "approve")
        delay.assert_called_once_with(run.pk)
        self.resume(run)
        contents = [message.content for message in self.model.calls[1][-2:]]
        self.assertEqual(contents[0], "Resposta do usuário:\nSQLite")
        self.assertIn("código de saída: 0", contents[1])
        self.assertTrue((workspace_path(run) / "b.txt").exists())

    def test_autonomous_run_does_not_stop_to_ask(self):
        run = self.make_run(autonomous=True)
        model = self.execute(run, tool_call("ask_user", question=self.question), AIMessage(content="Supus Postgres."))
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Supus Postgres."))
        self.assertIn("Modo autônomo: não há ninguém para responder", model.calls[1][-1].content)
        self.assertFalse(run.approvals.exists())

    def test_agent_without_the_tool_cannot_ask(self):
        self.agent.tools = ["run_command"]
        self.agent.save()
        run = self.start(tool_call("ask_user", question=self.question), AIMessage(content="Ok."))
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        self.assertEqual(self.model.calls[1][-1].content, "Ferramenta indisponível: ask_user")


class AutonomousRunTests(RunTestCase):
    agent_tools = ["run_command"]

    def test_sensitive_tool_runs_without_asking(self):
        run = self.make_run(autonomous=True)
        model = self.execute(run, tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Pronto."))
        self.assertTrue((workspace_path(run) / "feito.txt").exists())
        self.assertFalse(run.approvals.exists())
        self.assertEqual(len(model.calls), 2)
        self.assertTrue(run.events.filter(content__startswith="Modo autônomo").exists())
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, ">Autônomo</span>")

    def test_tool_the_agent_does_not_have_is_still_refused(self):
        run = self.make_run(autonomous=True)
        model = self.execute(run, tool_call("write_file", path="a.txt", content="1"), AIMessage(content="Ok."))
        self.assertEqual(model.calls[1][-1].content, "Ferramenta indisponível: write_file")
        self.assertFalse((workspace_path(run) / "a.txt").exists())

    def test_regular_run_says_nothing_about_autonomy(self):
        run = self.make_run()
        self.execute(run, AIMessage(content="Pronto."))
        self.assertFalse(run.events.filter(content__startswith="Modo autônomo").exists())
        self.assertNotContains(self.client.get(run.get_absolute_url()), ">Autônomo</span>")

    def test_chosen_when_sending_the_task_and_kept_on_retry(self):
        data = {"prompt": "Rode os testes", "project": self.project.pk, "agent": self.agent.pk}
        with mock.patch.object(tasks.execute_run, "delay"):
            self.client.post(reverse("home"), data)
            self.client.post(reverse("home"), data | {"autonomous": "on"})
            regular, autonomous = Run.objects.order_by("pk")
            self.assertEqual((regular.autonomous, autonomous.autonomous), (False, True))
            self.client.post(reverse("orchestrator:run_retry", args=[autonomous.pk]))
        self.assertTrue(Run.objects.order_by("pk").last().autonomous)

    def test_home_offers_the_option_unchecked(self):
        page = self.client.get(reverse("home"))
        self.assertContains(page, 'name="autonomous"')
        self.assertContains(page, "autonomous: false")


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
