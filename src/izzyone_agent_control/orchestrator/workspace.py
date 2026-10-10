"""Área de trabalho de uma execução: um worktree git próprio, isolado das outras execuções."""

import fcntl
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings


class WorkspaceError(Exception):
    pass


def git(*args, cwd, timeout=600):
    # GIT_TERMINAL_PROMPT=0: sem credencial o git falha em vez de ficar esperando digitação
    process = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if process.returncode != 0:
        raise WorkspaceError(f"git {args[0]} falhou: {process.stderr.strip() or process.stdout.strip()}")
    return process.stdout


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
        git("fetch", "--prune", "origin", cwd=repo)
        git("worktree", "prune", cwd=repo)
        git("worktree", "add", "-B", run.branch, str(path), f"origin/{project.default_branch}", cwd=repo)
    return path


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
