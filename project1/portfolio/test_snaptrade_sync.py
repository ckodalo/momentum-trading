from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from portfolio.models import Portfolio, Position
from trading.models import Stock
from trading.services.snaptrade_client import TradingExecutor


@override_settings(SNAPTRADE_CLIENT_ID="client", SNAPTRADE_CLIENT_SECRET="consumer")
class SnapTradeSyncTests(TestCase):
    def setUp(self):
        self.portfolio = Portfolio.objects.create(
            name="Paper", initial_cash=100, current_cash=100, total_value=100,
            snaptrade_user_id="user", snaptrade_user_secret="secret",
            snaptrade_account_id="account",
        )
        self.other = Portfolio.objects.create(name="Other", initial_cash=10, current_cash=10)
        stock = Stock.objects.create(ticker="OLD", name="Old stock")
        self.stale = Position.objects.create(portfolio=self.portfolio, stock=stock, quantity=2, current_value=20)
        self.other_position = Position.objects.create(portfolio=self.other, stock=stock, quantity=1)
        self.patch = patch("trading.services.snaptrade_client.SnapTrade")
        self.sdk = self.patch.start().return_value
        self.addCleanup(self.patch.stop)
        self.sdk.account_information.get_user_account_balance.return_value = SimpleNamespace(body=[
            {"currency": {"code": "CAD"}, "cash": 900},
            {"currency": {"code": "USD"}, "cash": "1250.50"},
        ])
        self.set_positions([])

    def set_positions(self, rows):
        self.sdk.account_information.get_all_account_positions.return_value = SimpleNamespace(body={"results": rows})

    def holding(self, units=2):
        return {"instrument": {"kind": "stock", "raw_symbol": "AAPL", "currency": "USD"},
                "units": units, "cost_basis": "80", "price": "100.25"}

    def test_sync_updates_cash_holdings_and_clears_stale_positions(self):
        self.set_positions([self.holding()])
        output = StringIO()
        call_command("sync_snaptrade_portfolio", self.portfolio.pk, stdout=output)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.current_cash, Decimal("1250.50"))
        self.assertEqual(self.portfolio.total_value, Decimal("1451.00"))
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.quantity, 0)
        self.other_position.refresh_from_db()
        self.assertEqual(self.other_position.quantity, 1)
        self.sdk.account_information.get_all_account_positions.assert_called_once_with(
            user_id="user", user_secret="secret", account_id="account")
        self.sdk.trading.place_force_order.assert_not_called()
        self.assertNotIn("secret", output.getvalue())

    def test_empty_account_clears_old_holdings(self):
        TradingExecutor().sync_portfolio_positions(self.portfolio)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.total_value, Decimal("1250.50"))
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.quantity, 0)

    def test_fractional_position_does_not_partially_update_database(self):
        self.set_positions([self.holding(), {**self.holding("0.5"), "instrument": {"kind": "stock", "raw_symbol": "MSFT", "currency": "USD"}}])
        with self.assertRaisesMessage(ValueError, "whole-share"):
            TradingExecutor().sync_portfolio_positions(self.portfolio)
        self.assertFalse(Stock.objects.filter(ticker="AAPL").exists())
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.current_cash, 100)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.quantity, 2)

    def test_balance_failure_preserves_existing_data(self):
        self.sdk.account_information.get_user_account_balance.side_effect = RuntimeError("request failed")
        with self.assertRaises(RuntimeError):
            TradingExecutor().sync_portfolio_positions(self.portfolio)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.quantity, 2)

    def test_missing_usd_balance_preserves_holdings(self):
        self.sdk.account_information.get_user_account_balance.return_value = SimpleNamespace(body=[])
        with self.assertRaisesMessage(ValueError, "USD cash balance"):
            TradingExecutor().sync_portfolio_positions(self.portfolio)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.quantity, 2)
