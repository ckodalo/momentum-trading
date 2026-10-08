from django.core.management.base import BaseCommand, CommandError
from portfolio.models import Portfolio
from trading.services.snaptrade_client import get_trading_executor


class Command(BaseCommand):
    help = "Read SnapTrade cash and positions into an existing portfolio (no orders)."

    def add_arguments(self, parser):
        parser.add_argument("portfolio_id", type=int)

    def handle(self, *args, **options):
        try:
            portfolio = Portfolio.objects.get(pk=options["portfolio_id"])
        except Portfolio.DoesNotExist:
            raise CommandError("Portfolio not found") from None
        try:
            positions = get_trading_executor().sync_portfolio_positions(portfolio)
        except Exception as exc:
            # SDK errors can contain signed request URLs and credentials.
            if isinstance(exc, ValueError):
                raise CommandError(str(exc)) from None
            raise CommandError(
                f"SnapTrade sync failed ({type(exc).__name__}). Check credentials, account access, and network connectivity."
            ) from None
        self.stdout.write(self.style.SUCCESS(
            f"Synced portfolio {portfolio.pk}: {len(positions)} holdings, "
            f"USD cash {portfolio.current_cash:.2f}, total value {portfolio.total_value:.2f}"
        ))
