"""Apoio comum aos testes: modelos de mentira e a base com repositório git temporário."""

import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from .. import tasks
from ..models import Agent, Project, Run
from ..workspace import git


# Guardado antes de o setUp trocar por um checkpointer em memória
real_open_checkpointer = tasks.open_checkpointer


class FakeModel:
    """Modelo de mentira: devolve as respostas na ordem e guarda o que recebeu."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = [item.name for item in tools]
        return self

    def invoke(self, messages):
        self.calls.append(messages)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response()
        return response


def tool_call(name, call_id="call_1", **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def use_model(model):
    return mock.patch("orchestrator.graph.build_chat_model", return_value=model)


def use_models(models):
    """Um modelo de mentira por agente, escolhido pelo nome do agente."""
    return mock.patch(
        "orchestrator.graph.build_chat_model", side_effect=lambda agent: models[agent.name]
    )


def delegate_call(agent, task, call_id="call_d"):
    return tool_call("delegate_task", call_id=call_id, agent=agent, task=task)


class RunTestCase(TestCase):
    """Base com usuário logado, um repositório git local como projeto e workspaces em pasta temporária."""

    agent_tools = []

    def setUp(self):
        self.user = get_user_model().objects.create_user("ana", password="x")
        self.client.force_login(self.user)

        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.remote = self.tmp / "remote"
        self.remote.mkdir()
        git("init", "-b", "main", cwd=self.remote)
        (self.remote / "README.md").write_text("# Site\nProjeto de exemplo.\n")
        git("add", "-A", cwd=self.remote)
        git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "inicial", cwd=self.remote)
        self.enterContext(self.settings(WORKSPACES_DIR=self.tmp / "workspaces"))

        # Um único checkpointer em memória por teste: o estado do grafo sobrevive entre chamadas da tarefa
        saver = InMemorySaver()

        @contextmanager
        def memory_checkpointer():
            yield saver

        self.enterContext(mock.patch.object(tasks, "open_checkpointer", memory_checkpointer))

        self.project = Project.objects.create(name="Site", repo_url=str(self.remote))
        self.agent = Agent.objects.create(
            name="Revisor",
            provider="anthropic",
            model="claude-sonnet-5-5",
            system_prompt="Seja direto.",
            tools=self.agent_tools,
        )

    def make_run(self, **kwargs):
        defaults = {"project": self.project, "agent": self.agent, "prompt": "Resuma o README", "created_by": self.user}
        return Run.objects.create(**defaults | kwargs)

    def execute(self, run, *responses):
        model = FakeModel(*responses)
        with use_model(model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        return model
