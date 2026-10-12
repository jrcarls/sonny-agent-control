from unittest import mock

from django.urls import reverse
from langchain_core.messages import AIMessage, HumanMessage

from .. import tasks
from ..models import Run, RunEvent
from ..workspace import remove_workspace, workspace_path
from .base import FakeModel, RunTestCase, real_open_checkpointer, tool_call, use_model

HTMX = {"HX-Request": "true"}


class FollowUpTests(RunTestCase):
    agent_tools = ["write_file"]

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.execute(self.run_, tool_call("write_file", path="a.txt", content="1"), AIMessage(content="Criei o arquivo."))
        self.url = reverse("orchestrator:run_follow_up", args=[self.run_.pk])

    def ask(self, message="Troque o conteúdo para 2", run=None):
        url = reverse("orchestrator:run_follow_up", args=[run.pk]) if run else self.url
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"message": message}, follow=True)
        return response, delay

    def test_finished_run_offers_the_form_on_both_tabs(self):
        self.assertContains(self.client.get(self.run_.get_absolute_url()), f'action="{self.url}"')
        diff = self.client.get(reverse("orchestrator:run_diff", args=[self.run_.pk]), headers=HTMX)
        self.assertContains(diff, f'action="{self.url}"')

    def test_asking_puts_the_run_back_in_the_queue(self):
        response, delay = self.ask("  Troque o conteúdo para 2  ")
        delay.assert_called_once_with(self.run_.pk)
        self.assertRedirects(response, self.run_.get_absolute_url())
        self.run_.refresh_from_db()
        self.assertEqual((self.run_.status, self.run_.follow_up), (Run.Status.QUEUED, "Troque o conteúdo para 2"))
        self.assertIsNone(self.run_.finished_at)
        note = self.run_.events.get(kind=RunEvent.Kind.NOTE)
        self.assertEqual(note.content, "Troque o conteúdo para 2")
        self.assertContains(response, "Ajuste pedido")
        self.assertContains(response, "ana pediu um ajuste")
        self.assertNotContains(response, f'action="{self.url}"')  # enquanto roda, não aceita outro pedido

    def test_agent_continues_the_same_conversation_in_the_same_worktree(self):
        self.ask()
        model = self.execute(
            self.run_, tool_call("write_file", call_id="call_2", path="a.txt", content="2"), AIMessage(content="Troquei.")
        )
        self.assertEqual((self.run_.status, self.run_.result, self.run_.follow_up), (Run.Status.SUCCEEDED, "Troquei.", ""))
        self.assertIsNotNone(self.run_.finished_at)
        self.assertEqual((workspace_path(self.run_) / "a.txt").read_text(), "2")
        # O modelo recebe a conversa inteira: a tarefa, o que ele já fez e, por último, o ajuste
        system, *history = model.calls[0]
        self.assertEqual(history[0].content, "Resuma o README")
        self.assertEqual(history[-2].content, "Criei o arquivo.")
        self.assertIsInstance(history[-1], HumanMessage)
        self.assertEqual(history[-1].content, "Troque o conteúdo para 2")
        self.assertEqual(self.run_.events.filter(content="Execução concluída.").count(), 2)
        # A tarefa não é reiniciada
        self.assertEqual(self.run_.events.filter(content__endswith="começou a trabalhar.").count(), 1)

    def test_can_be_asked_again_after_it_finishes(self):
        self.ask("primeiro")
        self.execute(self.run_, AIMessage(content="Feito 1."))
        self.ask("segundo")
        model = self.execute(self.run_, AIMessage(content="Feito 2."))
        contents = [message.content for message in model.calls[0][1:]]
        self.assertEqual(contents[-3:], ["primeiro", "Feito 1.", "segundo"])
        self.assertEqual(self.run_.result, "Feito 2.")

    def test_empty_message_is_refused(self):
        response, delay = self.ask("   ")
        delay.assert_not_called()
        self.assertContains(response, "Escreva o ajuste antes de enviar")
        self.assertEqual(Run.objects.get(pk=self.run_.pk).status, Run.Status.SUCCEEDED)

    def test_only_a_finished_run_with_its_worktree_accepts_it(self):
        failed = self.make_run()
        self.execute(failed, RuntimeError("fora do ar"))
        self.assertEqual(failed.status, Run.Status.FAILED)
        response, delay = self.ask(run=failed)
        delay.assert_not_called()
        self.assertContains(response, "não pode receber ajustes")
        self.assertNotContains(response, "Pedir um ajuste")

        remove_workspace(self.run_)
        response, delay = self.ask()
        delay.assert_not_called()
        self.assertContains(response, "não pode receber ajustes")

    def test_requires_login(self):
        self.client.logout()
        self.client.post(self.url, {"message": "x"})
        self.assertEqual(Run.objects.get(pk=self.run_.pk).follow_up, "")


class CheckpointerFollowUpTests(RunTestCase):
    def test_conversation_is_read_back_from_postgres(self):
        """Cada chamada da tarefa abre uma conexão nova: o ajuste continua a conversa salva no banco."""
        run = self.make_run()
        model = FakeModel(AIMessage(content="Primeira resposta."), AIMessage(content="Ajustado."))
        with mock.patch.object(tasks, "open_checkpointer", real_open_checkpointer), use_model(model):
            tasks.execute_run(run.pk)
            with mock.patch.object(tasks.execute_run, "delay"):
                self.client.post(reverse("orchestrator:run_follow_up", args=[run.pk]), {"message": "Mude o tom"})
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Ajustado."), run.events.last())
        self.assertEqual(
            [message.content for message in model.calls[1][1:]], ["Resuma o README", "Primeira resposta.", "Mude o tom"]
        )
