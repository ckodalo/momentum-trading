from django.core.management.base import BaseCommand, CommandError
from portfolio.models import Portfolio
from trading.services.snaptrade_client import get_trading_executor


class Command(BaseCommand):
    help = "Refresh saved SnapTrade order status and reconcile account cash and positions."

    def add_arguments(self, parser):
        parser.add_argument("portfolio_id", type=int)

    def handle(self, *args, **options):
        try:
            portfolio = Portfolio.objects.get(pk=options["portfolio_id"])
            executor = get_trading_executor()
            for trade in portfolio.trades.exclude(external_order_id=""):
                executor.update_trade_status(trade)
                self.stdout.write(f"Trade {trade.pk}: {trade.status}, filled {trade.filled_quantity}/{trade.quantity}")
            if portfolio.trades.filter(status="PENDING", external_order_id="").exists():
                self.stdout.write(self.style.WARNING("An order has an unknown submission outcome. Reconcile it in Alpaca Paper before retrying."))
        except Portfolio.DoesNotExist:
            raise CommandError("Portfolio not found") from None
        except Exception as exc:
            raise CommandError(f"Order refresh failed ({type(exc).__name__}); no new orders were submitted") from None
