from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from portfolio.models import Portfolio, Trade, Position
from trading import test_trade_views


class PaperOrderQuoteTests(TestCase):
    def setUp(self):
        test_trade_views.PaperTradeViewTests.setUp(self)
        self.quote_url = reverse("trading:paper_order_quote", args=[self.score.pk])
        self.client.force_login(self.user)
        self.patch = patch("trading.trade_views.get_trading_executor")
        self.executor = self.patch.start().return_value
        self.addCleanup(self.patch.stop)
        self.executor._credentials.return_value = {"user_id": "user", "user_secret": "private-secret", "account_id": "account"}
        self.executor._quote.return_value = {"ask_price": "150.101", "bid_price": "150.009", "last_trade_price": "150.05"}
        api = self.executor.snaptrade.account_information
        api.get_user_account_balance.return_value = SimpleNamespace(body=[
            {"currency": {"code": "CAD"}, "cash": "9000"},
            {"currency": {"code": "USD"}, "cash": "800.25"},
        ])
        api.get_all_account_positions.return_value = SimpleNamespace(body={"results": [{
            "instrument": {"raw_symbol": "AAPL", "kind": "stock", "currency": "USD"}, "units": "3"},
            {"instrument": {"raw_symbol": "MSFT"}, "units": "50"}]})

    def read_quote(self, **params):
        return self.client.get(self.quote_url, {"portfolio": self.portfolio.pk, **params})

    def test_quote_and_brokerage_cash_are_exposed_without_credentials_or_writes(self):
        response = self.read_quote()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["ask"], "150.101")
        self.assertEqual(data["bid"], "150.009")
        self.assertEqual(data["cash"], "800.25")
        self.assertEqual(data["shares_held"], "3")
        self.assertEqual(data["suggested_buy_limit"], "150.11")
        self.assertEqual(data["suggested_sell_limit"], "150.00")
        self.assertIn("retrieved_at", data)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertNotContains(response, "private-secret")
        self.executor.verify_paper_account.assert_called_once_with(self.portfolio)
        self.executor._quote.assert_called_once_with(self.portfolio, self.stock)
        self.executor.submit_paper_order.assert_not_called()
        self.executor.sync_portfolio_positions.assert_not_called()
        self.assertFalse(Trade.objects.exists())
        self.assertFalse(Position.objects.exists())
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.current_cash, 1000)

    def test_missing_and_invalid_prices_are_unavailable_not_zero(self):
        self.executor._quote.return_value = {"ask_price": 0, "bid_price": "NaN", "last_trade_price": None}
        data = self.read_quote().json()
        self.assertIsNone(data["ask"])
        self.assertIsNone(data["bid"])
        self.assertIsNone(data["last"])
        self.assertIsNone(data["suggested_buy_limit"])

    def test_missing_usd_cash_is_not_replaced_with_saved_cash(self):
        self.executor.snaptrade.account_information.get_user_account_balance.return_value = SimpleNamespace(body=[])
        self.assertIsNone(self.read_quote().json()["cash"])

    def test_invalid_or_inactive_portfolio_never_calls_api(self):
        self.assertEqual(self.read_quote(portfolio="invalid").status_code, 400)
        self.portfolio.is_active = False
        self.portfolio.save()
        self.assertEqual(self.read_quote().status_code, 400)
        self.executor.verify_paper_account.assert_not_called()

    def test_failure_hides_api_credentials(self):
        self.executor._quote.side_effect = Exception("private-secret signed URL")
        response = self.read_quote()
        self.assertEqual(response.status_code, 502)
        self.assertNotContains(response, "private-secret", status_code=502)
        self.assertIn("Could not load", response.json()["error"])

    def test_requires_trade_permissions(self):
        from django.contrib.auth.models import User
        viewer = User.objects.create_user(username="quote-viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.read_quote().status_code, 403)
        self.executor.verify_paper_account.assert_not_called()

    def test_form_contains_quote_and_cost_controls(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Latest quote (USD)")
        self.assertContains(response, "Available cash (USD)")
        self.assertContains(response, "Maximum share cost")
        self.assertContains(response, "Refresh prices")
        self.assertContains(response, "quote timestamp")
        self.executor.verify_paper_account.assert_not_called()
