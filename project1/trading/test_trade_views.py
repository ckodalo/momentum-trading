from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from portfolio.models import Portfolio, Trade
from trading.models import Stock, MomentumScore


class PaperTradeViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="trader", email="", password="test")
        self.portfolio = Portfolio.objects.create(name="Paper", initial_cash=1000, current_cash=1000,
            snaptrade_user_id="user", snaptrade_user_secret="private-secret", snaptrade_account_id="account")
        self.stock = Stock.objects.create(ticker="AAPL")
        self.score = MomentumScore.objects.create(stock=self.stock, calculation_date=date(2026, 1, 1),
            momentum_score="0.25", quintile=1, period_start=date(2025, 1, 1), period_end=date(2025, 12, 1))
        self.url = reverse("trading:paper_order", args=[self.score.pk])
        self.data = {"portfolio": self.portfolio.pk, "action": "BUY", "units": 1, "limit_price": "100.00"}

    def login(self):
        self.client.force_login(self.user)

    def create_trade(self):
        return Trade.objects.create(portfolio=self.portfolio, stock=self.stock, trade_type="BUY",
            quantity=1, price=100, status="SUBMITTED", external_order_id="broker-id")

    @patch("trading.trade_views.get_trading_executor")
    def test_unauthorized_users_cannot_submit(self, executor):
        self.assertEqual(self.client.post(self.url, self.data).status_code, 302)
        viewer = User.objects.create_user(username="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(self.url, self.data).status_code, 403)
        executor.assert_not_called()

    @patch("trading.trade_views.get_trading_executor")
    def test_get_order_form_only_reads_saved_data(self, executor):
        self.login()
        response = self.client.get(self.url, {"action": "SELL"})
        self.assertContains(response, "Trade AAPL")
        self.assertEqual(response.context["form"].initial["action"], "SELL")
        self.assertNotContains(response, "private-secret")
        self.assertContains(self.client.get(reverse("trading:rankings")), "?action=BUY")
        self.assertContains(self.client.get(reverse("trading:rankings")), "?action=SELL")
        executor.assert_not_called()

    @patch("trading.trade_views.get_trading_executor")
    def test_csrf_required_to_submit_and_refresh(self, executor):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, self.data).status_code, 403)
        trade = self.create_trade()
        self.assertEqual(client.post(reverse("trading:refresh_paper_trade", args=[trade.pk])).status_code, 403)
        executor.assert_not_called()

    @patch("trading.trade_views.get_trading_executor")
    def test_buy_and_sell_submit_selected_stock_and_redirect_to_order(self, factory):
        self.login()
        trade = self.create_trade()
        factory.return_value.submit_paper_order.return_value = trade
        for action in ["BUY", "SELL"]:
            response = self.client.post(self.url, {**self.data, "action": action, "stock": "MSFT"})
            self.assertRedirects(response, reverse("trading:paper_trade", args=[trade.pk]))
            factory.return_value.verify_paper_account.assert_called_with(self.portfolio)
            factory.return_value.sync_portfolio_positions.assert_called_with(self.portfolio)
            factory.return_value.submit_paper_order.assert_called_with(
                self.portfolio, self.stock, action, 1, Decimal("100.00"))

    @patch("trading.trade_views.get_trading_executor")
    def test_invalid_form_never_calls_brokerage(self, factory):
        self.login()
        for changes in [{"units": "0.5"}, {"units": 0}, {"limit_price": "0"},
                        {"limit_price": "1.001"}, {"action": "SHORT"}, {"portfolio": 9999}]:
            response = self.client.post(self.url, {**self.data, **changes})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.portfolio.is_active = False
        self.portfolio.save()
        response = self.client.post(self.url, self.data)
        self.assertTrue(response.context["form"].errors)
        factory.assert_not_called()

    @patch("trading.trade_views.get_trading_executor")
    def test_execution_error_does_not_claim_submission_and_links_pending_order(self, factory):
        self.login()
        trade = self.create_trade()
        factory.return_value.submit_paper_order.side_effect = ValueError("Refresh or reconcile the outstanding portfolio order")
        response = self.client.post(self.url, self.data)
        self.assertContains(response, "outstanding portfolio order")
        self.assertContains(response, reverse("trading:paper_trade", args=[trade.pk]))
        self.assertEqual(response.status_code, 200)

    @patch("trading.trade_views.get_trading_executor")
    def test_unexpected_api_errors_hide_sensitive_details(self, factory):
        self.login()
        factory.return_value.verify_paper_account.side_effect = Exception("private-secret signed URL")
        response = self.client.post(self.url, self.data)
        self.assertContains(response, "Paper order failed")
        self.assertNotContains(response, "private-secret")
        factory.return_value.submit_paper_order.assert_not_called()

    @patch("trading.trade_views.get_trading_executor")
    def test_order_detail_get_never_calls_api_and_refresh_only_checks_status(self, factory):
        self.login()
        trade = self.create_trade()
        url = reverse("trading:paper_trade", args=[trade.pk])
        self.assertContains(self.client.get(url), "Refresh order status")
        factory.assert_not_called()
        refresh_url = reverse("trading:refresh_paper_trade", args=[trade.pk])
        self.assertEqual(self.client.get(refresh_url).status_code, 405)
        response = self.client.post(refresh_url, follow=True)
        self.assertContains(response, "Order status refreshed")
        factory.return_value.update_trade_status.assert_called_once()
        factory.return_value.submit_paper_order.assert_not_called()

    @patch("trading.trade_views.get_trading_executor")
    def test_refresh_failure_shows_sanitized_diagnostic_without_changing_order(self, factory):
        self.login()
        trade = self.create_trade()
        factory.return_value.update_trade_status.side_effect = ValueError(
            "Invalid position for private-secret https://example.com/signed?token=secret")
        response = self.client.post(reverse("trading:refresh_paper_trade", args=[trade.pk]), follow=True)
        self.assertContains(response, "reading the order status and syncing account holdings")
        self.assertContains(response, "ValueError: Invalid position")
        self.assertNotContains(response, "private-secret")
        self.assertNotContains(response, "https://example.com")
        self.assertContains(response, "No new order was submitted")
        trade.refresh_from_db()
        self.assertEqual(trade.status, "SUBMITTED")
        factory.return_value.submit_paper_order.assert_not_called()

    @patch("trading.trade_views.get_trading_executor")
    def test_unknown_submission_cannot_be_refreshed_as_new_order(self, factory):
        self.login()
        trade = self.create_trade()
        trade.external_order_id = ""
        trade.status = "PENDING"
        trade.save()
        response = self.client.post(reverse("trading:refresh_paper_trade", args=[trade.pk]), follow=True)
        self.assertContains(response, "no brokerage order ID")
        factory.assert_not_called()
