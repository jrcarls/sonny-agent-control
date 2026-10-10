from django.urls import path

from . import views

app_name = "orchestrator"

urlpatterns = [
    path("sidebar/", views.SidebarView.as_view(), name="sidebar"),
    path("projetos/", views.ProjectListView.as_view(), name="project_list"),
    path("projetos/novo/", views.ProjectCreateView.as_view(), name="project_create"),
    path("projetos/<int:pk>/", views.ProjectUpdateView.as_view(), name="project_update"),
    path("projetos/<int:pk>/excluir/", views.ProjectDeleteView.as_view(), name="project_delete"),
    path("agentes/", views.AgentListView.as_view(), name="agent_list"),
    path("agentes/novo/", views.AgentCreateView.as_view(), name="agent_create"),
    path("agentes/<int:pk>/", views.AgentUpdateView.as_view(), name="agent_update"),
    path("agentes/<int:pk>/excluir/", views.AgentDeleteView.as_view(), name="agent_delete"),
    path("execucoes/", views.RunListView.as_view(), name="run_list"),
    path("execucoes/<int:pk>/", views.RunDetailView.as_view(), name="run_detail"),
    path("execucoes/<int:pk>/cancelar/", views.RunCancelView.as_view(), name="run_cancel"),
    path("execucoes/<int:pk>/continuar/", views.RunResumeView.as_view(), name="run_resume"),
    path("execucoes/<int:pk>/repetir/", views.RunRetryView.as_view(), name="run_retry"),
    path("aprovacoes/", views.ApprovalListView.as_view(), name="approval_list"),
    path("aprovacoes/<int:pk>/decidir/", views.ApprovalDecideView.as_view(), name="approval_decide"),
]
