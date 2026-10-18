
/* Read-only recovery policy activity; not human approvals. */
(() => {
  'use strict';

  const findMain = root => {
    let main = root.querySelector?.('main');
    if (main?.querySelector('[aria-label="Approval totals"]') &&
        main.querySelector('[aria-label="Queue"]')) return main;

    for (const element of root.querySelectorAll?.('*') || []) {
      if (element.shadowRoot) {
        main = findMain(element.shadowRoot);
        if (main) return main;
      }
    }
    return null;
  };

  const formatMoney = value => {
    const m = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(
      String(value ?? '')
    );
    if (!m) return '—';

    const paise = BigInt(m[2]) * 100n +
      BigInt((m[3] || '').padEnd(2, '0'));
    const sign = m[1] ? '−' : '';

    for (const [scale, suffix] of [
      [1000000000n, 'Cr'],
      [10000000n, 'L'],
      [100000n, 'k']
    ]) {
      if (paise >= scale) {
        const rounded = (paise * 100n + scale / 2n) / scale;
        const decimals = String(rounded % 100n)
          .padStart(2, '0')
          .replace(/0+$/, '');

        return sign + '₹' + rounded / 100n +
          (decimals ? '.' + decimals : '') + suffix;
      }
    }

    const decimals = String(paise % 100n)
      .padStart(2, '0')
      .replace(/0+$/, '');

    return sign + '₹' + paise / 100n +
      (decimals ? '.' + decimals : '');
  };

  const label = value => String(value ?? '—')
    .replaceAll('_', ' ')
    .toLowerCase()
    .replace(/\b\w/g, c => c.toUpperCase());

  const get = async path => {
    const response = await fetch('/api/workspace/' + path, {
      cache: 'no-store',
      credentials: 'same-origin'
    });
    const data = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw Error(data.detail || 'HTTP ' + response.status);
    }
    return data;
  };

  const csvCell = value => {
    let text = String(value ?? '');
    if (/^[\s]*[=+@\-\t\r]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };

  function mount(main) {
    if (main.querySelector('#iq-policy-activity')) return;

    const section = document.createElement('section');
    section.id = 'iq-policy-activity';
    section.className = 'card';
    section.style.cssText = 'padding:20px;min-width:0';

    section.innerHTML = `
      <div style="display:flex;justify-content:space-between;
                  flex-wrap:wrap;gap:12px">
        <div>
          <h2 style="font-size:16px;margin:0">
            Automated policy activity
          </h2>
          <p style="font-size:12px;color:#666">
            Recorded recovery actions and policy decisions.
            <strong>These are not human approvals.</strong>
          </p>
        </div>
        <button class="btn" type="button" data-reload>
          Refresh
        </button>
      </div>

      <p data-status role="status"
         style="font-size:12px;color:#666">Loading…</p>

      <div data-totals style="display:flex;flex-wrap:wrap;
                              gap:12px;margin:12px 0"></div>

      <div style="display:flex;flex-wrap:wrap;gap:8px;
                  margin-bottom:12px">
        <input data-search aria-label="Search activity"
               placeholder="Search company, invoice or action"
               style="flex:1 1 200px;padding:9px;
                      border:1px solid #ddd;border-radius:6px">
        <select data-filter aria-label="Filter action status"
                style="padding:8px;border:1px solid #ddd">
          <option value="">All statuses</option>
          <option value="EXECUTED">Executed</option>
          <option value="PENDING_APPROVAL">Pending approval</option>
          <option value="POLICY_REJECTED">Policy rejected</option>
        </select>
        <button class="btn" type="button" data-export>
          Export CSV
        </button>
      </div>

      <div style="overflow-x:auto">
        <table class="t" style="min-width:720px">
          <thead>
            <tr>
              <th>Company / Invoice</th>
              <th>Action</th>
              <th>Status</th>
              <th>Policy result</th>
              <th>Outstanding</th>
              <th>Date</th>
              <th>Case</th>
            </tr>
          </thead>
          <tbody data-rows></tbody>
        </table>
      </div>
    `;

    const wrapper = [...main.children].find(element =>
      element.tagName === 'DIV' &&
      element.querySelector('[aria-label="Approval totals"]')
    );

    (wrapper || main).append(section);

    const q = selector => section.querySelector(selector);
    let rows = [];
    let loaded = false;
    let busy = false;

    function render() {
      const totals = q('[data-totals]');
      totals.replaceChildren();

      const metrics = [
        ['Recorded actions', rows.length],
        ['Executed', rows.filter(r => r.status === 'EXECUTED').length],
        ['Policy approved', rows.filter(r => r.decision === 'APPROVED').length],
        ['Policy rejected', rows.filter(r => r.decision === 'REJECTED').length]
      ];

      for (const [name, value] of metrics) {
        const card = document.createElement('div');
        card.style.cssText =
          'border:1px solid #ddd;border-radius:7px;' +
          'padding:10px;min-width:120px';

        const heading = document.createElement('div');
        heading.style.cssText = 'font-size:11px;color:#666';
        heading.textContent = name;

        const number = document.createElement('strong');
        number.style.cssText =
          'display:block;font-size:21px;margin-top:4px';
        number.textContent = loaded ? String(value) : '—';

        card.append(heading, number);
        totals.append(card);
      }

      const search = q('[data-search]').value.toLowerCase();
      const filter = q('[data-filter]').value;

      const filtered = rows.filter(row =>
        (!filter || row.status === filter) &&
        (!search || [
          row.company, row.invoice, row.action,
          row.status, row.decision
        ].join(' ').toLowerCase().includes(search))
      );

      const body = q('[data-rows]');
      body.replaceChildren();

      if (!filtered.length) {
        const tr = body.insertRow();
        const td = tr.insertCell();
        td.colSpan = 7;
        td.textContent = loaded
          ? 'No matching action records.'
          : busy ? 'Loading action records…' : 'Activity unavailable.';
      }

      for (const row of filtered) {
        const tr = body.insertRow();
        const cells = [
          row.company + ' · ' + row.invoice,
          label(row.action),
          label(row.status),
          label(row.decision),
          formatMoney(row.balance),
          row.date
            ? new Date(row.date).toLocaleString('en-IN')
            : '—'
        ];

        for (const value of cells) {
          const td = tr.insertCell();
          td.textContent = value;
          td.style.whiteSpace = 'normal';
        }

        const td = tr.insertCell();
        const link = document.createElement('a');
        link.href = '/cases/' + encodeURIComponent(row.caseId);
        link.target = '_top';
        link.textContent = 'View';
        td.append(link);
      }

      q('[data-export]').disabled = !loaded || !rows.length;
    }

    async function load() {
      if (busy) return;
      busy = true;
      q('[data-reload]').disabled = true;
      q('[data-status]').textContent =
        'Loading authenticated case history…';
      render();

      try {
        const response = await get('cases');

        if (!Array.isArray(response.cases)) {
          throw Error('Unexpected case response');
        }

        const cases = response.cases;
        const caseBadge = main.parentElement?.querySelector('a[href="/cases"] span.mono:last-child');
        if (caseBadge) caseBadge.textContent = String(cases.length);
        const result = [];
        let failed = 0;
        let next = 0;

        await Promise.all(
          Array.from(
            {length: Math.min(4, cases.length)},
            async () => {
              while (next < cases.length) {
                const item = cases[next++];

                try {
                  const detail = await get(
                    'cases/' + encodeURIComponent(item.id)
                  );

                  if (!Array.isArray(detail.actions)) {
                    throw Error('Action history unavailable');
                  }

                  for (const action of detail.actions) {
                    const decisions =
                      Array.isArray(action.policy_decisions)
                        ? action.policy_decisions
                        : [];

                    const latest = decisions.slice().sort(
                      (a, b) => String(b.evaluated_at || '')
                        .localeCompare(String(a.evaluated_at || ''))
                    )[0];

                    result.push({
                      caseId: item.id,
                      company: String(item.company_name ?? (typeof item.company === 'object' ? item.company?.name : item.company) ?? '—'),
                      invoice: String(item.invoice_number ?? (typeof item.invoice === 'object' ? item.invoice?.invoice_number : item.invoice) ?? '—'),
                      balance: item.outstanding_amount ?? item.outstanding ?? null,
                      action: action.action_type,
                      status: action.status,
                      decision: latest?.decision || 'NOT_RECORDED',
                      date: action.created_at
                    });
                  }
                } catch {
                  failed++;
                }
              }
            }
          )
        );

        rows = result.sort(
          (a, b) => String(b.date || '')
            .localeCompare(String(a.date || ''))
        );

        loaded = true;

        q('[data-status]').textContent =
          `${rows.length} recorded actions from ` +
          `${cases.length - failed} accessible cases. ` +
          (failed
            ? `${failed} case details unavailable; counts may be incomplete.`
            : 'Human approvals are counted separately above.');

      } catch (error) {
        loaded = false;
        rows = [];
        q('[data-status]').textContent =
          'Activity unavailable: ' + error.message +
          '. Sign in to the InvoiceIQ workspace and refresh.';
      } finally {
        busy = false;
        q('[data-reload]').disabled = false;
        render();
      }
    }

    q('[data-search]').addEventListener('input', render);
    q('[data-filter]').addEventListener('change', render);
    q('[data-reload]').addEventListener('click', load);

    q('[data-export]').addEventListener('click', () => {
      if (!loaded || !rows.length) return;

      const data = [
        ['Company', 'Invoice', 'Case ID', 'Action',
         'Status', 'Policy decision', 'Outstanding INR', 'Date'],
        ...rows.map(r => [
          r.company, r.invoice, r.caseId, r.action,
          r.status, r.decision, r.balance, r.date
        ])
      ];

      const blob = new Blob([
        '\ufeff' + data.map(line =>
          line.map(csvCell).join(',')
        ).join('\r\n')
      ], {type: 'text/csv;charset=utf-8'});

      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'invoiceiq-policy-activity.csv';
      a.click();

      setTimeout(() => URL.revokeObjectURL(url), 1000);
    });

    load();
  }

  let attempts = 0;

  const timer = setInterval(() => {
    const main = findMain(document);

    if (main) {
      clearInterval(timer);
      mount(main);
    } else if (++attempts >= 150) {
      clearInterval(timer);
    }
  }, 100);
})();
