from django.contrib import admin

from .models import Agent, Approval, Project, Run, RunEvent, Schedule, Skill


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ["name", "repo_url", "default_branch"]
    search_fields = ["name", "repo_url"]


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ["name", "provider", "model", "is_active"]
    list_filter = ["provider", "is_active"]
    search_fields = ["name"]


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ["name", "description", "updated_at"]
    search_fields = ["name", "description"]


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


@admin.register(Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ["pk", "run", "tool_name", "status", "decided_by", "created_at"]
    list_filter = ["status", "tool_name"]


@admin.register(Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ["pk", "project", "agent", "frequency", "next_run_at", "is_active"]
    list_filter = ["frequency", "is_active", "project", "agent"]
