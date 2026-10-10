# Momentum Trading App

This project is a Django application intended to automate a momentum-based stock trading strategy. It uses Massive for historical market data, SnapTrade for brokerage integration, and a database to track portfolios and trading activity.

## Trading workflow

1. **Choose a stock universe.** The current implementation uses a fixed list of 50 stocks.
2. **Fetch historical prices from Massive.** Calculate momentum as the price change from approximately 12 months ago to one month ago, deliberately skipping the most recent month:

   ```text
   momentum = (price_one_month_ago - price_twelve_months_ago) / price_twelve_months_ago
   ```

3. **Rank stocks into five groups (quintiles).** Stocks with the highest momentum belong to the top group; those with the lowest momentum belong to the bottom group.
4. **Generate trading signals.** Buy top-quintile stocks that the portfolio does not already hold. Sell held stocks that fall into the bottom quintile. Keep existing holdings in the middle groups.
5. **Submit orders through SnapTrade.** Submit sell orders first, then allocate the buying budget equally among new buy candidates.
6. **Track portfolio activity.** Store positions, cash balances, trade records, momentum scores, signals, rebalance events, and performance metrics.
7. **Repeat periodically.** The strategy defaults to weekly rebalancing and also supports a monthly interval.

The strategy aims to capture persistence in relative stock performance by buying recent relative winners and exiting holdings that become relative losers. Existing top-quintile holdings are retained rather than automatically resized to equal weights.

## Project structure

The Django project lives in `project1/`.

| Component | Responsibility |
| --- | --- |
| `project1/momentum_trader/` | Django settings and application URL configuration |
| `project1/trading/` | Stocks, historical prices, momentum scores, trading signals, and rebalance events |
| `project1/trading/services/massive_client.py` | Historical market data access |
| `project1/trading/services/momentum_calculator.py` | Momentum calculations and stock rankings |
| `project1/trading/services/strategy_engine.py` | Signal generation and rebalance orchestration |
| `project1/trading/services/snaptrade_client.py` | Brokerage position synchronization and order operations |
| `project1/portfolio/` | Portfolios, holdings, trade records, and performance metrics |

## Current state

The application includes a portfolio list and detail page, momentum rankings, trading signals, and rebalance history. Normal page reads display saved database records. The rankings page has explicit actions to refresh market data and submit manual buy/sell orders to a verified Alpaca Paper account. Backtesting, full automation, and asynchronous rebalance continuation still need further work.

Weekly and monthly rebalancing are interval checks in the strategy code; a recurring job scheduler is not yet provided. The backtest method is a placeholder. View tests cover permissions, portfolio data isolation, filtering, pagination, and rendering.

## Local setup (Windows PowerShell)

From the repository root, with Python installed:

```powershell
cd project1
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
.\.venv\Scripts\python.exe manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in. SQLite is configured locally. API credentials are optional for browsing saved records; market data and brokerage services require their respective credentials in `.env`.

## Application pages

- `/portfolios/`: portfolio overview; click a name for holdings, the latest 50 trades, and the latest 30 performance snapshots.
- `/trading/rankings/`: latest saved momentum rankings, with calculation date and quintile filters. Momentum is displayed as a decimal return (0.25 means 25%). **Refresh rankings** fetches historical prices for the fixed 50-stock universe, recalculates today's momentum scores, saves them, and opens today's rankings with filters cleared. It may take a minute depending on API access and rate limits. The page reports success, skipped stocks, or failure. **Reset** only clears filters. Refresh submits no orders.
- `/trading/signals/`: saved signals, filterable by date, signal type, and execution status.
- `/trading/rebalances/`: saved rebalance runs, filterable by status.
- `/admin/`: manage database records and user permissions.

A superuser can access all pages. Other users need the matching model view permissions in Django admin. Portfolio details require `view_portfolio`; holdings, trades, and performance sections additionally require `view_position`, `view_trade`, and `view_performancemetric`. Trading pages require `view_momentumscore`, `view_tradingsignal`, and `view_rebalanceevent`, respectively. The current models have no user ownership field, so permissions provide application-wide access. Signals and rebalance events are not linked to individual portfolios.

Run checks and tests from `project1`:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test portfolio trading
```

## Fetch momentum rankings

You can also refresh from `/trading/rankings/` using **Refresh rankings**. A superuser can use the button; other users need `view_momentumscore`, `add_momentumscore`, `change_momentumscore`, and `add_stock` permissions. The action uses a CSRF-protected POST request and the same command described below. It runs during the request rather than as a background job. Ordinary page views and filtering still read saved scores only.

From `project1`, set `MASSIVE_API_KEY` in `.env` and run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py pull_massive_data
```

By default, the command uses the same fixed 50-stock universe as the strategy, defined in `MassiveAPIClient.get_sp500_tickers()`. This is a demonstration list, not a live list of all S&P 500 constituents. It creates stock records, fetches historical momentum prices, saves scores, and assigns ranks and quintiles for today's date. Refresh `/trading/rankings/` afterward. Repeating the command updates scores for the same stock and calculation date. It does not create holdings, signals, rebalance events, or brokerage orders. Stocks without sufficient available data are skipped, so fewer than 50 scores may be saved.

Override the default universe with `--tickers`, or choose a historical calculation date with `--date`:

```powershell
.\.venv\Scripts\python.exe manage.py pull_massive_data --tickers AAPL NVDA MSFT --date 2026-10-04
```

Missing price data is reported; if no scores can be saved, the command exits with an error. Rankings include all saved scores for the selected calculation date. Existing scores for skipped stocks remain unchanged. With fewer than five scores, the current quintile method places all scores in quintile 1; use a larger stock universe for meaningful five-group comparisons.

## Sync a connected SnapTrade portfolio

Set `SNAPTRADE_CLIENT_ID` and `SNAPTRADE_CLIENT_SECRET` (the SnapTrade Consumer Key) in `project1/.env`. This integration uses Commercial API key authentication. On the existing portfolio in Django admin, save the SnapTrade user ID, user secret, and connected account ID. For paper trading, connect Alpaca Paper through SnapTrade.

From `project1`, replace `1` with the portfolio ID shown in its admin edit URL:

```powershell
.\.venv\Scripts\python.exe manage.py sync_snaptrade_portfolio 1
```

The command reads the connected account's cash and positions, saves the USD balance and holdings, clears holdings no longer present, and recalculates total value. Refresh the portfolio detail page afterward. It submits no orders and leaves initial cash and trade history unchanged. Both responses are validated before any database changes. The current integer quantity model supports long, whole-share holdings only; fractional or short holdings stop the sync with an error. Missing prices or purchase costs and non-USD holdings also stop the sync. SnapTrade data freshness depends on the account's data-access plan; this command does not request a paid brokerage refresh. Order execution remains unfinished.

## Submit and track a paper order

From `/trading/rankings/`, click **Buy** or **Sell** beside a stock. Choose an active, configured portfolio, enter whole shares and a USD limit price, then click **Submit paper order**. The selected ticker comes from that ranking row. Before submission, the app verifies Alpaca Paper and synchronizes cash and holdings; insufficient cash, insufficient shares, and unresolved prior orders block submission. Manual orders can be placed for any ranked stock, independent of the strategy's quintile signals.

The order detail page shows requested shares, limit price, status, filled shares, and average fill price. Click **Refresh order status** to check the brokerage and synchronize balances and holdings without submitting another order. Existing trade history statuses link to these pages. If an order has no brokerage ID, check Alpaca Paper before reconciling or retrying it.

The trade form loads bid, ask, last-traded price, available USD cash, and shares held from the selected Alpaca Paper account through SnapTrade. Changing portfolios or clicking **Refresh prices** reloads this preview without changing the database or placing orders. Quotes may be delayed; the displayed retrieval time is the app's fetch time, not a market quote timestamp (the quote endpoint supplies none). Missing prices or balances display as unavailable. **Use ask as buy limit** / **Use bid as sell limit** copies a price only when clicked; fetching a quote never overwrites an entered limit.

As quantity or limit price changes, the form shows maximum share cost for buys or minimum share proceeds if fully filled for sells, excluding fees. It flags costs exceeding the displayed cash or sale quantities exceeding the displayed holdings. These are previews; submission still rechecks the account, cash, and holdings. Prices are loaded on selection and explicit refresh, without background polling.

Superusers can trade. Other users need `trading.view_momentumscore`, `portfolio.view_portfolio`, `portfolio.view_trade`, and the new `portfolio.execute_paper_trade` permission (**Can submit and refresh paper trades**). Apply migrations to create the permission. Permissions still apply across portfolios because the models have no user ownership field. The trade form loads a read-only quote and account preview for the selected portfolio; opening an order detail page reads saved records only. Submission and order-status refresh require CSRF-protected POST requests.

Paper execution uses the saved Commercial SnapTrade credentials and requires account metadata to identify the connected institution as Alpaca Paper. Orders use positive whole-share quantities and day limit prices. The one-order test command allows one share and a limit of at most USD 500. Choose a limit price based on the current Alpaca Paper quote; an order may remain open if that limit cannot be met. From `project1`:

```powershell
.\.venv\Scripts\python.exe manage.py paper_order 1 AAPL --limit-price 250.00
.\.venv\Scripts\python.exe manage.py refresh_snaptrade_orders 1
```

The first command submits an order and prints the local trade ID. It does not claim the order filled. The second reads brokerage order details, stores partial/full fill quantities and execution prices, and synchronizes cash and positions from the brokerage snapshot. Refresh the portfolio detail page to see the result. Repeat the refresh command later if the order is still submitted. An order may wait until market hours or expire. Never rerun the submission command merely to check status.

Only one unresolved order is allowed per portfolio. If submission times out or returns no brokerage order ID, the trade stays pending because the broker may have accepted it. Inspect Alpaca Paper and reconcile that trade before retrying; do not mark it rejected unless you have confirmed no order exists. Brokerage snapshots are authoritative, so fills are not separately added to holdings or deducted from cash. This prevents double-counting after a manual sync. Cash and position freshness still depends on SnapTrade's data plan.

The strategy passes saved user secrets, uses whole-share limit orders, stops when an order is not fully filled, marks only confirmed fills as executed, and budgets purchases from synchronized cash after confirmed sales. Full asynchronous rebalance continuation and linking later fills back to signals/rebalance events remain unfinished; the refresh command updates trades and portfolio records, not those event records.
