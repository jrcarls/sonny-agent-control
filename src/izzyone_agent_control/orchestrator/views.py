from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from .forms import AgentForm, ProjectForm
from .models import Agent, Project


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


class ProjectDeleteView(LoginRequiredMixin, SuccessMessageMixin, DeleteView):
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


class AgentDeleteView(LoginRequiredMixin, SuccessMessageMixin, DeleteView):
    model = Agent
    template_name = "orchestrator/confirm_delete.html"
    success_url = reverse_lazy("orchestrator:agent_list")
    success_message = "Agente excluído."
    extra_context = {"cancel_url": success_url}
