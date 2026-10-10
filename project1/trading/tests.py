from datetime import date

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from trading.models import MomentumScore, RebalanceEvent, Stock, TradingSignal


class TradingPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="reader")
        self.stock = Stock.objects.create(ticker="AAPL", name="Apple")
        self.old = MomentumScore.objects.create(stock=self.stock, calculation_date=date(2026, 9, 1), momentum_score="0.10", rank=1, quintile=1, period_start=date(2025, 9, 1), period_end=date(2026, 8, 1))
        self.latest = MomentumScore.objects.create(stock=self.stock, calculation_date=date(2026, 10, 1), momentum_score="0.25", rank=1, quintile=1, is_top_quintile=True, period_start=date(2025, 10, 1), period_end=date(2026, 9, 1))

    def login(self):
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label="trading", codename__startswith="view_"))
        self.client.force_login(self.user)

    def test_access_controls_for_all_pages(self):
        for name in ["rankings", "signals", "rebalances"]:
            url = reverse("trading:" + name)
            self.assertEqual(self.client.get(url).status_code, 302)
            self.client.force_login(self.user)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.client.logout()

    def test_latest_rankings_and_date_quintile_filters(self):
        self.login()
        url = reverse("trading:rankings")
        response = self.client.get(url)
        self.assertEqual(list(response.context["scores"]), [self.latest])
        self.assertContains(response, "0.2500")
        response = self.client.get(url, {"date": "2026-09-01"})
        self.assertEqual(list(response.context["scores"]), [self.old])
        self.assertContains(self.client.get(url, {"quintile": "5"}), "No momentum scores")

    def test_invalid_filters_render_errors(self):
        self.login()
        for name, params in [("rankings", {"date": "bad"}), ("signals", {"signal_type": "bad"}), ("rebalances", {"status": "bad"})]:
            response = self.client.get(reverse("trading:" + name), params)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["filter_form"].errors)

    def test_signal_filters_escaping_and_pagination(self):
        self.login()
        TradingSignal.objects.bulk_create([TradingSignal(stock=self.stock, signal_date=date(2026, 10, 1), signal_type="BUY", reason="<script>bad</script>", target_value=0) for _ in range(21)])
        TradingSignal.objects.create(stock=self.stock, signal_date=date(2026, 10, 1), signal_type="SELL", is_executed=True)
        url = reverse("trading:signals")
        params = {"signal_type": "BUY", "executed": "no", "date": "2026-10-01"}
        response = self.client.get(url, params)
        self.assertEqual(len(response.context["signals"]), 20)
        self.assertContains(response, "&lt;script&gt;")
        self.assertContains(response, "$0.00")
        self.assertContains(response, "signal_type=BUY")
        self.assertNotContains(response, "<script>")
        response = self.client.get(url, {**params, "page": 2})
        self.assertEqual(len(response.context["signals"]), 1)
        self.assertEqual(self.client.get(url, {"executed": "yes"}).context["paginator"].count, 1)

    def test_rebalance_filter_and_error(self):
        self.login()
        RebalanceEvent.objects.create(date=date(2026, 10, 1), total_stocks_analyzed=50, buy_signals_generated=10, sell_signals_generated=2, execution_status="FAILED", error_message="<unsafe>")
        url = reverse("trading:rebalances")
        self.assertContains(self.client.get(url, {"status": "FAILED"}), "&lt;unsafe&gt;")
        self.assertContains(self.client.get(url, {"status": "COMPLETED"}), "No rebalance events")

    def test_empty_pages(self):
        self.login()
        MomentumScore.objects.all().delete()
        for name, text in [("rankings", "No momentum scores"), ("signals", "No trading signals"), ("rebalances", "No rebalance events")]:
            self.assertContains(self.client.get(reverse("trading:" + name)), text)


class PullMassiveDataTests(TestCase):
    def test_default_uses_strategy_universe_and_ranks_all_five_groups(self):
        from unittest.mock import patch
        from django.test import override_settings
        from trading.services.massive_client import MassiveAPIClient
        with override_settings(MASSIVE_API_KEY="test-key"):
            tickers = MassiveAPIClient().get_sp500_tickers()
            self.assertEqual(len(set(tickers)), 50)
            data = {ticker: {"price_12m": 100, "price_1m": 100 + i} for i, ticker in enumerate(tickers)}
            with patch("trading.services.massive_client.MassiveAPIClient.fetch_bulk_momentum_data", return_value=data) as fetch:
                output = self.run_command(date=date(2026, 1, 1))
                self.assertEqual(fetch.call_args.kwargs["tickers"], tickers)
                self.assertEqual(Stock.objects.count(), 50)
                self.assertEqual(MomentumScore.objects.count(), 50)
                self.assertIn("Saved 50 momentum scores", output)
                for quintile in range(1, 6):
                    self.assertEqual(MomentumScore.objects.filter(quintile=quintile).count(), 10)

    def run_command(self, **options):
        from io import StringIO
        from django.core.management import call_command
        output = StringIO()
        call_command("pull_massive_data", stdout=output, **options)
        return output.getvalue()

    def test_fetch_persist_rank_and_rerun_without_duplicates(self):
        from unittest.mock import patch
        from django.test import override_settings
        with override_settings(MASSIVE_API_KEY="test-key"), patch("trading.services.massive_client.MassiveAPIClient.fetch_bulk_momentum_data", return_value={"AAPL": {"price_12m": 100, "price_1m": 125}, "NVDA": {"price_12m": 100, "price_1m": 150}}) as fetch:
            output = self.run_command(tickers=["AAPL", "NVDA"], date=date(2026, 1, 1))
            self.assertIn("Saved 2 momentum scores", output)
            self.assertEqual(Stock.objects.count(), 2)
            self.assertEqual(MomentumScore.objects.count(), 2)
            self.assertEqual(MomentumScore.objects.get(stock__ticker="NVDA").rank, 1)
            self.assertEqual(float(MomentumScore.objects.get(stock__ticker="AAPL").momentum_score), 0.25)
            self.run_command(tickers=["AAPL", "NVDA"], date=date(2026, 1, 1))
            self.assertEqual(MomentumScore.objects.count(), 2)
            self.assertEqual(fetch.call_args.kwargs["calculation_date"], date(2026, 1, 1))
            user = User.objects.create_superuser(username="command-reader", password="test", email="")
            self.client.force_login(user)
            response = self.client.get(reverse("trading:rankings"), {"date": "2026-01-01"})
            self.assertContains(response, "AAPL")
            self.assertContains(response, "NVDA")

    def test_custom_tickers_and_default_date(self):
        from unittest.mock import patch
        from django.test import override_settings
        from django.utils import timezone
        with override_settings(MASSIVE_API_KEY="test-key"), patch("trading.services.massive_client.MassiveAPIClient.fetch_bulk_momentum_data", return_value={"MSFT": {"price_12m": 100, "price_1m": 110}}) as fetch:
            self.run_command(tickers=["msft", "MSFT"])
            self.assertEqual(Stock.objects.count(), 1)
            self.assertEqual(fetch.call_args.kwargs["tickers"], ["MSFT"])
            self.assertEqual(fetch.call_args.kwargs["calculation_date"], timezone.localdate())

    def test_missing_data_reports_failure(self):
        from unittest.mock import patch
        from django.core.management.base import CommandError
        from django.test import override_settings
        with override_settings(MASSIVE_API_KEY="test-key"), patch("trading.services.massive_client.MassiveAPIClient.fetch_bulk_momentum_data", return_value={}):
            with self.assertRaisesMessage(CommandError, "No momentum scores were saved"):
                self.run_command(date=date(2026, 1, 1))
            self.assertEqual(MomentumScore.objects.count(), 0)

    def test_partial_data_reports_skipped_stock(self):
        from io import StringIO
        from unittest.mock import patch
        from django.test import override_settings
        errors = StringIO()
        with override_settings(MASSIVE_API_KEY="test-key"), patch("trading.services.massive_client.MassiveAPIClient.fetch_bulk_momentum_data", return_value={"AAPL": {"price_12m": 100, "price_1m": 125}}):
            self.run_command(tickers=["AAPL", "NVDA"], date=date(2026, 1, 1), stderr=errors)
            self.assertEqual(MomentumScore.objects.count(), 1)
            self.assertIn("NVDA", errors.getvalue())

    def test_missing_key_and_invalid_input(self):
        from django.core.management.base import CommandError
        from django.test import override_settings
        with override_settings(MASSIVE_API_KEY=""):
            with self.assertRaisesMessage(CommandError, "Massive API key is required"):
                self.run_command()
        with self.assertRaisesMessage(CommandError, "Invalid ticker"):
            self.run_command(tickers=["invalid symbol"])
        with self.assertRaisesMessage(CommandError, "future"):
            self.run_command(date=date(2999, 1, 1))
