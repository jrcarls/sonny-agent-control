"""Ferramentas que os agentes usam dentro do worktree da execução."""

import os
import subprocess
from dataclasses import dataclass

import httpx
from django.conf import settings
from langchain_core.tools import tool

from .workspace import git


@dataclass(frozen=True)
class ToolSpec:
    label: str
    description: str
    group: str
    requires_approval: bool = False


# Catálogo exibido no cadastro do agente; a implementação de cada uma fica em build_tools
TOOL_SPECS = {
    "list_files": ToolSpec("Listar arquivos", "Vê o conteúdo de pastas do repositório.", "Leitura"),
    "read_file": ToolSpec("Ler arquivo", "Lê arquivos do repositório.", "Leitura"),
    "search_files": ToolSpec("Buscar no código", "Procura um texto ou expressão regular nos arquivos.", "Leitura"),
    "write_file": ToolSpec("Escrever arquivo", "Cria ou sobrescreve arquivos no worktree da execução.", "Escrita"),
    "run_command": ToolSpec(
        "Rodar comando", "Executa comandos de shell no worktree.", "Ações sensíveis", requires_approval=True
    ),
    "open_pull_request": ToolSpec(
        "Abrir pull request",
        "Faz commit, envia a branch e abre um PR no GitHub.",
        "Ações sensíveis",
        requires_approval=True,
    ),
}

DEFAULT_TOOLS = ["list_files", "read_file", "search_files"]


class ToolError(Exception):
    """Erro esperado de uma ferramenta; a mensagem volta para o agente."""


def _truncate(text):
    limit = settings.TOOL_MAX_OUTPUT_CHARS
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (saída cortada: {len(text) - limit} caracteres omitidos)"


def _resolve(workspace, path):
    """Caminho dentro do worktree; recusa qualquer coisa que escape dele ou mexa no .git."""
    root = workspace.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ToolError(f"Caminho fora do repositório: {path}")
    if target != root and target.relative_to(root).parts[0] == ".git":
        raise ToolError("O diretório .git não pode ser acessado.")
    return target


def _github_repo(repo_url):
    """'org/repo' a partir de https://github.com/org/repo(.git) ou git@github.com:org/repo(.git)."""
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if repo_url.startswith(prefix):
            return repo_url.removeprefix(prefix).removesuffix("/").removesuffix(".git")
    raise ToolError("Só é possível abrir pull request em repositórios do GitHub.")


def build_tools(run, workspace, agent):
    """Ferramentas habilitadas para um agente, presas ao worktree da execução."""

    @tool
    def list_files(path: str = ".") -> str:
        """Lista arquivos e pastas de um diretório do repositório. Pastas terminam com /."""
        directory = _resolve(workspace, path)
        if not directory.is_dir():
            raise ToolError(f"Não é um diretório: {path}")
        entries = sorted(
            f"{entry.name}/" if entry.is_dir() else entry.name
            for entry in directory.iterdir()
            if entry.name != ".git"
        )
        return _truncate("\n".join(entries)) or "(diretório vazio)"

    @tool
    def read_file(path: str) -> str:
        """Lê um arquivo de texto do repositório. O caminho é relativo à raiz do repositório."""
        file = _resolve(workspace, path)
        if not file.is_file():
            raise ToolError(f"Arquivo não encontrado: {path}")
        try:
            return _truncate(file.read_text())
        except UnicodeDecodeError:
            raise ToolError(f"{path} não é um arquivo de texto.") from None

    @tool
    def search_files(pattern: str, path: str = ".") -> str:
        """Busca uma expressão regular nos arquivos do repositório e devolve as linhas encontradas."""
        target = _resolve(workspace, path)
        process = subprocess.run(
            ["git", "grep", "-n", "-I", "--untracked", "-E", "-e", pattern, "--", str(target)],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=settings.TOOL_SEARCH_TIMEOUT,
        )
        if process.returncode > 1:
            raise ToolError(process.stderr.strip())
        return _truncate(process.stdout) or "Nenhuma ocorrência encontrada."

    @tool
    def write_file(path: str, content: str) -> str:
        """Cria ou sobrescreve um arquivo de texto no repositório com o conteúdo informado."""
        file = _resolve(workspace, path)
        if file.is_dir():
            raise ToolError(f"{path} é um diretório.")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return f"{path} gravado ({len(content)} caracteres)."

    @tool
    def run_command(command: str) -> str:
        """Executa um comando de shell na raiz do repositório e devolve o código de saída e a saída."""
        try:
            process = subprocess.run(
                command,
                shell=True,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=settings.TOOL_COMMAND_TIMEOUT,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except subprocess.TimeoutExpired:
            raise ToolError(f"O comando passou do limite de {settings.TOOL_COMMAND_TIMEOUT}s e foi interrompido.") from None
        return _truncate(f"código de saída: {process.returncode}\n{process.stdout}{process.stderr}")

    @tool
    def open_pull_request(title: str, body: str = "") -> str:
        """Faz commit de todas as alterações, envia a branch e abre um pull request no GitHub.

        Se a execução já tiver um pull request aberto, só envia os novos commits para ele.
        """
        project = run.project
        repository = _github_repo(project.repo_url)
        if not settings.GITHUB_TOKEN:
            raise ToolError("GITHUB_TOKEN não está configurado no servidor.")

        git("add", "-A", cwd=workspace)
        if git("status", "--porcelain", cwd=workspace).strip():
            identity = ["-c", f"user.name={settings.AGENT_GIT_NAME}", "-c", f"user.email={settings.AGENT_GIT_EMAIL}"]
            git(*identity, "commit", "-m", title, cwd=workspace)
        if git("rev-list", "--count", f"origin/{project.default_branch}..HEAD", cwd=workspace).strip() == "0":
            raise ToolError("Não há alterações para enviar.")
        git("push", "origin", f"HEAD:refs/heads/{run.branch}", cwd=workspace)

        if run.pull_request_url:
            return f"Pull request atualizado: {run.pull_request_url}"
        response = httpx.post(
            f"https://api.github.com/repos/{repository}/pulls",
            headers={"Authorization": f"Bearer {settings.GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"title": title, "body": body, "head": run.branch, "base": project.default_branch},
            timeout=settings.GITHUB_API_TIMEOUT,
        )
        if response.status_code != 201:
            raise ToolError(f"O GitHub recusou o pull request ({response.status_code}): {response.text[:500]}")
        run.pull_request_url = response.json()["html_url"]
        run.save(update_fields=["pull_request_url"])
        return f"Pull request aberto: {run.pull_request_url}"

    available = [list_files, read_file, search_files, write_file, run_command, open_pull_request]
    return [item for item in available if item.name in agent.tools]
