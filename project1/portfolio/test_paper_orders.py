from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch, Mock
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from portfolio.models import Portfolio, Trade, Position
from trading.models import Stock, TradingSignal, RebalanceEvent
from trading.services.snaptrade_client import TradingExecutor
from trading.services.strategy_engine import MomentumTradingStrategy


@override_settings(SNAPTRADE_CLIENT_ID="client", SNAPTRADE_CLIENT_SECRET="consumer")
class PaperOrderTests(TestCase):
    def setUp(self):
        self.portfolio = Portfolio.objects.create(name="Paper", initial_cash=1000, current_cash=1000,
            total_value=1000, snaptrade_user_id="user", snaptrade_user_secret="secret", snaptrade_account_id="account")
        self.stock = Stock.objects.create(ticker="AAPL", name="Apple")
        sdk_patch = patch("trading.services.snaptrade_client.SnapTrade")
        self.sdk = sdk_patch.start().return_value
        self.addCleanup(sdk_patch.stop)
        self.sdk.account_information.get_user_account_details.return_value = SimpleNamespace(
            body={"id": "account", "institution_name": "Alpaca Paper"})
        self.sdk.trading.get_user_account_quotes.return_value = SimpleNamespace(body=[{
            "symbol": {"id": "symbol-id", "raw_symbol": "AAPL", "currency": {"code": "USD"}},
            "ask_price": 100, "bid_price": 99, "last_trade_price": 100}])
        self.sdk.trading.place_force_order.return_value = SimpleNamespace(body={"brokerage_order_id": "broker-order"})
        self.executor = TradingExecutor()
        self.snapshot(0, 1000)

    def snapshot(self, quantity, cash):
        rows = [{"instrument": {"kind": "stock", "raw_symbol": "AAPL", "currency": "USD"},
                 "units": quantity, "price": "100", "cost_basis": "100"}] if quantity else []
        self.sdk.account_information.get_all_account_positions.return_value = SimpleNamespace(body={"results": rows})
        self.sdk.account_information.get_user_account_balance.return_value = SimpleNamespace(
            body=[{"currency": {"code": "USD"}, "cash": cash}])

    def submit(self, units=1):
        return self.executor.submit_paper_order(self.portfolio, self.stock, "BUY", units, Decimal("100"))

    def status(self, status, quantity, price="100"):
        self.sdk.account_information.get_user_account_order_detail.return_value = SimpleNamespace(
            body={"status": status, "filled_quantity": quantity, "execution_price": price})

    def test_submission_uses_saved_secret_and_broker_order_id_without_claiming_fill(self):
        trade = self.submit()
        self.assertEqual(trade.external_order_id, "broker-order")
        self.assertEqual(trade.status, "SUBMITTED")
        self.assertEqual(trade.filled_quantity, 0)
        self.assertIsNone(trade.filled_at)
        self.sdk.trading.place_force_order.assert_called_once_with(
            user_id="user", user_secret="secret", account_id="account", action="BUY",
            universal_symbol_id="symbol-id", order_type="Limit", time_in_force="Day", units=1, price=100.0)
        self.assertFalse(Position.objects.exists())
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.current_cash, 1000)

    def test_live_account_cannot_submit(self):
        self.sdk.account_information.get_user_account_details.return_value = SimpleNamespace(
            body={"id": "account", "institution_name": "Alpaca"})
        with self.assertRaisesMessage(ValueError, "Alpaca Paper"):
            self.submit()
        self.sdk.trading.place_force_order.assert_not_called()
        self.assertFalse(Trade.objects.exists())

    def test_outstanding_order_blocks_duplicate(self):
        self.submit()
        with self.assertRaisesMessage(ValueError, "outstanding"):
            self.submit()
        self.assertEqual(self.sdk.trading.place_force_order.call_count, 1)

    def test_unknown_submission_is_retained_and_blocks_retry(self):
        self.sdk.trading.place_force_order.side_effect = TimeoutError("secret in signed url")
        with self.assertRaisesMessage(RuntimeError, "outcome unknown"):
            self.submit()
        trade = Trade.objects.get()
        self.assertEqual(trade.status, "PENDING")
        self.assertNotIn("secret", trade.error_message)
        with self.assertRaisesMessage(ValueError, "outstanding"):
            self.submit()
        self.assertEqual(self.sdk.trading.place_force_order.call_count, 1)

    def test_insufficient_cash_and_fractional_units_block_submission(self):
        with self.assertRaisesMessage(ValueError, "cash"):
            self.submit(11)
        with self.assertRaisesMessage(ValueError, "whole-share"):
            self.submit("0.5")
        self.sdk.trading.place_force_order.assert_not_called()

    def test_partial_then_full_fill_and_repeated_refresh_do_not_double_count(self):
        trade = self.submit(2)
        self.status("PARTIAL", "1")
        self.snapshot(1, 900)
        self.executor.update_trade_status(trade)
        self.assertEqual(trade.status, "PARTIALLY_FILLED")
        self.assertEqual(trade.filled_quantity, 1)
        self.assertIsNone(trade.filled_at)
        self.status("EXECUTED", "2")
        self.snapshot(2, 800)
        self.executor.update_trade_status(trade)
        filled_at = trade.filled_at
        self.executor.update_trade_status(trade)
        self.assertEqual(trade.status, "FILLED")
        self.assertEqual(trade.filled_at, filled_at)
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.current_cash, 800)
        self.assertEqual(Position.objects.get().quantity, 2)
        self.assertEqual(self.portfolio.total_value, 1000)
        self.sdk.account_information.get_user_account_order_detail.assert_called_with(
            user_id="user", user_secret="secret", account_id="account", brokerage_order_id="broker-order")

    def test_partial_cancel_keeps_fills_and_cash(self):
        trade = self.submit(2)
        self.status("PARTIAL_CANCELED", "1")
        self.snapshot(1, 900)
        self.executor.update_trade_status(trade)
        self.assertEqual(trade.status, "CANCELLED")
        self.assertEqual(trade.filled_quantity, 1)
        self.assertEqual(Position.objects.get().quantity, 1)

    def test_rejected_order_has_no_holdings_or_cash_delta(self):
        trade = self.submit()
        self.status("REJECTED", "0", None)
        self.executor.update_trade_status(trade)
        self.assertEqual(trade.status, "REJECTED")
        self.assertEqual(trade.filled_quantity, 0)
        self.assertFalse(Position.objects.exists())
        self.portfolio.refresh_from_db()
        self.assertEqual(self.portfolio.current_cash, 1000)

    def test_snapshot_failure_does_not_record_unreconciled_fill(self):
        trade = self.submit()
        self.status("EXECUTED", "1")
        self.sdk.account_information.get_user_account_balance.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.executor.update_trade_status(trade)
        trade.refresh_from_db()
        self.assertEqual(trade.status, "SUBMITTED")
        self.assertEqual(trade.filled_quantity, 0)
        self.assertFalse(Position.objects.exists())

    def test_test_command_is_bounded(self):
        output = StringIO()
        call_command("paper_order", self.portfolio.pk, "AAPL", limit_price="100", stdout=output)
        self.assertIn("Submitted paper trade", output.getvalue())
        self.assertEqual(Trade.objects.get().quantity, 1)

    def test_strategy_waits_for_sell_fill_and_does_not_mark_signal(self):
        sell = TradingSignal.objects.create(stock=self.stock, signal_type="SELL", signal_date=timezone.now().date())
        strategy = MomentumTradingStrategy.__new__(MomentumTradingStrategy)
        strategy.portfolio = self.portfolio
        strategy.trading_executor = Mock()
        strategy.trading_executor.execute_sell_orders.return_value = [SimpleNamespace(status="SUBMITTED", stock_id=self.stock.pk)]
        self.assertFalse(strategy.execute_trading_signals([], [sell], None))
        strategy.trading_executor.execute_buy_orders.assert_not_called()
        sell.refresh_from_db()
        self.assertFalse(sell.is_executed)

    def test_safe_api_error_redacts_credentials_and_signed_urls(self):
        exc = RuntimeError()
        exc.status = 403
        exc.body = '{"message":"Trading not enabled; secret https://example.com?userSecret=secret"}'
        message = self.executor.safe_api_error(exc, self.portfolio)
        self.assertIn("HTTP 403", message)
        self.assertIn("Trading not enabled", message)
        self.assertNotIn("secret", message)
        self.assertNotIn("https://", message)

    def test_diagnose_command_does_not_submit_or_change_trade(self):
        trade = Trade.objects.create(portfolio=self.portfolio, stock=self.stock, trade_type="BUY",
                                    quantity=1, price=100, status="PENDING")
        output = StringIO()
        call_command("diagnose_paper_order", trade.pk, stdout=output)
        self.sdk.trading.get_order_impact.assert_called_once()
        self.sdk.trading.place_force_order.assert_not_called()
        trade.refresh_from_db()
        self.assertEqual(trade.status, "PENDING")
        self.assertIn("No order was submitted", output.getvalue())

    def test_submission_quantity_passes_real_sdk_request_schema(self):
        from snaptrade_client.model.manual_trade_form_with_options import ManualTradeFormWithOptions
        from snaptrade_client.model.manual_trade_form import ManualTradeForm
        self.submit()
        args = self.sdk.trading.place_force_order.call_args.kwargs
        self.assertIsInstance(args["units"], float)
        payload = {key: args[key] for key in ("action", "order_type", "time_in_force", "units", "price")}
        payload.update(account_id="11111111-1111-4111-8111-111111111111",
                       universal_symbol_id="22222222-2222-4222-8222-222222222222")
        ManualTradeFormWithOptions(**payload)
        ManualTradeForm(**payload)

    def test_schema_diagnostic_includes_validation_detail(self):
        from snaptrade_client.model.manual_trade_form import ManualTradeForm
        try:
            ManualTradeForm(account_id="11111111-1111-4111-8111-111111111111",
                universal_symbol_id="22222222-2222-4222-8222-222222222222",
                action="BUY", order_type="Limit", time_in_force="Day", units=1, price=100.0)
        except Exception as exc:
            message = self.executor.safe_api_error(exc, self.portfolio)
            self.assertIn("units", message)
            self.assertIn("invalid argument", message)
        else:
            self.fail("Expected integer units to fail SDK validation")
