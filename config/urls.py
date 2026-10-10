from django.contrib import admin
from django.urls import include, path

from config.views import home

urlpatterns = [
    path("", home, name="home"),
    path("", include("izzyone_agent_control.orchestrator.urls")),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("__reload__/", include("django_browser_reload.urls")),
]

from django.conf import settings

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
