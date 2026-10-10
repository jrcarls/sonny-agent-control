from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django_htmx.http import HttpResponseClientRedirect
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from .forms import AgentForm, ProjectForm, RunForm
from .models import Agent, Approval, Project, Run, RunEvent
from .providers import MODEL_SUGGESTIONS, ModelListError, check_model, list_models, missing_api_keys
from .tasks import execute_run
from .workspace import WorkspaceError, list_remote_branches, workspace_path


# Itens por página nas listas e execuções recentes mostradas na tela inicial
PAGE_SIZE = 25
HOME_RECENT_RUNS = 5


def enqueue(run, event):
    run.events.create(kind=RunEvent.Kind.STATUS, content=event)
    # Só enfileira depois do commit, para o worker enxergar a execução no banco
    transaction.on_commit(lambda: execute_run.delay(run.pk))


class SidebarView(LoginRequiredMixin, TemplateView):
    """Só o menu lateral, para o htmx atualizar contadores e execuções recentes sem recarregar a página."""

    template_name = "layouts/sidebar_nav.html"


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

    def get_initial(self):
        """Já deixa escolhidos o projeto e o agente da última tarefa da pessoa, ou os únicos que existem."""
        initial = super().get_initial()
        projects = list(Project.objects.values_list("pk", flat=True)[:2])
        agents = list(Agent.objects.filter(is_active=True).values_list("pk", flat=True)[:2])
        last = Run.objects.filter(created_by=self.request.user).select_related("agent").first()
        if last:
            initial["project"] = last.project_id
            if last.agent.is_active:
                initial["agent"] = last.agent_id
        if len(projects) == 1:
            initial["project"] = projects[0]
        if len(agents) == 1:
            initial["agent"] = agents[0]
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agents = context["form"].fields["agent"].queryset
        has_project, has_agent, has_run = Project.objects.exists(), agents.exists(), Run.objects.exists()
        context["can_submit"] = has_project and has_agent
        context["recent_runs"] = Run.objects.select_related("project", "agent")[:HOME_RECENT_RUNS]
        # id do agente -> variável de ambiente que falta, para avisar antes de a execução falhar
        missing = missing_api_keys()
        context["agents_without_key"] = {
            str(agent.pk): missing[agent.provider] for agent in agents if agent.provider in missing
        }
        if not (has_project and has_agent and has_run):
            context["onboarding"] = [
                {
                    "title": "Adicionar um projeto",
                    "text": "Informe o repositório git em que os agentes vão trabalhar.",
                    "icon": "folders",
                    "url": reverse("orchestrator:project_list") + "?novo=1",
                    "done": has_project,
                },
                {
                    "title": "Criar um agente",
                    "text": "Defina o modelo, as instruções e o que ele tem permissão para fazer.",
                    "icon": "robot",
                    "url": reverse("orchestrator:agent_create"),
                    "done": has_agent,
                },
                {
                    "title": "Enviar a primeira tarefa",
                    "text": "Descreva o que precisa na caixa acima e acompanhe a execução.",
                    "icon": "player-play",
                    "url": "#prompt",
                    "done": has_run,
                },
            ]
        return context

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        enqueue(self.object, "Execução colocada na fila.")
        return response


class ProjectListView(LoginRequiredMixin, ListView):
    model = Project
    template_name = "projects/list.html"
    context_object_name = "projects"

    def get_context_data(self, **kwargs):
        # Criar e editar projeto acontecem em um modal nesta tela. ?novo=1 e ?editar=<id> já chegam
        # com ele aberto: o conteúdo é buscado no endereço abaixo assim que a página carrega.
        params, modal_url = self.request.GET, None
        if "novo" in params:
            modal_url = reverse("orchestrator:project_create")
        elif params.get("editar", "").isdigit():
            modal_url = reverse("orchestrator:project_update", args=[params["editar"]])
        return super().get_context_data(**kwargs, modal_url=modal_url)


class ProjectModalMixin:
    """Formulário de projeto dentro do modal da lista: ao htmx devolve só o conteúdo do modal.

    Sem htmx (endereço aberto direto, ou navegador sem JavaScript), o GET leva para a lista com o modal
    aberto e o POST com erro cai em uma página simples com o mesmo formulário.
    """

    model = Project
    form_class = ProjectForm
    modal_template = None

    def modal_page_url(self):
        raise NotImplementedError

    def get(self, request, *args, **kwargs):
        if not request.htmx:
            return redirect(self.modal_page_url())
        return super().get(request, *args, **kwargs)

    def get_template_names(self):
        return [self.modal_template] if self.request.htmx else ["projects/form.html"]

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs, modal_template=self.modal_template)

    def form_valid(self, form):
        response = super().form_valid(form)
        return HttpResponseClientRedirect(self.get_success_url()) if self.request.htmx else response


class ProjectCreateView(LoginRequiredMixin, ProjectModalMixin, SuccessMessageMixin, CreateView):
    modal_template = "projects/_modal_create.html"
    success_message = "Projeto “%(name)s” criado. Se o repositório for privado, cadastre nele a chave do projeto."

    def modal_page_url(self):
        return reverse("orchestrator:project_list") + "?novo=1"

    def get_success_url(self):
        # Reabre o modal no projeto recém-criado, onde está a chave pública a cadastrar no repositório
        return f"{reverse('orchestrator:project_list')}?editar={self.object.pk}"


class ProjectUpdateView(LoginRequiredMixin, ProjectModalMixin, SuccessMessageMixin, UpdateView):
    modal_template = "projects/_modal_edit.html"
    success_url = reverse_lazy("orchestrator:project_list")
    success_message = "Projeto “%(name)s” atualizado."

    def modal_page_url(self):
        return f"{reverse('orchestrator:project_list')}?editar={self.kwargs['pk']}"

    def get_object(self, queryset=None):
        project = super().get_object(queryset)
        if not project.ssh_public_key:
            project.save()  # projetos criados antes das chaves existirem ganham a sua ao abrir o modal
        return project


class ProjectAccessTestView(LoginRequiredMixin, View):
    """Confere se o sistema alcança o repositório do projeto, com o endereço e a chave que estão salvos."""

    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        context = {"project": project}
        try:
            context["branches"] = branches = list_remote_branches(project)
            context["has_default_branch"] = project.default_branch in branches
        except WorkspaceError as exc:
            context["error"] = str(exc)
        return render(request, "projects/_access_result.html", context)


class ProjectKeyRegenerateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        project = get_object_or_404(Project, pk=pk)
        project.generate_ssh_key()
        project.save()
        messages.success(request, "Chave nova gerada. A anterior deixou de valer: cadastre a nova no repositório.")
        return redirect(project)


class ProjectDeleteView(LoginRequiredMixin, ProtectedDeleteMixin, SuccessMessageMixin, DeleteView):
    model = Project
    template_name = "layouts/confirm_delete.html"
    success_url = reverse_lazy("orchestrator:project_list")
    success_message = "Projeto excluído."
    extra_context = {"cancel_url": success_url}


class AgentListView(LoginRequiredMixin, ListView):
    model = Agent
    template_name = "agents/list.html"
    context_object_name = "agents"


class AgentFormMixin:
    """Dados de apoio da tela do agente: sugestões de modelo e provedores sem chave configurada."""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs, missing_keys=missing_api_keys())
        # A tela abre com as sugestões fixas e troca pela lista real do provedor assim que ela chega
        context["models"] = MODEL_SUGGESTIONS.get(context["form"]["provider"].value(), [])
        return context


class AgentModelsView(LoginRequiredMixin, View):
    """Opções do campo "Modelo" para um provedor: a lista real dele ou, se a consulta falhar, as fixas."""

    def get(self, request):
        provider = request.GET.get("provider", "")
        context = {"models": [], "live": False, "provider": provider}
        if provider in Agent.Provider.values:
            try:
                context["models"], context["live"] = list_models(provider), True
            except ModelListError as exc:
                context["models"], context["error"] = MODEL_SUGGESTIONS[Agent.Provider(provider)], str(exc)
        return render(request, "agents/_model_options.html", context)


class AgentTestView(LoginRequiredMixin, View):
    """Testa o provedor e o modelo preenchidos no formulário, sem salvar nada."""

    def post(self, request):
        provider, model = request.POST.get("provider", ""), request.POST.get("model", "").strip()
        context = {"model": model}
        if provider not in Agent.Provider.values or not model:
            context["error"] = "Escolha o provedor e informe o modelo antes de testar."
        else:
            try:
                context["answer"], context["seconds"] = check_model(provider, model)
            except Exception as exc:  # qualquer falha do provedor é o próprio resultado do teste
                context["error"] = f"{type(exc).__name__}: {exc}"[:600]
        return render(request, "agents/_test_result.html", context)


class AgentCreateView(LoginRequiredMixin, AgentFormMixin, SuccessMessageMixin, CreateView):
    model = Agent
    form_class = AgentForm
    template_name = "agents/form.html"
    success_url = reverse_lazy("orchestrator:agent_list")
    success_message = "Agente “%(name)s” criado."


class AgentUpdateView(LoginRequiredMixin, AgentFormMixin, SuccessMessageMixin, UpdateView):
    model = Agent
    form_class = AgentForm
    template_name = "agents/form.html"
    success_url = reverse_lazy("orchestrator:agent_list")
    success_message = "Agente “%(name)s” atualizado."


class AgentDeleteView(LoginRequiredMixin, ProtectedDeleteMixin, SuccessMessageMixin, DeleteView):
    model = Agent
    template_name = "layouts/confirm_delete.html"
    success_url = reverse_lazy("orchestrator:agent_list")
    success_message = "Agente excluído."
    extra_context = {"cancel_url": success_url}


class RunListView(LoginRequiredMixin, ListView):
    model = Run
    template_name = "runs/list.html"
    context_object_name = "runs"
    paginate_by = PAGE_SIZE
    FILTERS = {
        "ativas": {"status__in": Run.ACTIVE_STATUSES},
        "finalizadas": {"status__in": [Run.Status.SUCCEEDED, Run.Status.FAILED, Run.Status.CANCELED]},
    }

    def get_queryset(self):
        queryset = super().get_queryset().select_related("project", "agent")
        return queryset.filter(**self.FILTERS.get(self.request.GET.get("situacao"), {}))

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs, situacao=self.request.GET.get("situacao", ""))


def can_resume(run):
    """Uma execução que falhou pode continuar do último passo salvo enquanto o worktree dela existir."""
    return run.status == Run.Status.FAILED and workspace_path(run).exists()


class RunDetailView(LoginRequiredMixin, DetailView):
    queryset = Run.objects.select_related("project", "agent")
    context_object_name = "run"

    def get_context_data(self, **kwargs):
        pending = self.object.approvals.filter(status=Approval.Status.PENDING).select_related("agent")
        events = self.object.events.select_related("agent")
        return super().get_context_data(
            **kwargs, pending_approvals=pending, events=events, can_resume=can_resume(self.object)
        )

    def get_template_names(self):
        # A tela consulta este mesmo endereço via htmx enquanto a execução está ativa
        return ["runs/_progress.html"] if self.request.htmx else ["runs/detail.html"]


class RunCancelView(LoginRequiredMixin, View):
    def post(self, request, pk):
        with transaction.atomic():
            run = get_object_or_404(Run.objects.select_for_update(), pk=pk)
            who = request.user.get_username()
            if run.status == Run.Status.RUNNING:
                # O worker vê o pedido no fim do passo atual e encerra a execução
                if not run.cancel_requested:
                    run.cancel_requested = True
                    run.save(update_fields=["cancel_requested"])
                    run.events.create(kind=RunEvent.Kind.STATUS, content=f"{who} pediu o cancelamento.")
            elif run.status in [Run.Status.QUEUED, Run.Status.WAITING]:
                run.events.create(kind=RunEvent.Kind.STATUS, content=f"{who} cancelou a execução.")
                run.mark_canceled()
            else:
                messages.error(request, "Esta execução já terminou.")
        return redirect(run)


class RunResumeView(LoginRequiredMixin, View):
    """Recoloca na fila uma execução que falhou; o grafo continua do último passo salvo."""

    def post(self, request, pk):
        with transaction.atomic():
            run = get_object_or_404(Run.objects.select_for_update(), pk=pk)
            if can_resume(run):
                run.status = Run.Status.QUEUED
                run.finished_at = None
                run.cancel_requested = False
                run.save(update_fields=["status", "finished_at", "cancel_requested"])
                enqueue(run, f"{request.user.get_username()} mandou continuar de onde parou.")
            else:
                messages.error(request, "Esta execução não pode ser continuada.")
        return redirect(run)


class RunRetryView(LoginRequiredMixin, View):
    """Cria uma execução nova com a mesma tarefa, projeto e agente, começando do zero."""

    def post(self, request, pk):
        original = get_object_or_404(Run, pk=pk)
        run = Run.objects.create(
            project=original.project, agent=original.agent, prompt=original.prompt, created_by=request.user
        )
        enqueue(run, f"Execução colocada na fila (nova tentativa de {original}).")
        return redirect(run)


class ApprovalListView(LoginRequiredMixin, ListView):
    template_name = "approvals/list.html"
    context_object_name = "approvals"
    paginate_by = PAGE_SIZE

    def get_queryset(self):
        queryset = Approval.objects.select_related("run__project", "run__agent", "agent", "decided_by")
        if self.request.GET.get("situacao") == "historico":
            return queryset.exclude(status=Approval.Status.PENDING)
        return queryset.filter(status=Approval.Status.PENDING)

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs, situacao=self.request.GET.get("situacao", ""))


class ApprovalDecideView(LoginRequiredMixin, View):
    """Aprova ou nega um pedido. Com o último pedido pendente decidido, a execução volta para a fila."""

    def post(self, request, pk):
        approval = get_object_or_404(Approval.objects.select_related("run"), pk=pk)
        approved = request.POST.get("decision") == "approve"
        status = Approval.Status.APPROVED if approved else Approval.Status.REJECTED
        with transaction.atomic():
            # Trava a execução: decisões simultâneas de pedidos diferentes passam uma de cada vez,
            # senão cada uma veria a outra como pendente e a execução nunca voltaria para a fila
            run = Run.objects.select_for_update().get(pk=approval.run_id)
            # O filtro por pendente garante que dois cliques simultâneos não decidem duas vezes
            decided = Approval.objects.filter(pk=pk, status=Approval.Status.PENDING).update(
                status=status, decided_by=request.user, decided_at=timezone.now()
            )
            if decided:
                verb = "aprovou" if approved else "negou"
                run.events.create(
                    kind=RunEvent.Kind.STATUS, content=f"{request.user.get_username()} {verb}: {approval.tool_label}."
                )
                still_pending = run.approvals.filter(status=Approval.Status.PENDING).exists()
                if not still_pending and Run.objects.filter(pk=run.pk, status=Run.Status.WAITING).update(
                    status=Run.Status.QUEUED
                ):
                    transaction.on_commit(lambda: execute_run.delay(run.pk))
        if not decided:
            messages.error(request, "Esse pedido já tinha sido decidido.")
        return redirect(run)
