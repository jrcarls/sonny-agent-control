import os
from unittest import mock

from django.urls import reverse

from ..models import Agent, Project, Run
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
        self.assertContains(page, "<title>Nova tarefa</title>")


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
