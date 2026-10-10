from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from portfolio.models import Portfolio, Trade
from trading.models import Stock, MomentumScore, TradingSignal
from trading.services.snaptrade_client import TradingExecutor


@override_settings(SNAPTRADE_CLIENT_ID="client", SNAPTRADE_CLIENT_SECRET="consumer")
class SignalWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="signal-trader", password="test", email="")
        self.client.force_login(self.user)
        self.portfolio = Portfolio.objects.create(name="Paper", initial_cash=1000, current_cash=1000,
            snaptrade_user_id="user", snaptrade_user_secret="private-secret", snaptrade_account_id="account")
        self.today = timezone.localdate()
        self.stock = Stock.objects.create(ticker="AAPL")
        self.bottom = Stock.objects.create(ticker="NVDA")
        for stock, quintile in [(self.stock, 1), (self.bottom, 5)]:
            MomentumScore.objects.create(stock=stock, calculation_date=self.today, momentum_score="0.25",
                rank=quintile, quintile=quintile, is_top_quintile=quintile == 1,
                period_start=self.today - timedelta(days=365), period_end=self.today - timedelta(days=30))
        sdk_patch = patch("trading.services.snaptrade_client.SnapTrade")
        self.sdk = sdk_patch.start().return_value
        self.addCleanup(sdk_patch.stop)
        self.sdk.account_information.get_user_account_details.return_value = SimpleNamespace(
            body={"id": "account", "institution_name": "Alpaca Paper"})
        self.snapshot()
        self.sdk.trading.get_user_account_quotes.return_value = SimpleNamespace(body=[{
            "symbol": {"id": "symbol", "raw_symbol": "AAPL", "currency": {"code": "USD"}},
            "ask_price": 100, "bid_price": 99, "last_trade_price": 100}])
        self.sdk.trading.place_force_order.return_value = SimpleNamespace(body={"brokerage_order_id": "broker"})
        self.generate_url = reverse("trading:generate_signals")

    def snapshot(self, cash=1000, aapl=0):
        rows = [{"instrument": {"kind": "stock", "raw_symbol": "NVDA", "currency": "USD"},
            "units": 2, "price": 100, "cost_basis": 90}]
        if aapl:
            rows.append({"instrument": {"kind": "stock", "raw_symbol": "AAPL", "currency": "USD"},
                "units": aapl, "price": 100, "cost_basis": 100})
        self.sdk.account_information.get_all_account_positions.return_value = SimpleNamespace(body={"results": rows})
        self.sdk.account_information.get_user_account_balance.return_value = SimpleNamespace(
            body=[{"currency": {"code": "USD"}, "cash": cash}])

    def generate(self):
        return self.client.post(self.generate_url, {"portfolio": self.portfolio.pk}, follow=True)

    def buy_signal(self):
        self.generate()
        return TradingSignal.objects.get(portfolio=self.portfolio, signal_type="BUY")

    def test_generation_syncs_portfolio_and_saves_recommendations_without_orders(self):
        response = self.generate()
        self.assertContains(response, "No orders were submitted")
        buy = TradingSignal.objects.get(signal_type="BUY")
        sell = TradingSignal.objects.get(signal_type="SELL")
        self.assertEqual(buy.target_value, Decimal("950"))
        self.assertEqual(sell.target_quantity, 2)
        self.assertEqual(sell.portfolio_id, self.portfolio.pk)
        self.assertContains(response, "Review and execute")
        self.assertFalse(Trade.objects.exists())
        self.sdk.trading.place_force_order.assert_not_called()
        self.generate()
        self.assertEqual(TradingSignal.objects.count(), 2)

    def test_generation_is_portfolio_specific(self):
        self.generate()
        other = Portfolio.objects.create(name="Other", initial_cash=1000, current_cash=1000,
            snaptrade_user_id="user", snaptrade_user_secret="private-secret", snaptrade_account_id="account")
        self.client.post(self.generate_url, {"portfolio": other.pk})
        self.assertEqual(TradingSignal.objects.count(), 4)
        response = self.client.get(reverse("trading:signals"), {"portfolio": self.portfolio.pk})
        self.assertEqual(len(response.context["signals"]), 2)

    def test_stale_rankings_and_missing_cash_do_not_generate_buys(self):
        MomentumScore.objects.update(calculation_date=self.today - timedelta(days=8))
        self.assertContains(self.generate(), "Refresh rankings first")
        self.sdk.account_information.get_user_account_details.assert_not_called()
        self.assertFalse(TradingSignal.objects.exists())
        MomentumScore.objects.update(calculation_date=self.today)
        self.snapshot(cash=0)
        self.generate()
        self.assertFalse(TradingSignal.objects.filter(signal_type="BUY").exists())
        self.assertTrue(TradingSignal.objects.filter(signal_type="SELL").exists())

    def test_generation_permissions_csrf_and_get_are_safe(self):
        self.assertContains(self.client.get(self.generate_url), "Generate signals")
        self.sdk.account_information.get_user_account_details.assert_not_called()
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(self.generate_url, {"portfolio": self.portfolio.pk}).status_code, 403)
        self.client.force_login(User.objects.create_user(username="viewer"))
        self.assertEqual(self.generate().status_code, 403)
        self.assertFalse(TradingSignal.objects.exists())

    def test_review_submit_links_order_and_refresh_marks_only_full_fill(self):
        signal = self.buy_signal()
        url = reverse("trading:execute_signal", args=[signal.pk])
        response = self.client.get(url)
        self.assertContains(response, "Buy budget: $950.00")
        self.sdk.trading.place_force_order.assert_not_called()
        response = self.client.post(url, {"portfolio": 999, "action": "SELL", "units": 2, "limit_price": "100"})
        trade = Trade.objects.get()
        self.assertEqual(trade.signal_id, signal.pk)
        self.assertEqual(trade.trade_type, "BUY")
        self.assertEqual(trade.portfolio_id, self.portfolio.pk)
        self.assertRedirects(response, reverse("trading:paper_trade", args=[trade.pk]))
        signal.refresh_from_db()
        self.assertFalse(signal.is_executed)
        executor = TradingExecutor()
        for status, quantity in [("PARTIAL", 1), ("EXECUTED", 2)]:
            self.snapshot(cash=1000 - quantity * 100, aapl=quantity)
            self.sdk.account_information.get_user_account_order_detail.return_value = SimpleNamespace(
                body={"status": status, "filled_quantity": quantity, "execution_price": 100})
            executor.update_trade_status(trade)
            signal.refresh_from_db()
            self.assertEqual(signal.is_executed, status == "EXECUTED")
        filled_at = signal.executed_at
        executor.update_trade_status(trade)
        signal.refresh_from_db()
        self.assertEqual(signal.executed_at, filled_at)

    def test_budget_and_duplicate_orders_are_blocked_even_after_cancellation(self):
        signal = self.buy_signal()
        url = reverse("trading:execute_signal", args=[signal.pk])
        response = self.client.post(url, {"units": 10, "limit_price": "100"})
        self.assertContains(response, "exceeds the signal")
        self.sdk.trading.place_force_order.assert_not_called()
        self.client.post(url, {"units": 1, "limit_price": "100"})
        trade = Trade.objects.get()
        trade.status = "CANCELLED"
        trade.save()
        response = self.client.post(url, {"units": 1, "limit_price": "100"})
        self.assertContains(response, "already has an order")
        self.assertEqual(self.sdk.trading.place_force_order.call_count, 1)
        self.assertEqual(Trade.objects.count(), 1)

    def test_unknown_submission_retains_signal_link_and_redacts_credentials(self):
        signal = self.buy_signal()
        self.sdk.trading.place_force_order.side_effect = TimeoutError("private-secret")
        response = self.client.post(reverse("trading:execute_signal", args=[signal.pk]),
            {"units": 1, "limit_price": "100"})
        self.assertContains(response, "unresolved order")
        self.assertNotContains(response, "private-secret")
        trade = Trade.objects.get()
        self.assertEqual(trade.signal_id, signal.pk)
        self.assertEqual(trade.status, "PENDING")

    def test_sell_quantity_cannot_be_changed(self):
        self.generate()
        signal = TradingSignal.objects.get(signal_type="SELL")
        self.sdk.trading.get_user_account_quotes.return_value.body[0]["symbol"]["raw_symbol"] = "NVDA"
        executor = TradingExecutor()
        with self.assertRaisesMessage(ValueError, "target shares"):
            executor.submit_paper_order(self.portfolio, self.bottom, "SELL", 1, Decimal("100"), signal=signal)
        self.sdk.trading.place_force_order.assert_not_called()
        response = self.client.post(reverse("trading:execute_signal", args=[signal.pk]),
            {"units": 1, "action": "BUY", "limit_price": "100"})
        self.assertEqual(response.status_code, 302)
        trade = Trade.objects.get()
        self.assertEqual(trade.quantity, 2)
        self.assertEqual(trade.trade_type, "SELL")
        self.assertEqual(trade.signal_id, signal.pk)

    def test_sync_failure_does_not_mark_linked_signal_executed(self):
        signal = self.buy_signal()
        executor = TradingExecutor()
        trade = executor.submit_paper_order(self.portfolio, self.stock, "BUY", 1, Decimal("100"), signal=signal)
        self.sdk.account_information.get_user_account_order_detail.return_value = SimpleNamespace(
            body={"status": "EXECUTED", "filled_quantity": 1, "execution_price": 100})
        self.sdk.account_information.get_user_account_balance.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            executor.update_trade_status(trade)
        trade.refresh_from_db()
        signal.refresh_from_db()
        self.assertEqual(trade.status, "SUBMITTED")
        self.assertFalse(signal.is_executed)

    def test_stale_signal_and_unauthorized_execution_do_not_submit(self):
        signal = self.buy_signal()
        signal.signal_date = self.today - timedelta(days=8)
        signal.save()
        url = reverse("trading:execute_signal", args=[signal.pk])
        response = self.client.post(url, {"units": 1, "limit_price": "100"})
        self.assertContains(response, "outside the last seven days")
        self.client.force_login(User.objects.create_user(username="non-trader"))
        self.assertEqual(self.client.post(url, {"units": 1, "limit_price": "100"}).status_code, 403)
        self.sdk.trading.place_force_order.assert_not_called()
        self.assertFalse(Trade.objects.exists())

    def test_generation_api_failure_is_sanitized_and_saves_no_recommendations(self):
        self.sdk.account_information.get_user_account_details.side_effect = ValueError("Bad private-secret https://example.com/signed")
        response = self.generate()
        self.assertContains(response, "Could not generate signals")
        self.assertNotContains(response, "private-secret")
        self.assertNotContains(response, "https://example.com")
        self.assertFalse(TradingSignal.objects.exists())
