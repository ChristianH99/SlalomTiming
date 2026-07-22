from django.urls import path

from . import views

app_name = "results"

urlpatterns = [
    path("", views.ResultsIndexView.as_view(), name="index"),
    path("settings/", views.ResultsSettingsView.as_view(), name="settings"),
    path("tie/", views.ResultsTieResolveView.as_view(), name="tie-resolve"),
    # PDF export (export/ before <int:pk> so it isn't shadowed by the class route).
    path("export/all/", views.ResultsExportAllView.as_view(), name="export-all"),
    path("export/sample/", views.ResultsExportSampleView.as_view(), name="export-sample"),
    path("pdf-logo/remove/", views.ResultsPdfLogoRemoveView.as_view(), name="pdf-logo-remove"),
    path("export/overall/<str:method>/<int:runs>/",
         views.ResultsExportOverallView.as_view(), name="export-overall"),
    path("export/<int:pk>/", views.ResultsExportClassView.as_view(), name="export-class"),
    path("overall/<str:method>/<int:runs>/", views.ResultsOverallView.as_view(), name="overall"),
    path("<int:pk>/", views.ResultsClassView.as_view(), name="class"),
]
