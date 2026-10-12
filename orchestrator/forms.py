from django import forms
from django.utils import timezone

from .models import Agent, Credential, Project, Run, Schedule, Skill
from .tools import DEFAULT_TOOLS, TOOL_SPECS


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "repo_url", "default_branch", "description"]
        widgets = {
            "repo_url": forms.TextInput(attrs={"placeholder": "git@github.com:org/repo.git"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "repo_url": "Para repositório privado, use o endereço SSH (git@…) e cadastre nele a chave do projeto.",
        }


class SkillForm(forms.ModelForm):
    class Meta:
        model = Skill
        fields = ["name", "description", "content"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Ex.: Convenções do projeto"}),
            "description": forms.TextInput(attrs={"placeholder": "Ex.: padrões de código e de commit do time"}),
            "content": forms.Textarea(
                attrs={
                    "rows": 18,
                    "placeholder": (
                        "Escreva em Markdown o que o agente deve seguir. Ex.:\n\n"
                        "- Rode os testes antes de dizer que terminou.\n"
                        "- Mensagens de commit em português, no imperativo."
                    ),
                }
            ),
        }
        help_texts = {
            "description": "Aparece na lista e no cadastro do agente, para você escolher a skill certa.",
            "content": "Entra inteiro nas instruções de cada agente que tiver esta skill marcada.",
        }


class AgentForm(forms.ModelForm):
    tools = forms.MultipleChoiceField(
        label="Ferramentas",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=[(name, spec.label) for name, spec in TOOL_SPECS.items()],
        initial=DEFAULT_TOOLS,
        help_text="Sem ferramentas, o agente só responde em texto.",
    )
    skills = forms.ModelMultipleChoiceField(
        label="Skills",
        queryset=Skill.objects.all(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Instruções reutilizáveis somadas às do agente em toda execução.",
    )
    delegates = forms.ModelMultipleChoiceField(
        label="Pode delegar para",
        queryset=Agent.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Este agente pode passar tarefas a esses agentes e esperar a resposta deles.",
    )

    class Meta:
        model = Agent
        fields = [
            "name",
            "description",
            "provider",
            "model",
            "input_price",
            "output_price",
            "system_prompt",
            "skills",
            "tools",
            "delegates",
            "is_active",
        ]
        # Aceita o número como é escrito em português (3,50) e o mostra do mesmo jeito
        localized_fields = ["input_price", "output_price"]
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "Ex.: revisa pull requests"}),
            "model": forms.TextInput(attrs={"placeholder": "Escolha uma sugestão ou digite o identificador"}),
            "input_price": forms.TextInput(attrs={"placeholder": "Ex.: 3,00", "inputmode": "decimal"}),
            "output_price": forms.TextInput(attrs={"placeholder": "Ex.: 15,00", "inputmode": "decimal"}),
            "system_prompt": forms.Textarea(
                attrs={
                    "rows": 16,
                    "placeholder": (
                        "Ex.: Você revisa pull requests deste projeto. Aponte bugs e riscos antes de estilo, "
                        "cite o arquivo e a linha, e seja direto."
                    ),
                }
            ),
        }
        help_texts = {
            "description": "Os outros agentes leem isto para decidir o que delegar a ele.",
            "model": "Identificador do modelo no provedor escolhido.",
            "input_price": "Em dólares por milhão de tokens. Opcional: serve para estimar o custo na tela de uso.",
            "output_price": "Em dólares por milhão de tokens.",
            "is_active": "Agentes inativos não podem receber novas tarefas.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Um agente não delega para si mesmo
        self.fields["delegates"].queryset = Agent.objects.exclude(pk=self.instance.pk)
        if not self.fields["delegates"].queryset.exists():
            del self.fields["delegates"]
        # Sem skill cadastrada, a tela mostra o caminho para criar a primeira
        if not self.fields["skills"].queryset.exists():
            del self.fields["skills"]
        # Agente novo nasce ativo; desativar só faz sentido para um que já existe
        if self.instance.pk is None:
            del self.fields["is_active"]

    def tool_groups(self):
        """[(grupo, [(opção do campo, ToolSpec), ...]), ...] para a tela desenhar as ferramentas por grupo."""
        options = {option.data["value"]: option for option in self["tools"]}
        groups = {}
        for name, spec in TOOL_SPECS.items():
            groups.setdefault(spec.group, []).append((options[name], spec))
        return list(groups.items())


class CredentialForm(forms.Form):
    """Chave de uma integração. Não é um ModelForm: o valor digitado nunca volta para a tela."""

    secret = forms.CharField(label="Chave", max_length=500)

    def save(self, service, user):
        credential = Credential.objects.filter(service=service).first() or Credential(service=service)
        credential.set_secret(self.cleaned_data["secret"])
        credential.updated_by = user
        credential.save()
        return credential


class RunForm(forms.ModelForm):
    class Meta:
        model = Run
        fields = ["prompt", "project", "agent", "autonomous"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["project"].empty_label = "Projeto"
        self.fields["agent"].empty_label = "Agente"
        self.fields["agent"].queryset = Agent.objects.filter(is_active=True)
        # O modelo ao lado do nome ajuda a escolher entre agentes parecidos
        self.fields["agent"].label_from_instance = lambda agent: f"{agent.name} · {agent.model}"


class ScheduleForm(forms.ModelForm):
    class Meta:
        model = Schedule
        fields = ["prompt", "project", "agent", "starts_at", "frequency", "autonomous", "is_active"]
        widgets = {
            "prompt": forms.Textarea(attrs={"rows": 6, "placeholder": "Ex.: Rode os testes e corrija o que falhar"}),
        }
        help_texts = {
            "frequency": (
                "As repetições saem no mesmo horário da primeira execução: no mesmo dia da semana, se for "
                "toda semana, ou no mesmo dia do mês, se for todo mês."
            ),
            "autonomous": (
                "O agente roda comandos e abre o pull request sem pedir aprovação. Desmarcado, a execução "
                "para e espera alguém aprovar, e a rodada seguinte é pulada enquanto ela não terminar."
            ),
            "is_active": "Agendamentos inativos não criam execuções.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent"].queryset = Agent.objects.filter(is_active=True)
        self.fields["agent"].label_from_instance = lambda agent: f"{agent.name} · {agent.model}"
        # Agendamento novo nasce ativo; pausar só faz sentido para um que já existe
        if self.instance.pk is None:
            del self.fields["is_active"]

    def clean(self):
        data = super().clean()
        starts_at, once = data.get("starts_at"), data.get("frequency") == Schedule.Frequency.ONCE
        # Só cobra quando o horário foi mexido: um agendamento que já rodou ainda pode ser editado
        if starts_at and once and {"starts_at", "frequency"} & set(self.changed_data) and starts_at <= timezone.now():
            self.add_error("starts_at", "Esse horário já passou. Escolha um no futuro ou uma repetição.")
        return data

    def save(self, commit=True):
        self.instance.next_run_at = self.instance.next_occurrence(timezone.now())
        return super().save(commit)
