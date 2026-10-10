from datetime import date
import re

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from trading.services.momentum_calculator import MomentumCalculator


class Command(BaseCommand):
    help = "Fetch historical prices, save momentum scores, and rank stocks."

    def add_arguments(self, parser):
        parser.add_argument("--tickers", nargs="+", default=None, help="Stock symbols separated by spaces (default: the strategy's fixed 50-stock universe).")
        parser.add_argument("--date", type=date.fromisoformat, default=None, help="Calculation date in YYYY-MM-DD format (default: today).")

    def handle(self, *args, **options):
        calculation_date = options["date"] or timezone.localdate()
        if calculation_date > timezone.localdate():
            raise CommandError("Calculation date cannot be in the future.")
        tickers = list(dict.fromkeys(ticker.strip().upper() for ticker in options["tickers"])) if options["tickers"] is not None else None
        if tickers is not None and any(not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,9}", ticker) for ticker in tickers):
            raise CommandError("Invalid ticker: use stock symbols of at most 10 letters, digits, dots, or hyphens.")
        try:
            calculator = MomentumCalculator()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        stocks = calculator.update_stock_universe(tickers)
        tickers = [stock.ticker for stock in stocks]
        self.stdout.write(f"Fetching historical momentum data for {', '.join(tickers)} as of {calculation_date}...")
        scores = calculator.calculate_momentum_scores_bulk(stocks, calculation_date)
        saved_tickers = {score.stock.ticker for score in scores}
        missing = [ticker for ticker in tickers if ticker not in saved_tickers]
        if not scores:
            raise CommandError("No momentum scores were saved. Check your Massive API key, plan access, and available price history.")
        calculator.rank_stocks_by_momentum(calculation_date)
        for score in scores:
            score.refresh_from_db()
            self.stdout.write(f"{score.stock.ticker}: momentum={score.momentum_score:.6f}, rank={score.rank}, quintile={score.quintile}")
        if missing:
            self.stderr.write(self.style.WARNING(f"Skipped stocks without sufficient price data: {', '.join(missing)}. Any previously saved scores for these stocks remain unchanged."))
        self.stdout.write(self.style.SUCCESS(f"Saved {len(scores)} momentum scores for {calculation_date}. Open /trading/rankings/?date={calculation_date} to view them."))
