from django.urls import path

from . import views

app_name = "participants"

urlpatterns = [
    path("", views.ParticipantListView.as_view(), name="list"),
    path("check/", views.participant_check, name="check"),
    path("set-bib/", views.participant_set_bib, name="set-bib"),
    path("set-draw/", views.participant_set_draw, name="set-draw"),
    path("set-dsq/", views.participant_set_dsq, name="set-dsq"),
    path("bib-assignment/", views.BibAssignmentView.as_view(), name="bib-assignment"),
    path("starter-list/", views.StarterListView.as_view(), name="starter-list"),
    path("add/", views.ParticipantCreateView.as_view(), name="add"),
    path("<int:pk>/edit/", views.ParticipantUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", views.ParticipantDeleteView.as_view(), name="delete"),
]
