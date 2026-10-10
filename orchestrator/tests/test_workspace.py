from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from .. import tasks
from ..models import Run
from ..workspace import git, prepare_workspace, workspace_path
from .base import RunTestCase


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


class CleanupTests(RunTestCase):
    def finished(self, days_ago, status=Run.Status.SUCCEEDED):
        run = self.make_run(status=status, finished_at=timezone.now() - timedelta(days=days_ago))
        prepare_workspace(run)
        return run

    def branches(self):
        repo = self.tmp / "workspaces" / "repos" / f"{self.project.pk}.git"
        return git("branch", "--format=%(refname:short)", cwd=repo).split()

    @override_settings(WORKSPACE_RETENTION_DAYS=7)
    def test_only_worktrees_of_runs_finished_long_ago_are_removed(self):
        old, failed_old, recent = self.finished(8), self.finished(30, Run.Status.FAILED), self.finished(6)
        active = self.make_run()
        prepare_workspace(active)

        self.assertEqual(tasks.cleanup_workspaces(), 2)
        self.assertFalse(workspace_path(old).exists())
        self.assertFalse(workspace_path(failed_old).exists())
        self.assertTrue(workspace_path(recent).exists())
        self.assertTrue(workspace_path(active).exists())
        self.assertEqual(sorted(self.branches()), sorted([recent.branch, active.branch]))
        self.assertEqual(tasks.cleanup_workspaces(), 0)
        # Sem o worktree, a execução que falhou não oferece mais "continuar"
        self.assertNotContains(self.client.get(failed_old.get_absolute_url()), "Continuar de onde parou")

    def test_command_accepts_days(self):
        run = self.finished(2)
        output = StringIO()
        call_command("cleanup_workspaces", days=1, stdout=output)
        self.assertIn("1 worktree(s) apagado(s).", output.getvalue())
        self.assertFalse(workspace_path(run).exists())

    def test_project_still_works_after_cleanup(self):
        old = self.finished(30)
        tasks.cleanup_workspaces()
        new = self.make_run()
        self.assertTrue((prepare_workspace(new) / "README.md").exists())
        self.assertNotIn(old.branch, self.branches())
