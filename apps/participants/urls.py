from django.urls import path

from . import views

app_name = "participants"

urlpatterns = [
    path("", views.ParticipantListView.as_view(), name="list"),
    path("add/", views.ParticipantCreateView.as_view(), name="add"),
    path("<int:pk>/edit/", views.ParticipantUpdateView.as_view(), name="edit"),
    path("<int:pk>/delete/", views.ParticipantDeleteView.as_view(), name="delete"),
]
