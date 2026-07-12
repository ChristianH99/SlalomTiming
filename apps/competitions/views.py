from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DeleteView, ListView

from .forms import CompetitionClassFormSet, CompetitionForm, CompetitionTypeForm
from .models import Competition, CompetitionClass, CompetitionType


class CompetitionListView(ListView):
    model = Competition
    context_object_name = "competitions"
    template_name = "competitions/competition_list.html"


class CompetitionCreateView(CreateView):
    model = Competition
    form_class = CompetitionForm
    template_name = "competitions/competition_form.html"

    def get_success_url(self):
        return reverse_lazy("competitions:edit", kwargs={"pk": self.object.pk})


class CompetitionEditView(View):
    template_name = "competitions/competition_form.html"

    def get(self, request, pk):
        competition = get_object_or_404(Competition, pk=pk)
        form = CompetitionForm(instance=competition)
        formset = CompetitionClassFormSet(queryset=competition.classes.order_by("code"))
        return render(request, self.template_name, {
            "object": competition, "form": form, "formset": formset,
        })

    def post(self, request, pk):
        competition = get_object_or_404(Competition, pk=pk)
        form = CompetitionForm(request.POST, instance=competition)
        formset = CompetitionClassFormSet(request.POST, queryset=competition.classes.order_by("code"))
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            return redirect("competitions:list")
        return render(request, self.template_name, {
            "object": competition, "form": form, "formset": formset,
        })


class CompetitionDeleteView(DeleteView):
    model = Competition
    template_name = "competitions/competition_confirm_delete.html"
    success_url = reverse_lazy("competitions:list")


@require_POST
def select_competition(request, pk):
    competition = get_object_or_404(Competition, pk=pk)
    with transaction.atomic():
        Competition.objects.exclude(pk=pk).update(is_active=False)
        competition.is_active = True
        competition.save(update_fields=["is_active"])
    return redirect("competitions:list")


@require_POST
def duplicate_competition(request, pk):
    original = get_object_or_404(Competition, pk=pk)
    with transaction.atomic():
        copy = Competition.objects.create(
            competition_type=original.competition_type,
            name=f"{original.name} (Copy)",
            date=original.date,
        )
        for original_class in original.classes.all():
            CompetitionClass.objects.filter(competition=copy, code=original_class.code).update(
                is_running=original_class.is_running,
                age_from=original_class.age_from,
                age_to=original_class.age_to,
            )
    return redirect("competitions:edit", pk=copy.pk)


class CompetitionTypeListView(ListView):
    model = CompetitionType
    context_object_name = "competition_types"
    template_name = "competitions/competitiontype_list.html"


class CompetitionTypeCreateView(CreateView):
    model = CompetitionType
    form_class = CompetitionTypeForm
    template_name = "competitions/competitiontype_form.html"
    success_url = reverse_lazy("competitions:type-list")
