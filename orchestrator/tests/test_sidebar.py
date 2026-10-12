from django.test import TestCase
from django.urls import reverse

from ..models import Approval, Run
from .base import RunTestCase


class SidebarTests(RunTestCase):
    def sidebar(self, url):
        return self.client.get(url).context["sidebar"]

    def test_current_section_is_highlighted(self):
        run = self.make_run()
        cases = {
            reverse("home"): "",
            reverse("orchestrator:run_list") + "?situacao=ativas": "runs",
            run.get_absolute_url(): "runs",
            reverse("orchestrator:approval_list"): "approvals",
            reverse("orchestrator:project_list") + "?novo=1": "projects",
            self.agent.get_absolute_url(): "agents",
        }
        for url, section in cases.items():
            with self.subTest(url=url):
                self.assertEqual(self.sidebar(url)["section"], section)
        page = self.client.get(reverse("orchestrator:project_list"))
        self.assertContains(page, 'aria-current="page"', count=1)

    def test_counters_and_recent_runs(self):
        home = reverse("home")
        self.assertEqual(self.sidebar(home)["pending_approvals"], 0)
        self.assertNotContains(self.client.get(home), "Recentes")

        waiting = self.make_run(prompt="Rodar os testes", status=Run.Status.WAITING)
        Approval.objects.create(run=waiting, tool_call_id="c1", tool_name="run_command", tool_args={})
        Approval.objects.create(
            run=waiting, tool_call_id="c2", tool_name="run_command", tool_args={}, status=Approval.Status.APPROVED
        )
        self.make_run(prompt="Corrigir o bug")
        self.make_run(prompt="Tarefa antiga", status=Run.Status.SUCCEEDED)

        sidebar = self.sidebar(home)
        self.assertEqual((sidebar["pending_approvals"], sidebar["running_runs"]), (1, 1))
        page = self.client.get(home)
        self.assertContains(page, '1<span class="sr-only"> pendentes</span>')
        self.assertContains(page, "Rodar os testes")
        self.assertContains(page, "Corrigir o bug")

    def test_only_the_latest_runs_are_listed(self):
        for number in range(10):
            self.make_run(prompt=f"tarefa {number}")
        prompts = [run.prompt for run in self.sidebar(reverse("home"))["recent_runs"]]
        self.assertEqual(prompts, [f"tarefa {number}" for number in range(9, 1, -1)])

    def test_htmx_refresh_returns_the_menu_for_the_page_the_user_is_on(self):
        run = self.make_run()
        current = "http://testserver" + run.get_absolute_url()
        response = self.client.get(
            reverse("orchestrator:sidebar"), headers={"HX-Request": "true", "HX-Current-URL": current}
        )
        self.assertTemplateUsed(response, "layouts/sidebar_nav.html")
        self.assertTemplateNotUsed(response, "layouts/app.html")
        self.assertEqual(response.context["sidebar"]["section"], "runs")
        self.assertContains(response, 'aria-current="page"', count=2)  # o item Execuções e a execução na lista

    def test_user_menu_items(self):
        page = self.client.get(reverse("home"))
        for label in ["Integrações", "Modo escuro", "Sair"]:
            self.assertContains(page, label)
        # Nada no menu aponta para lugar nenhum
        self.assertNotContains(page, 'href="#"')


class AnonymousSidebarTests(TestCase):
    def test_menu_fragment_requires_login_and_login_page_runs_no_sidebar_queries(self):
        response = self.client.get(reverse("orchestrator:sidebar"))
        self.assertEqual(response.status_code, 302)
        with self.assertNumQueries(0):
            self.assertEqual(self.client.get(reverse("account_login")).status_code, 200)
