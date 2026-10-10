from django.contrib import admin

from .models import Agent, Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["name", "repo_url", "default_branch"]
    search_fields = ["name", "repo_url"]


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "model", "is_active"]
    list_filter = ["provider", "is_active"]
    search_fields = ["name"]
