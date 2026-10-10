from django import forms

from .models import Agent, Project, Run
from .tools import DEFAULT_TOOLS, TOOL_SPECS


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "repo_url", "default_branch", "description"]
        widgets = {
            "repo_url": forms.TextInput(attrs={"placeholder": "https://github.com/org/repo.git"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }


class AgentForm(forms.ModelForm):
    tools = forms.MultipleChoiceField(
        label="Ferramentas",
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=[
            (name, f"{spec.label}: {spec.description}" + (" Pede aprovação a cada uso." if spec.requires_approval else ""))
            for name, spec in TOOL_SPECS.items()
        ],
        initial=DEFAULT_TOOLS,
        help_text="Sem ferramentas, o agente só responde em texto.",
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
        fields = ["name", "description", "provider", "model", "system_prompt", "tools", "delegates", "is_active"]
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "Ex.: revisa pull requests"}),
            "system_prompt": forms.Textarea(attrs={"rows": 8}),
        }
        help_texts = {
            "description": "Os outros agentes leem isto para decidir o que delegar a ele.",
            "model": "Identificador do modelo no provedor escolhido.",
            "is_active": "Agentes inativos não podem receber novas tarefas.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Um agente não delega para si mesmo
        self.fields["delegates"].queryset = Agent.objects.exclude(pk=self.instance.pk)
        if not self.fields["delegates"].queryset.exists():
            del self.fields["delegates"]


class RunForm(forms.ModelForm):
    class Meta:
        model = Run
        fields = ["prompt", "project", "agent"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent"].queryset = Agent.objects.filter(is_active=True)
