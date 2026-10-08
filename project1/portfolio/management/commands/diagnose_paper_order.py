from django.core.management.base import BaseCommand, CommandError
from portfolio.models import Trade
from trading.services.snaptrade_client import get_trading_executor


class Command(BaseCommand):
    help = "Validate an existing paper trade with SnapTrade's order-impact API; submits no order."

    def add_arguments(self, parser):
        parser.add_argument("trade_id", type=int)

    def handle(self, *args, **options):
        try:
            trade = Trade.objects.select_related("portfolio", "stock").get(pk=options["trade_id"])
        except Trade.DoesNotExist:
            raise CommandError("Trade not found") from None
        executor = get_trading_executor()
        try:
            executor.verify_paper_account(trade.portfolio)
            quote = executor._quote(trade.portfolio, trade.stock)
            response = executor.snaptrade.trading.get_order_impact(
                **executor._credentials(trade.portfolio), action=trade.trade_type,
                universal_symbol_id=quote["symbol"]["id"], order_type="Limit",
                time_in_force="Day", units=float(trade.quantity), price=float(trade.price),
            )
            self.stdout.write(self.style.SUCCESS(
                f"Trade {trade.pk}: paper account verified and order-impact validation returned successfully. No order was submitted."
            ))
        except Exception as exc:
            raise CommandError(executor.safe_api_error(exc, trade.portfolio)) from None
