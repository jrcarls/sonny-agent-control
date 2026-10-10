from unittest import mock

from django.test import override_settings
from langchain_core.messages import AIMessage

from ..models import Run, RunEvent
from ..tools import ToolError, build_tools
from ..workspace import git, prepare_workspace, workspace_path
from .base import RunTestCase, tool_call


class ToolTests(RunTestCase):
    agent_tools = ["list_files", "read_file", "search_files", "write_file", "run_command"]

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.workspace = prepare_workspace(self.run_)
        self.tools = {item.name: item for item in build_tools(self.run_, self.workspace, self.agent)}

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


@override_settings(GITHUB_TOKEN="token-de-teste", AGENT_GIT_NAME="Agente", AGENT_GIT_EMAIL="agente@example.com")
class PullRequestToolTests(RunTestCase):
    agent_tools = ["write_file", "open_pull_request"]

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.workspace = prepare_workspace(self.run_)
        # O clone já aponta para o repositório local; daqui em diante o projeto "é" do GitHub
        self.run_.project.repo_url = "git@github.com:org/site.git"
        self.tools = {item.name: item for item in build_tools(self.run_, self.workspace, self.agent)}

    def open_pr(self, status_code=201):
        response = mock.Mock(status_code=status_code, text="erro do github")
        response.json.return_value = {"html_url": "https://github.com/org/site/pull/7"}
        with mock.patch("orchestrator.tools.httpx.post", return_value=response) as post:
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
