(() => {
  const dialog = document.getElementById('workspace-dialog');
  const frame = document.getElementById('workspace-frame');
  const close = document.getElementById('dialog-close');
  const status = document.getElementById('dialog-status');
  let changed = false;
  let submitting = false;
  let opener = null;

  const portfolioForm = document.getElementById('workspace-portfolio-form');
  portfolioForm?.querySelector('select').addEventListener('change', () => portfolioForm.requestSubmit());
  const refreshForm = document.getElementById('refresh-rankings-form');
  refreshForm?.addEventListener('submit', () => {
    const button = document.getElementById('refresh-rankings-button');
    button.disabled = true;
    button.textContent = 'Refreshing rankings…';
    refreshForm.setAttribute('aria-busy', 'true');
  });
  if (!dialog || typeof dialog.showModal !== 'function') return;

  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-workspace-dialog]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    const url = new URL(link.href, window.location.origin);
    if (url.origin !== window.location.origin) return;
    event.preventDefault();
    opener = link;
    changed = false;
    submitting = false;
    close.disabled = false;
    document.getElementById('dialog-title').textContent = link.dataset.workspaceDialog;
    document.getElementById('dialog-full-page').href = url.href;
    frame.title = link.dataset.workspaceDialog;
    status.textContent = 'Loading…';
    url.searchParams.set('embed', '1');
    frame.src = url.href;
    dialog.showModal();
  });

  frame.addEventListener('load', () => {
    if (!dialog.open) return;
    submitting = false;
    close.disabled = false;
    try {
      const doc = frame.contentDocument;
      if (!doc?.body) throw new Error('The page could not be loaded.');
      doc.body.classList.add('embedded');
      const title = doc.querySelector('h1');
      if (title) document.getElementById('dialog-title').textContent = title.textContent;
      document.getElementById('dialog-full-page').href = frame.contentWindow.location.href;
      status.textContent = changed ? 'Action finished. Close to update the workspace.' : 'Review details below. Orders require explicit submission.';
      doc.addEventListener('click', event => {
        const link = event.target.closest('a');
        if (!link || link.target === '_blank') return;
        const destination = new URL(link.href, window.location.origin);
        if (destination.origin === window.location.origin && destination.pathname === window.location.pathname) {
          event.preventDefault();
          if (!submitting) dialog.close();
        }
      });
      doc.addEventListener('submit', event => {
        if (event.target.method.toLowerCase() !== 'post') return;
        changed = true;
        submitting = true;
        close.disabled = true;
        status.textContent = 'Processing… Please wait for the result before closing.';
      }, true);
      doc.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          event.preventDefault();
          if (!submitting) dialog.close();
        }
      });
    } catch {
      status.textContent = 'Could not display this page. Use Open full page to continue.';
    }
  });
  close.addEventListener('click', () => { if (!submitting) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (submitting) event.preventDefault(); });
  dialog.addEventListener('close', () => {
    frame.src = 'about:blank';
    if (changed) window.location.reload();
    else opener?.focus();
  });
})();
