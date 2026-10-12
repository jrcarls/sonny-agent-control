from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages.views import SuccessMessageMixin
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, ProtectedError, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.http import urlencode
from django.utils import timezone
from django.views import View
from django_htmx.http import HttpResponseClientRedirect
from django.views.generic import CreateView, DeleteView, DetailView, ListView, TemplateView, UpdateView

from .crypto import DecryptionError
from .diffs import parse_diff
from .forms import AgentForm, CredentialForm, ProjectForm, RunForm, ScheduleForm, SkillForm
from .integrations import INTEGRATIONS, IntegrationError, Service, environment_variable, github_login, mask
from .models import Agent, Approval, Credential, Project, Run, RunEvent, Schedule, Skill
from .providers import (
    MODEL_SUGGESTIONS,
    ModelListError,
    check_model,
    fetch_models,
    forget_models,
    list_models,
    missing_api_keys,
)
from .tasks import MAX_EVENT_LENGTH, enqueue, execute_run
from .timeline import build_timeline
from .workspace import WorkspaceError, list_remote_branches, workspace_diff, workspace_path


# Itens por página nas listas e execuções recentes mostradas na tela inicial
PAGE_SIZE = 25
HOME_RECENT_RUNS = 5
# Exemplos de tarefa na tela inicial: um clique preenche a caixa de texto
PROMPT_SUGGESTIONS = [
    "Explique como este projeto está organizado",
    "Rode os testes e corrija o que falhar",
    "Procure bugs prováveis e proponha correções",
    "Atualize o README com o estado atual do código",
]


class SidebarView(LoginRequiredMixin, TemplateView):
    """Só o menu lateral, para o htmx atualizar contadores e execuções recentes sem recarregar a página."""

    template_name = "layouts/sidebar_nav.html"


class ProtectedDeleteMixin:
    """Projetos e agentes com execuções ou agendamentos não podem ser excluídos: avisa em vez de dar erro."""

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except ProtectedError:
            messages.error(self.request, f"“{self.object}” tem execuções ou agendamentos e não pode ser excluído.")
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
        context["suggestions"] = PROMPT_SUGGESTIONS
        if has_run:
            context["stats"] = self.get_stats()
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

    def get_stats(self):
        """Números do painel da tela inicial; cada um leva à lista correspondente."""
        week_ago = timezone.now() - timedelta(days=7)
        finished = Run.objects.filter(finished_at__gte=week_ago)
        runs_url, approvals_url = reverse("orchestrator:run_list"), reverse("orchestrator:approval_list")
        return [
            {
                "label": "Em andamento",
                "value": Run.objects.filter(status__in=[Run.Status.QUEUED, Run.Status.RUNNING]).count(),
                "icon": "player-play",
                "url": f"{runs_url}?situacao=ativas",
            },
            {
                "label": "Aguardando aprovação",
                "value": Approval.objects.filter(status=Approval.Status.PENDING).count(),
                "icon": "checklist",
                "url": approvals_url,
                "attention": True,
            },
            {
                "label": "Concluídas em 7 dias",
                "value": finished.filter(status=Run.Status.SUCCEEDED).count(),
                "icon": "circle-check",
                "url": f"{runs_url}?situacao=finalizadas",
            },
            {
                "label": "Com falha em 7 dias",
                "value": finished.filter(status=Run.Status.FAILED).count(),
                "icon": "alert-triangle",
                "url": f"{runs_url}?situacao=finalizadas",
            },
        ]

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


class UsageView(LoginRequiredMixin, TemplateView):
    """Tokens gastos e custo estimado das execuções em um período, no total, por projeto e por agente."""

    template_name = "usage/index.html"
    # Valor de ?periodo= -> dias para trás; None é desde sempre
    PERIODS = {"7": 7, "30": 30, "tudo": None}
    DEFAULT_PERIOD = "30"
    TOP_RUNS = 10
    # Custo de uma execução pelos preços (dólares por milhão de tokens) do agente dela. Sem preço
    # cadastrado o resultado é nulo, e as somas deixam a execução de fora.
    COST = ExpressionWrapper(
        (
            F("input_tokens") * Coalesce(F("agent__input_price"), Value(Decimal(0)))
            + F("output_tokens") * Coalesce(F("agent__output_price"), Value(Decimal(0)))
        )
        / Value(Decimal(1_000_000)),
        output_field=DecimalField(max_digits=20, decimal_places=6),
    )
    UNPRICED = Q(agent__input_price__isnull=True, agent__output_price__isnull=True)
    TOTALS = {
        "runs": Count("pk"),
        "input": Coalesce(Sum("input_tokens"), 0),
        "output": Coalesce(Sum("output_tokens"), 0),
        "cost": Sum(COST, filter=~UNPRICED),
        # Execuções que gastaram tokens, mas ficaram fora do custo porque o agente não tem preço
        "unpriced": Count("pk", filter=UNPRICED & (Q(input_tokens__gt=0) | Q(output_tokens__gt=0))),
    }

    def breakdown(self, runs, field):
        rows = runs.values(name=F(field)).annotate(**self.TOTALS).filter(Q(input__gt=0) | Q(output__gt=0))
        return sorted(rows, key=lambda row: row["input"] + row["output"], reverse=True)

    def get_context_data(self, **kwargs):
        period = self.request.GET.get("periodo")
        if period not in self.PERIODS:
            period = self.DEFAULT_PERIOD
        runs = Run.objects.all()
        if days := self.PERIODS[period]:
            runs = runs.filter(created_at__gte=timezone.now() - timedelta(days=days))
        spenders = runs.annotate(total=F("input_tokens") + F("output_tokens")).filter(total__gt=0)
        return super().get_context_data(
            **kwargs,
            period=period,
            totals=runs.aggregate(**self.TOTALS),
            by_project=self.breakdown(runs, "project__name"),
            by_agent=self.breakdown(runs, "agent__name"),
            top_runs=spenders.select_related("project", "agent").order_by("-total")[: self.TOP_RUNS],
        )


class SkillListView(LoginRequiredMixin, ListView):
    queryset = Skill.objects.annotate(agent_count=Count("agents"))
    template_name = "skills/list.html"
    context_object_name = "skills"


class SkillCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Skill
    form_class = SkillForm
    template_name = "skills/form.html"
    success_url = reverse_lazy("orchestrator:skill_list")
    success_message = "Skill “%(name)s” criada. Marque-a nos agentes que devem segui-la."


class SkillUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Skill
    form_class = SkillForm
    template_name = "skills/form.html"
    success_url = reverse_lazy("orchestrator:skill_list")
    success_message = "Skill “%(name)s” atualizada. Vale a partir da próxima execução."

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs, agents=self.object.agents.all())


class SkillDeleteView(LoginRequiredMixin, SuccessMessageMixin, DeleteView):
    model = Skill
    template_name = "layouts/confirm_delete.html"
    success_url = reverse_lazy("orchestrator:skill_list")
    success_message = "Skill excluída."
    extra_context = {"cancel_url": success_url}


def get_service(value):
    if value not in Service.values:
        raise Http404
    return Service(value)


def agents_per_service():
    """Serviço -> agentes ativos que dependem da chave dele (no GitHub, os que podem abrir pull request)."""
    active = Agent.objects.filter(is_active=True)
    usage = dict(active.values_list("provider").annotate(Count("pk")))
    usage[Service.GITHUB] = active.filter(tools__contains=["open_pull_request"]).count()
    return usage


class IntegrationListView(LoginRequiredMixin, TemplateView):
    """Chaves de API dos provedores e do GitHub: de onde vem a que está em uso e o campo para cadastrar outra."""

    template_name = "integrations/list.html"

    def get_context_data(self, **kwargs):
        credentials = {item.service: item for item in Credential.objects.select_related("updated_by")}
        usage = agents_per_service()
        # Depois de salvar, a tela volta com ?verificar=<serviço> e confere a chave nova sozinha
        verify = self.request.GET.get("verificar")
        items = []
        for service, integration in INTEGRATIONS.items():
            item = {
                "service": service,
                "integration": integration,
                "variable": environment_variable(service),
                "agents": usage.get(service, 0),
            }
            credential = credentials.get(service)
            if credential:
                item["credential"] = credential
                try:
                    item["hint"] = mask(credential.get_secret())
                except DecryptionError:
                    item["unreadable"] = True
            # De onde vem a chave em uso: a salva aqui vale antes da variável de ambiente
            item["source"] = "stored" if "hint" in item else "environment" if item["variable"] else ""
            item["verify"] = verify == service and bool(item["source"])
            # O painel da chave já vem aberto quando há algo para a pessoa ver ou resolver nele
            item["open"] = item["verify"] or "unreadable" in item
            # Agentes de um provedor sem chave falham; sem o token do GitHub o agente só entrega o link
            item["broken"] = bool(item["agents"]) and not item["source"] and service != Service.GITHUB
            items.append(item)
        return super().get_context_data(**kwargs, integrations=items)


class IntegrationSaveView(LoginRequiredMixin, View):
    def post(self, request, service):
        service = get_service(service)
        form = CredentialForm(request.POST)
        url = reverse("orchestrator:integration_list")
        if not form.is_valid():
            messages.error(request, f"A chave de {service.label} não foi salva: {form.errors['secret'][0]}")
            return redirect(url)
        form.save(service, request.user)
        forget_models(service)
        messages.success(request, f"Chave de {service.label} salva. Ela já vale para as próximas execuções.")
        return redirect(f"{url}?verificar={service}")


class IntegrationDeleteView(LoginRequiredMixin, View):
    def post(self, request, service):
        service = get_service(service)
        Credential.objects.filter(service=service).delete()
        forget_models(service)
        messages.success(request, f"Chave de {service.label} removida.")
        return redirect("orchestrator:integration_list")


class IntegrationTestView(LoginRequiredMixin, View):
    """Confere a chave em uso consultando o serviço: a lista de modelos do provedor ou a conta do token."""

    def post(self, request, service):
        service = get_service(service)
        context = {"service": service}
        try:
            if service == Service.GITHUB:
                context["detail"] = f"O token pertence à conta {github_login()}."
            else:
                context["detail"] = f"O provedor respondeu com {len(fetch_models(service))} modelos disponíveis."
        except (IntegrationError, ModelListError) as exc:
            context["error"] = str(exc)
        return render(request, "integrations/_test_result.html", context)


class ScheduleListView(LoginRequiredMixin, ListView):
    # A ordem é repetida aqui porque a anotação agrupa a consulta e descarta a ordem padrão do modelo
    queryset = (
        Schedule.objects.select_related("project", "agent")
        .annotate(last_run_id=Max("runs__pk"))
        .order_by(F("next_run_at").asc(nulls_last=True), "-created_at")
    )
    template_name = "schedules/list.html"
    context_object_name = "schedules"
    paginate_by = PAGE_SIZE


class ScheduleCreateView(LoginRequiredMixin, SuccessMessageMixin, CreateView):
    model = Schedule
    form_class = ScheduleForm
    template_name = "schedules/form.html"
    success_url = reverse_lazy("orchestrator:schedule_list")
    success_message = "Agendamento criado."

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        return super().form_valid(form)


class ScheduleUpdateView(LoginRequiredMixin, SuccessMessageMixin, UpdateView):
    model = Schedule
    form_class = ScheduleForm
    template_name = "schedules/form.html"
    success_url = reverse_lazy("orchestrator:schedule_list")
    success_message = "Agendamento atualizado."
    RECENT_RUNS = 10

    def get_context_data(self, **kwargs):
        runs = self.object.runs.select_related("project", "agent")[: self.RECENT_RUNS]
        return super().get_context_data(**kwargs, runs=runs)


class ScheduleDeleteView(LoginRequiredMixin, SuccessMessageMixin, DeleteView):
    model = Schedule
    template_name = "layouts/confirm_delete.html"
    success_url = reverse_lazy("orchestrator:schedule_list")
    success_message = "Agendamento excluído. As execuções que ele criou continuam na lista."
    extra_context = {"cancel_url": success_url}


class RunListView(LoginRequiredMixin, ListView):
    model = Run
    template_name = "runs/list.html"
    context_object_name = "runs"
    paginate_by = PAGE_SIZE
    # Abas de situação: valor de ?situacao= -> (rótulo, filtro)
    SITUATIONS = {
        "": ("Todas", {}),
        "ativas": ("Em andamento", {"status__in": Run.ACTIVE_STATUSES}),
        "finalizadas": (
            "Finalizadas",
            {"status__in": [Run.Status.SUCCEEDED, Run.Status.FAILED, Run.Status.CANCELED]},
        ),
    }

    def get_filters(self):
        """Filtros pedidos na URL, já conferidos: situação, projeto, agente e texto da tarefa."""
        params = self.request.GET
        situation = params.get("situacao", "")
        return {
            "situacao": situation if situation in self.SITUATIONS else "",
            "projeto": params.get("projeto", "") if params.get("projeto", "").isdigit() else "",
            "agente": params.get("agente", "") if params.get("agente", "").isdigit() else "",
            "q": params.get("q", "").strip(),
        }

    def get_queryset(self):
        filters = self.get_filters()
        queryset = super().get_queryset().select_related("project", "agent")
        queryset = queryset.filter(**self.SITUATIONS[filters["situacao"]][1])
        if filters["projeto"]:
            queryset = queryset.filter(project_id=filters["projeto"])
        if filters["agente"]:
            queryset = queryset.filter(agent_id=filters["agente"])
        if filters["q"]:
            queryset = queryset.filter(prompt__icontains=filters["q"])
        return queryset

    def get_context_data(self, **kwargs):
        filters = self.get_filters()
        url = reverse("orchestrator:run_list")
        # As abas trocam só a situação e mantêm os outros filtros
        others = {name: value for name, value in filters.items() if value and name != "situacao"}
        tabs = [
            {
                "label": label,
                "href": f"{url}?{urlencode(others | ({'situacao': value} if value else {}))}".rstrip("?"),
                "active": value == filters["situacao"],
            }
            for value, (label, _) in self.SITUATIONS.items()
        ]
        return super().get_context_data(
            **kwargs,
            filters=filters,
            filtered=bool(others),
            tabs=tabs,
            # Parâmetros que a paginação precisa manter ao trocar de página
            query=urlencode({name: value for name, value in filters.items() if value}),
            projects=Project.objects.all(),
            agents=Agent.objects.all(),
        )


def can_resume(run):
    """Uma execução que falhou pode continuar do último passo salvo enquanto o worktree dela existir."""
    return run.status == Run.Status.FAILED and workspace_path(run).exists()


def can_follow_up(run):
    """Uma execução concluída aceita pedidos de ajuste enquanto o worktree dela existir."""
    return run.status == Run.Status.SUCCEEDED and workspace_path(run).exists()


class RunDetailView(LoginRequiredMixin, DetailView):
    queryset = Run.objects.select_related("project", "agent")
    context_object_name = "run"

    def get_context_data(self, **kwargs):
        pending = self.object.approvals.filter(status=Approval.Status.PENDING).select_related("agent")
        timeline = build_timeline(self.object, self.object.events.select_related("agent"))
        context = super().get_context_data(
            **kwargs,
            pending_approvals=pending,
            timeline=timeline,
            last_event=max((item.last for item in timeline), default=0),
            can_resume=can_resume(self.object),
            can_follow_up=can_follow_up(self.object),
            tab="diff" if self.request.GET.get("aba") == "alteracoes" else "progress",
        )
        if self.request.htmx:
            # A consulta periódica diz até que evento a tela já tem (?depois=) e recebe só o que mudou:
            # os itens novos e os que já estavam lá e ganharam o resultado da ferramenta
            after = self.request.GET.get("depois", "")
            after = int(after) if after.isdigit() else 0
            changed = [item for item in timeline if item.last > after]
            context["new_items"] = [item for item in changed if item.key > after]
            context["updated_items"] = [item for item in changed if item.key <= after]
        return context

    def get_template_names(self):
        # A tela consulta este mesmo endereço via htmx enquanto a execução está ativa
        return ["runs/_poll.html"] if self.request.htmx else ["runs/detail.html"]

    def render_to_response(self, context, **kwargs):
        response = super().render_to_response(context, **kwargs)
        # A execução parou (terminou ou espera aprovação): avisa a aba "Alterações" para se atualizar
        if self.request.htmx and not self.object.is_working:
            response["HX-Trigger"] = "run-settled"
        return response


class RunDiffView(LoginRequiredMixin, View):
    """Aba "Alterações" da execução: o diff do worktree dela contra a branch padrão, buscado pelo htmx."""

    # Até este número de arquivos, todos vêm abertos; acima, recolhidos, e a pessoa abre pelo índice
    EXPANDED_FILES = 10

    def get(self, request, pk):
        run = get_object_or_404(Run.objects.select_related("project"), pk=pk)
        if not request.htmx:
            return redirect(f"{run.get_absolute_url()}?aba=alteracoes")
        context = {"run": run, "can_follow_up": can_follow_up(run)}
        if not workspace_path(run).exists():
            # Antes de o worker preparar o repositório não há o que comparar; depois da limpeza, também não
            context["missing"] = "pending" if run.status == Run.Status.QUEUED and not run.started_at else "removed"
        else:
            try:
                files = parse_diff(workspace_diff(run), settings.RUN_DIFF_MAX_FILE_LINES)
            except WorkspaceError as exc:
                context["error"] = str(exc)
            else:
                context["files"] = files
                context["expanded"] = len(files) <= self.EXPANDED_FILES
                context["additions"] = sum(item.additions for item in files)
                context["deletions"] = sum(item.deletions for item in files)
        return render(request, "runs/_diff.html", context)


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


class RunFollowUpView(LoginRequiredMixin, View):
    """Pede um ajuste em uma execução concluída: o agente continua a mesma conversa, no mesmo worktree."""

    def post(self, request, pk):
        message = request.POST.get("message", "").strip()
        with transaction.atomic():
            run = get_object_or_404(Run.objects.select_for_update(), pk=pk)
            if not message:
                messages.error(request, "Escreva o ajuste antes de enviar.")
            elif not can_follow_up(run):
                messages.error(request, "Esta execução não pode receber ajustes.")
            else:
                run.follow_up = message
                run.status = Run.Status.QUEUED
                # O relógio recomeça: a duração mostrada passa a ser a deste ajuste, sem o tempo parado
                run.started_at = None
                run.finished_at = None
                run.cancel_requested = False
                run.save(update_fields=["follow_up", "status", "started_at", "finished_at", "cancel_requested"])
                run.events.create(kind=RunEvent.Kind.NOTE, content=message[:MAX_EVENT_LENGTH])
                enqueue(run, f"{request.user.get_username()} pediu um ajuste; a execução voltou para a fila.")
        return redirect(run)


class RunRetryView(LoginRequiredMixin, View):
    """Cria uma execução nova com a mesma tarefa, projeto e agente, começando do zero."""

    def post(self, request, pk):
        original = get_object_or_404(Run, pk=pk)
        run = Run.objects.create(
            project=original.project,
            agent=original.agent,
            prompt=original.prompt,
            autonomous=original.autonomous,
            created_by=request.user,
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
