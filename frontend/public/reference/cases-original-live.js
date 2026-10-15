/* Live data for the original InvoiceIQ Recovery Cases HTML. */
(() => {
  'use strict';

  const api = '/api/workspace/cases';
  const requestedCase = new URLSearchParams(location.search).get('case_id');
  const deepLinkedCase = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(requestedCase || '')
    ? requestedCase.toLowerCase() : null;
  const columns = [
    'New',
    'Investigating',
    'Awaiting approval',
    'Action taken',
    'Resolved'
  ];

  const esc = value =>
    String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    })[char]);

  const inPaise = value => {
    const match = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(
      String(value ?? '').trim()
    );

    if (!match) return null;

    const amount =
      BigInt(match[2]) * 100n +
      BigInt((match[3] || '').padEnd(2, '0'));

    return match[1] ? -amount : amount;
  };

  const moneyPaise = amount => {
    if (amount === null) return '—';
    const abs = amount < 0n ? -amount : amount;
    const prefix = amount < 0n ? '−' : '';
    for (const [scale, label] of [[1000000000n, 'Cr'], [10000000n, 'L'], [100000n, 'k']]) {
      if (abs >= scale) {
        const hundredths = (abs * 100n + scale / 2n) / scale;
        const fraction = String(hundredths % 100n).padStart(2, '0').replace(/0+$/, '');
        return prefix + '₹' + String(hundredths / 100n) + (fraction ? '.' + fraction : '') + label;
      }
    }
    const fraction = String(abs % 100n).padStart(2, '0').replace(/0+$/, '');
    return prefix + '₹' + String(abs / 100n) + (fraction ? '.' + fraction : '');
  };

  const money = value => moneyPaise(inPaise(value));

  const sum = rows => {
    let result = 0n;

    for (const row of rows) {
      const amount = inPaise(row.outstanding);
      if (amount === null) return null;
      result += amount;
    }

    return result;
  };

  const fmtDate = value =>
    value ? String(value).slice(0, 10) : 'Not recorded';

  const age = (value, now) => {
    const time = Date.parse(value || '');

    return Number.isFinite(time)
      ? Math.max(0, Math.floor((now - time) / 86400000))
      : null;
  };

  const resolved = row =>
    /^(CLOSED|RESOLVED|RECOVERED|COMPLETED)$/i.test(
      row.status || ''
    );

  const stage = row =>
    resolved(row)
      ? 'Resolved'
      : row.pending
        ? 'Awaiting approval'
        : /ESCALAT|INVESTIGAT/i.test(row.status)
          ? 'Investigating'
          : /ACTION|CONTACT|REMIND|FOLLOW|MONITOR/i.test(row.status)
            ? 'Action taken'
            : 'New';

  const clean = value =>
    String(value || 'Unknown').replaceAll('_', ' ');

  const state = {
    cases: [],
    loaded: false,
    error: false,
    mode: 'board',
    range: 'all',
    now: 0,
    selected: deepLinkedCase,
    detail: null,
    resolved30: null
  };

  let main, board, kpis, evidence, status;
  let buttons = [];

  function locate(root) {
    const element = root.querySelector?.('main');

    if (
      element?.querySelector('[aria-label="Case totals"]') &&
      element.querySelector('[aria-label="Case board"]')
    ) {
      return element;
    }

    for (const child of root.querySelectorAll?.('*') || []) {
      if (child.shadowRoot) {
        const result = locate(child.shadowRoot);
        if (result) return result;
      }
    }

    return null;
  }

  function rows() {
    return state.cases.filter(row =>
      state.range === 'all' ||
      (
        age(row.opened, state.now) !== null &&
        age(row.opened, state.now) <= 90
      )
    );
  }

  function median(values) {
    const numbers = values
      .filter(Number.isFinite)
      .sort((a, b) => a - b);

    if (!numbers.length) return null;

    return (
      numbers[(numbers.length - 1) >> 1] +
      numbers[numbers.length >> 1]
    ) / 2;
  }

  function updateMetrics() {
    const slots = [...kpis.children];

    if (slots.length !== 5) {
      throw Error('Original five KPI cards not found');
    }

    const visible = rows();
    const active = visible.filter(row => !resolved(row));
    const ages = active.map(row => age(row.opened, state.now));
    const middle = median(ages);

    const validAges = ages.filter(Number.isFinite);
    const oldest = validAges.length
      ? Math.max(...validAges)
      : null;

    const values = [
      [
        String(active.length),
        `${new Set(active.map(row => row.company)).size} companies`
      ],
      [
        moneyPaise(sum(active)),
        'Current outstanding in active cases'
      ],
      [
        '—',
        'Requires overdue invoices without linked cases'
      ],
      [
        middle === null ? '—' : `${Math.round(middle)} d`,
        oldest === null
          ? 'Age unavailable'
          : `Oldest ${oldest} d`
      ],
      [
        state.resolved30 === null
          ? '—'
          : String(state.resolved30),
        'Closed during the preceding 30 days'
      ]
    ];

    slots.forEach((slot, index) => {
      const number = slot.querySelector('.num');
      const description = slot.lastElementChild;

      if (number) {
        number.textContent = state.loaded
          ? values[index][0]
          : '—';
      }

      if (description) {
        description.textContent = state.loaded
          ? values[index][1]
          : 'Loading live cases';
      }
    });
  }

  function caseCard(row) {
    const days = age(row.opened, state.now);

    return `
      <button
        type="button"
        class="live-case"
        data-case="${esc(row.id)}"
        style="
          display:flex;
          flex-direction:column;
          gap:8px;
          background:#FFFFFF;
          border:1px solid #E5E5E2;
          border-radius:8px;
          padding:12px;
          width:100%;
          text-align:left;
          color:#0B0B0B;
          cursor:pointer;
          font:inherit
        "
      >
        <div style="display:flex;justify-content:space-between;gap:6px;
                    font-size:11.5px;color:#525252">
          <span class="mono">${esc(row.invoice)}</span>
          <span>${esc(clean(row.status))}</span>
        </div>

        <div style="font-size:14px;font-weight:600">
          ${esc(row.company)}
        </div>

        <div class="num" style="font-size:20px;font-weight:600">
          ${money(row.outstanding)}
        </div>

        <div style="font-size:12px;color:#3F3F3F">
          Next: inspect recorded actions
        </div>

        <div style="display:flex;gap:4px;flex-wrap:wrap">
          <span class="pill">${esc(clean(row.status))}</span>
          ${row.pending
            ? '<span class="pill">Approval pending</span>'
            : ''}
        </div>

        <div style="
          border-top:1px solid #F0F0EE;
          padding-top:8px;
          display:flex;
          justify-content:space-between;
          font-size:11.5px;
          color:#6B6B6B
        ">
          <span>Owner: not recorded</span>
          <span>${days === null ? 'Age unavailable' : days + ' d'}</span>
        </div>
      </button>`;
  }


  // IQ_CASES_COLLAPSIBLE_V1
  // Keep original board and display additional cases in a dropdown.
  function compactCaseCards(records) {
    const preview = records.slice(0, 2)
      .map(caseCard).join('');

    const remaining = records.slice(2);

    if (!remaining.length) return preview;

    return preview + `
      <details
        class="iq-cases-dropdown"
        style="width:100%;min-width:0"
      >
        <summary
          style="
            cursor:pointer;
            border:1px solid #D8D8D5;
            background:#FFFFFF;
            border-radius:8px;
            padding:10px 12px;
            font-size:12px;
            font-weight:600;
            color:#0B0B0B;
          "
        >
          Show ${remaining.length} more
          ${remaining.length === 1 ? 'case' : 'cases'}
        </summary>

        <div
          style="
            display:flex;
            flex-direction:column;
            gap:10px;
            margin-top:10px;
          "
        >
          ${remaining.map(caseCard).join('')}
        </div>
      </details>
    `;
  }
  function drawBoard() {
    if (!state.loaded) {board.innerHTML=state.error?'':'<p role="status" style="padding:18px;color:#666">Loading real recovery cases…</p>';return;}
    const visible = rows();

    if (state.mode === 'table') {
      board.innerHTML = `
        <div class="card" style="overflow-x:auto">
          <table class="t" aria-label="Live recovery case table">
            <thead>
              <tr>
                <th>Invoice</th>
                <th>Company</th>
                <th>Status</th>
                <th>Outstanding</th>
                <th>Opened</th>
                <th>Details</th>
              </tr>
            </thead>
            <tbody>
              ${visible.map(row => `
                <tr>
                  <td class="mono">${esc(row.invoice)}</td>
                  <td>${esc(row.company)}</td>
                  <td>${esc(clean(row.status))}</td>
                  <td class="num">${money(row.outstanding)}</td>
                  <td>${fmtDate(row.opened)}</td>
                  <td>
                    <button class="btn" data-case="${esc(row.id)}">
                      View case
                    </button>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>

          ${visible.length
            ? ''
            : '<p style="padding:15px">No matching cases.</p>'}
        </div>`;
      return;
    }

    board.innerHTML = `
      <div style="
        display:grid;
        grid-template-columns:repeat(5,minmax(220px,1fr));
        gap:14px;
        min-width:1120px
      ">
        ${columns.map(name => {
          const records = visible.filter(row => stage(row) === name);

          return `
            <div style="
              background:#EDEDEB;
              border-radius:10px;
              padding:12px;
              display:flex;
              flex-direction:column;
              gap:10px;
              min-height:360px
            ">
              <div style="
                display:flex;
                justify-content:space-between;
                align-items:baseline;
                padding:2px 4px
              ">
                <span style="font-size:13px;font-weight:600">
                  ${name}
                  <span class="mono" style="color:#6B6B6B;font-weight:400">
                    ${records.length}
                  </span>
                </span>

                <span class="mono num" style="font-size:11.5px">
                  ${moneyPaise(sum(records))}
                </span>
              </div>

              ${records.length
                ? compactCaseCards(records)
                : `
                  <div style="
                    border:1px dashed #BDBDBA;
                    border-radius:8px;
                    padding:16px;
                    font-size:12px;
                    color:#6B6B6B;
                    text-align:center
                  ">
                    No cases in this stage
                  </div>
                `}
            </div>`;
        }).join('')}
      </div>`;
  }

  function renderDetail() {
    const row = state.cases.find(
      item => item.id === state.selected
    );

    if (!row) {
      evidence.innerHTML = `
        <p style="padding:22px">
          Select a case to inspect policy limits and interventions.
        </p>`;
      return;
    }

    if (!state.detail) {
      evidence.innerHTML = `
        <p style="padding:22px">
          Loading evidence for ${esc(row.invoice)}…
        </p>`;
      return;
    }

    const data = state.detail;
    const actions = Array.isArray(data.actions) ? data.actions : [];
    const audits = Array.isArray(data.audit_logs) ? data.audit_logs : [];
    const policies = actions.flatMap(
      action => action.policy_decisions || []
    );

    evidence.innerHTML = `
      <div style="
        display:flex;
        justify-content:space-between;
        flex-wrap:wrap;
        align-items:center;
        gap:12px;
        padding:18px 22px;
        border-bottom:1px solid #E5E5E2
      ">
        <div>
          <h2 class="mono" style="margin:0;font-size:17px">
            ${esc(row.invoice)}
            <span class="pill">${esc(clean(row.status))}</span>
          </h2>

          <div style="font-size:13px;color:#525252;margin-top:7px">
            ${esc(row.company)}
            · ${money(row.outstanding)}
            · opened ${fmtDate(row.opened)}
          </div>
        </div>

        <div style="display:flex;gap:8px">
          ${row.pending
            ? '<a class="btn dark" href="/approvals" target="_top">Open approvals</a>'
            : ''}

          <button class="btn" data-refresh-case>
            Refresh case
          </button>
        </div>
      </div>

      <div style="display:flex;flex-wrap:wrap">
        <div style="
          flex:1 1 420px;
          min-width:0;
          padding:18px 22px;
          border-right:1px solid #F0F0EE
        ">
          <div class="lbl" style="margin-bottom:12px">
            Policy limits on this case
          </div>

          <table class="t">
            <thead>
              <tr>
                <th>Rule</th>
                <th>Current</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              ${policies.length
                ? policies.map(policy => `
                    <tr>
                      <td>${esc(policy.policy_name)}</td>
                      <td>${esc(
                        policy.rule || policy.reason || 'Recorded policy'
                      )}</td>
                      <td>${esc(policy.decision)}</td>
                    </tr>`).join('')
                : `
                  <tr>
                    <td colspan="3">
                      No recorded policy decisions.
                    </td>
                  </tr>`}
            </tbody>
          </table>
        </div>

        <div style="
          flex:1 1 380px;
          min-width:0;
          padding:18px 22px
        ">
          <div class="lbl" style="margin-bottom:12px">
            Interventions and outcomes
          </div>

          ${actions.length
            ? actions.map(action => `
                <div style="
                  display:grid;
                  grid-template-columns:75px 1fr auto;
                  gap:12px;
                  padding:10px 0;
                  border-bottom:1px solid #F0F0EE;
                  font-size:12.5px
                ">
                  <span class="mono">${fmtDate(action.created_at)}</span>
                  <div>
                    <strong>${esc(clean(action.action_type))}</strong>
                    <div style="color:#525252">
                      ${esc(clean(action.proposed_by))}
                    </div>
                  </div>
                  <span class="pill">${esc(clean(action.status))}</span>
                </div>`).join('')
            : '<p>No recorded interventions.</p>'}

          ${audits.length
            ? `
              <div class="lbl" style="margin-top:14px">
                Audit history
              </div>
              ${audits.slice(-6).map(audit => `
                <div style="
                  border-top:1px solid #eee;
                  padding:8px 0;
                  font-size:12px
                ">
                  ${fmtDate(audit.occurred_at)}
                  · ${esc(audit.display_event_type || audit.event_type)}

                  <div style="color:#525252">
                    ${esc(audit.description)}
                  </div>
                </div>`).join('')}
              `
            : ''}
        </div>
      </div>`;
  }

  let detailController, listController;

  async function selectCase(id) {
    if (!state.cases.some(row => row.id === id)) return;

    detailController?.abort();
    const controller = detailController = new AbortController();
    state.selected = id;
    state.detail = null;
    renderDetail();

    try {
      const response = await fetch(
        api + '/' + encodeURIComponent(id),
        { cache: 'no-store', signal: controller.signal }
      );

      const data = await response.json();

      if (!response.ok) {
        throw Error(
          typeof data.detail === 'string'
            ? data.detail
            : 'HTTP ' + response.status
        );
      }

      if (!controller.signal.aborted && state.selected === id) {
        state.detail = data;
        renderDetail();
      }
    } catch (error) {
      if (!controller.signal.aborted && state.selected === id) {
        evidence.innerHTML = `
          <p role="alert" style="padding:20px;color:#A11">
            Case evidence unavailable: ${esc(error.message)}
          </p>`;
      }
    }
  }

  async function load() {
    state.error = false;
    status.textContent = 'Loading real recovery cases…';
    if (listController) return;
    const controller = listController = new AbortController();
    main.setAttribute('aria-busy', 'true');
    updateMetrics();

    try {
      const response = await fetch(api, { cache: 'no-store', signal: controller.signal });
      const data = await response.json();

      if (!response.ok) {
        throw Object.assign(Error(data.detail || 'HTTP ' + response.status), {status:response.status});
      }

      if (!Array.isArray(data.cases)) {
        throw Error('Unexpected Cases API response');
      }

      if (controller.signal.aborted) return;
      state.cases = data.cases;
      state.now = Date.now();
      state.loaded = true;
      state.resolved30 = null;

      status.textContent =
        `${data.cases.length} real cases · INR · workspace scoped`;

      updateMetrics();
      drawBoard();

      if (!state.cases.some(row => row.id === state.selected)) {
        state.selected = deepLinkedCase
          ? null : state.cases[0]?.id || null;
        state.detail = null;
        if (deepLinkedCase) {
          status.textContent = 'Case unavailable in this workspace.';
        }
      }

      if (state.selected && state.detail) {
        renderDetail();
      } else if (state.selected) {
        void selectCase(state.selected);
      } else {
        renderDetail();
      }

      const loadTime = state.now;
      const closedCases = state.cases.filter(resolved);

      // Closure timestamps are part of the list contract: avoid one detail
      // request per resolved case on every refresh.
      const ages = closedCases.map(row => age(row.closed_at, loadTime));
      state.resolved30 = ages.every(value => value !== null)
        ? ages.filter(value => value >= 0 && value <= 30).length : null;
      updateMetrics();
    } catch (error) {
      if (controller.signal.aborted) return;
      if (!state.loaded || [401,403].includes(error.status)) {
      detailController?.abort();
      state.selected = null;
      state.detail = null;
      state.error = true;
      state.loaded = false;
      state.cases = [];
      updateMetrics();
      board.innerHTML = '';
      evidence.innerHTML = '';
      }

      status.textContent =
        'Unable to load Cases: ' + error.message;

      status.style.cssText =
        'padding:12px;border:1px solid #B91C1C;' +
        'color:#B91C1C;background:white';
    } finally {
      listController = null;
      main.setAttribute('aria-busy', 'false');
      document.querySelector('x-dc')
        ?.style.setProperty('visibility', 'visible');
    }
  }

  function initialize() {
    main = locate(document);
    if (!main) return false;

    kpis = main.querySelector('[aria-label="Case totals"]');
    board = main.querySelector('[aria-label="Case board"]');
    evidence = board?.nextElementSibling;

    const header = main.querySelector('header');

    if (
      !kpis ||
      !board ||
      !header ||
      !evidence?.classList.contains('card')
    ) {
      return false;
    }

    // Wait until the original HTML template renderer is ready.
    if (board.textContent.includes('{{c.t}}')) return false;

    const group = header.querySelector(
      '[role="group"][aria-label="View"]'
    );

    buttons = [...(group?.querySelectorAll('button') || [])];

    if (buttons.length !== 2) return false;

    buttons.forEach((button, index) => {
      button.addEventListener('click', () => {
        state.mode = index === 0 ? 'board' : 'table';

        buttons.forEach((item, current) => {
          item.classList.toggle('on', index === current);
        });

        drawBoard();
      });
    });

    const controls = [...header.querySelectorAll('button')]
      .filter(button => !buttons.includes(button));

    const owner = controls.find(button =>
      button.textContent.includes('Owner:')
    );

    const opened = controls.find(button =>
      button.textContent.includes('Opened:')
    );

    const sweep = controls.find(button =>
      button.textContent.includes('risk sweep')
    );

    if (owner) {
      owner.disabled = true;
      owner.textContent = 'Owner: unavailable';
      owner.title = 'Owner is not provided by the current Cases API';
    }

    if (sweep) {
      sweep.disabled = true;
      sweep.title = 'Risk sweep requires safe workspace authorization';
    }

    if (opened) {
      opened.textContent = 'Opened: All dates ▾';

      opened.addEventListener('click', () => {
        state.range = state.range === 'all' ? '90' : 'all';

        opened.textContent = state.range === 'all'
          ? 'Opened: All dates ▾'
          : 'Opened: Last 90 days ▾';

        updateMetrics();
        drawBoard();
      });
    }

    board.addEventListener('click', event => {
      const button = event.target.closest('[data-case]');
      if (button) void selectCase(button.dataset.case);
    });

    evidence.addEventListener('click', event => {
      if (
        event.target.closest('[data-refresh-case]') &&
        state.selected
      ) {
        void selectCase(state.selected);
      }
    });

    status = document.createElement('div');
    status.setAttribute('role', 'status');
    status.style.cssText =
      'position:absolute;width:1px;height:1px;' +
      'overflow:hidden;clip-path:inset(50%)';

    kpis.before(status);

    board.innerHTML = '';
    evidence.innerHTML =
      '<p style="padding:20px">Loading recorded case evidence…</p>';

    updateMetrics();
    drawBoard();
    document.getElementById("iq-initial-loading")?.remove();
    document.getElementById("dc-root")?.style.setProperty("visibility", "visible");
    void load();

    return true;
  }

  let attempts = 0;

  const timer = setInterval(() => {
    try {
      if (initialize()) {
        clearInterval(timer);
        return;
      }
    } catch (error) {
      clearInterval(timer);

      document.body.insertAdjacentHTML(
        'afterbegin',
        `<p role="alert" style="background:white;color:#B91C1C;padding:16px">
          Cases initialization error: ${esc(error.message)}
        </p>`
      );
      return;
    }

    if (++attempts >= 150) {
      clearInterval(timer);

      document.body.insertAdjacentHTML(
        'afterbegin',
        '<p role="alert" style="background:white;color:#B91C1C;padding:16px">' +
        'Original Cases HTML could not initialize. ' +
        'Sample financial figures will not be displayed.' +
        '</p>'
      );
    }
  }, 100);
})();
