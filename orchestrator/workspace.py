"""Área de trabalho de uma execução: um worktree git próprio, isolado das outras execuções."""

import base64
import fcntl
import os
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings

from .crypto import DecryptionError, decrypt


class WorkspaceError(Exception):
    pass


def _ssh_key_file(project):
    """Arquivo com a chave privada do projeto, legível só pelo dono, para o ssh usar."""
    if not project.ssh_private_key:
        project.save()  # projetos criados antes das chaves existirem ganham a sua aqui
    try:
        key = decrypt(project.ssh_private_key)
    except DecryptionError:
        raise WorkspaceError(
            "A chave SSH do projeto não pôde ser lida (a SECRET_KEY mudou?). Gere uma nova na tela do projeto."
        ) from None
    directory = Path(settings.WORKSPACES_DIR) / "keys"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / str(project.pk)
    if not path.exists() or path.read_text() != key:
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(key)
    return path


GITHUB_HTTPS = "https://github.com/"


def _github_token_config(project):
    """Variáveis que fazem o git usar o token de Integrações nos endereços https do GitHub.

    Vai num cabeçalho só deste comando: o token não entra no endereço, no .git/config nem nas mensagens de erro.
    """
    if not project.repo_url.startswith(GITHUB_HTTPS):
        return {}
    from .integrations import Service, stored_secrets  # import local: integrations depende de models

    token = stored_secrets().get(Service.GITHUB)
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{GITHUB_HTTPS}.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _git_env(project):
    # GIT_TERMINAL_PROMPT=0 e BatchMode: sem credencial o git falha em vez de ficar esperando digitação
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if project is not None:
        known_hosts = Path(settings.WORKSPACES_DIR) / "known_hosts"
        # IdentitiesOnly: usa só a chave do projeto, nunca outra que exista na máquina.
        # accept-new: guarda a identidade do servidor no primeiro acesso e recusa se ela mudar depois.
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {shlex.quote(str(_ssh_key_file(project)))} -o IdentitiesOnly=yes -o BatchMode=yes "
            f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={shlex.quote(str(known_hosts))}"
        )
        env.update(_github_token_config(project))
    return env


def git(*args, cwd, project=None, timeout=None, env=None):
    """Roda um comando git. Com `project`, as operações por SSH usam a chave dele; `env` soma variáveis ao ambiente."""
    try:
        process = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout or settings.GIT_TIMEOUT,
            env={**_git_env(project), **(env or {})},
        )
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {args[0]} passou do prazo sem resposta do repositório") from None
    if process.returncode != 0:
        raise WorkspaceError(f"git {args[0]} falhou: {process.stderr.strip() or process.stdout.strip()}")
    return process.stdout


def list_remote_branches(project):
    """Branches do repositório do projeto, lidas com a chave dele. Serve para testar o acesso."""
    root = Path(settings.WORKSPACES_DIR)
    root.mkdir(parents=True, exist_ok=True)
    output = git(
        "ls-remote", "--heads", project.repo_url, cwd=root, project=project, timeout=settings.REPOSITORY_TEST_TIMEOUT
    )
    return [line.split("refs/heads/", 1)[1] for line in output.splitlines() if "refs/heads/" in line]


@contextmanager
def _locked(path):
    """Uma execução por vez mexe no clone de um projeto (fetch e worktree add não são concorrentes)."""
    with open(path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def workspaces_root():
    return Path(settings.WORKSPACES_DIR) / "runs"


def workspace_path(run):
    return workspaces_root() / str(run.pk)


def _repo_path(project_id):
    return Path(settings.WORKSPACES_DIR) / "repos" / f"{project_id}.git"


def prepare_workspace(run):
    """Devolve o worktree da execução, criando-o a partir da branch padrão do projeto se ainda não existir."""
    path = workspace_path(run)
    if path.exists():
        return path

    project = run.project
    repo = _repo_path(project.pk)
    repo.parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)

    with _locked(repo.with_suffix(".lock")):
        if not repo.exists():
            git("init", "--bare", str(repo), cwd=repo.parent)
            git("remote", "add", "origin", project.repo_url, cwd=repo)
        else:
            git("remote", "set-url", "origin", project.repo_url, cwd=repo)
        git("fetch", "--prune", "origin", cwd=repo, project=project)
        git("worktree", "prune", cwd=repo)
        git("worktree", "add", "-B", run.branch, str(path), f"origin/{project.default_branch}", cwd=repo)
    return path


def workspace_diff(run, target=None):
    """Diff de tudo o que a execução mudou desde que saiu da branch padrão: os commits e o que ainda não foi commitado.

    `target` limita o diff a um arquivo ou pasta do worktree.
    """
    path = workspace_path(run)
    timeout = settings.RUN_DIFF_TIMEOUT
    base = git("merge-base", "HEAD", f"origin/{run.project.default_branch}", cwd=path, timeout=timeout).strip()
    # Arquivos novos só entram no diff se estiverem no índice. Um índice temporário os inclui sem mexer
    # no índice do worktree, que o agente pode estar usando neste momento.
    with tempfile.TemporaryDirectory() as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
        git("read-tree", "HEAD", cwd=path, timeout=timeout, env=env)
        git("add", "-A", cwd=path, timeout=timeout, env=env)
        options = ["-c", "core.quotePath=false", "diff", "--cached", "--no-color", "--no-ext-diff", "-M"]
        return git(*options, base, "--", *([target] if target else []), cwd=path, timeout=timeout, env=env)


def remove_workspace(run):
    """Apaga o worktree e a branch local da execução. O que já foi enviado ao remoto continua lá."""
    path = workspace_path(run)
    repo = _repo_path(run.project_id)
    if not repo.exists():
        shutil.rmtree(path, ignore_errors=True)
        return
    with _locked(repo.with_suffix(".lock")):
        if path.exists():
            git("worktree", "remove", "--force", str(path), cwd=repo)
        git("worktree", "prune", cwd=repo)
        try:
            git("branch", "-D", run.branch, cwd=repo)
        except WorkspaceError:
            pass  # a branch pode nem ter sido criada
