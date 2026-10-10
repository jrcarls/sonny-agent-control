import stat
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .. import crypto, workspace
from ..models import Project, Run
from .base import RunTestCase

HTMX = {"HX-Request": "true"}


class CryptoTests(SimpleTestCase):
    def test_keypair_is_openssh_ed25519(self):
        private, public = crypto.generate_ssh_keypair("sonny-Site")
        self.assertTrue(private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertRegex(public, r"^ssh-ed25519 \S+ sonny-Site$")

    def test_encryption_depends_on_the_secret_key(self):
        token = crypto.encrypt("segredo")
        self.assertNotIn("segredo", token)
        self.assertEqual(crypto.decrypt(token), "segredo")
        with override_settings(SECRET_KEY="outra"), self.assertRaises(crypto.DecryptionError):
            crypto.decrypt(token)


class ProjectKeyTests(RunTestCase):
    def test_every_project_gets_its_own_key_stored_encrypted(self):
        other = Project.objects.create(name="API", repo_url="git@github.com:org/api.git")
        self.assertTrue(self.project.ssh_public_key.startswith("ssh-ed25519 "))
        self.assertNotEqual(self.project.ssh_public_key, other.ssh_public_key)
        self.assertNotIn("OPENSSH PRIVATE KEY", other.ssh_private_key)
        self.assertIn("OPENSSH PRIVATE KEY", crypto.decrypt(other.ssh_private_key))
        # Salvar de novo não troca a chave
        key = other.ssh_public_key
        other.description = "x"
        other.save()
        self.assertEqual(Project.objects.get(pk=other.pk).ssh_public_key, key)

    def test_project_modal_shows_only_the_public_key(self):
        page = self.client.get(self.project.get_absolute_url(), headers=HTMX)
        self.assertContains(page, self.project.ssh_public_key)
        self.assertContains(page, "Deploy keys")
        self.assertNotContains(page, "PRIVATE KEY")
        self.assertNotContains(page, self.project.ssh_private_key)
        self.assertContains(page, "não é SSH")  # o projeto de teste usa um caminho local

    def test_project_from_before_keys_existed_gets_one_when_opened(self):
        Project.objects.filter(pk=self.project.pk).update(ssh_private_key="", ssh_public_key="")
        page = self.client.get(self.project.get_absolute_url(), headers=HTMX)
        self.project.refresh_from_db()
        self.assertContains(page, self.project.ssh_public_key)
        self.assertTrue(self.project.ssh_private_key)

    def test_regenerating_replaces_the_key(self):
        old = self.project.ssh_public_key
        response = self.client.post(reverse("orchestrator:project_key_regenerate", args=[self.project.pk]), follow=True)
        self.project.refresh_from_db()
        self.assertNotEqual(self.project.ssh_public_key, old)
        # Volta para a lista com o modal do projeto aberto de novo, já com a chave nova
        self.assertRedirects(response, f"{reverse('orchestrator:project_list')}?editar={self.project.pk}")
        self.assertContains(response, "Chave nova gerada")
        modal = self.client.get(self.project.get_absolute_url(), headers=HTMX)
        self.assertContains(modal, self.project.ssh_public_key)

    def test_git_uses_the_project_key_from_a_private_file(self):
        with mock.patch.object(workspace.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            workspace.git("fetch", "origin", cwd=self.tmp, project=self.project)
            command = run.call_args.kwargs["env"]["GIT_SSH_COMMAND"]
            workspace.git("status", cwd=self.tmp)
            self.assertNotIn("GIT_SSH_COMMAND", run.call_args.kwargs["env"])
        key_file = self.tmp / "workspaces" / "keys" / str(self.project.pk)
        self.assertIn(f"-i {key_file}", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertEqual(key_file.read_text(), crypto.decrypt(self.project.ssh_private_key))

    def test_unreadable_key_is_reported_clearly(self):
        with override_settings(SECRET_KEY="outra"), self.assertRaisesMessage(workspace.WorkspaceError, "Gere uma nova"):
            workspace.git("fetch", "origin", cwd=self.tmp, project=self.project)


class AccessTestTests(RunTestCase):
    def test(self):
        return self.client.post(reverse("orchestrator:project_access_test", args=[self.project.pk]))

    def test_reachable_repository_with_the_default_branch(self):
        self.project.default_branch = "main"
        self.project.save()
        response = self.test()
        self.assertTemplateUsed(response, "projects/_access_result.html")
        self.assertContains(response, "Acesso de leitura confirmado")
        self.assertContains(response, "1 branch,")

    def test_default_branch_missing_in_the_repository(self):
        self.project.default_branch = "develop"
        self.project.save()
        response = self.test()
        self.assertContains(response, "a branch padrão não existe")
        self.assertContains(response, "Branches encontradas: main")

    def test_unreachable_repository_shows_the_git_error(self):
        self.project.repo_url = str(self.tmp / "nao-existe")
        self.project.save()
        response = self.test()
        self.assertContains(response, "não conseguiu acessar o repositório")
        self.assertContains(response, "git ls-remote falhou")

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.test().status_code, 302)


class PullRequestLinkTests(RunTestCase):
    def test_run_page_offers_to_open_the_pull_request_after_a_push_without_one(self):
        self.project.repo_url = "git@github.com:org/site.git"
        self.project.save()
        run = self.make_run(status=Run.Status.SUCCEEDED)
        self.assertNotContains(self.client.get(run.get_absolute_url()), "Abrir pull request no GitHub")
        run.pushed_at = timezone.now()
        run.save()
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "Abrir pull request no GitHub")
        self.assertContains(page, f"https://github.com/org/site/compare/main...{run.branch}?expand=1")
        run.pull_request_url = "https://github.com/org/site/pull/7"
        run.save()
        page = self.client.get(run.get_absolute_url())
        self.assertContains(page, "Ver pull request")
        self.assertNotContains(page, "Abrir pull request no GitHub")
