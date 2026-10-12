from unittest import mock

from django.test import override_settings
from langchain_core.messages import AIMessage

from ..models import Run, RunEvent
from ..tools import ToolError, build_tools
from ..workspace import git, prepare_workspace, workspace_path
from .base import RunTestCase, tool_call


class ToolTests(RunTestCase):
    agent_tools = ["list_files", "read_file", "search_files", "find_files", "write_file", "edit_file", "run_command"]

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

    @override_settings(TOOL_MAX_OUTPUT_CHARS=10)
    def test_output_is_cut_at_the_configured_size(self):
        self.call("write_file", path="grande.txt", content="x" * 25)
        self.assertEqual(self.call("read_file", path="grande.txt"), "x" * 10 + "\n… (saída cortada: 15 caracteres omitidos)")

    @override_settings(TOOL_COMMAND_TIMEOUT=1)
    def test_command_is_stopped_at_the_configured_timeout(self):
        with self.assertRaisesMessage(ToolError, "passou do limite de 1s"):
            self.call("run_command", command="sleep 5")

    def test_read_file_by_line_range(self):
        self.call("write_file", path="linhas.txt", content="um\ndois\ntrês\nquatro\n")
        self.assertEqual(self.call("read_file", path="linhas.txt", start_line=2, end_line=3), "dois\ntrês\n")
        self.assertEqual(self.call("read_file", path="linhas.txt", start_line=4), "quatro\n")
        with self.assertRaisesMessage(ToolError, "tem só 4 linhas"):
            self.call("read_file", path="linhas.txt", start_line=5)
        with self.assertRaisesMessage(ToolError, "Intervalo de linhas inválido"):
            self.call("read_file", path="linhas.txt", start_line=3, end_line=2)

    @override_settings(TOOL_MAX_OUTPUT_CHARS=10)
    def test_long_file_is_cut_at_a_line_and_says_where_to_continue(self):
        self.call("write_file", path="linhas.txt", content="um\ndois\ntrês\nquatro\n")
        first = self.call("read_file", path="linhas.txt")
        self.assertEqual(first, "um\ndois\n… (cortado na linha 2 de 4; continue com start_line=3)")
        second = self.call("read_file", path="linhas.txt", start_line=3)
        self.assertEqual(second, "três\n… (cortado na linha 3 de 4; continue com start_line=4)")

    def test_edit_file_replaces_one_exact_excerpt(self):
        self.call("write_file", path="app.py", content="a = 1\r\nb = 1\r\n")
        self.assertEqual(
            self.call("edit_file", path="app.py", old_text="b = 1", new_text="b = 2"),
            "app.py editado (1 trecho substituído).",
        )
        # O resto do arquivo fica como estava, inclusive as quebras de linha do Windows
        self.assertEqual((self.workspace / "app.py").read_bytes(), b"a = 1\r\nb = 2\r\n")

    def test_edit_file_refuses_missing_or_ambiguous_excerpt(self):
        self.call("write_file", path="app.py", content="x = 1\nx = 1\n")
        with self.assertRaisesMessage(ToolError, "Trecho não encontrado"):
            self.call("edit_file", path="app.py", old_text="y = 1", new_text="y = 2")
        with self.assertRaisesMessage(ToolError, "aparece 2 vezes"):
            self.call("edit_file", path="app.py", old_text="x = 1", new_text="x = 2")
        with self.assertRaises(ToolError):
            self.call("edit_file", path="../fora.txt", old_text="a", new_text="b")
        self.assertEqual((self.workspace / "app.py").read_text(), "x = 1\nx = 1\n")
        self.assertIn("2 trechos", self.call("edit_file", path="app.py", old_text="x = 1", new_text="x = 2", replace_all=True))
        self.assertEqual((self.workspace / "app.py").read_text(), "x = 2\nx = 2\n")

    def test_find_files_by_name_or_glob(self):
        for path in ["src/app.py", "src/tests/test_app.py", "src/página.html", "docs/guia.md"]:
            self.call("write_file", path=path, content="x")
        self.assertEqual(self.call("find_files", pattern="*.py"), "src/app.py\nsrc/tests/test_app.py")
        self.assertEqual(self.call("find_files", pattern="test_*.py"), "src/tests/test_app.py")
        # Com barra o padrão é relativo a path, e a resposta continua a partir da raiz
        self.assertEqual(self.call("find_files", pattern="tests/*.py", path="src"), "src/tests/test_app.py")
        self.assertEqual(self.call("find_files", pattern="*.py", path="src/tests"), "src/tests/test_app.py")
        self.assertEqual(self.call("find_files", pattern="**/*.html"), "src/página.html")
        self.assertEqual(self.call("find_files", pattern="*.rs"), "Nenhum arquivo encontrado.")

    def test_find_files_skips_ignored_and_deleted_files_and_stays_in_the_worktree(self):
        self.call("write_file", path=".gitignore", content="build/\n")
        self.call("write_file", path="build/saida.txt", content="x")
        self.call("write_file", path="notas.txt", content="x")
        (self.workspace / "README.md").unlink()
        self.assertEqual(self.call("find_files", pattern="*"), ".gitignore\nnotas.txt")
        self.assertEqual(self.call("find_files", pattern="config"), "Nenhum arquivo encontrado.")
        for args in [{"pattern": "*", "path": ".."}, {"pattern": "../*"}, {"pattern": "*", "path": "notas.txt"}]:
            with self.subTest(args=args), self.assertRaises(ToolError):
                self.call("find_files", **args)

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

    def test_without_token_the_branch_is_pushed_and_the_agent_gets_a_link_to_open_the_pull_request(self):
        self.tools["write_file"].invoke({"path": "a.txt", "content": "1"})
        with self.settings(GITHUB_TOKEN=""):
            result, post = self.open_pr()
        post.assert_not_called()
        self.assertIn("a.txt", git("show", "--stat", self.run_.branch, cwd=self.remote))
        link = f"https://github.com/org/site/compare/main...{self.run_.branch}?expand=1"
        self.assertIn(link + "&title=Adiciona+p%C3%A1gina&body=Detalhes", result)
        self.assertEqual((self.run_.pull_request_url, self.run_.compare_url), ("", link))
        self.assertIsNotNone(Run.objects.get(pk=self.run_.pk).pushed_at)

    def test_outside_github_the_branch_is_only_pushed(self):
        self.run_.project.repo_url = "https://gitlab.com/org/site.git"
        self.tools["write_file"].invoke({"path": "a.txt", "content": "1"})
        result, post = self.open_pr()
        post.assert_not_called()
        self.assertEqual(result, f"Branch {self.run_.branch} enviada para o repositório.")
        self.assertEqual(self.run_.compare_url, "")


class ListPullRequestsToolTests(RunTestCase):
    agent_tools = ["list_pull_requests"]

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.workspace = prepare_workspace(self.run_)
        self.run_.project.repo_url = "git@github.com:org/site.git"
        self.tools = {item.name: item for item in build_tools(self.run_, self.workspace, self.agent)}

    def pull(self, number, title, **kwargs):
        defaults = {
            "number": number,
            "title": title,
            "state": "open",
            "draft": False,
            "merged_at": None,
            "head": {"ref": f"sonny/run-{number}"},
            "base": {"ref": "main"},
            "user": {"login": "ana"},
            "html_url": f"https://github.com/org/site/pull/{number}",
        }
        return defaults | kwargs

    def list_prs(self, pulls=(), status_code=200, **args):
        response = mock.Mock(status_code=status_code, text="erro do github")
        response.json.return_value = list(pulls)
        with mock.patch("orchestrator.tools.httpx.get", return_value=response) as get:
            result = self.tools["list_pull_requests"].invoke(args)
        return result, get

    def test_lists_pull_requests_with_the_token(self):
        pulls = [
            self.pull(7, "Adiciona página"),
            self.pull(6, "Em preparo", draft=True),
            self.pull(5, "Corrige rodapé", state="closed", merged_at="2026-10-01T12:00:00Z"),
            self.pull(4, "Descartado", state="closed"),
        ]
        with self.settings(GITHUB_TOKEN="segredo"):
            result, get = self.list_prs(pulls, state="all")
        self.assertEqual(
            result.splitlines()[0],
            "#7 [aberto] Adiciona página (sonny/run-7 → main, por ana) https://github.com/org/site/pull/7",
        )
        for text in ("#6 [rascunho]", "#5 [mesclado]", "#4 [fechado]"):
            self.assertIn(text, result)
        self.assertEqual(get.call_args.args[0], "https://api.github.com/repos/org/site/pulls")
        self.assertEqual(get.call_args.kwargs["params"]["state"], "all")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer segredo")

    def test_without_token_the_query_goes_unauthenticated(self):
        with self.settings(GITHUB_TOKEN=""):
            result, get = self.list_prs()
            self.assertEqual(result, "Nenhum pull request (abertos) em org/site.")
            self.assertNotIn("Authorization", get.call_args.kwargs["headers"])
            with self.assertRaisesMessage(ToolError, "só repositórios públicos"):
                self.list_prs(status_code=404)

    def test_invalid_state_and_repository_outside_github_are_errors(self):
        with self.assertRaisesMessage(ToolError, "Situação inválida"):
            self.list_prs(state="merged")
        self.run_.project.repo_url = "https://gitlab.com/org/site.git"
        with self.assertRaisesMessage(ToolError, "não é do GitHub"):
            self.list_prs()

    def test_listing_does_not_ask_for_approval(self):
        run = self.make_run()
        with mock.patch("orchestrator.tools.httpx.get", return_value=mock.Mock(status_code=200, json=lambda: [])):
            self.execute(run, tool_call("list_pull_requests"), AIMessage(content="Nenhum aberto."))
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Nenhum aberto."))


class GitReadToolTests(RunTestCase):
    agent_tools = ["write_file", "git_status", "git_diff", "git_log"]

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run()
        self.workspace = prepare_workspace(self.run_)
        self.tools = {item.name: item for item in build_tools(self.run_, self.workspace, self.agent)}

    def call(self, name, **args):
        return self.tools[name].invoke(args)

    def commit(self, message):
        git("add", "-A", cwd=self.workspace)
        git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", message, cwd=self.workspace)

    def test_status_shows_branch_and_pending_files(self):
        self.call("write_file", path="novo.txt", content="oi\n")
        status = self.call("git_status")
        self.assertIn(self.run_.branch, status)
        self.assertIn("?? novo.txt", status)

    def test_diff_without_revision_is_everything_the_run_changed(self):
        self.assertEqual(self.call("git_diff"), "Nenhuma diferença.")
        self.call("write_file", path="README.md", content="# Site\nProjeto novo.\n")
        self.commit("muda o README")
        self.call("write_file", path="docs/novo.txt", content="oi\n")  # ainda sem commit e fora do índice
        diff = self.call("git_diff")
        self.assertIn("+Projeto novo.", diff)
        self.assertIn("docs/novo.txt", diff)
        only_docs = self.call("git_diff", path="docs")
        self.assertIn("docs/novo.txt", only_docs)
        self.assertNotIn("README.md", only_docs)

    def test_diff_against_a_revision_or_between_two(self):
        self.call("write_file", path="README.md", content="# Site\nProjeto novo.\n")
        self.assertIn("+Projeto novo.", self.call("git_diff", revision="origin/main"))
        self.commit("muda o README")
        self.assertIn("+Projeto novo.", self.call("git_diff", revision="origin/main...HEAD", path="README.md"))
        self.assertEqual(self.call("git_diff", revision="HEAD"), "Nenhuma diferença.")

    def test_log_lists_commits_and_filters_by_path(self):
        self.call("write_file", path="novo.txt", content="oi\n")
        self.commit("adiciona novo")
        log = self.call("git_log")
        self.assertRegex(log.splitlines()[0], r"^[0-9a-f]+ \d{4}-\d{2}-\d{2} T: adiciona novo$")
        self.assertIn("inicial", log)
        self.assertNotIn("adiciona novo", self.call("git_log", revision="origin/main"))
        self.assertNotIn("inicial", self.call("git_log", path="novo.txt"))
        self.assertEqual(len(self.call("git_log", limit=1).splitlines()), 1)

    def test_revision_cannot_be_read_as_a_git_option(self):
        for revision in ["--output=vazou.txt", "-p", "HEAD --output=vazou.txt"]:
            with self.subTest(revision=revision), self.assertRaisesMessage(ToolError, "Referência inválida"):
                self.call("git_diff", revision=revision)
            with self.subTest(revision=revision), self.assertRaisesMessage(ToolError, "Referência inválida"):
                self.call("git_log", revision=revision)
        self.assertFalse((self.workspace / "vazou.txt").exists())
        with self.assertRaisesMessage(ToolError, "bad revision"):
            self.call("git_diff", revision="nao-existe")
        with self.assertRaises(ToolError):
            self.call("git_diff", path="../fora")

    def test_git_reading_does_not_ask_for_approval(self):
        run = self.make_run()
        self.execute(run, tool_call("git_status"), AIMessage(content="Tudo limpo."))
        self.assertEqual((run.status, run.result), (Run.Status.SUCCEEDED, "Tudo limpo."))
