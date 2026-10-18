(() => {
  'use strict';

  const approvals = location.pathname.includes('/approvals/');
  const endpoint = approvals
    ? '/api/workspace/reviews'
    : '/api/workspace/cases';

  const state = {
    rows: [],
    ready: false,
    busy: false,
    reviewer: '',
    search: '',
    range: 'all',
    mode: 'board',
    selected: '',
    detail: null
  };

  const money = value =>
    value == null || !Number.isFinite(Number(value))
      ? 'Unavailable'
      : new Intl.NumberFormat('en-IN', {
          style: 'currency',
          currency: 'INR',
          maximumFractionDigits: 0
        }).format(Number(value));

  const esc = value =>
    String(value ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    })[c]);

  const day = value =>
    value ? String(value).slice(0, 10) : 'Not recorded';

  const daysOld = value => {
    const time = Date.parse(value || '');
    return Number.isFinite(time)
      ? Math.max(0, Math.floor((Date.now() - time) / 86400000))
      : null;
  };

  const median = values => {
    const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
    return sorted.length
      ? (sorted[(sorted.length - 1) >> 1] +
         sorted[sorted.length >> 1]) / 2
      : null;
  };

  const closed = record =>
    /^(RESOLVED|RECOVERED|CLOSED|COMPLETED)$/.test(record.status || '');

  const bucket = record =>
    closed(record) ? 'Resolved'
    : record.pending ? 'Awaiting approval'
    : /ACTION|CONTACTED|FOLLOW/.test(record.status || '')
      ? 'Action taken'
    : /INVESTIGAT|REVIEW|PROGRESS/.test(record.status || '')
      ? 'Investigating'
    : 'New';

  function find(root, selector) {
    const element = root.querySelector?.(selector);
    if (element) return element;

    for (const node of root.querySelectorAll?.('*') || []) {
      if (node.shadowRoot) {
        const found = find(node.shadowRoot, selector);
        if (found) return found;
      }
    }

    return null;
  }

  let mountPoint;
  let main;
  let header;
  let notice;

  function announce(message) {
    if (notice) notice.textContent = message;
  }

  function shown() {
    return state.rows.filter(record => {
      const text = [
        record.company,
        record.invoice,
        record.status,
        record.action,
        record.id
      ].join(' ').toLowerCase();

      const matches = text.includes(state.search.toLowerCase());

      const inRange = approvals ||
        state.range === 'all' ||
        (
          daysOld(record.opened) !== null &&
          daysOld(record.opened) <= 90
        );

      return matches && inRange;
    });
  }

  function tile(record) {
    return `
      <button type="button"
        data-detail="${esc(record.case_id || record.id)}"
        class="btn"
        style="display:block;width:100%;height:auto;white-space:normal;
               text-align:left;margin-top:9px;padding:13px">
        <span class="mono" style="font-size:11px;color:#666">
          ${esc(record.invoice)}
        </span>
        <div style="font-size:14px;font-weight:600;margin:7px 0">
          ${esc(record.company)}
        </div>
        <strong class="num" style="font-size:20px">
          ${money(record.outstanding)}
        </strong>
        <div style="font-size:11px;margin-top:8px;color:#666">
          ${esc(record.status || record.action || 'Unspecified')}
          · ${day(record.opened || record.created)}
        </div>
      </button>`;
  }

  function caseBoard(rows) {
    const groups = [
      'New',
      'Investigating',
      'Awaiting approval',
      'Action taken',
      'Resolved'
    ];

    return `
      <section aria-label="Live case board" style="overflow-x:auto">
        <div style="display:grid;grid-template-columns:
                    repeat(5,minmax(220px,1fr));
                    gap:14px;min-width:1120px">
          ${groups.map(group => {
            const members = rows.filter(r => bucket(r) === group);
            const exposure = members.reduce(
              (total, r) => total + Number(r.outstanding || 0), 0
            );

            return `
              <div style="background:#EDEDEB;border-radius:10px;
                          padding:12px;min-height:340px">
                <div style="display:flex;justify-content:space-between;
                            gap:5px;align-items:baseline;padding:4px">
                  <strong style="font-size:13px">
                    ${group}
                    <span class="mono" style="color:#666;font-weight:400">
                      ${members.length}
                    </span>
                  </strong>
                  <span class="mono" style="font-size:11px">
                    ${money(exposure)}
                  </span>
                </div>

                ${members.length
                  ? members.map(tile).join('')
                  : `<div style="border:1px dashed #bbb;margin-top:12px;
                                 padding:16px;border-radius:8px;
                                 color:#777;font-size:12px">
                       No cases
                     </div>`}
              </div>`;
          }).join('')}
        </div>
      </section>`;
  }

  function table(rows) {
    return `
      <section class="card" style="overflow-x:auto">
        <table class="t">
          <thead>
            <tr>
              <th>Company</th>
              <th>Invoice</th>
              <th>${approvals ? 'Proposed action' : 'Case status'}</th>
              <th>Outstanding</th>
              <th>Date</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            ${rows.map(r => `
              <tr>
                <td>${esc(r.company)}</td>
                <td class="mono">${esc(r.invoice)}</td>
                <td>${esc(r.action || r.status)}</td>
                <td class="num">${money(r.outstanding)}</td>
                <td>${day(r.created || r.opened)}</td>
                <td>
                  <button type="button" class="btn"
                          data-detail="${esc(r.case_id || r.id)}">
                    Details
                  </button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
        ${rows.length ? '' : `
          <p style="padding:18px;color:#666">
            No records in this selection.
          </p>`}
      </section>`;
  }

  function kpi(label, value, note) {
    return `
      <div style="padding:16px 20px;border-right:1px solid #F0F0EE">
        <div class="lbl">${label}</div>
        <div class="num" style="font-size:24px;font-weight:600;margin-top:6px">
          ${value}
        </div>
        <div style="font-size:12px;color:#6B6B6B;margin-top:5px">
          ${note}
        </div>
      </div>`;
  }

  function stats(rows) {
    if (!state.ready) {
      return `
        <section class="card" style="padding:20px">
          Waiting for authenticated live data…
        </section>`;
    }

    if (approvals) {
      const ages = rows.map(r => daysOld(r.created));
      const oldest = Math.max(...ages.filter(Number.isFinite));
      const exposure = rows.reduce(
        (sum, r) => sum + Number(r.outstanding || 0), 0
      );

      return `
        <section class="card" aria-label="Approval totals"
          style="display:grid;grid-template-columns:
                 repeat(auto-fit,minmax(160px,1fr))">
          ${kpi('Waiting', rows.length,
            money(exposure) + ' balance affected')}
          ${kpi('Oldest waiting',
            Number.isFinite(oldest) ? oldest + ' d' : '—',
            'Based on creation timestamp')}
          ${kpi('Decided, 30 d', '—',
            'Decision history not available from this API')}
          ${kpi('Median time to decide', '—',
            'Decision timestamps not available')}
        </section>`;
    }

    const active = rows.filter(r => !closed(r));
    const ages = active.map(r => daysOld(r.opened));
    const middle = median(ages);
    const oldest = Math.max(...ages.filter(Number.isFinite));

    return `
      <section class="card" aria-label="Case totals"
        style="display:grid;grid-template-columns:
               repeat(auto-fit,minmax(160px,1fr))">
        ${kpi('Open cases', active.length,
          new Set(active.map(r => r.company)).size + ' companies')}
        ${kpi('Exposure in cases',
          money(active.reduce(
            (sum, r) => sum + Number(r.outstanding || 0), 0
          )),
          'Outstanding on open cases')}
        ${kpi('Overdue, no case', '—',
          'Requires cross-invoice reconciliation')}
        ${kpi('Median case age',
          middle === null ? '—' : Math.round(middle) + ' d',
          Number.isFinite(oldest) ? 'Oldest ' + oldest + ' d'
                                  : 'Age unavailable')}
        ${kpi('Resolved, 30 d', '—',
          'Closure timestamps not provided in list')}
      </section>`;
  }

  function detailHTML(data) {
    if (!data) return '';

    const invoice = data.invoice || {};
    const actions = Array.isArray(data.actions) ? data.actions : [];
    const audits = Array.isArray(data.audit_logs) ? data.audit_logs : [];

    return `
      <section class="card" style="margin-top:18px;overflow:hidden">
        <div style="padding:18px 22px;border-bottom:1px solid #E5E5E2;
                    display:flex;justify-content:space-between;gap:8px">
          <div>
            <div class="lbl">Real case details</div>
            <h2 class="mono" style="font-size:16px;margin:8px 0">
              ${esc(invoice.invoice_number || data.id)}
            </h2>
            <div style="font-size:13px;color:#666">
              ${esc(data.status)} · Opened ${day(data.opened_at)}
              · ${money(data.revenue_at_risk)} revenue at risk
            </div>
          </div>
          <button type="button" class="btn" data-close="1">Close</button>
        </div>

        <div style="display:flex;flex-wrap:wrap">
          <div style="flex:1 1 360px;padding:20px">
            <div class="lbl" style="margin-bottom:12px">
              Recorded actions and policy decisions
            </div>
            ${actions.length
              ? actions.map(action => `
                <div style="border-bottom:1px solid #eee;padding:10px 0">
                  <strong>${esc(action.action_type)}</strong>
                  <span class="pill">${esc(action.status)}</span>
                  <div style="font-size:11px;color:#666;margin-top:4px">
                    ${day(action.created_at)}
                  </div>
                  ${(action.policy_decisions || []).map(policy => `
                    <p style="font-size:12px">
                      ${esc(policy.policy_name)}:
                      ${esc(policy.decision)} —
                      ${esc(policy.reason)}
                    </p>`).join('')}
                </div>`).join('')
              : '<p>No recorded actions.</p>'}
          </div>

          <div style="flex:1 1 360px;padding:20px;
                      border-left:1px solid #eee">
            <div class="lbl" style="margin-bottom:12px">
              Case audit timeline
            </div>
            ${audits.length
              ? audits.map(audit => `
                <div style="padding:10px 0;border-bottom:1px solid #eee">
                  <div style="font-size:12px;font-weight:600">
                    ${esc(audit.display_event_type || audit.event_type)}
                    · ${day(audit.occurred_at)}
                  </div>
                  <div style="font-size:12px;color:#666">
                    ${esc(audit.description)}
                  </div>
                </div>`).join('')
              : '<p>No audit events recorded.</p>'}
          </div>
        </div>
      </section>`;
  }

  function queue(rows) {
    return `
      <div style="display:flex;flex-wrap:wrap;gap:20px;
                  align-items:flex-start">
        <section class="card" style="flex:1 1 290px;overflow:hidden">
          <div class="lbl" style="padding:15px 18px;
                                  border-bottom:1px solid #eee">
            Waiting for reviewer
          </div>
          ${rows.length
            ? rows.map(r => `
              <button type="button" data-detail="${esc(r.case_id)}"
                style="display:block;text-align:left;width:100%;
                       padding:16px 18px;border:0;
                       border-bottom:1px solid #eee;
                       background:${state.selected === r.case_id
                         ? '#f4f4f2' : '#fff'};cursor:pointer">
                <span class="mono">${esc(r.invoice)}</span>
                <small>${day(r.created)}</small>
                <div style="font-weight:600;margin:8px 0">
                  ${esc(r.action)}
                </div>
                <div style="font-size:12px;color:#666">
                  ${esc(r.company)} · ${money(r.outstanding)}
                </div>
              </button>`).join('')
            : '<p style="padding:18px;color:#666">No pending reviews in the authorized scope.</p>'}
        </section>

        <section class="card" style="flex:3 1 570px;padding:22px">
          <div class="lbl">Review evidence</div>
          <p style="font-size:13px;color:#555">
            Select an action to view its linked case, recorded policy
            decisions, and audit events.
            Approve and Reject are unavailable until the authorized
            write endpoint is installed and tested.
          </p>
          <button class="btn" disabled
            title="Decision endpoint not installed">Approve</button>
          <button class="btn" disabled
            title="Decision endpoint not installed">Reject</button>
        </section>
      </div>`;
  }

  function render() {
    if (!mountPoint) return;

    const rows = shown();
    const pane = mountPoint.querySelector('#iq-live-data');

    pane.innerHTML =
      stats(rows) +
      `<div style="margin-top:18px">
         ${approvals
           ? queue(rows)
           : state.mode === 'board' ? caseBoard(rows) : table(rows)}
       </div>` +
      `<div id="iq-details">
         ${state.selected && state.detail ? detailHTML(state.detail) : ''}
       </div>`;
  }

  async function load() {
    if (state.busy) return;

    state.busy = true;
    state.ready = false;
    state.selected = '';
    state.detail = null;

    render();
    announce('Loading real records…');

    try {
      const headers = approvals
        ? { 'X-InvoiceIQ-Reviewer-Key': state.reviewer }
        : {};

      const response = await fetch(endpoint, {
        headers,
        cache: 'no-store'
      });

      const data = await response.json();

      if (!response.ok) {
        throw Error(
          typeof data.detail === 'string'
            ? data.detail
            : 'HTTP ' + response.status
        );
      }

      const list = approvals ? data.reviews : data.cases;

      if (!Array.isArray(list)) {
        throw Error('Unexpected API response');
      }

      state.rows = list;
      state.ready = true;

      announce(
        `${list.length} ${
          approvals ? 'authorized pending reviews' : 'real recovery cases'
        } loaded`
      );

      if (approvals) {
        const badge = header.querySelector('.pill');
        if (badge) badge.textContent = 'Reviewer authorization verified';
      }
    } catch (error) {
      state.rows = [];
      state.ready = false;
      render();
      announce(
        'Could not load live records: ' +
        (error.message || 'Unknown error')
      );
      return;
    } finally {
      state.busy = false;
      render();
    }
  }

  async function detail(id) {
    state.selected = id;
    state.detail = null;

    render();
    announce('Loading case evidence…');

    try {
      const response = await fetch(
        '/api/workspace/cases/' + encodeURIComponent(id),
        { cache: 'no-store' }
      );

      const data = await response.json();

      if (!response.ok) {
        throw Error(
          typeof data.detail === 'string'
            ? data.detail
            : 'HTTP ' + response.status
        );
      }

      if (state.selected !== id) return;

      state.detail = data;
      render();
      announce('Case evidence loaded');

      mountPoint.querySelector('#iq-details')?.scrollIntoView({
        block: 'nearest',
        behavior: 'smooth'
      });
    } catch (error) {
      announce(
        'Case evidence unavailable: ' +
        (error.message || 'Unknown error')
      );
    }
  }

  function install(element) {
    if (mountPoint) return;

    main = element;
    header = main.querySelector('header');

    const original = [...main.children].find(
      child => child !== header
    );

    if (!original || !header) return;

    const nav = main.parentElement?.querySelector('nav');

    nav?.querySelectorAll(
      'a[href="/cases"] span.mono:not(.ix),' +
      'a[href="/approvals"] span.mono:not(.ix)'
    ).forEach(node => node.remove());

    original.style.display = 'none';

    mountPoint = document.createElement('div');
    mountPoint.id = 'iq-live-workspace';
    mountPoint.style.cssText =
      'padding:24px 32px 48px;display:flex;' +
      'flex-direction:column;gap:14px';

    mountPoint.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input id="iq-search" class="btn"
          aria-label="Search live records"
          placeholder="Search company, invoice or status"
          style="min-width:235px">

        ${approvals ? `
          <input id="iq-reviewer" class="btn" type="password"
            autocomplete="off"
            placeholder="Reviewer credential"
            aria-label="Authorized reviewer credential">
          <button class="btn dark" id="iq-connect">
            Load authorized queue
          </button>` : ''}

        <button class="btn" id="iq-refresh">Refresh</button>
      </div>

      <p role="status" id="iq-status"
         style="margin:0;color:#666;font-size:12px"></p>

      <div id="iq-live-data"></div>`;

    main.append(mountPoint);
    notice = mountPoint.querySelector('#iq-status');

    mountPoint.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (!button) return;

      if (button.dataset.detail) {
        void detail(button.dataset.detail);
      } else if (button.dataset.close) {
        state.selected = '';
        state.detail = null;
        render();
      } else if (button.id === 'iq-refresh') {
        if (!approvals || state.reviewer) {
          void load();
        } else {
          announce('Enter a reviewer credential first.');
        }
      } else if (button.id === 'iq-connect') {
        state.reviewer = mountPoint
          .querySelector('#iq-reviewer')
          .value.trim();

        if (state.reviewer) {
          void load();
        } else {
          announce('Reviewer credential required.');
        }
      }
    });

    mountPoint.querySelector('#iq-search')
      .addEventListener('input', event => {
        state.search = event.target.value;
        render();
      });

    if (approvals) {
      const badge = header.querySelector('.pill');
      if (badge) {
        badge.textContent = 'Reviewer authorization required';
      }
    } else {
      const controls = [...header.querySelectorAll('button')];

      const view = header.querySelector(
        '[role="group"][aria-label="View"]'
      );

      const buttons = [...(view?.querySelectorAll('button') || [])];

      function setMode(mode) {
        state.mode = mode;
        buttons.forEach((button, index) => {
          button.classList.toggle(
            'on',
            (index === 0) === (mode === 'board')
          );
        });
        render();
      }

      buttons[0]?.addEventListener('click', () => setMode('board'));
      buttons[1]?.addEventListener('click', () => setMode('table'));

      const owner = controls.find(
        button => button.textContent.includes('Owner:')
      );

      if (owner) {
        owner.disabled = true;
        owner.title =
          'Owner assignment is not supplied by the current case API';
        owner.textContent = 'Owner: unavailable';
      }

      const opened = controls.find(
        button => button.textContent.includes('Opened:')
      );

      if (opened) {
        opened.textContent = 'Opened: All dates ▾';

        opened.addEventListener('click', () => {
          state.range = state.range === 'all' ? '90' : 'all';
          opened.textContent = state.range === '90'
            ? 'Opened: Last 90 days ▾'
            : 'Opened: All dates ▾';
          render();
        });
      }

      const risk = controls.find(
        button => button.textContent.includes('risk sweep')
      );

      if (risk) {
        risk.disabled = true;
        risk.title =
          'Global risk sweep requires workspace-scoped authorization';
      }
    }

    render();

    if (!approvals) {
      void load();
    } else {
      announce(
        'Enter your authorized reviewer credential. ' +
        'It is not stored in the browser.'
      );
    }
  }

  let attempts = 0;

  const timer = setInterval(() => {
    const element = find(document, 'x-dc main');

    if (
      element?.querySelector('header') &&
      [...element.children].some(
        child => child.tagName !== 'HEADER'
      )
    ) {
      clearInterval(timer);
      install(element);
    } else if (++attempts > 150) {
      clearInterval(timer);

      const error = document.createElement('p');
      error.textContent =
        'Live workspace failed to initialize. ' +
        'Sample preview must not be used as real data.';
      error.style.cssText =
        'background:white;color:#900;padding:15px';

      document.body.prepend(error);
    }
  }, 100);
})();
