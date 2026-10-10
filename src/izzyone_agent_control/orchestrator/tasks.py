from contextlib import contextmanager

from celery import shared_task
from django.db import connection
from django.utils import timezone
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.conninfo import make_conninfo

from .graph import build_graph, initial_state
from .models import Approval, Run, RunEvent
from .workspace import prepare_workspace, workspace_path

MAX_EVENT_LENGTH = 4000
# Teto de passos do grafo por execução, para um agente em laço não rodar para sempre
RECURSION_LIMIT = 100


@contextmanager
def open_checkpointer():
    """Checkpointer do LangGraph no mesmo Postgres do Django, com tabelas próprias."""
    db = connection.settings_dict
    conninfo = make_conninfo(
        dbname=db["NAME"], user=db["USER"], password=db["PASSWORD"], host=db["HOST"], port=db["PORT"] or None
    )
    with PostgresSaver.from_conn_string(conninfo) as checkpointer:
        checkpointer.setup()
        yield checkpointer


def _record_log(run, update):
    """Grava os eventos de um passo do grafo; devolve a última mensagem do agente principal, se houver."""
    text = ""
    for entry in (update or {}).get("log", []):
        run.events.create(kind=entry["kind"], agent_id=entry["agent_id"], content=entry["content"][:MAX_EVENT_LENGTH])
        if entry["kind"] == RunEvent.Kind.MESSAGE and entry["agent_id"] == run.agent_id:
            text = entry["content"]
    return text


def _graph_input(run, snapshot):
    """Entrada do grafo: a tarefa (execução nova) ou a retomada do ponto em que ele parou."""
    if snapshot.interrupts:
        requested = [item["id"] for item in snapshot.interrupts[0].value]
        approvals = run.approvals.filter(tool_call_id__in=requested)
        return Command(resume={item.tool_call_id: item.status == Approval.Status.APPROVED for item in approvals})
    if snapshot.next:
        return None  # parou no meio (ex.: worker reiniciado): continua do último passo salvo
    run.events.create(kind=RunEvent.Kind.STATUS, content=f"Agente {run.agent.name} começou a trabalhar.")
    return initial_state(run)


@shared_task
def execute_run(run_id):
    """Roda o grafo da execução até ele terminar ou parar esperando aprovação."""
    # Só começa se estiver na fila: evita rodar duas vezes se a tarefa for reentregue
    if not Run.objects.filter(pk=run_id, status=Run.Status.QUEUED).update(status=Run.Status.RUNNING):
        return
    run = Run.objects.select_related("project", "agent").get(pk=run_id)
    if run.started_at is None:
        run.started_at = timezone.now()
        run.save(update_fields=["started_at"])

    result = ""
    requested = []
    try:
        if not workspace_path(run).exists():
            run.events.create(kind=RunEvent.Kind.STATUS, content="Preparando o repositório.")
        workspace = prepare_workspace(run)
        with open_checkpointer() as checkpointer:
            graph = build_graph(run, workspace, checkpointer)
            # thread_id liga o estado salvo do grafo a esta execução
            config = {"configurable": {"thread_id": str(run.pk)}, "recursion_limit": RECURSION_LIMIT}
            graph_input = _graph_input(run, graph.get_state(config))
            for update in graph.stream(graph_input, config, stream_mode="updates"):
                for node, node_update in update.items():
                    if node == "__interrupt__":
                        requested = node_update[0].value
                    else:
                        result = _record_log(run, node_update) or result
    except Exception as exc:
        run.events.create(kind=RunEvent.Kind.ERROR, content=f"{type(exc).__name__}: {exc}")
        run.status = Run.Status.FAILED
        run.finished_at = timezone.now()
    else:
        if requested:
            for item in requested:
                run.approvals.get_or_create(
                    tool_call_id=item["id"],
                    defaults={"tool_name": item["name"], "tool_args": item["args"], "agent_id": item["agent_id"]},
                )
            run.status = Run.Status.WAITING
            run.events.create(kind=RunEvent.Kind.STATUS, content="Aguardando aprovação para continuar.")
        else:
            run.result = result
            run.status = Run.Status.SUCCEEDED
            run.finished_at = timezone.now()
            run.events.create(kind=RunEvent.Kind.STATUS, content="Execução concluída.")
    run.save(update_fields=["result", "status", "finished_at"])
