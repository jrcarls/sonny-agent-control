"""Andamento da execução como a tela mostra: cada chamada de ferramenta e o resultado dela viram um item só."""

import json
import re
from dataclasses import dataclass

from .models import Run, RunEvent
from .tools import ASK_TOOL, TOOL_SPECS

Kind = RunEvent.Kind

# Ferramenta -> (verbo do resumo, argumento que identifica o que ela fez)
SUMMARIES = {
    "list_files": ("Listou", "path"),
    "read_file": ("Leu", "path"),
    "search_files": ("Buscou", "pattern"),
    "find_files": ("Procurou", "pattern"),
    "git_status": ("Viu a situação do git", ""),
    "git_diff": ("Viu o diff", "revision"),
    "git_log": ("Viu o histórico", "path"),
    "list_pull_requests": ("Listou pull requests", "state"),
    "read_pull_request": ("Leu o pull request", "number"),
    "write_file": ("Escreveu", "path"),
    "edit_file": ("Editou", "path"),
    "ask_user": ("Perguntou", "question"),
    "run_command": ("Rodou", "command"),
    "run_tests": ("Rodou os testes", ""),
    "open_pull_request": ("Abriu pull request", "title"),
    "comment_pull_request": ("Comentou no pull request", "body"),
}
# Começo do resultado quando a ferramenta não fez o que foi pedido
FAILURES = ("Erro:", "Ação negada", "Ferramenta indisponível")
_EXIT_CODE = re.compile(r"código de saída: (-?\d+)")


@dataclass
class Item:
    """Um item do andamento: um evento, ou uma chamada de ferramenta com o resultado (kind "tool")."""

    kind: str
    event: RunEvent
    result: RunEvent | None = None
    verb: str = ""
    target: str = ""
    # "pending" (sem resultado ainda), "ok", "warning" (comando com código de saída diferente de zero) ou "error"
    state: str = ""
    label: str = ""
    # Pergunta ao usuário: enquanto pendente, espera uma resposta e não uma aprovação
    question: bool = False

    @property
    def key(self):
        return self.event.pk

    @property
    def last(self):
        """Evento mais recente do item: é por ele que a tela sabe o que mudou desde a última consulta."""
        return (self.result or self.event).pk


def _argument(arguments, name):
    try:
        return str(json.loads(arguments).get(name, ""))
    except (ValueError, AttributeError):
        # Chamada longa demais é guardada cortada e deixa de ser JSON válido: pega o argumento pelo texto
        match = re.search(rf'"{name}": "((?:[^"\\]|\\.)*)', arguments)
        return match[1] if match else ""


def _tool_item(event):
    name, _, arguments = event.content.partition("\n")
    verb, argument = SUMMARIES.get(name, (TOOL_SPECS[name].label if name in TOOL_SPECS else name, ""))
    target = _argument(arguments, argument).splitlines()[0:1] if argument else []
    # Listar sem dizer a pasta é listar a raiz do repositório
    default = "." if name == "list_files" else ""
    target = target[0] if target else default
    return Item(kind="tool", event=event, verb=verb, target=target, state="pending", question=name == ASK_TOOL)


def _finish(item, result):
    item.result = result
    if result.content.startswith(FAILURES):
        item.state, item.label = "error", "Negada" if result.content.startswith("Ação negada") else "Erro"
    elif (match := _EXIT_CODE.match(result.content)) and match[1] != "0":
        item.state, item.label = "warning", f"código {match[1]}"
    else:
        item.state = "ok"


def build_timeline(run, events):
    """Itens do andamento, na ordem dos eventos."""
    items, waiting = [], {}  # waiting: agente -> chamadas dele que ainda esperam resultado, na ordem
    for event in events:
        if event.kind == Kind.TOOL_CALL:
            item = _tool_item(event)
            waiting.setdefault(event.agent_id, []).append(item)
            items.append(item)
        elif event.kind == Kind.TOOL_RESULT and waiting.get(event.agent_id):
            # Os resultados chegam na mesma ordem das chamadas do agente
            _finish(waiting[event.agent_id].pop(0), event)
        else:
            items.append(Item(kind=event.kind, event=event))
    for pending in waiting.values():
        for item in pending:
            if run.status == Run.Status.WAITING:
                item.label = "Aguardando resposta" if item.question else "Aguardando aprovação"
            elif run.is_working:
                item.label = "Em andamento"
            else:
                item.state, item.label = "skipped", "Não executada"
    return items
