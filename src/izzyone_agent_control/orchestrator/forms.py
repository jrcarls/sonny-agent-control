from django import forms

from .models import Agent, Project, Run


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "repo_url", "default_branch", "description"]
        widgets = {
            "repo_url": forms.TextInput(attrs={"placeholder": "https://github.com/org/repo.git"}),
            "description": forms.Textarea(attrs={"rows": 3}),
        }


class AgentForm(forms.ModelForm):
    class Meta:
        model = Agent
        fields = ["name", "description", "provider", "model", "system_prompt", "is_active"]
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "Ex.: revisa pull requests"}),
            "system_prompt": forms.Textarea(attrs={"rows": 8}),
        }
        help_texts = {
            "model": "Identificador do modelo no provedor escolhido.",
            "is_active": "Agentes inativos não podem receber novas tarefas.",
        }


class RunForm(forms.ModelForm):
    class Meta:
        model = Run
        fields = ["prompt", "project", "agent"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["agent"].queryset = Agent.objects.filter(is_active=True)
