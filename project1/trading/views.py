from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django import forms
from django.views.generic import ListView
from django.views import View
from django.contrib import messages
from django.core.management import call_command
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.db.models import Count
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.utils.decorators import method_decorator
from io import StringIO

from trading.models import MomentumScore, RebalanceEvent, TradingSignal
from portfolio.models import Portfolio


class RankingFilterForm(forms.Form):
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    quintile = forms.ChoiceField(required=False, choices=[("", "All quintiles")] + [(str(i), str(i)) for i in range(1, 6)])


class SignalFilterForm(forms.Form):
    portfolio = forms.ModelChoiceField(queryset=Portfolio.objects.all(), required=False)
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    signal_type = forms.ChoiceField(required=False, choices=[("", "All signals")] + TradingSignal.SIGNAL_TYPES)
    executed = forms.ChoiceField(required=False, choices=[("", "Any execution status"), ("yes", "Executed"), ("no", "Unexecuted")])


class RebalanceFilterForm(forms.Form):
    status = forms.ChoiceField(required=False, choices=[("", "All statuses")] + list(RebalanceEvent._meta.get_field("execution_status").choices))


@method_decorator(xframe_options_sameorigin, name="dispatch")
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
    paginate_by = 100
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
        context["ranked_count"] = MomentumScore.objects.filter(calculation_date=context["calculation_date"]).count()
        context["quintile_counts"] = MomentumScore.objects.filter(calculation_date=context["calculation_date"]).values("quintile").annotate(total=Count("pk")).order_by("quintile")
        user = self.request.user
        if user.has_perm("portfolio.view_portfolio"):
            portfolios = Portfolio.objects.filter(is_active=True)
            context["workspace_portfolios"] = portfolios
            selected_id = self.request.GET.get("portfolio")
            selected = None
            if "portfolio" in self.request.GET:
                try:
                    selected = portfolios.filter(pk=int(selected_id)).first()
                except (ValueError, TypeError):
                    pass
            else:
                selected = portfolios.first()
            context["selected_portfolio"] = selected
            if selected:
                if user.has_perm("trading.view_tradingsignal"):
                    context["workspace_signals"] = selected.signals.select_related("stock").prefetch_related("orders").order_by("-signal_date", "-pk")[:20]
                if user.has_perm("portfolio.view_trade"):
                    context["workspace_orders"] = selected.trades.select_related("stock").all()[:20]
                    context["outstanding_count"] = selected.trades.filter(status__in=["PENDING", "SUBMITTED", "PARTIALLY_FILLED"]).count()
                if user.has_perm("portfolio.view_position"):
                    context["workspace_positions"] = selected.get_current_positions()
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
        destination = f"{reverse('trading:rankings')}?date={calculation_date}"
        if request.user.has_perm("portfolio.view_portfolio"):
            try:
                selected = Portfolio.objects.filter(pk=int(request.POST.get("portfolio", "")), is_active=True).first()
                if selected:
                    destination += f"&portfolio={selected.pk}"
            except (ValueError, TypeError):
                pass
        return redirect(destination)


class TradingSignalListView(FilteredListView):
    model = TradingSignal
    permission_required = "trading.view_tradingsignal"
    template_name = "trading/signals.html"
    context_object_name = "signals"
    form_class = SignalFilterForm

    def filter_queryset(self, queryset, filters):
        if filters.get("portfolio"):
            queryset = queryset.filter(portfolio=filters["portfolio"])
        if filters["date"]:
            queryset = queryset.filter(signal_date=filters["date"])
        if filters["signal_type"]:
            queryset = queryset.filter(signal_type=filters["signal_type"])
        if filters["executed"]:
            queryset = queryset.filter(is_executed=filters["executed"] == "yes")
        return queryset.select_related("stock", "portfolio").prefetch_related("orders").order_by("-signal_date", "-created_at", "-pk")


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
