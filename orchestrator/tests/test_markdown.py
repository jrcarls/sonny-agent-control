from django.test import SimpleTestCase

from ..models import Run, RunEvent
from ..templatetags.markdown import render_markdown
from .base import RunTestCase


class RenderMarkdownTests(SimpleTestCase):
    def test_common_markdown_becomes_html(self):
        html = render_markdown("# Título\n\nTexto com **negrito** e `código`.\n\n- um\n- dois\n\n```\n?? NOTAS.md\n```")
        self.assertInHTML("<h1>Título</h1>", html)
        self.assertInHTML("<p>Texto com <strong>negrito</strong> e <code>código</code>.</p>", html)
        self.assertInHTML("<ul><li>um</li><li>dois</li></ul>", html)
        self.assertInHTML("<pre><code>?? NOTAS.md\n</code></pre>", html)

    def test_tables_are_supported(self):
        html = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("<table>", html)
        self.assertInHTML("<td>2</td>", html)

    def test_html_written_by_the_agent_is_shown_as_text(self):
        html = render_markdown('Oi <script>alert(1)</script> <img src=x onerror="alert(2)">')
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_dangerous_links_are_not_turned_into_links(self):
        html = render_markdown("[clique](javascript:alert(1)) e [ok](https://example.com)")
        self.assertNotIn('href="javascript', html)
        self.assertIn('<a href="https://example.com" target="_blank" rel="noopener noreferrer nofollow">ok</a>', html)

    def test_empty_text(self):
        self.assertEqual(render_markdown(""), "")
        self.assertEqual(render_markdown(None), "")


class RunMarkdownTests(RunTestCase):
    def test_result_and_agent_messages_are_rendered_but_other_events_stay_plain(self):
        run = self.make_run(status=Run.Status.SUCCEEDED, result="Criei o arquivo `NOTAS.md`.")
        run.events.create(kind=RunEvent.Kind.MESSAGE, agent=self.agent, content="Vou **ler** o README.")
        run.events.create(kind=RunEvent.Kind.TOOL_RESULT, agent=self.agent, content="**não é markdown** <b>x</b>")
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "Criei o arquivo <code>NOTAS.md</code>.")
        self.assertContains(page, "Vou <strong>ler</strong> o README.")
        self.assertContains(page, "**não é markdown** &lt;b&gt;x&lt;/b&gt;")
