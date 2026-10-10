from unittest import mock

from django.urls import reverse
from langchain_core.messages import AIMessage

from .. import tasks
from ..models import Agent, Run
from ..workspace import workspace_path
from .base import FakeModel, RunTestCase, delegate_call, real_open_checkpointer, tool_call, use_models


class DelegationTests(RunTestCase):
    """O agente principal (Revisor) delega para outros, espera e continua com a resposta."""

    def setUp(self):
        super().setUp()
        self.coder = Agent.objects.create(
            name="Programador", description="Escreve código", provider="openai", model="gpt-x", tools=["read_file"]
        )
        self.tester = Agent.objects.create(
            name="Testador", description="Roda os testes", provider="google", model="gemini-x", tools=["run_command"]
        )
        self.agent.delegates.set([self.coder, self.tester])

    def execute_team(self, run, **responses):
        """responses: nome do agente -> lista de respostas do modelo dele."""
        if not hasattr(self, "models"):
            self.models = {name: FakeModel(*items) for name, items in responses.items()}
        with use_models(self.models):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        return self.models

    def test_lead_waits_for_delegate_and_uses_its_answer(self):
        run = self.make_run()
        models = self.execute_team(
            run,
            Revisor=[delegate_call("Programador", "Leia o README e resuma"), AIMessage(content="Resumo: é um site.")],
            Programador=[tool_call("read_file", path="README.md"), AIMessage(content="É um projeto de exemplo.")],
        )
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Resumo: é um site."))
        lead, coder = models["Revisor"], models["Programador"]
        self.assertEqual(lead.bound_tools, ["delegate_task"])
        self.assertEqual(coder.bound_tools, ["read_file"])

        # O Programador recebe a tarefa com a origem e trabalha com as ferramentas dele
        self.assertEqual(coder.calls[0][-1].content, "Tarefa recebida de Revisor:\nLeia o README e resuma")
        self.assertIn("Você foi chamado pelo agente Revisor", coder.calls[0][0].content)
        self.assertIn("Projeto de exemplo.", coder.calls[1][-1].content)
        # O Revisor só é chamado de novo depois da resposta, que chega como resultado da delegação
        self.assertEqual(len(lead.calls), 2)
        self.assertEqual(lead.calls[1][-1].content, "É um projeto de exemplo.")
        self.assertEqual(lead.calls[1][-1].tool_call_id, "call_d")

        events = [(event.agent.name if event.agent else None, event.kind) for event in run.events.all()]
        self.assertEqual(
            events[2:],
            [
                ("Revisor", "delegation"),
                ("Programador", "tool_call"),
                ("Programador", "tool_result"),
                ("Programador", "message"),
                ("Programador", "delegation"),
                ("Revisor", "message"),
                (None, "status"),
            ],
        )
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "Revisor pediu a Programador")
        self.assertContains(page, "Programador · Mensagem")

    def test_two_delegations_in_one_step_run_one_after_the_other(self):
        run = self.make_run()
        both = AIMessage(
            content="",
            tool_calls=[
                {"name": "delegate_task", "args": {"agent": "Programador", "task": "Parte 1"}, "id": "call_1"},
                {"name": "delegate_task", "args": {"agent": "Testador", "task": "Parte 2"}, "id": "call_2"},
            ],
        )
        models = self.execute_team(
            run,
            Revisor=[both, AIMessage(content="Tudo feito.")],
            Programador=[AIMessage(content="Parte 1 pronta.")],
            Testador=[AIMessage(content="Parte 2 pronta.")],
        )
        self.assertEqual(run.result, "Tudo feito.")
        answers = {message.tool_call_id: message.content for message in models["Revisor"].calls[1][-2:]}
        self.assertEqual(answers, {"call_1": "Parte 1 pronta.", "call_2": "Parte 2 pronta."})

    def test_delegate_remembers_earlier_conversation_in_the_same_run(self):
        run = self.make_run()
        models = self.execute_team(
            run,
            Revisor=[
                delegate_call("Programador", "Crie a função", "call_1"),
                delegate_call("Programador", "Agora renomeie", "call_2"),
                AIMessage(content="Fim."),
            ],
            Programador=[AIMessage(content="Criei soma()."), AIMessage(content="Renomeei para somar().")],
        )
        self.assertEqual(run.result, "Fim.")
        second = [message.content for message in models["Programador"].calls[1][1:]]
        self.assertEqual(
            second,
            ["Tarefa recebida de Revisor:\nCrie a função", "Criei soma().", "Tarefa recebida de Revisor:\nAgora renomeie"],
        )

    def test_delegate_can_delegate_further_but_not_back_to_who_is_waiting(self):
        self.coder.delegates.set([self.tester, self.agent])
        run = self.make_run()
        models = self.execute_team(
            run,
            Revisor=[delegate_call("Programador", "Implemente e teste"), AIMessage(content="Entregue.")],
            Programador=[
                delegate_call("Revisor", "Faça você", "call_back"),
                delegate_call("Testador", "Rode os testes", "call_t"),
                AIMessage(content="Implementado e testado."),
            ],
            Testador=[AIMessage(content="Testes passaram.")],
        )
        self.assertEqual(run.result, "Entregue.")
        coder = models["Programador"]
        self.assertIn("Revisor está esperando esta tarefa terminar", coder.calls[1][-1].content)
        self.assertEqual(coder.calls[2][-1].content, "Testes passaram.")
        self.assertIn("Você foi chamado pelo agente Programador", models["Testador"].calls[0][0].content)

    def test_unknown_or_inactive_agent_cannot_receive_tasks(self):
        self.tester.is_active = False
        self.tester.save()
        run = self.make_run()
        models = self.execute_team(
            run,
            Revisor=[
                delegate_call("Testador", "Rode os testes", "call_1"),
                delegate_call("Fulano", "Qualquer coisa", "call_2"),
                AIMessage(content="Não deu."),
            ],
        )
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        lead = models["Revisor"]
        self.assertEqual(lead.calls[1][-1].content, "Agente indisponível: Testador. Opções: Programador.")
        self.assertIn("Agente indisponível: Fulano", lead.calls[2][-1].content)

    def test_agent_without_delegates_has_no_delegation_tool(self):
        self.agent.delegates.clear()
        run = self.make_run()
        models = self.execute_team(
            run, Revisor=[delegate_call("Programador", "Faça"), AIMessage(content="Sozinho então.")]
        )
        self.assertIsNone(models["Revisor"].bound_tools)
        self.assertIn("Ferramenta indisponível: delegate_task", models["Revisor"].calls[1][-1].content)

    def test_approval_requested_by_a_delegate_pauses_and_resumes_the_whole_run(self):
        run = self.make_run()
        responses = {
            "Revisor": [delegate_call("Testador", "Rode os testes"), AIMessage(content="Testes ok.")],
            "Testador": [tool_call("run_command", command="touch testado.txt"), AIMessage(content="Passaram.")],
        }
        # Checkpointer real: a pilha de agentes e as conversas passam pelo Postgres entre a pausa e a retomada
        with mock.patch.object(tasks, "open_checkpointer", real_open_checkpointer):
            self.execute_team(run, **responses)
            self.assertEqual(run.status, Run.Status.WAITING, run.events.last())
            approval = run.approvals.get()
            self.assertEqual((approval.agent, approval.tool_name), (self.tester, "run_command"))
            self.assertContains(self.client.get(reverse("orchestrator:approval_list")), "Testador em Site")

            with mock.patch.object(tasks.execute_run, "delay"):
                self.client.post(reverse("orchestrator:approval_decide", args=[approval.pk]), {"decision": "approve"})
            models = self.execute_team(run)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Testes ok."), run.events.last())
        self.assertTrue((workspace_path(run) / "testado.txt").exists())
        self.assertEqual((len(models["Revisor"].calls), len(models["Testador"].calls)), (2, 2))
        self.assertEqual(models["Revisor"].calls[1][-1].content, "Passaram.")
