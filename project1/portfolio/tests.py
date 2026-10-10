from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from portfolio.models import Portfolio


class PortfolioListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="viewer", password="test-password")
        self.url = reverse("portfolio:list")

    def grant_access(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="portfolio", codename="view_portfolio")
        )
        self.client.force_login(self.user)

    def test_anonymous_user_must_sign_in(self):
        self.assertRedirects(self.client.get(self.url), "/accounts/login/?next=/portfolios/")

    def test_user_without_permission_cannot_read_portfolios(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_empty_state(self):
        self.grant_access()
        self.assertContains(self.client.get(self.url), "No portfolios yet")

    def test_portfolio_values_status_and_escaping(self):
        self.grant_access()
        Portfolio.objects.create(
            name="Growth", description="<script>alert(1)</script>",
            initial_cash="10000", current_cash="1250.50", total_value="12000.25",
        )
        Portfolio.objects.create(
            name="Retired", initial_cash="100", current_cash="100",
            total_value="100", is_active=False,
        )
        response = self.client.get(self.url)
        self.assertContains(response, "$12,000.25")
        self.assertContains(response, "$1,250.50")
        self.assertContains(response, "Inactive")
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>")

    def test_login_returns_to_rankings(self):
        self.grant_access()
        self.user.user_permissions.add(Permission.objects.get(content_type__app_label="trading", codename="view_momentumscore"))
        self.client.logout()
        self.assertRedirects(
            self.client.post(reverse("login"), {"username": "viewer", "password": "test-password"}),
            reverse("trading:rankings"),
        )

    def test_pagination(self):
        self.grant_access()
        Portfolio.objects.bulk_create([
            Portfolio(name=f"Portfolio {i:02}", initial_cash=100, current_cash=100)
            for i in range(21)
        ])
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["portfolios"]), 20)
        self.assertContains(response, "Next")
        self.assertContains(self.client.get(self.url + "?page=2"), "Portfolio 20")


class PortfolioDetailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="detail-reader")
        self.portfolio = Portfolio.objects.create(name="Growth", initial_cash=1000, current_cash=500, total_value=1200)
        self.other = Portfolio.objects.create(name="Other", initial_cash=100, current_cash=100)
        self.url = reverse("portfolio:detail", args=[self.portfolio.pk])

    def grant(self, *codenames):
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label="portfolio", codename__in=codenames))
        self.client.force_login(self.user)

    def test_access_and_missing_portfolio(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.grant("view_portfolio")
        self.assertEqual(self.client.get(reverse("portfolio:detail", args=[999])).status_code, 404)

    def test_related_data_requires_permissions_and_is_scoped(self):
        from portfolio.models import Position, Trade, PerformanceMetric
        from trading.models import Stock
        stock = Stock.objects.create(ticker="AAPL")
        Position.objects.create(portfolio=self.portfolio, stock=stock, quantity=2, current_value=700)
        Position.objects.create(portfolio=self.other, stock=stock, quantity=999)
        Trade.objects.create(portfolio=self.portfolio, stock=stock, trade_type="BUY", quantity=2)
        Trade.objects.create(portfolio=self.other, stock=stock, trade_type="SELL", quantity=999)
        PerformanceMetric.objects.create(portfolio=self.portfolio, date="2026-10-01", total_value=1200, cash_value=500, positions_value=700, daily_return=0)
        self.grant("view_portfolio")
        response = self.client.get(self.url)
        self.assertNotIn("positions", response.context)
        self.assertNotContains(response, "AAPL")
        self.grant("view_position", "view_trade", "view_performancemetric")
        response = self.client.get(self.url)
        self.assertContains(response, "AAPL")
        self.assertEqual(len(response.context["positions"]), 1)
        self.assertEqual(len(response.context["trades"]), 1)
        self.assertContains(response, "0.00%")
        self.assertContains(self.client.get(reverse("portfolio:list")), self.url)

    def test_empty_detail(self):
        self.grant("view_portfolio", "view_position", "view_trade", "view_performancemetric")
        response = self.client.get(self.url)
        for text in ["No holdings yet", "No trades yet", "No performance snapshots yet"]:
            self.assertContains(response, text)
