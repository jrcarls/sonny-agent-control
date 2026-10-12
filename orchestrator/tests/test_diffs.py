from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ..diffs import parse_diff
from ..models import Run
from ..workspace import git, prepare_workspace, remove_workspace, workspace_diff
from .base import RunTestCase

HTMX = {"HX-Request": "true"}

SAMPLE = """\
diff --git a/app/views.py b/app/views.py
index 1111111..2222222 100644
--- a/app/views.py
+++ b/app/views.py
@@ -10,4 +10,5 @@ def home(request):
     context = {}
-    return render(request, "home.html")
+    context["user"] = request.user
+    return render(request, "home.html", context)
 
\\ No newline at end of file
@@ -40,2 +41,2 @@
 a
-b
+c
diff --git a/novo arquivo.txt b/novo arquivo.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/novo arquivo.txt
@@ -0,0 +1 @@
+olá
diff --git a/velho.txt b/velho.txt
deleted file mode 100644
index 4444444..0000000
--- a/velho.txt
+++ /dev/null
@@ -1 +0,0 @@
-tchau
diff --git a/a.txt b/pasta/b.txt
similarity index 100%
rename from a.txt
rename to pasta/b.txt
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..5555555
Binary files /dev/null and b/logo.png differ
"""


class ParseDiffTests(SimpleTestCase):
    def test_reads_every_kind_of_file_change(self):
        files = parse_diff(SAMPLE, 100)
        self.assertEqual(
            [(item.path, item.status, item.additions, item.deletions) for item in files],
            [
                ("app/views.py", "modified", 3, 2),
                ("novo arquivo.txt", "added", 1, 0),
                ("velho.txt", "deleted", 0, 1),
                ("pasta/b.txt", "renamed", 0, 0),
                ("logo.png", "added", 0, 0),
            ],
        )
        self.assertEqual(files[3].old_path, "a.txt")
        self.assertEqual([item.binary for item in files], [False, False, False, False, True])

    def test_lines_carry_their_numbers_on_each_side(self):
        first, second = parse_diff(SAMPLE, 100)[0].hunks
        self.assertEqual(
            [(line.kind, line.old, line.new) for line in first.lines],
            [("context", 10, 10), ("del", 11, None), ("add", None, 11), ("add", None, 12), ("context", 12, 13)],
        )
        self.assertEqual(first.lines[1].text, '    return render(request, "home.html")')
        self.assertEqual([(line.kind, line.old, line.new) for line in second.lines][0], ("context", 40, 41))

    def test_file_over_the_limit_keeps_only_the_counts(self):
        big, small = parse_diff(SAMPLE, 6)[:2]
        self.assertEqual((big.too_large, big.hunks, big.additions, big.deletions), (True, [], 3, 2))
        self.assertEqual((small.too_large, len(small.hunks)), (False, 1))

    def test_empty_diff_has_no_files(self):
        self.assertEqual(parse_diff("", 100), [])


class WorkspaceDiffTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(status=Run.Status.RUNNING, started_at=timezone.now())
        self.workspace = prepare_workspace(self.run_)
        self.url = reverse("orchestrator:run_diff", args=[self.run_.pk])

    def change(self):
        """Um commit, uma edição ainda não commitada e um arquivo novo que o git ainda não acompanha."""
        (self.workspace / "commitado.txt").write_text("um\n")
        git("add", "-A", cwd=self.workspace)
        git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "x", cwd=self.workspace)
        (self.workspace / "README.md").write_text("# Site\nProjeto novo.\n")
        (self.workspace / "solto.txt").write_text("dois\n")

    def test_diff_covers_commits_and_uncommitted_work_without_touching_the_index(self):
        self.assertEqual(workspace_diff(self.run_), "")
        self.change()
        files = {item.path: item for item in parse_diff(workspace_diff(self.run_), 100)}
        self.assertEqual(
            {path: item.status for path, item in files.items()},
            {"README.md": "modified", "commitado.txt": "added", "solto.txt": "added"},
        )
        self.assertEqual((files["README.md"].additions, files["README.md"].deletions), (1, 1))
        # O índice do worktree continua como o agente deixou
        self.assertEqual(git("status", "--porcelain", cwd=self.workspace), " M README.md\n?? solto.txt\n")

    def test_changes_that_landed_on_the_default_branch_afterwards_are_not_shown(self):
        (self.remote / "outro.txt").write_text("de outra pessoa\n")
        git("add", "-A", cwd=self.remote)
        git("-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-m", "outro", cwd=self.remote)
        prepare_workspace(self.make_run())  # busca a branch padrão de novo
        self.change()
        self.assertNotIn("outro.txt", workspace_diff(self.run_))

    def test_tab_lists_the_files_with_totals(self):
        self.change()
        page = self.client.get(self.url, headers=HTMX)
        self.assertTemplateUsed(page, "runs/_diff.html")
        self.assertContains(page, "3 arquivos")
        self.assertEqual((page.context["additions"], page.context["deletions"]), (3, 1))
        self.assertContains(page, "solto.txt")
        self.assertContains(page, "Projeto novo.")
        self.assertContains(page, ">Novo</span>", count=2)

    def test_index_jumps_to_each_file_and_many_files_come_collapsed(self):
        self.change()
        page = self.client.get(self.url, headers=HTMX)
        self.assertContains(page, 'aria-label="Arquivos alterados"')
        self.assertContains(page, 'href="#diff-file-3"')
        self.assertContains(page, 'id="diff-file-3" data-diff-file open')
        self.assertContains(page, "Abrir todos")

        for number in range(10):
            (self.workspace / f"extra-{number}.txt").write_text("x\n")
        page = self.client.get(self.url, headers=HTMX)
        self.assertFalse(page.context["expanded"])
        self.assertContains(page, "13 arquivos")
        self.assertContains(page, 'id="diff-file-', count=13)
        self.assertNotContains(page, "data-diff-file open")
        self.assertContains(page, "eles vêm recolhidos")

    def test_single_file_has_no_index(self):
        (self.workspace / "solto.txt").write_text("dois\n")
        page = self.client.get(self.url, headers=HTMX)
        self.assertNotContains(page, 'aria-label="Arquivos alterados"')
        self.assertNotContains(page, "Abrir todos")
        self.assertContains(page, 'id="diff-file-1" data-diff-file open')

    @override_settings(RUN_DIFF_MAX_FILE_LINES=1)
    def test_large_file_is_not_rendered(self):
        self.change()
        page = self.client.get(self.url, headers=HTMX)
        self.assertContains(page, "Diff grande demais")
        self.assertNotContains(page, "Projeto novo.")

    def test_agent_text_is_escaped(self):
        (self.workspace / "x.html").write_text("<script>alert(1)</script>\n")
        page = self.client.get(self.url, headers=HTMX)
        self.assertContains(page, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(page, "<script>alert(1)")

    def test_no_changes(self):
        self.assertContains(self.client.get(self.url, headers=HTMX), "O agente ainda não mudou nenhum arquivo")

    def test_git_failure_is_shown(self):
        self.run_.project.default_branch = "nao-existe"
        self.run_.project.save()
        self.assertContains(self.client.get(self.url, headers=HTMX), "Não foi possível montar o diff")

    def test_run_that_has_not_started_and_run_already_cleaned_up(self):
        queued = self.make_run()
        page = self.client.get(reverse("orchestrator:run_diff", args=[queued.pk]), headers=HTMX)
        self.assertContains(page, "A execução ainda não começou")
        remove_workspace(self.run_)
        Run.objects.filter(pk=self.run_.pk).update(status=Run.Status.SUCCEEDED)
        page = self.client.get(self.url, headers=HTMX)
        self.assertContains(page, "não existe mais")
        self.assertContains(page, "Nada foi enviado ao repositório")

    def test_opened_directly_goes_to_the_run_with_the_tab_selected(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{self.run_.get_absolute_url()}?aba=alteracoes")
        self.assertEqual(self.client.get(response["Location"]).context["tab"], "diff")
        self.assertContains(self.client.get(self.run_.get_absolute_url()), f'hx-get="{self.url}"')

    def test_progress_tells_the_tab_to_refresh_when_the_run_stops(self):
        url = self.run_.get_absolute_url()
        self.assertNotIn("HX-Trigger", self.client.get(url, headers=HTMX))
        Run.objects.filter(pk=self.run_.pk).update(status=Run.Status.SUCCEEDED)
        self.assertEqual(self.client.get(url, headers=HTMX)["HX-Trigger"], "run-settled")
        self.assertNotIn("HX-Trigger", self.client.get(url))

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url, headers=HTMX).status_code, 302)
