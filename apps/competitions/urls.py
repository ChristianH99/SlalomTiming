from django.urls import path

from . import views

app_name = "competitions"

urlpatterns = [
    path("", views.CompetitionListView.as_view(), name="list"),
    path("add/", views.CompetitionCreateView.as_view(), name="add"),
    path("<int:pk>/edit/", views.CompetitionEditView.as_view(), name="edit"),
    path("<int:pk>/delete/", views.CompetitionDeleteView.as_view(), name="delete"),
    path("<int:pk>/select/", views.select_competition, name="select"),
    path("<int:pk>/duplicate/", views.duplicate_competition, name="duplicate"),
    path("types/", views.CompetitionTypeListView.as_view(), name="type-list"),
    path("types/add/", views.CompetitionTypeCreateView.as_view(), name="type-add"),
    path("types/<int:pk>/delete/", views.delete_competition_type, name="type-delete"),
]
