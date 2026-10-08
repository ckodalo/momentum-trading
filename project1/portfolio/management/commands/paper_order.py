from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand, CommandError
from portfolio.models import Portfolio
from trading.models import Stock
from trading.services.snaptrade_client import get_trading_executor


class Command(BaseCommand):
    help = "Submit one bounded whole-share limit order to a verified Alpaca Paper account."

    def add_arguments(self, parser):
        parser.add_argument("portfolio_id", type=int)
        parser.add_argument("ticker")
        parser.add_argument("--action", choices=["BUY", "SELL"], default="BUY")
        parser.add_argument("--units", type=int, default=1)
        parser.add_argument("--limit-price", required=True)

    def handle(self, *args, **options):
        try:
            portfolio = Portfolio.objects.get(pk=options["portfolio_id"])
            price = Decimal(options["limit_price"])
            if not price.is_finite() or price <= 0 or options["units"] != 1 or price > 500:
                raise ValueError("The test command allows one share with a limit price up to USD 500")
            ticker = options["ticker"].upper()
            if not ticker.isascii() or not ticker.replace(".", "").isalnum() or len(ticker) > 10:
                raise ValueError("Invalid stock ticker")
            executor = get_trading_executor()
            executor.verify_paper_account(portfolio)
            executor.sync_portfolio_positions(portfolio)
            stock, _ = Stock.objects.get_or_create(ticker=ticker, defaults={"name": ticker})
            trade = executor.submit_paper_order(portfolio, stock, options["action"], 1, price)
        except Portfolio.DoesNotExist:
            raise CommandError("Portfolio not found") from None
        except (ValueError, InvalidOperation, RuntimeError) as exc:
            raise CommandError(str(exc)) from None
        except Exception as exc:
            raise CommandError(f"Paper order failed ({type(exc).__name__}); check connection and account access") from None
        self.stdout.write(self.style.SUCCESS(
            f"Submitted paper trade {trade.pk}: {trade.trade_type} 1 {ticker}, limit USD {price:.2f}. "
            "This is a submission, not a confirmed fill."
        ))
