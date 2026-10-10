from contextlib import contextmanager

from celery import shared_task
from django.db import connection
from django.utils import timezone
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.conninfo import make_conninfo

from .graph import build_graph
from .models import Run, RunEvent


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


@shared_task
def execute_run(run_id):
    # Só começa se ainda estiver na fila: evita rodar duas vezes se a tarefa for reentregue
    started = Run.objects.filter(pk=run_id, status=Run.Status.QUEUED).update(
        status=Run.Status.RUNNING, started_at=timezone.now()
    )
    if not started:
        return
    run = Run.objects.select_related("project", "agent").get(pk=run_id)
    run.events.create(kind=RunEvent.Kind.STATUS, content=f"Agente {run.agent.name} começou a trabalhar.")

    try:
        result = ""
        with open_checkpointer() as checkpointer:
            graph = build_graph(run, checkpointer)
            # thread_id liga o estado salvo do grafo a esta execução
            config = {"configurable": {"thread_id": str(run.pk)}}
            for update in graph.stream({"messages": [HumanMessage(run.prompt)]}, config, stream_mode="updates"):
                for node_update in update.values():
                    for message in (node_update or {}).get("messages", []):
                        result = str(message.text)
                        run.events.create(kind=RunEvent.Kind.MESSAGE, content=result)
    except Exception as exc:
        run.events.create(kind=RunEvent.Kind.ERROR, content=f"{type(exc).__name__}: {exc}")
        run.status = Run.Status.FAILED
    else:
        run.result = result
        run.status = Run.Status.SUCCEEDED
        run.events.create(kind=RunEvent.Kind.STATUS, content="Execução concluída.")
    run.finished_at = timezone.now()
    run.save(update_fields=["result", "status", "finished_at"])
