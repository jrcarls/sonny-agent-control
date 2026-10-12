import threading
from contextlib import contextmanager
from datetime import timedelta

from celery import shared_task
from celery.signals import worker_ready
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.conninfo import make_conninfo

from .graph import USAGE, build_graph, follow_up_state, initial_state
from .models import Approval, Run, RunEvent, Schedule
from .workspace import prepare_workspace, remove_workspace, workspace_path, workspaces_root

# Tamanho máximo, em caracteres, do texto guardado em cada evento do andamento
MAX_EVENT_LENGTH = 4000
# De quantos em quantos segundos o worker renova o sinal de vida de uma execução
HEARTBEAT_INTERVAL = 20


class TokenLimitExceeded(Exception):
    pass


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


def _count_tokens(run, entry):
    run.input_tokens += entry["input"]
    run.output_tokens += entry["output"]
    run.save(update_fields=["input_tokens", "output_tokens"])
    limit = settings.RUN_MAX_TOKENS
    if limit and run.total_tokens > limit:
        raise TokenLimitExceeded(f"A execução passou do limite de {limit} tokens (usou {run.total_tokens}).")


def _record_log(run, update):
    """Grava os eventos de um passo do grafo; devolve a última mensagem do agente principal, se houver."""
    text = ""
    for entry in (update or {}).get("log", []):
        if entry["kind"] == USAGE:
            _count_tokens(run, entry)
            continue
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
        return None  # parou no meio (falha ou worker reiniciado): continua do último passo salvo
    if run.follow_up and snapshot.values.get("frames"):
        # A execução já tinha terminado e a pessoa pediu um ajuste: o agente continua a mesma conversa
        return follow_up_state(snapshot.values, run.follow_up)
    run.events.create(kind=RunEvent.Kind.STATUS, content=f"Agente {run.agent.name} começou a trabalhar.")
    if run.autonomous:
        run.events.create(
            kind=RunEvent.Kind.STATUS, content="Modo autônomo: as ações sensíveis rodam sem pedir aprovação."
        )
    return initial_state(run)


@contextmanager
def _heartbeat(run):
    """Mantém o sinal de vida da execução em dia enquanto o bloco roda, mesmo durante um passo demorado."""
    stop = threading.Event()

    def beat():
        try:
            while not stop.wait(HEARTBEAT_INTERVAL):
                Run.objects.filter(pk=run.pk).update(heartbeat_at=timezone.now())
        finally:
            connection.close()  # a thread tem a própria conexão com o banco

    thread = threading.Thread(target=beat, name=f"heartbeat-run-{run.pk}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


@shared_task(bind=True)
def execute_run(self, run_id):
    """Roda o grafo da execução até ele terminar, parar esperando aprovação ou ser cancelado."""
    # Só começa se estiver na fila: evita rodar duas vezes se a tarefa for reentregue
    claimed = Run.objects.filter(pk=run_id, status=Run.Status.QUEUED).update(
        status=Run.Status.RUNNING, heartbeat_at=timezone.now(), worker=self.request.hostname or ""
    )
    if not claimed:
        return
    run = Run.objects.select_related("project", "agent").get(pk=run_id)
    if run.started_at is None:
        run.started_at = timezone.now()
        run.save(update_fields=["started_at"])

    result, requested, canceled, taken_away, error = "", [], False, False, None
    try:
        with _heartbeat(run):
            if not workspace_path(run).exists():
                run.events.create(kind=RunEvent.Kind.STATUS, content="Preparando o repositório.")
            workspace = prepare_workspace(run)
            with open_checkpointer() as checkpointer:
                graph = build_graph(run, workspace, checkpointer)
                # thread_id liga o estado salvo do grafo a esta execução
                config = {"configurable": {"thread_id": str(run.pk)}, "recursion_limit": settings.RUN_MAX_STEPS}
                graph_input = _graph_input(run, graph.get_state(config))
                # durability="sync": cada passo só conta como feito depois de gravado no banco, para que
                # uma execução interrompida à força continue exatamente do último passo concluído
                for update in graph.stream(graph_input, config, stream_mode="updates", durability="sync"):
                    for node, node_update in update.items():
                        if node == "__interrupt__":
                            requested = node_update[0].value
                        else:
                            result = _record_log(run, node_update) or result
                    # Entre um passo e outro: o pedido de cancelamento vale aqui, e se a execução já não
                    # está mais em andamento (foi dada como falha por falta de sinal) este worker a larga
                    status, cancel_requested = Run.objects.values_list("status", "cancel_requested").get(pk=run.pk)
                    if status != Run.Status.RUNNING:
                        taken_away = True
                        break
                    if cancel_requested:
                        canceled = True
                        break
    except Exception as exc:
        error = exc

    with transaction.atomic():
        # Mesma trava da tela de cancelar: o pedido chega antes ou depois deste desfecho, nunca no meio
        current = Run.objects.select_for_update().get(pk=run.pk)
        if taken_away or current.status != Run.Status.RUNNING:
            return  # outro processo já definiu o desfecho; não sobrescreve
        if error is not None:
            run.events.create(kind=RunEvent.Kind.ERROR, content=f"{type(error).__name__}: {error}")
            run.status = Run.Status.FAILED
            run.finished_at = timezone.now()
        elif canceled or (requested and current.cancel_requested):
            run.mark_canceled()
        elif requested:
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
            run.follow_up = ""
            run.events.create(kind=RunEvent.Kind.STATUS, content="Execução concluída.")
        run.save(update_fields=["result", "status", "finished_at", "follow_up"])


def enqueue(run, event):
    run.events.create(kind=RunEvent.Kind.STATUS, content=event)
    # Só enfileira depois do commit, para o worker enxergar a execução no banco
    transaction.on_commit(lambda: execute_run.delay(run.pk))


@shared_task
def run_due_schedules():
    """Cria as execuções dos agendamentos cujo horário chegou. Devolve quantas foram criadas."""
    now = timezone.now()
    due = Schedule.objects.filter(is_active=True, next_run_at__lte=now)
    created = 0
    for pk in list(due.values_list("pk", flat=True)):
        with transaction.atomic():
            # Trava e confere de novo: outra rodada ou uma edição na tela pode ter passado na frente
            schedule = due.select_for_update(of=("self",)).select_related("agent").filter(pk=pk).first()
            if schedule is None:
                continue
            if not schedule.agent.is_active:
                reason = "O agente está inativo."
            elif schedule.runs.filter(status__in=Run.ACTIVE_STATUSES).exists():
                # Não empilha: se a execução anterior ainda não terminou, esta rodada é pulada
                reason = "A execução anterior ainda não tinha terminado."
            else:
                reason = ""
                run = schedule.runs.create(
                    project=schedule.project,
                    agent=schedule.agent,
                    prompt=schedule.prompt,
                    autonomous=schedule.autonomous,
                    created_by=schedule.created_by,
                )
                enqueue(run, "Execução criada pelo agendamento e colocada na fila.")
                created += 1
            schedule.skipped_at, schedule.skip_reason = (now if reason else None), reason
            # Conta a partir de agora: se o beat ficou parado, as rodadas perdidas não saem todas de uma vez
            schedule.next_run_at = schedule.next_occurrence(now)
            schedule.save(update_fields=["skipped_at", "skip_reason", "next_run_at"])
    return created


def _fail_orphaned(runs, reason):
    """Dá como falha execuções "em andamento" que nenhum worker está mais tocando. Devolve quantas."""
    failed = 0
    for run in runs.filter(status=Run.Status.RUNNING):
        # O filtro por situação evita atropelar uma execução que acabou de terminar normalmente
        if Run.objects.filter(pk=run.pk, status=Run.Status.RUNNING).update(
            status=Run.Status.FAILED, finished_at=timezone.now()
        ):
            run.events.create(
                kind=RunEvent.Kind.ERROR,
                content=f"{reason} O trabalho feito até o último passo está salvo: use “Continuar de onde parou”.",
            )
            failed += 1
    return failed


@shared_task
def fail_stale_runs():
    """Execuções em andamento sem sinal de vida há tempo demais: o worker morreu no meio do trabalho."""
    cutoff = timezone.now() - timedelta(seconds=settings.RUN_STALE_SECONDS)
    stale = Run.objects.filter(Q(heartbeat_at__lt=cutoff) | Q(heartbeat_at__isnull=True))
    return _fail_orphaned(stale, "O worker parou de responder no meio da execução.")


@worker_ready.connect
def _fail_runs_of_restarted_worker(sender, **kwargs):
    """Ao subir, o worker encerra as execuções que eram dele antes de reiniciar, sem esperar a verificação."""
    _fail_orphaned(Run.objects.filter(worker=sender.hostname), "O worker foi reiniciado no meio da execução.")
    fail_stale_runs()


@shared_task
def cleanup_workspaces(days=None):
    """Apaga worktrees de execuções finalizadas há mais de `days` dias. Devolve quantos foram apagados."""
    days = settings.WORKSPACE_RETENTION_DAYS if days is None else days
    cutoff = timezone.now() - timedelta(days=days)
    root = workspaces_root()
    removed = 0
    for path in root.iterdir() if root.exists() else []:
        run = Run.objects.filter(pk=path.name).first() if path.name.isdigit() else None
        if run is not None and run.finished_at and run.finished_at < cutoff:
            remove_workspace(run)
            removed += 1
    return removed
