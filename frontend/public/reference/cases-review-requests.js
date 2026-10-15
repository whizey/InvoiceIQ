
(() => {
  const escape = s => String(s ?? '').replace(
    /[&<>"']/g,
    c => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[c])
  );

  async function init() {
    const main = document.querySelector('main');

    if (!main || document.querySelector('#iq-review-requests')) {
      return;
    }

    const el = document.createElement('section');

    el.id = 'iq-review-requests';

    el.style.cssText =
      'margin:20px 28px;padding:20px;border:1px solid #ddd;' +
      'border-radius:9px;background:white;color:#111';

    el.innerHTML = `
      <h3>Approval requests</h3>
      <p>
        Only eligible high-value cases can create an approval hold.
        No email is sent when requesting approval.
      </p>
      <label>
        Reviewer key
        <input type="password" data-key autocomplete="off"
               placeholder="Paste reviewer credential">
      </label>
      <button type="button" data-refresh>
        Check eligibility
      </button>
      <div data-results role="status"
           style="margin-top:12px"></div>
    `;

    main.append(el);

    const results = el.querySelector('[data-results]');

    async function request(url, opts = {}) {
      const response = await fetch(url, {
        cache: 'no-store',
        credentials: 'same-origin',
        ...opts
      });

      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw Error(data.detail || 'HTTP ' + response.status);
      }

      return data;
    }

    async function refresh() {
      results.textContent = 'Checking live policy rules…';

      try {
        const data = await request('/api/review-requests');
        const rows = data.candidates || [];

        if (!rows.length) {
          results.textContent =
            'No high-value cases are in this authorized workspace.';
          return;
        }

        results.replaceChildren();

        for (const row of rows) {
          const line = document.createElement('div');

          line.style.cssText =
            'padding:12px 0;border-top:1px solid #ddd';

          const label = document.createElement('span');

          label.textContent =
            row.company_name + ' · ' +
            row.invoice_number + ' · ₹' +
            Number(row.outstanding_amount).toLocaleString('en-IN') +
            ' — ' + row.reason;

          line.append(label);

          if (row.eligible) {
            const btn = document.createElement('button');

            btn.type = 'button';
            btn.style.marginLeft = '12px';
            btn.textContent = 'Request approval';

            btn.onclick = async () => {
              const key = el.querySelector('[data-key]').value.trim();

              if (!key) {
                alert('Paste reviewer credential');
                return;
              }

              const note = prompt(
                'Reason for human approval:',
                'Review this high-value reminder before customer contact.'
              );

              if (note === null) return;

              if (note.trim().length < 3 || note.length > 1000) {
                alert('Reason must contain 3–1000 characters');
                return;
              }

              if (!confirm(
                'Create a pending approval? No email will be sent.'
              )) {
                return;
              }

              btn.disabled = true;

              try {
                const saved = await request('/api/review-requests', {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    'X-InvoiceIQ-Reviewer-Key': key
                  },
                  body: JSON.stringify({
                    case_id: row.case_id,
                    note: note.trim()
                  })
                });

                alert(saved.message);
                location.href = '/approvals';
              } catch (error) {
                alert('Not created: ' + error.message);
                btn.disabled = false;
                await refresh();
              }
            };

            line.append(btn);
          }

          results.append(line);
        }
      } catch (error) {
        results.textContent =
          'Eligibility unavailable: ' + error.message;
      }
    }

    el.querySelector('[data-refresh]').onclick = refresh;
    await refresh();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, {
      once: true
    });
  } else {
    init();
  }
})();
