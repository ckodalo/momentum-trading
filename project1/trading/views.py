from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django import forms
from django.views.generic import ListView
from django.views import View
from django.contrib import messages
from django.core.management import call_command
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from io import StringIO

from trading.models import MomentumScore, RebalanceEvent, TradingSignal


class RankingFilterForm(forms.Form):
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    quintile = forms.ChoiceField(required=False, choices=[("", "All quintiles")] + [(str(i), str(i)) for i in range(1, 6)])


class SignalFilterForm(forms.Form):
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    signal_type = forms.ChoiceField(required=False, choices=[("", "All signals")] + TradingSignal.SIGNAL_TYPES)
    executed = forms.ChoiceField(required=False, choices=[("", "Any execution status"), ("yes", "Executed"), ("no", "Unexecuted")])


class RebalanceFilterForm(forms.Form):
    status = forms.ChoiceField(required=False, choices=[("", "All statuses")] + list(RebalanceEvent._meta.get_field("execution_status").choices))


class FilteredListView(LoginRequiredMixin, PermissionRequiredMixin, ListView):
    paginate_by = 20

    def get_queryset(self):
        self.filter_form = self.form_class(self.request.GET)
        queryset = super().get_queryset()
        if not self.filter_form.is_valid():
            return queryset.none()
        return self.filter_queryset(queryset, self.filter_form.cleaned_data)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["filter_form"] = self.filter_form
        params = self.request.GET.copy()
        params.pop("page", None)
        context["filter_query"] = params.urlencode()
        return context


class MomentumRankingView(FilteredListView):
    model = MomentumScore
    permission_required = "trading.view_momentumscore"
    template_name = "trading/rankings.html"
    context_object_name = "scores"
    form_class = RankingFilterForm

    def filter_queryset(self, queryset, filters):
        self.calculation_date = filters["date"] or queryset.order_by("-calculation_date").values_list("calculation_date", flat=True).first()
        queryset = queryset.filter(calculation_date=self.calculation_date)
        if filters["quintile"]:
            queryset = queryset.filter(quintile=filters["quintile"])
        return queryset.select_related("stock").order_by("-momentum_score", "stock__ticker", "pk")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["calculation_date"] = getattr(self, "calculation_date", None)
        return context


class RefreshMomentumRankingsView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = (
        "trading.view_momentumscore", "trading.add_momentumscore",
        "trading.change_momentumscore", "trading.add_stock",
    )
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        calculation_date = timezone.localdate()
        output, errors = StringIO(), StringIO()
        try:
            call_command("pull_massive_data", date=calculation_date,
                         stdout=output, stderr=errors, no_color=True)
        except Exception:
            # API exceptions may include credentials or signed URLs.
            messages.error(request, "Could not refresh rankings. Check your Massive API key, plan access, network connection, and available price history. Saved rankings are still available.")
            return redirect("trading:rankings")
        messages.success(request, f"Rankings refreshed for {calculation_date} using the 50-stock universe.")
        if errors.getvalue():
            messages.warning(request, "Some stocks were skipped because price history was unavailable. Previously saved scores for skipped stocks remain unchanged.")
        return redirect(f"{reverse('trading:rankings')}?date={calculation_date}")


class TradingSignalListView(FilteredListView):
    model = TradingSignal
    permission_required = "trading.view_tradingsignal"
    template_name = "trading/signals.html"
    context_object_name = "signals"
    form_class = SignalFilterForm

    def filter_queryset(self, queryset, filters):
        if filters["date"]:
            queryset = queryset.filter(signal_date=filters["date"])
        if filters["signal_type"]:
            queryset = queryset.filter(signal_type=filters["signal_type"])
        if filters["executed"]:
            queryset = queryset.filter(is_executed=filters["executed"] == "yes")
        return queryset.select_related("stock").order_by("-signal_date", "-created_at", "-pk")


class RebalanceEventListView(FilteredListView):
    model = RebalanceEvent
    permission_required = "trading.view_rebalanceevent"
    template_name = "trading/rebalances.html"
    context_object_name = "events"
    form_class = RebalanceFilterForm

    def filter_queryset(self, queryset, filters):
        if filters["status"]:
            queryset = queryset.filter(execution_status=filters["status"])
        return queryset.order_by("-date", "-created_at", "-pk")
