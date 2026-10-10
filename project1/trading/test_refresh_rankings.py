from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from trading.models import MomentumScore, Stock
from trading.services.massive_client import MassiveAPIClient


class RefreshRankingsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rankings-reader")
        self.url = reverse("trading:refresh_rankings")

    def grant_permissions(self, write=False):
        names = ["view_momentumscore"]
        if write:
            names += ["add_momentumscore", "change_momentumscore", "add_stock"]
        self.user.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="trading", codename__in=names))
        self.client.force_login(self.user)

    @patch("trading.views.call_command")
    def test_anonymous_and_view_only_users_cannot_refresh(self, command):
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.grant_permissions()
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertNotContains(self.client.get(reverse("trading:rankings")), "Refresh rankings")
        command.assert_not_called()

    @patch("trading.views.call_command")
    def test_get_and_reset_never_fetch_data(self, command):
        self.grant_permissions(write=True)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertContains(self.client.get(reverse("trading:rankings")), "Refresh rankings")
        command.assert_not_called()

    @patch("trading.views.call_command")
    def test_refresh_requires_csrf(self, command):
        self.grant_permissions(write=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url).status_code, 403)
        command.assert_not_called()

    @override_settings(MASSIVE_API_KEY="test-key")
    def test_refresh_fetches_default_universe_saves_and_displays_rankings(self):
        self.grant_permissions(write=True)
        tickers = MassiveAPIClient().get_sp500_tickers()
        data = {ticker: {"price_12m": 100, "price_1m": 100 + i} for i, ticker in enumerate(tickers)}
        with patch("trading.services.massive_client.MassiveAPIClient.fetch_bulk_momentum_data", return_value=data) as fetch:
            response = self.client.post(self.url, {"date": "2000-01-01", "tickers": "OTHER"}, follow=True)
        self.assertEqual(fetch.call_args.kwargs["tickers"], tickers)
        self.assertEqual(fetch.call_args.kwargs["calculation_date"], timezone.localdate())
        self.assertEqual(MomentumScore.objects.count(), 50)
        self.assertContains(response, "Rankings refreshed")
        self.assertEqual(response.context["calculation_date"], timezone.localdate())
        self.assertEqual(response.context["paginator"].count, 50)

    @override_settings(MASSIVE_API_KEY="test-key")
    def test_partial_data_displays_warning(self):
        self.grant_permissions(write=True)
        with patch("trading.services.massive_client.MassiveAPIClient.fetch_bulk_momentum_data", return_value={
            "AAPL": {"price_12m": 100, "price_1m": 125}}):
            response = self.client.post(self.url, follow=True)
        self.assertContains(response, "Some stocks were skipped")
        self.assertEqual(MomentumScore.objects.count(), 1)

    @patch("trading.views.call_command", side_effect=CommandError("secret API key in signed URL"))
    def test_failure_is_safe_and_preserves_saved_rankings(self, command):
        self.grant_permissions(write=True)
        stock = Stock.objects.create(ticker="AAPL")
        score = MomentumScore.objects.create(stock=stock, calculation_date=date(2026, 1, 1),
            momentum_score="0.25", period_start=date(2025, 1, 1), period_end=date(2025, 12, 1))
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, "Could not refresh rankings")
        self.assertNotContains(response, "secret API key")
        self.assertTrue(MomentumScore.objects.filter(pk=score.pk).exists())
        self.assertContains(response, "AAPL")
