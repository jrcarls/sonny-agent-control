import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from . import tasks
from .models import Agent, Approval, Project, Run, RunEvent
from .tools import ToolError, build_tools
from .workspace import git, prepare_workspace, workspace_path


class LoginRequiredTests(TestCase):
    def test_anonymous_is_redirected_to_login(self):
        for name in ["home", "orchestrator:project_list", "orchestrator:agent_create", "orchestrator:run_list"]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/accounts/login/", response["Location"])


class ProjectViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("ana", password="x"))

    def test_empty_list(self):
        response = self.client.get(reverse("orchestrator:project_list"))
        self.assertContains(response, "Nenhum projeto ainda")

    def test_create(self):
        response = self.client.post(
            reverse("orchestrator:project_create"),
            {"name": "Site", "repo_url": "git@github.com:org/site.git", "default_branch": "main", "description": ""},
            follow=True,
        )
        self.assertRedirects(response, reverse("orchestrator:project_list"))
        self.assertContains(response, "git@github.com:org/site.git")
        self.assertContains(response, "criado")

    def test_create_requires_repo(self):
        response = self.client.post(reverse("orchestrator:project_create"), {"name": "Site", "default_branch": "main"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Project.objects.exists())
        self.assertContains(response, "Este campo é obrigatório")

    def test_update(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        response = self.client.get(project.get_absolute_url())
        self.assertContains(response, 'value="https://example.com/a.git"')
        self.client.post(
            project.get_absolute_url(),
            {"name": "Site", "repo_url": "https://example.com/b.git", "default_branch": "develop", "description": "x"},
        )
        project.refresh_from_db()
        self.assertEqual((project.repo_url, project.default_branch), ("https://example.com/b.git", "develop"))

    def test_delete(self):
        project = Project.objects.create(name="Site", repo_url="https://example.com/a.git")
        url = reverse("orchestrator:project_delete", args=[project.pk])
        self.assertContains(self.client.get(url), "Excluir “Site”?")
        self.assertRedirects(self.client.post(url), reverse("orchestrator:project_list"))
        self.assertFalse(Project.objects.exists())


class AgentViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("ana", password="x"))

    def test_create_and_list(self):
        response = self.client.post(
            reverse("orchestrator:agent_create"),
            {"name": "Revisor", "description": "Revisa PRs", "provider": "anthropic", "model": "claude-sonnet-5-5",
             "system_prompt": "Seja direto.", "is_active": "on"},
            follow=True,
        )
        self.assertRedirects(response, reverse("orchestrator:agent_list"))
        self.assertContains(response, "Revisor")
        self.assertContains(response, "Anthropic")
        self.assertContains(response, "Ativo")

    def test_update_keeps_selected_provider_and_can_deactivate(self):
        agent = Agent.objects.create(name="Revisor", provider="openai", model="gpt-x")
        response = self.client.get(agent.get_absolute_url())
        self.assertContains(response, '<option value="openai" selected>OpenAI</option>', html=True)
        self.client.post(agent.get_absolute_url(), {"name": "Revisor", "provider": "openai", "model": "gpt-x"})
        agent.refresh_from_db()
        self.assertFalse(agent.is_active)

    def test_rejects_unknown_provider(self):
        response = self.client.post(
            reverse("orchestrator:agent_create"), {"name": "X", "provider": "nope", "model": "m"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Agent.objects.exists())

    def test_delete(self):
        agent = Agent.objects.create(name="Revisor", provider="openai", model="gpt-x")
        self.client.post(reverse("orchestrator:agent_delete", args=[agent.pk]))
        self.assertFalse(Agent.objects.exists())


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
        return self.responses.pop(0)


def tool_call(name, call_id="call_1", **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def use_model(model):
    return mock.patch("izzyone_agent_control.orchestrator.graph.build_chat_model", return_value=model)


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


class HomeViewTests(RunTestCase):
    def test_submitting_prompt_creates_run_and_enqueues_it(self):
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("home"), {"prompt": "Resuma o README", "project": self.project.pk, "agent": self.agent.pk}
            )
        run = Run.objects.get()
        self.assertRedirects(response, run.get_absolute_url())
        self.assertEqual((run.status, run.created_by), (Run.Status.QUEUED, self.user))
        delay.assert_called_once_with(run.pk)

    def test_inactive_agent_is_not_accepted(self):
        self.agent.is_active = False
        self.agent.save()
        with mock.patch.object(tasks.execute_run, "delay") as delay:
            response = self.client.post(
                reverse("home"), {"prompt": "x", "project": self.project.pk, "agent": self.agent.pk}
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Run.objects.exists())
        delay.assert_not_called()

    def test_form_is_disabled_without_projects(self):
        self.project.delete()
        response = self.client.get(reverse("home"))
        self.assertFalse(response.context["can_submit"])
        self.assertContains(response, "adicione um projeto")

    def test_recent_runs_are_listed(self):
        self.make_run(prompt="Atualize as dependências")
        self.assertContains(self.client.get(reverse("home")), "Atualize as dependências")


class ExecuteRunTests(RunTestCase):
    def test_success_stores_result_and_events(self):
        run = self.make_run()
        model = self.execute(run, AIMessage(content="É um orquestrador."))
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "É um orquestrador."))
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(
            list(run.events.values_list("kind", flat=True)),
            [RunEvent.Kind.STATUS, RunEvent.Kind.STATUS, RunEvent.Kind.MESSAGE, RunEvent.Kind.STATUS],
        )
        self.assertIsNone(model.bound_tools)  # agente sem ferramentas só conversa

    def test_agent_receives_instructions_project_and_task(self):
        run = self.make_run()
        model = self.execute(run, AIMessage(content="ok"))
        system, human = model.calls[0]
        self.assertIn("Seja direto.", system.content)
        self.assertIn(str(self.remote), system.content)
        self.assertIn(f"izzyone/run-{run.pk}", system.content)
        self.assertEqual(human.content, "Resuma o README")

    def test_failure_is_recorded(self):
        run = self.make_run()
        with mock.patch(
            "izzyone_agent_control.orchestrator.graph.build_chat_model", side_effect=RuntimeError("sem chave")
        ):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(run.events.last().content, "RuntimeError: sem chave")

    def test_unreachable_repository_fails_the_run(self):
        self.project.repo_url = str(self.tmp / "nao-existe")
        self.project.save()
        run = self.make_run()
        self.execute(run)
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIn("git fetch falhou", run.events.last().content)

    def test_run_that_already_started_is_not_executed_again(self):
        run = self.make_run(status=Run.Status.RUNNING)
        with mock.patch("izzyone_agent_control.orchestrator.graph.build_chat_model") as build:
            tasks.execute_run(run.pk)
        build.assert_not_called()
        self.assertFalse(run.events.exists())


class CheckpointerTests(RunTestCase):
    def test_graph_state_is_saved_in_postgres(self):
        run = self.make_run()
        with mock.patch.object(tasks, "open_checkpointer", real_open_checkpointer):
            self.execute(run, AIMessage(content="ok"))
        self.assertEqual(run.status, Run.Status.SUCCEEDED, run.events.last())
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from checkpoints where thread_id = %s", [str(run.pk)])
            self.assertGreater(cursor.fetchone()[0], 0)


class CheckpointerApprovalTests(RunTestCase):
    agent_tools = ["run_command"]

    def test_run_resumes_from_state_saved_in_postgres(self):
        """Cada chamada da tarefa abre uma conexão nova: a pausa e a retomada passam pelo banco."""
        run = self.make_run()
        model = FakeModel(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        with mock.patch.object(tasks, "open_checkpointer", real_open_checkpointer), use_model(model):
            tasks.execute_run(run.pk)
            run.refresh_from_db()
            self.assertEqual(run.status, Run.Status.WAITING, run.events.last())
            with mock.patch.object(tasks.execute_run, "delay"):
                self.client.post(
                    reverse("orchestrator:approval_decide", args=[run.approvals.get().pk]), {"decision": "approve"}
                )
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Pronto."), run.events.last())
        self.assertTrue((workspace_path(run) / "feito.txt").exists())


class WorkspaceTests(RunTestCase):
    def test_each_run_gets_its_own_worktree_and_branch(self):
        first, second = self.make_run(), self.make_run()
        first_path, second_path = prepare_workspace(first), prepare_workspace(second)
        self.assertNotEqual(first_path, second_path)
        self.assertEqual((first_path / "README.md").read_text(), "# Site\nProjeto de exemplo.\n")
        self.assertEqual(git("branch", "--show-current", cwd=first_path).strip(), f"izzyone/run-{first.pk}")
        (first_path / "novo.txt").write_text("x")
        self.assertFalse((second_path / "novo.txt").exists())
        self.assertFalse((self.remote / "novo.txt").exists())

    def test_existing_worktree_is_reused(self):
        run = self.make_run()
        path = prepare_workspace(run)
        (path / "rascunho.txt").write_text("em andamento")
        self.assertEqual(prepare_workspace(run), path)
        self.assertTrue((path / "rascunho.txt").exists())

    def test_new_run_sees_commits_made_after_the_first_clone(self):
        prepare_workspace(self.make_run())
        (self.remote / "depois.txt").write_text("novo")
        git("add", "-A", cwd=self.remote)
        git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "depois", cwd=self.remote)
        self.assertTrue((prepare_workspace(self.make_run()) / "depois.txt").exists())


class ToolTests(RunTestCase):
    agent_tools = ["list_files", "read_file", "search_files", "write_file", "run_command"]

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.workspace = prepare_workspace(self.run_)
        self.tools = {item.name: item for item in build_tools(self.run_, self.workspace)}

    def call(self, name, **args):
        return self.tools[name].invoke(args)

    def test_only_enabled_tools_are_built(self):
        self.assertEqual(set(self.tools), set(self.agent_tools))

    def test_read_list_search_and_write(self):
        self.assertEqual(self.call("list_files"), "README.md")
        self.assertIn("Projeto de exemplo.", self.call("read_file", path="README.md"))
        self.call("write_file", path="src/app.py", content="print('oi')\n")
        self.assertEqual((self.workspace / "src/app.py").read_text(), "print('oi')\n")
        self.assertEqual(self.call("list_files"), "README.md\nsrc/")
        self.assertIn("src/app.py:1:print('oi')", self.call("search_files", pattern="print"))
        self.assertEqual(self.call("search_files", pattern="inexistente"), "Nenhuma ocorrência encontrada.")

    def test_paths_cannot_leave_the_worktree(self):
        secret = self.tmp / "segredo.txt"
        secret.write_text("senha")
        (self.workspace / "atalho").symlink_to(secret)
        for path in ["../../segredo.txt", str(secret), "atalho", ".git", ".git/config"]:
            with self.subTest(path=path), self.assertRaises(ToolError):
                self.call("read_file", path=path)
        with self.assertRaises(ToolError):
            self.call("write_file", path="../fora.txt", content="x")
        self.assertFalse((self.workspace.parent / "fora.txt").exists())

    def test_run_command_reports_exit_code_and_output(self):
        output = self.call("run_command", command="echo oi && ls && exit 3")
        self.assertIn("código de saída: 3", output)
        self.assertIn("oi\nREADME.md", output)


class ToolLoopTests(RunTestCase):
    agent_tools = ["read_file"]

    def test_agent_uses_tool_and_answers(self):
        run = self.make_run()
        model = self.execute(run, tool_call("read_file", path="README.md"), AIMessage(content="É um site."))
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "É um site."))
        self.assertEqual(model.bound_tools, ["read_file"])
        # Na segunda chamada o modelo recebe o conteúdo que a ferramenta leu
        self.assertIn("Projeto de exemplo.", model.calls[1][-1].content)
        kinds = list(run.events.values_list("kind", flat=True))
        self.assertIn(RunEvent.Kind.TOOL_CALL, kinds)
        self.assertIn("Projeto de exemplo.", run.events.get(kind=RunEvent.Kind.TOOL_RESULT).content)
        self.assertFalse(run.approvals.exists())

    def test_tool_errors_and_unknown_tools_go_back_to_the_agent(self):
        run = self.make_run()
        model = self.execute(
            run,
            tool_call("read_file", path="nao-existe.txt"),
            tool_call("run_command", call_id="call_2", command="rm -rf ."),
            AIMessage(content="Não consegui."),
        )
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        self.assertIn("Arquivo não encontrado", model.calls[1][-1].content)
        # run_command não está habilitado para este agente: nem executa nem pede aprovação
        self.assertIn("Ferramenta indisponível", model.calls[2][-1].content)
        self.assertTrue((workspace_path(run) / "README.md").exists())


class ApprovalFlowTests(RunTestCase):
    agent_tools = ["run_command"]

    def start(self, *responses):
        run = self.make_run()
        self.model = FakeModel(*responses)
        with use_model(self.model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()
        return run

    def decide(self, approval, decision):
        with mock.patch.object(tasks.execute_run, "delay") as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("orchestrator:approval_decide", args=[approval.pk]), {"decision": decision}, follow=True
            )
        return response, delay

    def resume(self, run):
        with use_model(self.model):
            tasks.execute_run(run.pk)
        run.refresh_from_db()

    def test_sensitive_tool_waits_for_approval_before_running(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        self.assertEqual(run.status, Run.Status.WAITING)
        self.assertIsNone(run.finished_at)
        approval = run.approvals.get()
        self.assertEqual((approval.status, approval.tool_args), (Approval.Status.PENDING, {"command": "touch feito.txt"}))
        self.assertFalse((workspace_path(run) / "feito.txt").exists())

        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "touch feito.txt")
        self.assertNotContains(page, "hx-trigger")

    def test_approving_runs_the_tool_and_finishes(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Pronto."))
        response, delay = self.decide(run.approvals.get(), "approve")
        delay.assert_called_once_with(run.pk)
        self.assertRedirects(response, run.get_absolute_url())
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.QUEUED)
        approval = run.approvals.get()
        self.assertEqual((approval.status, approval.decided_by), (Approval.Status.APPROVED, self.user))

        self.resume(run)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Pronto."))
        self.assertTrue((workspace_path(run) / "feito.txt").exists())
        self.assertEqual(len(self.model.calls), 2)  # o modelo não é chamado de novo ao retomar a aprovação

    def test_rejecting_skips_the_tool_and_tells_the_agent(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"), AIMessage(content="Entendido."))
        self.decide(run.approvals.get(), "reject")
        self.resume(run)
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Entendido."))
        self.assertFalse((workspace_path(run) / "feito.txt").exists())
        self.assertEqual(self.model.calls[1][-1].content, "Ação negada pelo usuário.")
        self.assertEqual(run.approvals.get().status, Approval.Status.REJECTED)

    def test_run_resumes_only_after_every_pending_request_is_decided(self):
        both = AIMessage(
            content="",
            tool_calls=[
                {"name": "run_command", "args": {"command": "touch a.txt"}, "id": "call_a"},
                {"name": "run_command", "args": {"command": "touch b.txt"}, "id": "call_b"},
            ],
        )
        run = self.start(both, AIMessage(content="Feito."))
        first, second = run.approvals.order_by("tool_call_id")
        _, delay = self.decide(first, "approve")
        delay.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, Run.Status.WAITING)
        _, delay = self.decide(second, "reject")
        delay.assert_called_once_with(run.pk)

        self.resume(run)
        self.assertEqual(run.status, Run.Status.SUCCEEDED)
        self.assertTrue((workspace_path(run) / "a.txt").exists())
        self.assertFalse((workspace_path(run) / "b.txt").exists())

    def test_request_cannot_be_decided_twice(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"))
        approval = run.approvals.get()
        self.decide(approval, "reject")
        response, delay = self.decide(approval, "approve")
        delay.assert_not_called()
        self.assertContains(response, "já tinha sido decidido")
        approval.refresh_from_db()
        self.assertEqual(approval.status, Approval.Status.REJECTED)

    def test_approval_list_separates_pending_from_history(self):
        run = self.start(tool_call("run_command", command="touch feito.txt"))
        url = reverse("orchestrator:approval_list")
        self.assertContains(self.client.get(url), "touch feito.txt")
        self.assertContains(self.client.get(url, {"situacao": "historico"}), "Nenhuma decisão registrada")
        self.decide(run.approvals.get(), "approve")
        self.assertContains(self.client.get(url), "Nenhum pedido pendente")
        self.assertContains(self.client.get(url, {"situacao": "historico"}), "Aprovada")


@override_settings(GITHUB_TOKEN="token-de-teste", AGENT_GIT_NAME="Agente", AGENT_GIT_EMAIL="agente@example.com")
class PullRequestToolTests(RunTestCase):
    agent_tools = ["write_file", "open_pull_request"]

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.workspace = prepare_workspace(self.run_)
        # O clone já aponta para o repositório local; daqui em diante o projeto "é" do GitHub
        self.run_.project.repo_url = "git@github.com:org/site.git"
        self.tools = {item.name: item for item in build_tools(self.run_, self.workspace)}

    def open_pr(self, status_code=201):
        response = mock.Mock(status_code=status_code, text="erro do github")
        response.json.return_value = {"html_url": "https://github.com/org/site/pull/7"}
        with mock.patch("izzyone_agent_control.orchestrator.tools.httpx.post", return_value=response) as post:
            result = self.tools["open_pull_request"].invoke({"title": "Adiciona página", "body": "Detalhes"})
        return result, post

    def test_commits_pushes_and_opens_pull_request(self):
        self.tools["write_file"].invoke({"path": "pagina.html", "content": "<h1>Oi</h1>"})
        result, post = self.open_pr()
        self.assertIn("https://github.com/org/site/pull/7", result)
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.pull_request_url, "https://github.com/org/site/pull/7")

        pushed = git("log", "-1", "--format=%an <%ae> %s", self.run_.branch, cwd=self.remote).strip()
        self.assertEqual(pushed, "Agente <agente@example.com> Adiciona página")
        self.assertIn("pagina.html", git("show", "--stat", self.run_.branch, cwd=self.remote))
        self.assertEqual(post.call_args.args[0], "https://api.github.com/repos/org/site/pulls")
        self.assertEqual(
            post.call_args.kwargs["json"],
            {"title": "Adiciona página", "body": "Detalhes", "head": self.run_.branch, "base": "main"},
        )

    def test_second_call_updates_the_same_pull_request(self):
        self.tools["write_file"].invoke({"path": "a.txt", "content": "1"})
        self.open_pr()
        self.tools["write_file"].invoke({"path": "b.txt", "content": "2"})
        result, post = self.open_pr()
        post.assert_not_called()
        self.assertIn("atualizado", result)
        self.assertIn("b.txt", git("show", "--stat", self.run_.branch, cwd=self.remote))

    def test_nothing_to_send_is_an_error(self):
        with self.assertRaisesMessage(ToolError, "Não há alterações"):
            self.open_pr()

    def test_github_refusal_is_reported(self):
        self.tools["write_file"].invoke({"path": "a.txt", "content": "1"})
        with self.assertRaisesMessage(ToolError, "recusou o pull request (422)"):
            self.open_pr(status_code=422)
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.pull_request_url, "")

    def test_requires_token_and_github_repository(self):
        with self.settings(GITHUB_TOKEN=""), self.assertRaisesMessage(ToolError, "GITHUB_TOKEN"):
            self.open_pr()
        self.run_.project.repo_url = "https://gitlab.com/org/site.git"
        with self.assertRaisesMessage(ToolError, "GitHub"):
            self.open_pr()


class AgentToolsFormTests(RunTestCase):
    def test_tools_are_saved_from_the_form(self):
        response = self.client.post(
            self.agent.get_absolute_url(),
            {"name": "Revisor", "provider": "anthropic", "model": "m", "tools": ["read_file", "run_command"]},
        )
        self.assertEqual(response.status_code, 302)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.tools, ["read_file", "run_command"])
        page = self.client.get(self.agent.get_absolute_url())
        self.assertContains(page, 'value="run_command" id="id_tools_4"\n                           checked')

    def test_new_agent_form_suggests_read_only_tools(self):
        page = self.client.get(reverse("orchestrator:agent_create"))
        self.assertEqual(page.context["form"]["tools"].value(), ["list_files", "read_file", "search_files"])

    def test_unknown_tool_is_rejected(self):
        response = self.client.post(
            self.agent.get_absolute_url(), {"name": "Revisor", "provider": "anthropic", "model": "m", "tools": ["rm"]}
        )
        self.assertEqual(response.status_code, 200)


class RunViewTests(RunTestCase):
    def test_list_filters_by_situation(self):
        self.make_run(prompt="tarefa ativa")
        self.make_run(prompt="tarefa esperando", status=Run.Status.WAITING)
        self.make_run(prompt="tarefa pronta", status=Run.Status.SUCCEEDED)
        url = reverse("orchestrator:run_list")
        self.assertContains(self.client.get(url), "tarefa ativa")
        self.assertContains(self.client.get(url), "tarefa pronta")
        active = self.client.get(url, {"situacao": "ativas"})
        self.assertContains(active, "tarefa ativa")
        self.assertContains(active, "tarefa esperando")
        self.assertNotContains(active, "tarefa pronta")
        finished = self.client.get(url, {"situacao": "finalizadas"})
        self.assertNotContains(finished, "tarefa ativa")

    def test_detail_polls_only_while_working(self):
        run = self.make_run()
        self.assertContains(self.client.get(run.get_absolute_url()), 'hx-trigger="every 2s"')
        run.status = Run.Status.SUCCEEDED
        run.result = "Pronto."
        run.save()
        response = self.client.get(run.get_absolute_url())
        self.assertNotContains(response, "hx-trigger")
        self.assertContains(response, "Pronto.")

    def test_htmx_request_gets_only_the_progress_fragment(self):
        run = self.make_run()
        run.events.create(kind=RunEvent.Kind.STATUS, content="Execução colocada na fila.")
        response = self.client.get(run.get_absolute_url(), headers={"HX-Request": "true"})
        self.assertTemplateUsed(response, "runs/_progress.html")
        self.assertTemplateNotUsed(response, "runs/detail.html")
        self.assertContains(response, "Execução colocada na fila.")

    def test_project_with_runs_cannot_be_deleted(self):
        self.make_run()
        response = self.client.post(reverse("orchestrator:project_delete", args=[self.project.pk]), follow=True)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertContains(response, "não pode ser excluído")
