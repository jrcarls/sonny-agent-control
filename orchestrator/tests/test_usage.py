from datetime import timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from ..models import Agent, Project, Run
from .base import RunTestCase


class UsageTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("orchestrator:usage")
        self.agent.input_price, self.agent.output_price = Decimal("3"), Decimal("15")
        self.agent.save()
        self.free = Agent.objects.create(name="Local", provider="ollama", model="llama3.3")
        self.api = Project.objects.create(name="API", repo_url="git@github.com:org/api.git")

    def spend(self, input_tokens, output_tokens, days_ago=0, **kwargs):
        run = self.make_run(input_tokens=input_tokens, output_tokens=output_tokens, **kwargs)
        Run.objects.filter(pk=run.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return run

    def test_totals_and_breakdowns(self):
        self.spend(1_000_000, 200_000)  # 3 + 3 = US$ 6
        self.spend(500_000, 0, project=self.api)  # US$ 1,50
        self.spend(40_000, 10_000, agent=self.free)  # sem preço
        self.make_run()  # sem consumo: conta como execução, não aparece nas tabelas
        page = self.client.get(self.url)
        totals = page.context["totals"]
        self.assertEqual((totals["runs"], totals["input"], totals["output"]), (4, 1_540_000, 210_000))
        self.assertEqual((totals["cost"], totals["unpriced"]), (Decimal("7.5"), 1))
        self.assertEqual(
            [(row["name"], row["runs"], row["input"], row["cost"]) for row in page.context["by_project"]],
            [("Site", 3, 1_040_000, Decimal("6")), ("API", 1, 500_000, Decimal("1.5"))],
        )
        self.assertEqual(
            [(row["name"], row["cost"], row["unpriced"]) for row in page.context["by_agent"]],
            [("Revisor", Decimal("7.5"), 0), ("Local", None, 1)],
        )
        self.assertContains(page, "US$ 7,50")
        self.assertContains(page, "1.540.000")
        self.assertContains(page, "1 execução ficou fora do custo")

    def test_period_filter(self):
        self.spend(100, 0)
        self.spend(200, 0, days_ago=10)
        self.spend(400, 0, days_ago=60)
        for period, expected in [("7", 100), ("30", 300), ("tudo", 700), ("invalido", 300)]:
            with self.subTest(period=period):
                page = self.client.get(self.url, {"periodo": period})
                self.assertEqual(page.context["totals"]["input"], expected)
        self.assertEqual(self.client.get(self.url).context["period"], "30")

    def test_top_runs_are_ordered_by_tokens_and_link_to_the_run(self):
        small, big = self.spend(10, 0, prompt="pequena"), self.spend(5_000, 5_000, prompt="grande")
        page = self.client.get(self.url)
        self.assertEqual(list(page.context["top_runs"]), [big, small])
        self.assertContains(page, f'href="{big.get_absolute_url()}"')

    def test_empty_period_and_sidebar(self):
        page = self.client.get(self.url)
        self.assertContains(page, "Nenhum consumo neste período")
        self.assertEqual(page.context["sidebar"]["section"], "usage")

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)


class AgentPriceTests(RunTestCase):
    def test_price_is_typed_and_shown_with_a_comma(self):
        data = {"name": "Revisor", "provider": "anthropic", "model": "m", "input_price": "3,5", "output_price": "15"}
        self.assertEqual(self.client.post(self.agent.get_absolute_url(), data).status_code, 302)
        self.agent.refresh_from_db()
        self.assertEqual((self.agent.input_price, self.agent.output_price), (Decimal("3.5"), Decimal("15")))
        self.assertContains(self.client.get(self.agent.get_absolute_url()), 'value="3,5000"')

    def test_price_is_optional_and_must_be_a_number(self):
        data = {"name": "Revisor", "provider": "anthropic", "model": "m"}
        self.assertEqual(self.client.post(self.agent.get_absolute_url(), data).status_code, 302)
        self.assertEqual(self.client.post(self.agent.get_absolute_url(), data | {"input_price": "caro"}).status_code, 200)

    def test_run_shows_its_estimated_cost_only_when_the_agent_has_a_price(self):
        run = self.make_run(input_tokens=1_000_000, output_tokens=100_000)
        self.assertIsNone(run.estimated_cost)
        self.assertNotContains(self.client.get(run.get_absolute_url()), "US$")
        Agent.objects.filter(pk=self.agent.pk).update(input_price=Decimal("3"), output_price=Decimal("15"))
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "1.000.000 tokens de entrada, 100.000 de saída · cerca de US$ 4,50")
