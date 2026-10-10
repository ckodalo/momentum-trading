from django.urls import path
from trading.views import MomentumRankingView, RebalanceEventListView, TradingSignalListView, RefreshMomentumRankingsView
from trading.trade_views import PaperOrderView, PaperTradeDetailView, RefreshPaperTradeView, PaperOrderQuoteView
from trading.signal_views import GenerateSignalsView, SignalOrderView

app_name = "trading"
urlpatterns = [
    path("rankings/", MomentumRankingView.as_view(), name="rankings"),
    path("rankings/refresh/", RefreshMomentumRankingsView.as_view(), name="refresh_rankings"),
    path("rankings/<int:score_id>/trade/", PaperOrderView.as_view(), name="paper_order"),
    path("rankings/<int:score_id>/quote/", PaperOrderQuoteView.as_view(), name="paper_order_quote"),
    path("orders/<int:pk>/", PaperTradeDetailView.as_view(), name="paper_trade"),
    path("orders/<int:pk>/refresh/", RefreshPaperTradeView.as_view(), name="refresh_paper_trade"),
    path("signals/", TradingSignalListView.as_view(), name="signals"),
    path("signals/generate/", GenerateSignalsView.as_view(), name="generate_signals"),
    path("signals/<int:pk>/execute/", SignalOrderView.as_view(), name="execute_signal"),
    path("rebalances/", RebalanceEventListView.as_view(), name="rebalances"),
]
