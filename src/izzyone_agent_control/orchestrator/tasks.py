import json
from contextlib import contextmanager

from celery import shared_task
from django.db import connection
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.conninfo import make_conninfo

from .graph import build_graph
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


def _record_messages(run, update):
    """Transforma as mensagens de um passo do grafo em eventos; devolve o último texto do agente."""
    text = ""
    for message in (update or {}).get("messages", []):
        if isinstance(message, AIMessage):
            if message.text:
                text = str(message.text)
                run.events.create(kind=RunEvent.Kind.MESSAGE, content=text)
            for call in message.tool_calls:
                arguments = json.dumps(call["args"], ensure_ascii=False, indent=2)
                run.events.create(kind=RunEvent.Kind.TOOL_CALL, content=f"{call['name']}\n{arguments}")
        elif isinstance(message, ToolMessage):
            run.events.create(kind=RunEvent.Kind.TOOL_RESULT, content=str(message.content)[:MAX_EVENT_LENGTH])
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
    return {"messages": [HumanMessage(run.prompt)]}


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
                        result = _record_messages(run, node_update) or result
    except Exception as exc:
        run.events.create(kind=RunEvent.Kind.ERROR, content=f"{type(exc).__name__}: {exc}")
        run.status = Run.Status.FAILED
        run.finished_at = timezone.now()
    else:
        if requested:
            for item in requested:
                run.approvals.get_or_create(
                    tool_call_id=item["id"], defaults={"tool_name": item["name"], "tool_args": item["args"]}
                )
            run.status = Run.Status.WAITING
            run.events.create(kind=RunEvent.Kind.STATUS, content="Aguardando aprovação para continuar.")
        else:
            run.result = result
            run.status = Run.Status.SUCCEEDED
            run.finished_at = timezone.now()
            run.events.create(kind=RunEvent.Kind.STATUS, content="Execução concluída.")
    run.save(update_fields=["result", "status", "finished_at"])
