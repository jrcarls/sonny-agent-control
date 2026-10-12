"""Fluxo completo pelas telas, do cadastro ao resultado, com modelos de mentira no lugar dos provedores."""

from decimal import Decimal
from unittest import mock

from django.urls import reverse
from langchain_core.messages import AIMessage

from .. import tasks
from ..models import Agent, Approval, Run, RunEvent, Skill
from ..workspace import workspace_path
from .base import FakeModel, RunTestCase, delegate_call, tool_call, use_models

HTMX = {"HX-Request": "true"}


def reply(content="", calls=(), tokens=(0, 0)):
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens)}
    return AIMessage(content=content, tool_calls=list(calls), usage_metadata=usage)


def call(name, call_id, **args):
    return tool_call(name, call_id=call_id, **args).tool_calls[0]


class FullFlowTests(RunTestCase):
    def post(self, url, data):
        """Envia um formulário como a tela faz; o que iria para a fila do Celery fica só registrado."""
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, data, follow=True)
        return response, delay

    def work(self, run, **models):
        """Faz o papel do worker: roda a execução com um modelo de mentira por agente."""
        with use_models(models):
            tasks.execute_run(run.pk)
        run.refresh_from_db()

    def test_from_setup_to_review(self):
        # 1. Cadastros: uma skill, um revisor e um programador que segue a skill e delega ao revisor
        self.post(reverse("orchestrator:skill_create"), {"name": "Convenções", "content": "- Sempre rode o que escrever."})
        skill = Skill.objects.get()
        self.post(
            reverse("orchestrator:agent_create"),
            {"name": "Leitor", "description": "Revisa textos", "provider": "openai", "model": "gpt-5", "tools": ["read_file"]},
        )
        reviewer = Agent.objects.get(name="Leitor")
        self.post(
            reverse("orchestrator:agent_create"),
            {
                "name": "Programador",
                "provider": "anthropic",
                "model": "claude-sonnet-5-5",
                "input_price": "3,00",
                "output_price": "15,00",
                "skills": [skill.pk],
                "tools": ["read_file", "write_file", "run_command"],
                "delegates": [reviewer.pk],
            },
        )
        coder = Agent.objects.get(name="Programador")
        self.assertEqual((list(coder.skills.all()), list(coder.delegates.all())), ([skill], [reviewer]))

        # 2. Tarefa enviada pela tela inicial, em modo autônomo
        response, delay = self.post(
            reverse("home"),
            {"prompt": "Crie o NOTAS.md e peça revisão", "project": self.project.pk, "agent": coder.pk, "autonomous": "on"},
        )
        run = Run.objects.get()
        delay.assert_called_once_with(run.pk)
        self.assertRedirects(response, run.get_absolute_url())
        self.assertEqual((run.status, run.autonomous), (Run.Status.QUEUED, True))

        # 3. O worker roda: escreve, executa um comando sem pedir aprovação, delega e responde
        lead = FakeModel(
            reply("Vou criar o arquivo.", [call("write_file", "c1", path="NOTAS.md", content="# Notas\n")], (1000, 50)),
            reply(calls=[call("run_command", "c2", command="cat NOTAS.md")], tokens=(1200, 20)),
            reply(calls=[delegate_call("Leitor", "Revise o NOTAS.md", call_id="c3").tool_calls[0]], tokens=(1300, 30)),
            reply("Arquivo criado e revisado.", tokens=(1500, 40)),
        )
        helper = FakeModel(
            reply(calls=[call("read_file", "r1", path="NOTAS.md")], tokens=(400, 10)), reply("Está bom.", tokens=(450, 5))
        )
        self.work(run, Programador=lead, Leitor=helper)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Arquivo criado e revisado."), run.events.last())
        self.assertFalse(run.approvals.exists())
        self.assertIn("## Skill: Convenções", lead.calls[0][0].content)
        self.assertNotIn("## Skill: Convenções", helper.calls[0][0].content)
        self.assertEqual((run.input_tokens, run.output_tokens), (5850, 155))

        # 4. Tela da execução: ferramentas resumidas, delegação, resultado e as opções de revisão
        page = self.client.get(run.get_absolute_url())
        for text in ["Escreveu", "Rodou", "Leu", "Leitor entregou a resposta a Programador", ">Autônomo</span>",
                     "Arquivo criado e revisado.", "Pedir um ajuste", "5.850 tokens de entrada", "cerca de US$ 0,02"]:
            self.assertContains(page, text)
        self.assertContains(page, "<details", count=3)

        # 5. Aba Alterações: o arquivo novo, ainda sem commit
        diff = self.client.get(reverse("orchestrator:run_diff", args=[run.pk]), headers=HTMX)
        self.assertEqual([(item.path, item.status) for item in diff.context["files"]], [("NOTAS.md", "added")])

        # 6. Pedido de ajuste: o agente continua a conversa no mesmo worktree
        _, delay = self.post(reverse("orchestrator:run_follow_up", args=[run.pk]), {"message": "Acrescente uma linha"})
        delay.assert_called_once_with(run.pk)
        again = FakeModel(
            reply(calls=[call("write_file", "c4", path="NOTAS.md", content="# Notas\nmais uma\n")], tokens=(2000, 30)),
            reply("Acrescentei.", tokens=(2100, 10)),
        )
        self.work(run, Programador=again)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Acrescentei."))
        self.assertEqual((workspace_path(run) / "NOTAS.md").read_text(), "# Notas\nmais uma\n")
        self.assertEqual(again.calls[0][-1].content, "Acrescente uma linha")
        self.assertTrue(run.events.filter(kind=RunEvent.Kind.NOTE).exists())

        # 7. Sem o modo autônomo, o mesmo comando para e espera a decisão de uma pessoa
        _, delay = self.post(reverse("home"), {"prompt": "Rode os testes", "project": self.project.pk, "agent": coder.pk})
        careful = Run.objects.exclude(pk=run.pk).get()
        asking = FakeModel(reply(calls=[call("run_command", "c5", command="touch testado.txt")]), reply("Rodei."))
        self.work(careful, Programador=asking)
        self.assertEqual(careful.status, Run.Status.WAITING)
        self.assertContains(self.client.get(reverse("home")), 'data-pending-approvals="1"')
        _, delay = self.post(
            reverse("orchestrator:approval_decide", args=[careful.approvals.get().pk]), {"decision": "approve"}
        )
        delay.assert_called_once_with(careful.pk)
        self.work(careful, Programador=asking)
        self.assertEqual((careful.status, careful.approvals.get().status), (Run.Status.SUCCEEDED, Approval.Status.APPROVED))
        self.assertTrue((workspace_path(careful) / "testado.txt").exists())

        # 8. Lista de execuções e uso: as duas aparecem, com o custo pelos preços do agente
        listed = self.client.get(reverse("orchestrator:run_list"), {"agente": coder.pk, "situacao": "finalizadas"})
        self.assertEqual(set(listed.context["runs"]), {run, careful})
        usage = self.client.get(reverse("orchestrator:usage"))
        self.assertEqual(usage.context["totals"]["input"], 5850 + 4100)
        self.assertEqual(usage.context["totals"]["cost"], (Decimal(9950) * 3 + Decimal(195) * 15) / Decimal(1_000_000))
        self.assertEqual([row["name"] for row in usage.context["by_agent"]], ["Programador"])
