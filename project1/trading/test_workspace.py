from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from portfolio.models import Portfolio, Position, Trade
from trading.models import Stock, MomentumScore, TradingSignal


class WorkspaceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="workspace-user", password="test", email="")
        self.client.force_login(self.user)
        self.portfolio = Portfolio.objects.create(name="Primary", initial_cash=1000, current_cash=750,
            snaptrade_user_id="user", snaptrade_user_secret="private-secret", snaptrade_account_id="account")
        self.other = Portfolio.objects.create(name="Secondary", initial_cash=2000, current_cash=1500,
            snaptrade_user_id="other-user", snaptrade_user_secret="other-secret", snaptrade_account_id="other-account")
        self.stock = Stock.objects.create(ticker="AAPL")
        self.score = MomentumScore.objects.create(stock=self.stock, calculation_date=date(2026, 10, 10),
            momentum_score="0.25", rank=1, quintile=1, is_top_quintile=True,
            period_start=date(2025, 10, 10), period_end=date(2026, 9, 10))
        self.signal = TradingSignal.objects.create(portfolio=self.portfolio, stock=self.stock,
            signal_date=self.score.calculation_date, momentum_score=self.score, signal_type="BUY", target_value=500)
        self.trade = Trade.objects.create(portfolio=self.portfolio, stock=self.stock, trade_type="BUY",
            quantity=1, price=100, status="SUBMITTED", external_order_id="broker")
        Trade.objects.create(portfolio=self.other, stock=self.stock, trade_type="SELL", quantity=777, price=100)
        Position.objects.create(portfolio=self.portfolio, stock=self.stock, quantity=2, current_value=200)
        self.url = reverse("trading:rankings")

    def test_home_and_default_login_land_on_workspace(self):
        self.assertRedirects(self.client.get("/"), self.url)
        self.client.logout()
        self.assertRedirects(self.client.post(reverse("login"), {"username": "workspace-user", "password": "test"}), self.url)

    @patch("trading.trade_views.get_trading_executor")
    def test_workspace_is_read_only_and_scopes_related_data(self, executor):
        response = self.client.get(self.url, {"portfolio": self.portfolio.pk})
        self.assertEqual(response.context["selected_portfolio"], self.portfolio)
        self.assertEqual(list(response.context["workspace_orders"]), [self.trade])
        self.assertEqual(list(response.context["workspace_signals"]), [self.signal])
        self.assertEqual(response.context["outstanding_count"], 1)
        self.assertContains(response, 'id="workspace-dialog"')
        self.assertContains(response, "Review →")
        self.assertContains(response, "&amp;portfolio=" + str(self.portfolio.pk))
        self.assertNotContains(response, "private-secret")
        self.assertNotContains(response, "other-secret")
        executor.assert_not_called()

    def test_selection_and_invalid_portfolio_do_not_fall_back_to_other_account(self):
        response = self.client.get(self.url, {"portfolio": self.other.pk})
        self.assertEqual(response.context["selected_portfolio"], self.other)
        self.assertEqual(response.context["workspace_orders"][0].quantity, 777)
        for selection in ["", "bad", "9999"]:
            response = self.client.get(self.url, {"portfolio": selection})
            self.assertIsNone(response.context["selected_portfolio"])
            self.assertNotIn("workspace_orders", response.context)

    def test_related_information_still_requires_model_permissions(self):
        viewer = User.objects.create_user(username="rankings-only")
        viewer.user_permissions.add(Permission.objects.get(content_type__app_label="trading", codename="view_momentumscore"))
        self.client.force_login(viewer)
        response = self.client.get(self.url, {"portfolio": self.portfolio.pk})
        self.assertNotIn("selected_portfolio", response.context)
        self.assertNotContains(response, self.portfolio.name)
        self.assertNotContains(response, 'id="refresh-rankings-button"')
        viewer.user_permissions.add(Permission.objects.get(content_type__app_label="portfolio", codename="view_portfolio"))
        response = self.client.get(self.url, {"portfolio": self.portfolio.pk})
        self.assertIn("selected_portfolio", response.context)
        for name in ["workspace_signals", "workspace_orders", "workspace_positions"]:
            self.assertNotIn(name, response.context)

    def test_fifty_stock_universe_fits_one_page(self):
        for i in range(49):
            stock = Stock.objects.create(ticker=f"TEST{i}")
            MomentumScore.objects.create(stock=stock, calculation_date=self.score.calculation_date,
                momentum_score="0.1", rank=i + 2, quintile=2, period_start=self.score.period_start, period_end=self.score.period_end)
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["scores"]), 50)
        self.assertEqual(response.context["ranked_count"], 50)
        self.assertFalse(response.context["is_paginated"])

    def test_dialog_pages_allow_only_same_origin_framing_and_preselect_portfolio(self):
        pages = [reverse("portfolio:list"), reverse("portfolio:detail", args=[self.portfolio.pk]),
            reverse("trading:signals"), reverse("trading:rebalances"), reverse("trading:generate_signals"),
            reverse("trading:paper_order", args=[self.score.pk]), reverse("trading:execute_signal", args=[self.signal.pk]),
            reverse("trading:paper_trade", args=[self.trade.pk])]
        for url in pages:
            response = self.client.get(url, {"embed": "1", "portfolio": self.portfolio.pk})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
            self.assertContains(response, 'class="embedded"')
        for name, args in [("generate_signals", []), ("paper_order", [self.score.pk])]:
            response = self.client.get(reverse("trading:" + name, args=args), {"portfolio": self.other.pk})
            self.assertEqual(response.context["form"].initial["portfolio"], self.other)

    @patch("trading.views.call_command")
    def test_refresh_keeps_selected_portfolio(self, command):
        response = self.client.post(reverse("trading:refresh_rankings"), {"portfolio": self.other.pk})
        self.assertIn(f"&portfolio={self.other.pk}", response.url)
