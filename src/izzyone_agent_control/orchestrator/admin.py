from django.contrib import admin

from .models import Agent, Project, Run, RunEvent


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["name", "repo_url", "default_branch"]
    search_fields = ["name", "repo_url"]


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "model", "is_active"]
    list_filter = ["provider", "is_active"]
    search_fields = ["name"]


class RunEventInline(admin.TabularInline):
    model = RunEvent
    extra = 0
    readonly_fields = ["kind", "content", "created_at"]
    can_delete = False


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ["pk", "project", "agent", "status", "created_at"]
    list_filter = ["status", "project", "agent"]
    inlines = [RunEventInline]
