from collections.abc import Mapping
from django.conf import settings
from snaptrade_client import SnapTrade, SnapTradeAuth
from django.db import transaction
from portfolio.models import Portfolio, Trade
from typing import List
from portfolio.models import Position
from trading.models import Stock
from decimal import Decimal, InvalidOperation
from typing import Optional
from datetime import datetime
from django.utils import timezone
import logging
import re
import json

logger = logging.getLogger(__name__)


class TradingExecutor:
    def __init__(self):
        self.snaptrade = SnapTrade(
            auth=SnapTradeAuth.commercial_api_key(
                consumer_key=settings.SNAPTRADE_CLIENT_SECRET,
                client_id=settings.SNAPTRADE_CLIENT_ID,
            )
        )

    def sync_portfolio_positions(
        self, portfolio: Portfolio, user_secret: str = None
    ) -> List[Position]:
        """Read the connected account and atomically reconcile its USD holdings."""
        user_secret = user_secret or portfolio.snaptrade_user_secret
        if not portfolio.snaptrade_user_id or not portfolio.snaptrade_account_id:
            raise ValueError("Portfolio must have SnapTrade user and account IDs")
        if not user_secret:
            raise ValueError("Portfolio must have a SnapTrade user secret")

        credentials = dict(
            user_id=portfolio.snaptrade_user_id,
            user_secret=user_secret,
            account_id=portfolio.snaptrade_account_id,
        )
        positions_data = self.snaptrade.account_information.get_all_account_positions(
            **credentials
        ).body
        if isinstance(positions_data, Mapping):
            positions_data = positions_data.get("results")
        balances = self.snaptrade.account_information.get_user_account_balance(
            **credentials
        ).body
        if not isinstance(positions_data, (list, tuple)) or not isinstance(balances, (list, tuple)):
            raise ValueError("SnapTrade returned an invalid account snapshot")

        usd_balances = [b for b in balances if (b.get("currency") or {}).get("code") == "USD"]
        if len(usd_balances) != 1 or usd_balances[0].get("cash") is None:
            raise ValueError("SnapTrade must return one USD cash balance")

        def number(value, label):
            try:
                result = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError(f"SnapTrade returned an invalid {label}") from None
            if not result.is_finite():
                raise ValueError(f"SnapTrade returned an invalid {label}")
            return result

        cash = number(usd_balances[0]["cash"], "cash balance")
        holdings = []
        symbols = set()
        for item in positions_data:
            # Cash equivalents are already included in the cash balance.
            if item.get("cash_equivalent"):
                continue
            quantity = number(item.get("units"), "position quantity")
            if quantity == 0:
                continue
            if quantity < 0 or quantity != quantity.to_integral_value():
                raise ValueError("Sync supports long, whole-share positions only; fractional or short holdings cannot be saved")
            symbol_data = item.get("instrument") or {}
            if symbol_data.get("kind") not in ("stock", "etf", "adr", "cef", "mutualfund"):
                raise ValueError("Sync supports stock and fund holdings only")
            symbol = symbol_data.get("raw_symbol")
            if not symbol or symbol in symbols:
                raise ValueError("SnapTrade returned a missing or duplicate stock symbol")
            currency = item.get("currency") or symbol_data.get("currency")
            if currency != "USD":
                raise ValueError("Sync supports USD holdings only")
            price = number(item.get("price"), "position price")
            cost = number(item.get("cost_basis"), "average purchase price")
            if price < 0 or cost < 0:
                raise ValueError("SnapTrade returned a negative position price or cost")
            symbols.add(symbol)
            holdings.append((symbol, int(quantity), cost, price))

        synced_positions = []
        with transaction.atomic():
            locked_portfolio = Portfolio.objects.select_for_update().get(pk=portfolio.pk)
            for symbol, quantity, cost, price in holdings:
                stock, _ = Stock.objects.get_or_create(
                    ticker=symbol, defaults={"name": symbol, "is_active": True}
                )
                position, _ = Position.objects.update_or_create(
                    portfolio=locked_portfolio,
                    stock=stock,
                    defaults={"quantity": quantity, "average_cost": cost, "current_price": price},
                )
                position.update_current_value()
                position.save()
                synced_positions.append(position)
            # Preserve historical rows while removing holdings no longer in the account.
            locked_portfolio.positions.exclude(stock__ticker__in=symbols).update(
                quantity=0, current_value=0, unrealized_pnl=0, unrealized_pnl_percent=0
            )
            locked_portfolio.current_cash = cash
            locked_portfolio.calculate_total_value()
            locked_portfolio.save(update_fields=["current_cash", "total_value", "updated_at"])
        portfolio.current_cash = locked_portfolio.current_cash
        portfolio.total_value = locked_portfolio.total_value
        return synced_positions

    def _credentials(self, portfolio, user_secret=None):
        secret = user_secret or portfolio.snaptrade_user_secret
        if not portfolio.snaptrade_user_id or not portfolio.snaptrade_account_id or not secret:
            raise ValueError("Portfolio must have SnapTrade user ID, account ID, and user secret")
        return dict(user_id=portfolio.snaptrade_user_id, user_secret=secret,
                    account_id=portfolio.snaptrade_account_id)

    def verify_paper_account(self, portfolio, user_secret=None):
        account = self.snaptrade.account_information.get_user_account_details(
            **self._credentials(portfolio, user_secret)
        ).body
        if not isinstance(account, Mapping) or account.get("institution_name", "").strip().casefold() != "alpaca paper":
            raise ValueError("Order submission requires a verified Alpaca Paper account")
        if account.get("id") != portfolio.snaptrade_account_id:
            raise ValueError("SnapTrade returned a different account")
        return account

    def _quote(self, portfolio, stock, user_secret=None):
        quotes = self.snaptrade.trading.get_user_account_quotes(
            **self._credentials(portfolio, user_secret), symbols=stock.ticker, use_ticker=True
        ).body
        if not quotes:
            raise ValueError("No quote returned for the stock")
        quote = quotes[0]
        symbol = quote.get("symbol") or {}
        if symbol.get("raw_symbol") != stock.ticker or not symbol.get("id"):
            raise ValueError("Quote does not identify the requested stock")
        if (symbol.get("currency") or {}).get("code") != "USD":
            raise ValueError("Paper orders support USD stocks only")
        return quote

    @staticmethod
    def safe_api_error(exc, portfolio):
        """Expose useful error details without signed URLs or stored credentials."""
        details = type(exc).__name__
        status = getattr(exc, "status", None)
        if status is not None:
            details += f" HTTP {status}"
        body = getattr(exc, "body", None)
        if isinstance(body, (str, bytes)):
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                body = None
        message = ""
        if isinstance(body, Mapping):
            for key in ("detail", "message", "error", "description"):
                if isinstance(body.get(key), str):
                    message = body[key]
                    break
        # Local validation errors contain no signed request data, but still scrub them.
        if not message and (isinstance(exc, (ValueError, TypeError)) or type(exc).__name__ == "SchemaValidationError"):
            message = str(exc)
        for secret in (settings.SNAPTRADE_CLIENT_SECRET, settings.SNAPTRADE_CLIENT_ID,
                       portfolio.snaptrade_user_secret, portfolio.snaptrade_user_id,
                       portfolio.snaptrade_account_id):
            if secret:
                message = message.replace(secret, "[redacted]")
        message = re.sub(r"https?://[^\s]+", "[redacted URL]", message)
        message = re.sub(r"(?i)(userSecret|consumerKey|signature|token|authorization)[\s:=]+[^\s,;]+",
                         r"\1=[redacted]", message)
        message = " ".join(message.split())[:500]
        return details + (f": {message}" if message else "")

    def submit_paper_order(self, portfolio, stock, action, units, limit_price, user_secret=None):
        units = Decimal(str(units))
        limit_price = Decimal(str(limit_price))
        if action not in ("BUY", "SELL") or not units.is_finite() or units <= 0 or units != units.to_integral_value():
            raise ValueError("Paper orders require BUY or SELL and a positive whole-share quantity")
        if not limit_price.is_finite() or limit_price <= 0 or limit_price != limit_price.quantize(Decimal("0.01")):
            raise ValueError("A positive limit price with at most two decimal places is required")
        credentials = self._credentials(portfolio, user_secret)
        self.verify_paper_account(portfolio, user_secret)
        quote = self._quote(portfolio, stock, user_secret)
        with transaction.atomic():
            locked = Portfolio.objects.select_for_update().get(pk=portfolio.pk)
            # Ambiguous submissions remain PENDING until manually reconciled.
            if locked.trades.filter(status__in=["PENDING", "SUBMITTED", "PARTIALLY_FILLED"]).exists():
                raise ValueError("Refresh or reconcile the outstanding portfolio order before submitting another")
            if action == "BUY" and units * limit_price > locked.current_cash:
                raise ValueError("Insufficient synced cash for the limit order")
            if action == "SELL":
                position = locked.positions.filter(stock=stock).first()
                if not position or position.quantity < units:
                    raise ValueError("Insufficient synced shares for the sell order")
            trade = Trade.objects.create(portfolio=locked, stock=stock, trade_type=action,
                quantity=int(units), price=limit_price, order_value=units * limit_price)
        try:
            result = self.snaptrade.trading.place_force_order(
                **credentials, action=action, universal_symbol_id=quote["symbol"]["id"],
                order_type="Limit", time_in_force="Day", units=float(units), price=float(limit_price),
            ).body
            order_id = result.get("brokerage_order_id") if isinstance(result, Mapping) else None
            if not order_id:
                raise ValueError("SnapTrade did not return a brokerage order ID")
        except Exception as exc:
            diagnostic = self.safe_api_error(exc, portfolio)
            trade.error_message = f"Submission outcome unknown ({diagnostic}); reconcile trade {trade.pk} with Alpaca Paper before retrying."
            trade.save(update_fields=["error_message"])
            raise RuntimeError(trade.error_message) from None
        trade.external_order_id = order_id
        trade.status = "SUBMITTED"
        trade.submitted_at = timezone.now()
        trade.save(update_fields=["external_order_id", "status", "submitted_at"])
        return trade

    def execute_buy_orders(self, portfolio, buy_list, total_value, user_secret=None):
        if not buy_list:
            return []
        allocation = Decimal(str(total_value)) / len(buy_list)
        trades = []
        for stock in buy_list:
            quote = self._quote(portfolio, stock, user_secret)
            price = Decimal(str(quote.get("ask_price") or quote.get("last_trade_price")))
            if not price.is_finite() or price <= 0:
                raise ValueError("No usable buy quote")
            limit = (price * Decimal("1.01")).quantize(Decimal("0.01"))
            units = int(allocation / limit)
            if units < 1:
                continue
            trade = self.submit_paper_order(portfolio, stock, "BUY", units, limit, user_secret)
            trades.append(trade)
            self.update_trade_status(trade, user_secret)
            if trade.status != "FILLED":
                break
        return trades

    def execute_sell_orders(self, portfolio, sell_list, user_secret=None):
        trades = []
        for stock in sell_list:
            position = portfolio.get_current_positions().filter(stock=stock).first()
            if not position:
                continue
            quote = self._quote(portfolio, stock, user_secret)
            price = Decimal(str(quote.get("bid_price") or quote.get("last_trade_price")))
            if not price.is_finite() or price <= 0:
                raise ValueError("No usable sell quote")
            limit = (price * Decimal("0.99")).quantize(Decimal("0.01"))
            trade = self.submit_paper_order(portfolio, stock, "SELL", position.quantity, limit, user_secret)
            trades.append(trade)
            self.update_trade_status(trade, user_secret)
            if trade.status != "FILLED":
                break
        return trades

    def update_trade_status(self, trade, user_secret=None):
        if not trade.external_order_id:
            raise ValueError("Trade has no brokerage order ID; reconcile its submission first")
        result = self.snaptrade.account_information.get_user_account_order_detail(
            **self._credentials(trade.portfolio, user_secret),
            brokerage_order_id=trade.external_order_id,
        ).body
        statuses = {
            "EXECUTED": "FILLED", "PARTIAL": "PARTIALLY_FILLED",
            "CANCELED": "CANCELLED", "PARTIAL_CANCELED": "CANCELLED",
            "EXPIRED": "CANCELLED", "REJECTED": "REJECTED", "FAILED": "REJECTED",
            "NONE": "SUBMITTED", "PENDING": "SUBMITTED", "ACCEPTED": "SUBMITTED",
            "QUEUED": "SUBMITTED", "TRIGGERED": "SUBMITTED", "ACTIVATED": "SUBMITTED",
            "CANCEL_PENDING": "SUBMITTED", "REPLACE_PENDING": "SUBMITTED",
        }
        status = statuses.get(result.get("status"))
        if status is None:
            raise ValueError("Unsupported brokerage order status; inspect the order in Alpaca Paper")
        quantity = Decimal(str(result.get("filled_quantity") or 0))
        if not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value() or quantity > trade.quantity:
            raise ValueError("Invalid or fractional fill quantity")
        if status == "FILLED" and quantity != trade.quantity:
            raise ValueError("Executed order does not report the complete fill quantity")
        price = Decimal(str(result.get("execution_price"))) if quantity else None
        if price is not None and (not price.is_finite() or price <= 0):
            raise ValueError("Invalid execution price")
        # Brokerage snapshots are authoritative: never apply a fill as a second cash/holding delta.
        with transaction.atomic():
            locked = Trade.objects.select_for_update().get(pk=trade.pk)
            if quantity < locked.filled_quantity:
                raise ValueError("Brokerage fill quantity moved backwards")
            self.sync_portfolio_positions(locked.portfolio, user_secret)
            locked.status = status
            locked.filled_quantity = int(quantity)
            locked.filled_price = price
            locked.order_value = quantity * price if price is not None else locked.order_value
            if status == "FILLED" and not locked.filled_at:
                locked.filled_at = timezone.now()
            locked.save()
        trade.refresh_from_db()
        trade.portfolio.refresh_from_db()
        return True

    def _get_current_stock_price(self, ticker: str) -> Optional[Decimal]:
        # Simplified implementation - in production, use real-time price feed
        try:
            from trading.services.massive_client import get_massive_client

            massive_client = get_massive_client()
            price = massive_client.get_price_on_date(ticker, datetime.now())
            return Decimal(str(price)) if price else None
        except (ValueError, TypeError) as e:
            logger.error(f"Error getting current price for {ticker}: {str(e)}")
            return None

    def get_available_cash_for_trading(self, portfolio: Portfolio) -> Decimal:
        # Reserve 5% cash buffer
        return portfolio.current_cash * Decimal("0.95")


def get_trading_executor() -> TradingExecutor:
    return TradingExecutor()
