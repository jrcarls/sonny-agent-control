from django.core.management.base import BaseCommand

from orchestrator.tasks import cleanup_workspaces


class Command(BaseCommand):
    help = "Apaga os worktrees de execuções finalizadas há mais de N dias."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, help="Padrão: WORKSPACE_RETENTION_DAYS")

    def handle(self, *args, days, **options):
        self.stdout.write(f"{cleanup_workspaces(days)} worktree(s) apagado(s).")
