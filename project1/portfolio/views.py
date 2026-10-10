from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.views.generic import DetailView, ListView
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.utils.decorators import method_decorator

from portfolio.models import Portfolio


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PortfolioListView(LoginRequiredMixin, PermissionRequiredMixin, ListView):
    model = Portfolio
    template_name = "portfolio/portfolio_list.html"
    context_object_name = "portfolios"
    permission_required = "portfolio.view_portfolio"
    paginate_by = 20


@method_decorator(xframe_options_sameorigin, name="dispatch")
class PortfolioDetailView(LoginRequiredMixin, PermissionRequiredMixin, DetailView):
    model = Portfolio
    template_name = "portfolio/portfolio_detail.html"
    context_object_name = "portfolio"
    permission_required = "portfolio.view_portfolio"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        if user.has_perm("portfolio.view_position"):
            context["positions"] = self.object.positions.select_related("stock").filter(quantity__gt=0)
        if user.has_perm("portfolio.view_trade"):
            context["trades"] = self.object.trades.select_related("stock")[:50]
        if user.has_perm("portfolio.view_performancemetric"):
            context["metrics"] = self.object.performance_metrics.all()[:30]
        return context
