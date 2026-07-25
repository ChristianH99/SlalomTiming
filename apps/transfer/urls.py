from django.urls import path

from . import views

app_name = "transfer"

urlpatterns = [
    path("", views.ExportView.as_view(), name="export"),
    path("import/", views.ImportView.as_view(), name="import"),
    path("import/review/", views.ImportReviewView.as_view(), name="review"),
    path("import/cancel/", views.cancel_import, name="cancel"),
    path("import/participants/sample/", views.csv_sample, name="csv-sample"),
]
