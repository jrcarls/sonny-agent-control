from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from .crypto import encrypt, generate_ssh_keypair
from .tools import TOOL_SPECS, github_repository


class Project(models.Model):
    name = models.CharField("nome", max_length=100, unique=True)
    # CharField e não URLField: aceita também endereços SSH (git@host:org/repo.git)
    repo_url = models.CharField("repositório", max_length=255)
    default_branch = models.CharField("branch padrão", max_length=100, default="main")
    description = models.TextField("descrição", blank=True)
    # Par de chaves SSH do projeto. A pública é cadastrada no repositório (deploy key no GitHub);
    # a privada fica criptografada e só é usada pelo git ao clonar e enviar.
    ssh_private_key = models.TextField(blank=True, editable=False)
    ssh_public_key = models.TextField("chave pública", blank=True, editable=False)
    created_at = models.DateTimeField("criado em", auto_now_add=True)
    updated_at = models.DateTimeField("atualizado em", auto_now=True)

    class Meta:
        verbose_name = "projeto"
        verbose_name_plural = "projetos"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, update_fields=None, **kwargs):
        # Todo projeto tem uma chave: é gerada na criação e também para os que vieram de antes dela existir
        if not self.ssh_private_key:
            self.generate_ssh_key()
            if update_fields is not None:
                update_fields = {*update_fields, "ssh_private_key", "ssh_public_key"}
        super().save(*args, update_fields=update_fields, **kwargs)

    def generate_ssh_key(self):
        """Troca o par de chaves (sem salvar). A chave anterior deixa de valer para este projeto."""
        private, self.ssh_public_key = generate_ssh_keypair(f"izzyone-{self.name}".replace(" ", "-"))
        self.ssh_private_key = encrypt(private)

    @property
    def uses_ssh(self):
        """Só endereços SSH usam a chave do projeto; https e caminhos locais não."""
        return self.repo_url.startswith(("git@", "ssh://"))

    def get_absolute_url(self):
        return reverse("orchestrator:project_update", args=[self.pk])


class Agent(models.Model):
    class Provider(models.TextChoices):
        ANTHROPIC = "anthropic", "Anthropic"
        OPENAI = "openai", "OpenAI"
        GOOGLE = "google", "Google"
        OLLAMA = "ollama", "Ollama"

    name = models.CharField("nome", max_length=100, unique=True)
    description = models.CharField("papel", max_length=255, blank=True)
    provider = models.CharField("provedor", max_length=20, choices=Provider)
    model = models.CharField("modelo", max_length=100)
    system_prompt = models.TextField("instruções", blank=True)
    # Nomes das ferramentas habilitadas (chaves de tools.TOOL_SPECS)
    tools = models.JSONField("ferramentas", default=list, blank=True)
    delegates = models.ManyToManyField(
        "self", verbose_name="pode delegar para", symmetrical=False, blank=True, related_name="delegated_by"
    )
    is_active = models.BooleanField("ativo", default=True)
    created_at = models.DateTimeField("criado em", auto_now_add=True)
    updated_at = models.DateTimeField("atualizado em", auto_now=True)

    class Meta:
        verbose_name = "agente"
        verbose_name_plural = "agentes"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("orchestrator:agent_update", args=[self.pk])


class Run(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Na fila"
        RUNNING = "running", "Em andamento"
        WAITING = "waiting_approval", "Aguardando aprovação"
        SUCCEEDED = "succeeded", "Concluída"
        FAILED = "failed", "Falhou"
        CANCELED = "canceled", "Cancelada"

    ACTIVE_STATUSES = [Status.QUEUED, Status.RUNNING, Status.WAITING]

    project = models.ForeignKey(Project, verbose_name="projeto", on_delete=models.PROTECT, related_name="runs")
    agent = models.ForeignKey(Agent, verbose_name="agente", on_delete=models.PROTECT, related_name="runs")
    prompt = models.TextField("tarefa")
    status = models.CharField("situação", max_length=20, choices=Status, default=Status.QUEUED)
    result = models.TextField("resultado", blank=True)
    pull_request_url = models.URLField("pull request", blank=True)
    # Última vez que a branch da execução foi enviada ao repositório remoto
    pushed_at = models.DateTimeField("enviada em", null=True, blank=True)
    # Pedido de cancelamento de uma execução em andamento; o worker para no fim do passo atual
    cancel_requested = models.BooleanField(default=False)
    # Sinal de vida: o worker atualiza este horário enquanto trabalha na execução. Se ele para de
    # atualizar, é porque o processo morreu, e a execução é dada como falha (tasks.fail_stale_runs).
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    # Nome do worker que está (ou esteve por último) com a execução
    worker = models.CharField(max_length=255, blank=True)
    input_tokens = models.PositiveBigIntegerField("tokens de entrada", default=0)
    output_tokens = models.PositiveBigIntegerField("tokens de saída", default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="criada por", on_delete=models.PROTECT, related_name="runs"
    )
    created_at = models.DateTimeField("criada em", auto_now_add=True)
    started_at = models.DateTimeField("iniciada em", null=True, blank=True)
    finished_at = models.DateTimeField("finalizada em", null=True, blank=True)

    class Meta:
        verbose_name = "execução"
        verbose_name_plural = "execuções"
        ordering = ["-created_at"]

    def __str__(self):
        return f"Execução #{self.pk}"

    def get_absolute_url(self):
        return reverse("orchestrator:run_detail", args=[self.pk])

    @property
    def is_working(self):
        """Na fila ou rodando: a tela da execução se atualiza sozinha enquanto isso."""
        return self.status in [self.Status.QUEUED, self.Status.RUNNING]

    @property
    def branch(self):
        return f"izzyone/run-{self.pk}"

    @property
    def compare_url(self):
        """Página do GitHub que abre um pull request da branch enviada; vazio se não há o que abrir."""
        repository = github_repository(self.project.repo_url)
        if not (self.pushed_at and repository):
            return ""
        return f"https://github.com/{repository}/compare/{self.project.default_branch}...{self.branch}?expand=1"

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    def mark_canceled(self):
        """Encerra a execução como cancelada; os pedidos de aprovação que esperavam resposta caem junto."""
        self.status = self.Status.CANCELED
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "finished_at"])
        self.approvals.filter(status=Approval.Status.PENDING).update(status=Approval.Status.CANCELED)
        self.events.create(kind=RunEvent.Kind.STATUS, content="Execução cancelada.")


class RunEvent(models.Model):
    class Kind(models.TextChoices):
        STATUS = "status", "Situação"
        MESSAGE = "message", "Mensagem"
        TOOL_CALL = "tool_call", "Ferramenta"
        TOOL_RESULT = "tool_result", "Resultado da ferramenta"
        DELEGATION = "delegation", "Delegação"
        ERROR = "error", "Erro"

    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="events")
    # Agente que gerou o evento; vazio nos eventos da própria execução (fila, conclusão, erro)
    agent = models.ForeignKey(Agent, on_delete=models.PROTECT, null=True, blank=True, related_name="events")
    kind = models.CharField("tipo", max_length=20, choices=Kind)
    content = models.TextField("conteúdo")
    created_at = models.DateTimeField("criado em", auto_now_add=True)

    class Meta:
        verbose_name = "evento"
        verbose_name_plural = "eventos"
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.content[:50]}"


class Approval(models.Model):
    """Pedido de autorização para uma chamada de ferramenta sensível feita por um agente."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pendente"
        APPROVED = "approved", "Aprovada"
        REJECTED = "rejected", "Negada"
        CANCELED = "canceled", "Cancelada"

    run = models.ForeignKey(Run, verbose_name="execução", on_delete=models.CASCADE, related_name="approvals")
    agent = models.ForeignKey(
        Agent, verbose_name="pedido por", on_delete=models.PROTECT, null=True, blank=True, related_name="approvals"
    )
    tool_call_id = models.CharField(max_length=255)
    tool_name = models.CharField("ferramenta", max_length=100)
    tool_args = models.JSONField("argumentos", default=dict)
    status = models.CharField("situação", max_length=20, choices=Status, default=Status.PENDING)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="decidida por",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approvals",
    )
    decided_at = models.DateTimeField("decidida em", null=True, blank=True)
    created_at = models.DateTimeField("criada em", auto_now_add=True)

    class Meta:
        verbose_name = "aprovação"
        verbose_name_plural = "aprovações"
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["run", "tool_call_id"], name="unique_approval_per_tool_call")]

    def __str__(self):
        return f"{self.tool_label} ({self.run})"

    @property
    def tool_label(self):
        spec = TOOL_SPECS.get(self.tool_name)
        return spec.label if spec else self.tool_name
