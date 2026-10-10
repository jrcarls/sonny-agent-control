from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import AgentForm, ProjectForm, RunForm
from .models import Agent, Project, Run, RunEvent
from .tasks import execute_run


class ProtectedDeleteMixin:
    """Projetos e agentes com execuções não podem ser excluídos: avisa em vez de dar erro."""

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, f"“{self.object}” tem execuções e não pode ser excluído.")
            return redirect(self.success_url)


class HomeView(LoginRequiredMixin, CreateView):
    model = Run
    form_class = RunForm
    template_name = "home/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context["form"]
        context["can_submit"] = form.fields["project"].queryset.exists() and form.fields["agent"].queryset.exists()
        context["recent_runs"] = Run.objects.select_related("project", "agent")[:5]
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        run = self.object
        run.events.create(kind=RunEvent.Kind.STATUS, content="Execução colocada na fila.")
        # Só enfileira depois do commit, para o worker enxergar a execução no banco
        transaction.on_commit(lambda: execute_run.delay(run.pk))
        return response


class ProjectListView(LoginRequiredMixin, ListView):
    model = Project
    template_name = "projects/list.html"
    context_object_name = "projects"


class ProjectCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/form.html"
    success_url = reverse_lazy("orchestrator:project_list")
    success_message = "Projeto “%(name)s” criado."


class ProjectUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Project
    form_class = ProjectForm
    template_name = "projects/form.html"
    success_url = reverse_lazy("orchestrator:project_list")
    success_message = "Projeto “%(name)s” atualizado."


class ProjectDeleteView(LoginRequiredMixin, ProtectedDeleteMixin, SuccessMessageMixin, DeleteView):
    model = Project
    template_name = "orchestrator/confirm_delete.html"
    success_url = reverse_lazy("orchestrator:project_list")
    success_message = "Projeto excluído."
    extra_context = {"cancel_url": success_url}


class AgentListView(LoginRequiredMixin, ListView):
    model = Agent
    template_name = "agents/list.html"
    context_object_name = "agents"


class AgentCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Agent
    form_class = AgentForm
    template_name = "agents/form.html"
    success_url = reverse_lazy("orchestrator:agent_list")
    success_message = "Agente “%(name)s” criado."


class AgentUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Agent
    form_class = AgentForm
    template_name = "agents/form.html"
    success_url = reverse_lazy("orchestrator:agent_list")
    success_message = "Agente “%(name)s” atualizado."


class AgentDeleteView(LoginRequiredMixin, ProtectedDeleteMixin, SuccessMessageMixin, DeleteView):
    model = Agent
    template_name = "orchestrator/confirm_delete.html"
    success_url = reverse_lazy("orchestrator:agent_list")
    success_message = "Agente excluído."
    extra_context = {"cancel_url": success_url}


class RunListView(LoginRequiredMixin, ListView):
    model = Run
    template_name = "runs/list.html"
    context_object_name = "runs"
    paginate_by = 25
    FILTERS = {
        "ativas": {"status__in": Run.ACTIVE_STATUSES},
        "finalizadas": {"status__in": [Run.Status.SUCCEEDED, Run.Status.FAILED]},
    }

    def get_queryset(self):
        queryset = super().get_queryset().select_related("project", "agent")
        return queryset.filter(**self.FILTERS.get(self.request.GET.get("situacao"), {}))

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs, situacao=self.request.GET.get("situacao", ""))


class RunDetailView(LoginRequiredMixin, DetailView):
    queryset = Run.objects.select_related("project", "agent")
    context_object_name = "run"

    def get_template_names(self):
        # A tela consulta este mesmo endereço via htmx enquanto a execução está ativa
        return ["runs/_progress.html"] if self.request.htmx else ["runs/detail.html"]
