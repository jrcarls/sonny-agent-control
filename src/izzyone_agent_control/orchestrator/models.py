from django.db import models
from django.urls import reverse


class Project(models.Model):
    name = models.CharField("nome", max_length=100, unique=True)
    # CharField e não URLField: aceita também endereços SSH (git@host:org/repo.git)
    repo_url = models.CharField("repositório", max_length=255)
    default_branch = models.CharField("branch padrão", max_length=100, default="main")
    description = models.TextField("descrição", blank=True)
    created_at = models.DateTimeField("criado em", auto_now_add=True)
    updated_at = models.DateTimeField("atualizado em", auto_now=True)

    class Meta:
        verbose_name = "projeto"
        verbose_name_plural = "projetos"
        ordering = ["name"]

    def __str__(self):
        return self.name

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
