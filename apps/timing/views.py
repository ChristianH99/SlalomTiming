from django.views.generic import ListView

from .models import TimingEvent


class DashboardView(ListView):
    model = TimingEvent
    context_object_name = "events"
    template_name = "timing/dashboard.html"
    paginate_by = 50
