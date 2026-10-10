import os
from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from ..models import Agent, Approval, Project, Run
from .base import RunTestCase


class HomeDefaultsTests(RunTestCase):
    def initial(self):
        form = self.client.get(reverse("home")).context["form"]
        return form["project"].value(), form["agent"].value()

    def test_the_only_project_and_agent_come_selected(self):
        self.assertEqual(self.initial(), (self.project.pk, self.agent.pk))

    def test_with_several_options_nothing_is_selected_before_the_first_task(self):
        Project.objects.create(name="API", repo_url="https://example.com/api.git")
        Agent.objects.create(name="Programador", provider="openai", model="gpt-x")
        self.assertEqual(self.initial(), (None, None))

    def test_last_choice_of_the_user_is_remembered(self):
        api = Project.objects.create(name="API", repo_url="https://example.com/api.git")
        coder = Agent.objects.create(name="Programador", provider="openai", model="gpt-x")
        self.make_run(project=api, agent=coder)
        self.assertEqual(self.initial(), (api.pk, coder.pk))
        # Um agente desativado depois não volta selecionado
        coder.is_active = False
        coder.save()
        Agent.objects.create(name="Testador", provider="openai", model="gpt-x")
        self.assertEqual(self.initial(), (api.pk, None))

    def test_agent_options_show_the_model(self):
        page = self.client.get(reverse("home"))
        self.assertContains(page, "Revisor · claude-sonnet-5-5")
        self.assertContains(page, ">Projeto</option>")
        self.assertContains(page, "<title>Nova tarefa · Sonny</title>")


class HomeKeyWarningTests(RunTestCase):
    def test_agents_whose_provider_has_no_key_are_flagged(self):
        other = Agent.objects.create(name="Programador", provider="openai", model="gpt-x")
        Agent.objects.create(name="Local", provider="ollama", model="llama3.3")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-teste", "OPENAI_API_KEY": ""}):
            page = self.client.get(reverse("home"))
        self.assertEqual(page.context["agents_without_key"], {str(other.pk): "OPENAI_API_KEY"})
        self.assertContains(page, 'id="agents-without-key"')


class OnboardingTests(RunTestCase):
    def steps(self):
        onboarding = self.client.get(reverse("home")).context.get("onboarding")
        return onboarding and [step["done"] for step in onboarding]

    def test_steps_reflect_what_is_already_done(self):
        self.assertEqual(self.steps(), [True, True, False])
        page = self.client.get(reverse("home"))
        self.assertContains(page, "Concluído", count=2)
        self.assertContains(page, "Enviar a primeira tarefa")
        self.assertNotContains(page, "Conectar o GitHub")
        self.assertContains(page, reverse("orchestrator:agent_create"))

    def test_nothing_done_yet(self):
        self.agent.delete()
        self.project.delete()
        self.assertEqual(self.steps(), [False, False, False])

    def test_block_disappears_after_the_first_task(self):
        self.make_run()
        self.assertIsNone(self.steps())
        self.assertNotContains(self.client.get(reverse("home")), "Primeiros passos")
        self.assertTrue(Run.objects.exists())


class HomePanelTests(RunTestCase):
    def test_stats_are_hidden_until_there_is_a_run(self):
        page = self.client.get(reverse("home"))
        self.assertNotIn("stats", page.context)
        self.assertNotContains(page, "Em andamento")

    def test_stats_count_each_situation(self):
        now = timezone.now()
        self.make_run()
        self.make_run(status=Run.Status.RUNNING)
        waiting = self.make_run(status=Run.Status.WAITING)
        Approval.objects.create(run=waiting, tool_call_id="c1", tool_name="run_command", tool_args={})
        self.make_run(status=Run.Status.SUCCEEDED, finished_at=now - timedelta(days=1))
        self.make_run(status=Run.Status.SUCCEEDED, finished_at=now - timedelta(days=30))
        self.make_run(status=Run.Status.FAILED, finished_at=now)
        self.make_run(status=Run.Status.CANCELED, finished_at=now)
        page = self.client.get(reverse("home"))
        self.assertEqual([stat["value"] for stat in page.context["stats"]], [2, 1, 1, 1])
        self.assertContains(page, "Aguardando aprovação")
        self.assertContains(page, reverse("orchestrator:approval_list"))

    def test_recent_activity_links_to_each_run_and_to_the_full_list(self):
        run = self.make_run(prompt="Atualize as dependências")
        page = self.client.get(reverse("home"))
        self.assertContains(page, "Atualize as dependências")
        self.assertContains(page, f'href="{run.get_absolute_url()}"')
        self.assertContains(page, "Site · Revisor · há")
        self.assertContains(page, "Ver todas")

    def test_suggestions_appear_only_when_a_task_can_be_sent(self):
        self.assertContains(self.client.get(reverse("home")), "Rode os testes e corrija o que falhar")
        self.agent.delete()
        self.assertNotContains(self.client.get(reverse("home")), "Rode os testes e corrija o que falhar")
