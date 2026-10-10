from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import FormView

from trading.models import MomentumScore, TradingSignal
from trading.trade_views import PaperOrderForm, PaperOrderView
from trading.services.strategy_engine import get_strategy_engine
from trading.services.snaptrade_client import TradingExecutor, get_trading_executor


class GenerateSignalsForm(forms.Form):
    portfolio = forms.ModelChoiceField(queryset=PaperOrderForm().fields["portfolio"].queryset)


class GenerateSignalsView(LoginRequiredMixin, PermissionRequiredMixin, FormView):
    permission_required = ("trading.view_tradingsignal", "trading.add_tradingsignal",
        "trading.view_momentumscore", "portfolio.view_portfolio", "portfolio.change_portfolio")
    template_name = "trading/generate_signals.html"
    form_class = GenerateSignalsForm

    def form_valid(self, form):
        portfolio = form.cleaned_data["portfolio"]
        calculation_date = MomentumScore.objects.order_by("-calculation_date").values_list(
            "calculation_date", flat=True).first()
        today = timezone.localdate()
        if not calculation_date or not today - timedelta(days=7) <= calculation_date <= today:
            form.add_error(None, "Refresh rankings first. Signal generation requires rankings from the last seven days.")
            return self.form_invalid(form)
        try:
            strategy = get_strategy_engine(portfolio)
            strategy.trading_executor.verify_paper_account(portfolio)
            strategy.trading_executor.sync_portfolio_positions(portfolio)
            # Serialize generation for this portfolio; repeated requests reuse saved signals.
            with transaction.atomic():
                from portfolio.models import Portfolio
                strategy.portfolio = Portfolio.objects.select_for_update().get(pk=portfolio.pk)
                buys, sells = strategy.generate_trading_signals(calculation_date)
        except Exception as exc:
            form.add_error(None, "Could not generate signals. " + TradingExecutor.safe_api_error(exc, portfolio))
            return self.form_invalid(form)
        messages.success(self.request, f"Recommendations ready: {len(buys)} buys and {len(sells)} sells. No orders were submitted.")
        return redirect(f"{reverse('trading:signals')}?portfolio={portfolio.pk}&date={calculation_date}")


class SignalOrderView(PaperOrderView):
    permission_required = PaperOrderView.permission_required + ("trading.view_tradingsignal",)

    def get_signal(self):
        if not hasattr(self, "signal"):
            self.signal = get_object_or_404(TradingSignal.objects.select_related("portfolio", "stock", "momentum_score"),
                pk=self.kwargs["pk"], portfolio__is_active=True, momentum_score__isnull=False,
                signal_type__in=["BUY", "SELL"])
        return self.signal

    def get_score(self):
        return self.get_signal().momentum_score

    def get_initial(self):
        signal = self.get_signal()
        return {"portfolio": signal.portfolio_id, "action": signal.signal_type,
            "units": signal.target_quantity if signal.signal_type == "SELL" else 1}

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        # The portfolio and direction are fixed by the reviewed recommendation.
        form.fields["portfolio"].disabled = True
        form.fields["action"].disabled = True
        if self.get_signal().signal_type == "SELL":
            form.fields["units"].disabled = True
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["signal"] = self.get_signal()
        context["signal_order"] = self.get_signal().orders.first()
        return context

    def form_valid(self, form):
        signal = self.get_signal()
        portfolio = signal.portfolio
        try:
            today = timezone.localdate()
            if not today - timedelta(days=7) <= signal.signal_date <= today:
                raise ValueError("This signal's date is outside the last seven days. Generate fresh recommendations before trading")
            executor = get_trading_executor()
            executor.verify_paper_account(portfolio)
            executor.sync_portfolio_positions(portfolio)
            trade = executor.submit_paper_order(portfolio, signal.stock, signal.signal_type,
                form.cleaned_data["units"], form.cleaned_data["limit_price"], signal=signal)
        except Exception as exc:
            self.pending_trade = portfolio.trades.filter(status__in=["PENDING", "SUBMITTED", "PARTIALLY_FILLED"]).first()
            form.add_error(None, TradingExecutor.safe_api_error(exc, portfolio))
            return self.form_invalid(form)
        messages.success(self.request, "Signal order submitted. The signal becomes executed only after a confirmed full fill.")
        return redirect("trading:paper_trade", pk=trade.pk)
