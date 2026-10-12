from django.urls import reverse
from langchain_core.messages import AIMessage

from ..models import Agent, Skill
from .base import FakeModel, RunTestCase, delegate_call, use_models


class SkillCrudTests(RunTestCase):
    def test_create_edit_and_delete(self):
        self.assertContains(self.client.get(reverse("orchestrator:skill_list")), "Nenhuma skill ainda")
        data = {"name": "Convenções", "description": "Padrões do time", "content": "- Rode os testes."}
        response = self.client.post(reverse("orchestrator:skill_create"), data, follow=True)
        self.assertRedirects(response, reverse("orchestrator:skill_list"))
        self.assertContains(response, "Padrões do time")
        self.assertContains(response, "Nenhum agente usa")
        skill = Skill.objects.get()

        self.client.post(skill.get_absolute_url(), data | {"content": "- Rode o lint."})
        skill.refresh_from_db()
        self.assertEqual(skill.content, "- Rode o lint.")

        self.agent.skills.add(skill)
        self.assertContains(self.client.get(skill.get_absolute_url()), f'href="{self.agent.get_absolute_url()}"')
        self.assertContains(self.client.get(reverse("orchestrator:skill_delete", args=[skill.pk])), "Excluir “Convenções”?")
        self.client.post(reverse("orchestrator:skill_delete", args=[skill.pk]))
        self.assertFalse(Skill.objects.exists())
        self.assertTrue(Agent.objects.filter(pk=self.agent.pk).exists())  # o agente só perde a skill

    def test_name_and_content_are_required(self):
        response = self.client.post(reverse("orchestrator:skill_create"), {"name": "", "content": ""})
        self.assertContains(response, "Este campo é obrigatório", count=2)

    def test_section_is_highlighted_in_the_sidebar(self):
        page = self.client.get(reverse("orchestrator:skill_list"))
        self.assertEqual(page.context["sidebar"]["section"], "skills")

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("orchestrator:skill_list")).status_code, 302)


class AgentSkillsTests(RunTestCase):
    def test_agent_form_points_to_skills_when_there_is_none(self):
        page = self.client.get(self.agent.get_absolute_url())
        self.assertNotIn("skills", page.context["form"].fields)
        self.assertContains(page, reverse("orchestrator:skill_create"))

    def test_skills_are_chosen_on_the_agent_form(self):
        first = Skill.objects.create(name="Convenções", description="Padrões do time", content="x")
        Skill.objects.create(name="Revisão", content="y")
        page = self.client.get(self.agent.get_absolute_url())
        self.assertContains(page, "Padrões do time")
        self.client.post(
            self.agent.get_absolute_url(), {"name": "Revisor", "provider": "anthropic", "model": "m", "skills": [first.pk]}
        )
        self.assertEqual(list(self.agent.skills.all()), [first])

    def test_skill_content_goes_into_the_instructions_of_the_agent(self):
        self.agent.skills.add(Skill.objects.create(name="Convenções", content="- Commits em português."))
        Skill.objects.create(name="De outro agente", content="não deve aparecer")
        run = self.make_run()
        model = self.execute(run, AIMessage(content="ok"))
        system = model.calls[0][0].content
        self.assertIn("Seja direto.\n\n## Skill: Convenções\n\n- Commits em português.\n\nProjeto: Site", system)
        self.assertNotIn("não deve aparecer", system)

    def test_each_agent_of_a_delegation_gets_only_its_own_skills(self):
        helper = Agent.objects.create(name="Programador", provider="openai", model="m")
        helper.skills.add(Skill.objects.create(name="Testes", content="Sempre escreva testes."))
        self.agent.delegates.add(helper)
        lead = FakeModel(delegate_call("Programador", "Faça"), AIMessage(content="Feito."))
        worker = FakeModel(AIMessage(content="Pronto."))
        run = self.make_run()
        with use_models({"Revisor": lead, "Programador": worker}):
            from .. import tasks

            tasks.execute_run(run.pk)
        self.assertIn("Sempre escreva testes.", worker.calls[0][0].content)
        self.assertNotIn("Sempre escreva testes.", lead.calls[0][0].content)
