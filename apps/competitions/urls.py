from django.urls import path

from . import views

app_name = "competitions"

urlpatterns = [
    path("", views.CompetitionListView.as_view(), name="list"),
    path("add/", views.CompetitionCreateView.as_view(), name="add"),
    path("general/", views.GeneralView.as_view(), name="general"),
    path("classes/", views.ClassesView.as_view(), name="classes"),
    path("run-order/", views.RunOrderView.as_view(), name="runorder"),
    path("<int:pk>/delete/", views.CompetitionDeleteView.as_view(), name="delete"),
    path("<int:pk>/select/", views.select_competition, name="select"),
    path("<int:pk>/duplicate/", views.duplicate_competition, name="duplicate"),
    path("types/", views.CompetitionTypeListView.as_view(), name="type-list"),
    path("types/add/", views.CompetitionTypeCreateView.as_view(), name="type-add"),
    path("types/<int:pk>/delete/", views.delete_competition_type, name="type-delete"),
]
