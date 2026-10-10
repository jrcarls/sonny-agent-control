from django.contrib import admin
from django.urls import include, path

from orchestrator.views import HomeView

urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("", include("orchestrator.urls")),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("__reload__/", include("django_browser_reload.urls")),
]

from django.conf import settings

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [path("__debug__/", include(debug_toolbar.urls))] + urlpatterns
