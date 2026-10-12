from langchain_core.messages import AIMessage

from ..models import Run, RunEvent
from ..timeline import build_timeline
from .base import RunTestCase, tool_call

HTMX = {"HX-Request": "true"}
Kind = RunEvent.Kind


class TimelineTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(status=Run.Status.RUNNING)

    def add(self, kind, content, agent=True):
        return self.run_.events.create(kind=kind, content=content, agent=self.agent if agent else None)

    def timeline(self):
        return build_timeline(self.run_, self.run_.events.select_related("agent"))

    def test_tool_call_and_its_result_become_one_item(self):
        self.add(Kind.STATUS, "Na fila.", agent=False)
        call = self.add(Kind.TOOL_CALL, 'read_file\n{\n  "path": "app/views.py"\n}')
        result = self.add(Kind.TOOL_RESULT, "def home(): ...")
        self.add(Kind.MESSAGE, "Li o arquivo.")
        status, tool, message = self.timeline()
        self.assertEqual([status.kind, tool.kind, message.kind], ["status", "tool", "message"])
        self.assertEqual((tool.verb, tool.target, tool.state, tool.label), ("Leu", "app/views.py", "ok", ""))
        self.assertEqual((tool.key, tool.last, tool.result), (call.pk, result.pk, result))

    def test_results_are_matched_in_order_per_agent(self):
        first = self.add(Kind.TOOL_CALL, 'read_file\n{"path": "a.txt"}')
        second = self.add(Kind.TOOL_CALL, 'run_command\n{"command": "npm test\\nmais"}')
        self.add(Kind.TOOL_RESULT, "Erro: arquivo não encontrado")
        self.add(Kind.TOOL_RESULT, "código de saída: 1\nfalhou")
        one, two = self.timeline()
        self.assertEqual((one.key, one.state, one.label), (first.pk, "error", "Erro"))
        self.assertEqual((two.key, two.target, two.state, two.label), (second.pk, "npm test", "warning", "código 1"))

    def test_command_that_passed_and_denied_action(self):
        self.add(Kind.TOOL_CALL, 'run_command\n{"command": "ls"}')
        self.add(Kind.TOOL_RESULT, "código de saída: 0\nREADME.md")
        self.add(Kind.TOOL_CALL, 'run_command\n{"command": "rm -rf ."}')
        self.add(Kind.TOOL_RESULT, "Ação negada pelo usuário.")
        passed, denied = self.timeline()
        self.assertEqual((passed.state, passed.label), ("ok", ""))
        self.assertEqual((denied.state, denied.label), ("error", "Negada"))

    def test_call_without_result_says_why(self):
        self.add(Kind.TOOL_CALL, 'run_command\n{"command": "ls"}')
        self.assertEqual(self.timeline()[0].label, "Em andamento")
        self.run_.status = Run.Status.WAITING
        self.assertEqual(self.timeline()[0].label, "Aguardando aprovação")
        question = self.add(Kind.TOOL_CALL, 'ask_user\n{"question": "Qual banco?\\nPostgres ou SQLite?"}')
        asked = self.timeline()[1]
        self.assertEqual((asked.key, asked.verb, asked.target, asked.label), (question.pk, "Perguntou", "Qual banco?", "Aguardando resposta"))
        self.run_.status = Run.Status.CANCELED
        self.assertEqual((self.timeline()[0].state, self.timeline()[0].label), ("skipped", "Não executada"))

    def test_truncated_call_and_unknown_tool_still_get_a_summary(self):
        self.add(Kind.TOOL_CALL, 'write_file\n{\n  "path": "docs/guia.md",\n  "content": "texto cortado no mei')
        self.add(Kind.TOOL_CALL, "deploy\n{}")
        written, unknown = self.timeline()
        self.assertEqual((written.verb, written.target), ("Escreveu", "docs/guia.md"))
        self.assertEqual((unknown.verb, unknown.target), ("deploy", ""))

    def test_result_without_a_call_is_shown_on_its_own(self):
        self.add(Kind.TOOL_RESULT, "resultado antigo")
        self.assertEqual([item.kind for item in self.timeline()], ["tool_result"])


class ProgressPageTests(RunTestCase):
    agent_tools = ["write_file"]

    def test_page_shows_tools_collapsed_with_a_summary(self):
        run = self.make_run()
        self.execute(run, tool_call("write_file", path="a.txt", content="<b>1</b>"), AIMessage(content="Criei."))
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "<details", count=1)
        self.assertNotContains(page, "<details open")
        self.assertContains(page, '<span class="font-medium text-heading">Escreveu</span>')
        self.assertContains(page, "&lt;b&gt;1&lt;/b&gt;")
        self.assertContains(page, 'id="run-poller"')
        self.assertNotContains(page, "every 2s")

    def test_agent_answers_can_be_copied_as_written(self):
        run = self.make_run()
        self.execute(run, tool_call("write_file", path="a.txt", content="1"), AIMessage(content="**Pronto** <fim>"))
        page = self.client.get(run.get_absolute_url())
        # Uma cópia na resposta do agente, uma na chamada da ferramenta e duas de resultado: o da
        # ferramenta e o da execução. As linhas de situação não têm.
        self.assertContains(page, "Copiar resposta</span>", count=1)
        self.assertContains(page, "Copiar chamada</span>", count=1)
        self.assertContains(page, "Copiar resultado</span>", count=2)
        self.assertNotContains(page, "Copiar texto")
        # O texto copiado é o Markdown original, não o HTML que a tela desenha
        raw = '<textarea x-ref="text" hidden readonly tabindex="-1" aria-hidden="true">**Pronto** &lt;fim&gt;</textarea>'
        self.assertContains(page, raw, count=2)
        self.assertContains(page, "js/clipboard.js")

    def test_errors_notes_and_delegations_can_be_copied_too(self):
        run = self.make_run(status=Run.Status.FAILED)
        run.events.create(kind=Kind.STATUS, content="Na fila.")
        run.events.create(kind=Kind.NOTE, content="Mude o tom")
        run.events.create(kind=Kind.DELEGATION, content="Revisor pediu a Programador:\nFaça", agent=self.agent)
        run.events.create(kind=Kind.ERROR, content="RuntimeError: fora do ar")
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "Copiar texto</span>", count=3)
        self.assertContains(page, 'aria-hidden="true">RuntimeError: fora do ar</textarea>')

    def test_poll_returns_only_what_changed_since_the_last_event_on_screen(self):
        run = self.make_run(status=Run.Status.RUNNING)
        old = run.events.create(kind=Kind.STATUS, content="Na fila.")
        call = run.events.create(kind=Kind.TOOL_CALL, content='read_file\n{"path": "a.txt"}', agent=self.agent)
        url = run.get_absolute_url()

        page = self.client.get(url)
        self.assertContains(page, f'hx-get="{url}?depois={call.pk}"')

        # Nada novo: só a situação e o próximo ponto de consulta
        quiet = self.client.get(url, {"depois": call.pk}, headers=HTMX)
        self.assertEqual((quiet.context["new_items"], quiet.context["updated_items"]), ([], []))
        self.assertContains(quiet, 'id="run-status" hx-swap-oob="true"')
        self.assertNotContains(quiet, "Na fila.")

        result = run.events.create(kind=Kind.TOOL_RESULT, content="conteúdo", agent=self.agent)
        message = run.events.create(kind=Kind.MESSAGE, content="Li.", agent=self.agent)
        poll = self.client.get(url, {"depois": call.pk}, headers=HTMX)
        self.assertEqual([item.key for item in poll.context["updated_items"]], [call.pk])
        self.assertEqual([item.key for item in poll.context["new_items"]], [message.pk])
        self.assertContains(poll, f'id="event-{call.pk}" hx-swap-oob="true"')
        self.assertContains(poll, 'hx-swap-oob="beforeend:#run-events"')
        self.assertContains(poll, f'id="event-{message.pk}" >')
        self.assertContains(poll, f"depois={message.pk}")
        self.assertNotContains(poll, f'id="event-{old.pk}"')
        self.assertGreater(message.pk, result.pk)

    def test_poll_stops_when_the_run_settles(self):
        run = self.make_run(status=Run.Status.SUCCEEDED, result="Pronto.")
        poll = self.client.get(run.get_absolute_url(), {"depois": 0}, headers=HTMX)
        self.assertContains(poll, '<div id="run-poller" ></div>')
        self.assertContains(poll, "Pronto.")
        self.assertEqual(poll["HX-Trigger"], "run-settled")
