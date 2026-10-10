from django.urls import Resolver404, resolve
from django.utils.functional import SimpleLazyObject

from .models import Approval, Run

RECENT_RUNS = 8
# Prefixo do nome da rota -> item do menu que fica destacado
SECTIONS = {"run": "runs", "approval": "approvals", "project": "projects", "agent": "agents"}


def _section(path):
    try:
        match = resolve(path)
    except Resolver404:
        return ""
    if match.namespace != "orchestrator":
        return ""
    return SECTIONS.get(match.url_name.split("_")[0], "")


def _build(request):
    # No pedido do htmx que atualiza o menu, a página em que a pessoa está vem no cabeçalho
    path = request.htmx.current_url_abs_path if request.htmx else request.path
    path = (path or "").split("?")[0]
    return {
        "path": path,
        "section": _section(path),
        "pending_approvals": Approval.objects.filter(status=Approval.Status.PENDING).count(),
        "running_runs": Run.objects.filter(status__in=[Run.Status.QUEUED, Run.Status.RUNNING]).count(),
        "recent_runs": list(Run.objects.only("prompt", "status")[:RECENT_RUNS]),
    }


def sidebar(request):
    """Dados do menu lateral. Preguiçoso: só consulta o banco nas páginas que desenham a sidebar."""
    return {"sidebar": SimpleLazyObject(lambda: _build(request))}
