from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views import View
from django.views.generic import DetailView, FormView

from portfolio.models import Portfolio, Trade
from trading.models import MomentumScore
from trading.services.snaptrade_client import TradingExecutor, get_trading_executor


class PaperOrderForm(forms.Form):
    portfolio = forms.ModelChoiceField(queryset=Portfolio.objects.none())
    action = forms.ChoiceField(choices=[("BUY", "Buy"), ("SELL", "Sell")])
    units = forms.IntegerField(label="Whole shares", min_value=1, max_value=1000000, initial=1)
    limit_price = forms.DecimalField(label="Limit price per share (USD)", min_value=Decimal("0.01"),
        max_digits=10, decimal_places=2, widget=forms.NumberInput(attrs={"step": "0.01"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["portfolio"].queryset = Portfolio.objects.filter(is_active=True).exclude(
            snaptrade_user_id="").exclude(snaptrade_account_id="").exclude(snaptrade_user_secret="")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("units") and cleaned.get("limit_price"):
            if cleaned["units"] * cleaned["limit_price"] > Decimal("9999999999999.99"):
                raise forms.ValidationError("The order value is too large.")
        return cleaned


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PaperOrderView(LoginRequiredMixin, PermissionRequiredMixin, FormView):
    permission_required = ("trading.view_momentumscore", "portfolio.view_portfolio",
                           "portfolio.view_trade", "portfolio.execute_paper_trade")
    form_class = PaperOrderForm
    template_name = "trading/paper_order.html"

    def get_score(self):
        if not hasattr(self, "score"):
            self.score = get_object_or_404(MomentumScore.objects.select_related("stock"), pk=self.kwargs["score_id"])
        return self.score

    def get_initial(self):
        initial = super().get_initial()
        initial["action"] = "SELL" if self.request.GET.get("action") == "SELL" else "BUY"
        portfolios = list(PaperOrderForm().fields["portfolio"].queryset[:2])
        selected_id = self.request.GET.get("portfolio")
        if selected_id:
            try:
                selected = PaperOrderForm().fields["portfolio"].queryset.filter(pk=int(selected_id)).first()
                if selected:
                    initial["portfolio"] = selected
                    return initial
            except (ValueError, TypeError):
                pass
        if len(portfolios) == 1:
            initial["portfolio"] = portfolios[0]
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["score"] = self.get_score()
        context["pending_trade"] = getattr(self, "pending_trade", None)
        return context

    def form_valid(self, form):
        stock = self.get_score().stock
        portfolio = form.cleaned_data["portfolio"]
        try:
            executor = get_trading_executor()
            # Verify the institution before trusting the selected account for execution.
            executor.verify_paper_account(portfolio)
            executor.sync_portfolio_positions(portfolio)
            trade = executor.submit_paper_order(portfolio, stock, form.cleaned_data["action"],
                form.cleaned_data["units"], form.cleaned_data["limit_price"])
        except Exception as exc:
            self.pending_trade = portfolio.trades.filter(
                status__in=["PENDING", "SUBMITTED", "PARTIALLY_FILLED"]).order_by("-pk").first()
            # The executor only returns sanitized local validation/submission messages.
            if type(exc) in (ValueError, RuntimeError):
                error = str(exc)
                for secret in (portfolio.snaptrade_user_secret, portfolio.snaptrade_user_id,
                               portfolio.snaptrade_account_id):
                    if secret:
                        error = error.replace(secret, "[redacted]")
            else:
                error = f"Paper order failed ({type(exc).__name__}). Check your account connection before retrying."
            form.add_error(None, error)
            return self.form_invalid(form)
        messages.success(self.request, "Paper order submitted. It has not yet been confirmed filled.")
        return redirect("trading:paper_trade", pk=trade.pk)


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PaperTradeDetailView(LoginRequiredMixin, PermissionRequiredMixin, DetailView):
    permission_required = ("portfolio.view_portfolio", "portfolio.view_trade")
    model = Trade
    template_name = "trading/paper_trade.html"
    context_object_name = "trade"

    def get_queryset(self):
        return Trade.objects.select_related("stock", "portfolio")


@method_decorator(never_cache, name="dispatch")
class PaperOrderQuoteView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = PaperOrderView.permission_required
    http_method_names = ["get"]

    def get(self, request, score_id):
        score = get_object_or_404(MomentumScore.objects.select_related("stock"), pk=score_id)
        field = PaperOrderForm().fields["portfolio"]
        try:
            portfolio = field.clean(request.GET.get("portfolio"))
        except forms.ValidationError:
            return JsonResponse({"error": "Choose an active, connected portfolio."}, status=400)

        def number(value, positive=False):
            try:
                result = Decimal(str(value))
                if not result.is_finite() or (positive and result <= 0):
                    return None
                return result
            except (InvalidOperation, ValueError, TypeError):
                return None

        try:
            executor = get_trading_executor()
            executor.verify_paper_account(portfolio)
            quote = executor._quote(portfolio, score.stock)
            credentials = executor._credentials(portfolio)
            balances = executor.snaptrade.account_information.get_user_account_balance(**credentials).body
            usd = [item for item in balances if (item.get("currency") or {}).get("code") == "USD"]
            cash = number(usd[0].get("cash")) if len(usd) == 1 else None
            positions = executor.snaptrade.account_information.get_all_account_positions(**credentials).body["results"]
            held = Decimal(0)
            for item in positions:
                instrument = item.get("instrument") or {}
                if instrument.get("raw_symbol") == score.stock.ticker:
                    amount = number(item.get("units"))
                    if amount is None:
                        held = None
                        break
                    held += amount
            ask = number(quote.get("ask_price"), positive=True)
            bid = number(quote.get("bid_price"), positive=True)
            last = number(quote.get("last_trade_price"), positive=True)
        except Exception:
            return JsonResponse({"error": "Could not load prices and account balances. Check the connection and try Refresh prices again."}, status=502)
        return JsonResponse({
            "ticker": score.stock.ticker, "portfolio_id": portfolio.pk,
            "ask": str(ask) if ask is not None else None,
            "bid": str(bid) if bid is not None else None,
            "last": str(last) if last is not None else None,
            "cash": str(cash) if cash is not None else None,
            "shares_held": str(held) if held is not None else None,
            "suggested_buy_limit": str(ask.quantize(Decimal("0.01"), rounding=ROUND_CEILING)) if ask is not None else None,
            "suggested_sell_limit": str(bid.quantize(Decimal("0.01"), rounding=ROUND_FLOOR)) if bid is not None else None,
            "retrieved_at": timezone.now().isoformat(),
        })


class RefreshPaperTradeView(LoginRequiredMixin, PermissionRequiredMixin, View):
    permission_required = ("portfolio.view_portfolio", "portfolio.view_trade", "portfolio.execute_paper_trade")
    http_method_names = ["post"]

    def post(self, request, pk):
        trade = get_object_or_404(Trade.objects.select_related("portfolio"), pk=pk)
        if not trade.external_order_id:
            messages.error(request, "This trade has no brokerage order ID. Check Alpaca Paper and reconcile the submission before retrying.")
        else:
            stage = "initializing the brokerage connection"
            try:
                executor = get_trading_executor()
                stage = "verifying the Alpaca Paper account"
                executor.verify_paper_account(trade.portfolio)
                stage = "reading the order status and syncing account holdings"
                executor.update_trade_status(trade)
                messages.success(request, f"Order status refreshed: {trade.get_status_display()}.")
            except Exception as exc:
                diagnostic = TradingExecutor.safe_api_error(exc, trade.portfolio)
                messages.error(request, f"Could not refresh the order while {stage}. {diagnostic}. No new order was submitted.")
        return redirect("trading:paper_trade", pk=trade.pk)
