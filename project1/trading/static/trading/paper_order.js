(() => {
  const form = document.getElementById('paper-order-form');
  if (!form) return;
  const portfolio = document.getElementById('id_portfolio');
  const action = document.getElementById('id_action');
  const units = document.getElementById('id_units');
  const limit = document.getElementById('id_limit_price');
  const refresh = document.getElementById('refresh-prices');
  const useQuote = document.getElementById('use-quote');
  const status = document.getElementById('quote-status');
  let snapshot = null;
  let requestNumber = 0;
  const usd = value => value === null || value === undefined ? 'Unavailable' :
    Number(value).toLocaleString(undefined, {style: 'currency', currency: 'USD'});
  const cents = value => {
    if (!/^\d+(\.\d{1,2})?$/.test(value)) return null;
    const [whole, fraction = ''] = value.split('.');
    return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'));
  };
  const money = amount => '$' + (amount / 100n).toLocaleString() + '.' +
    (amount % 100n).toString().padStart(2, '0');

  function updateCost() {
    const buying = action.value === 'BUY';
    document.getElementById('order-cost-label').textContent = buying ? 'Maximum share cost' : 'Minimum share proceeds if fully filled';
    const price = cents(limit.value);
    const quantity = /^\d+$/.test(units.value) ? BigInt(units.value) : null;
    const total = price !== null && price > 0n && quantity !== null && quantity > 0n ? price * quantity : null;
    document.getElementById('order-cost').textContent = total === null ? '—' : money(total);
    useQuote.textContent = buying ? 'Use ask as buy limit' : 'Use bid as sell limit';
    useQuote.disabled = !snapshot || !(buying ? snapshot.suggested_buy_limit : snapshot.suggested_sell_limit);
    let warning = '';
    if (snapshot && total !== null) {
      if (buying && snapshot.cash !== null && Number(total) / 100 > Number(snapshot.cash)) {
        warning = 'The maximum cost exceeds the available cash shown.';
      } else if (!buying && snapshot.shares_held !== null && Number(quantity) > Number(snapshot.shares_held)) {
        warning = 'The quantity exceeds the shares held in this portfolio.';
      }
    }
    document.getElementById('order-budget-warning').textContent = warning;
  }

  function clearSnapshot() {
    snapshot = null;
    for (const id of ['quote-ask', 'quote-bid', 'quote-last', 'account-cash', 'account-shares']) {
      document.getElementById(id).textContent = '—';
    }
    updateCost();
  }

  async function loadQuote() {
    const request = ++requestNumber;
    const selectedPortfolio = portfolio.value;
    clearSnapshot();
    if (!selectedPortfolio) {
      refresh.disabled = false;
      status.textContent = 'Choose a portfolio to load prices.';
      return;
    }
    refresh.disabled = true;
    status.textContent = 'Loading prices and account balance…';
    try {
      const url = new URL(form.dataset.quoteUrl, window.location.origin);
      url.searchParams.set('portfolio', selectedPortfolio);
      const response = await fetch(url, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
      const data = await response.json();
      if (request !== requestNumber) return;
      if (!response.ok) throw new Error(data.error || 'Could not load prices.');
      if (String(data.portfolio_id) !== selectedPortfolio) throw new Error('Portfolio changed. Refresh prices again.');
      snapshot = data;
      document.getElementById('quote-ask').textContent = usd(data.ask);
      document.getElementById('quote-bid').textContent = usd(data.bid);
      document.getElementById('quote-last').textContent = usd(data.last);
      document.getElementById('account-cash').textContent = usd(data.cash);
      document.getElementById('account-shares').textContent = data.shares_held === null ? 'Unavailable' : data.shares_held;
      status.textContent = 'Retrieved ' + new Date(data.retrieved_at).toLocaleString() + ' from Alpaca Paper via SnapTrade.';
      updateCost();
    } catch (error) {
      if (request === requestNumber) status.textContent = error.message || 'Could not load prices. Try Refresh prices again.';
    } finally {
      if (request === requestNumber) refresh.disabled = false;
    }
  }

  portfolio.addEventListener('change', loadQuote);
  refresh.addEventListener('click', loadQuote);
  action.addEventListener('change', updateCost);
  units.addEventListener('input', updateCost);
  limit.addEventListener('input', updateCost);
  useQuote.addEventListener('click', () => {
    if (!snapshot) return;
    limit.value = action.value === 'BUY' ? snapshot.suggested_buy_limit : snapshot.suggested_sell_limit;
    updateCost();
  });
  form.addEventListener('submit', () => {
    const button = document.getElementById('paper-order-button');
    button.disabled = true;
    button.textContent = 'Submitting paper order…';
  });
  updateCost();
  loadQuote();
})();
