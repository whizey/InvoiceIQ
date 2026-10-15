/* Invalidate document-local records when another tab changes workspace session. */
window.addEventListener('storage', event => {
  if (event.key === 'invoiceiq-session-change') {
    document.documentElement.style.visibility = 'hidden';
    window.location.reload();
  }
});
