/* InvoiceIQ scenario portfolio: deterministic, clearly identified simulated records.
 * Never calls or modifies production storage. All writes are browser-local demonstrations.
 */
(function (global) {
  'use strict';
  const active = new URLSearchParams(global.location.search).get('portal') !== '1';
  const DAY = 86400000;
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  const epoch = today.getTime();
  const iso = t => new Date(t).toISOString().slice(0, 10);
  const days = n => iso(epoch + n * DAY);
  const toMs = d => Date.parse(String(d).slice(0, 10) + 'T00:00:00Z');
  const amounts = n => (n / 100).toFixed(2);
  const companyNames = [
    'Northwind Traders', 'Bluepeak Logistics', 'Vertex Infrastructure',
    'Sundial Retail', 'Aarav Textiles', 'Orbit Manufacturing',
    'Cedar Healthcare', 'Pioneer Components', 'Meridian Foods',
    'Atlas Paperworks', 'Kaveri Packaging', 'Tangent Electronics',
    'Praxis Industries', 'Horizon Distribution', 'Indigo Warehousing',
    'Keystone Equipment', 'Seabright Exports', 'Crestline Materials'
  ];
  const uuid = n => 'a71a0000-0000-4000-8000-' + n.toString(16).padStart(12, '0');
  const companies = companyNames.map((name, i) => ({id: uuid(1000 + i), name}));
  // All money stored as integer paise; source INR is explicit in this scenario.
  const invoices = Array.from({length: 468}, (_, i) => {
    const companyIndex = (i * 17 + Math.floor(i / 9)) % companies.length;
    const co = companies[companyIndex];
    const posted = -(i * 3 % 410) - 1;
    const term = [15, 30, 45, 60][i % 4];
    const dueOffset = posted + term;
    const amount = (6500 + ((i * 113173 + i * i * 53) % 145000)) * 100;
    // Illustrative scores only; not predictions from a trained model.
    const companyFactor = (((companyIndex * 7) % 13) - 6) * .043;
    const termFactor = {15:.045, 30:.015, 45:-.015, 60:-.035}[term];
    const sizeFactor = (amount / 900000000 - .5) * .08;
    const variation = (((i * 31 + companyIndex * 13) % 17) - 8) * .01;
    const risk = Math.max(.16, Math.min(.82,
      .47 + companyFactor + termFactor + sizeFactor + variation));
    const late = (i % 7 === 0 || i % 7 === 3 || i % 11 === 0);
    const paid = (i % 5 === 0 || i % 7 === 2 || i % 13 === 0);
    const part = !paid && (i % 4 === 0 || i % 9 === 1);
    const settlement = paid ? dueOffset + (late ? 12 + (i % 38) : -2 - (i % 6)) : null;
    const payments = [];
    const cancel = i % 97 === 0;
    const writeoff = i % 131 === 0 && !cancel;
    if (!cancel && !writeoff && paid) payments.push({date: days(settlement), paise: amount});
    if (!cancel && !writeoff && part) payments.push({date: days(Math.min(-1, dueOffset + (i % 9))), paise: Math.floor(amount * (25 + i % 45) / 100)});
    // Some invoices are written off/cancelled; no payments are attributed to them.
    return {
      id: uuid(20000 + i), invoice_number: 'IQ-SC-' + String(i + 1).padStart(5, '0'),
      company_id: co.id, company: co.name, posting_date: days(posted),
      due_date: days(dueOffset), amount, payment_term: term, payments,
      probability: Number(risk.toFixed(2)), cancelled: cancel, written_off: writeoff
    };
  });
  const paidBy = (inv, cutoff) => inv.payments.reduce((total, p) => total + (p.date <= cutoff ? p.paise : 0), 0);
  const balanceAt = (inv, cutoff) => inv.cancelled || inv.written_off ? 0 : Math.max(0, inv.amount - paidBy(inv, cutoff));
  const records = invoices.map((inv, i) => {
    const historicalPaid = inv.payments.some(p => p.date <= days(0) && p.paise >= inv.amount);
    const open = balanceAt(inv, days(0)) > 0;
    if (!open && !historicalPaid) return null;
    if (!open || toMs(inv.due_date) >= epoch || i % 4 !== 1) return null;
    return inv;
  }).filter(Boolean).slice(0, 42);
  // Resolved cases have documented fully settled overdue invoices, not open balances.
  const completed = invoices.filter(inv =>
    !inv.cancelled && !inv.written_off &&
    paidBy(inv,days(0)) >= inv.amount &&
    inv.payments.some(p => p.date > inv.due_date && p.date <= days(0))
  ).slice(0, 8);
  const caseRecords = [...records, ...completed];
  const cases = caseRecords.map((inv, i) => ({
    id: uuid(30000 + i), invoice_id: inv.id, invoice: inv.invoice_number,
    company: inv.company, company_id: inv.company_id,
    status: balanceAt(inv,days(0)) === 0 ? 'RESOLVED' : i % 5 === 0 ? 'ACTION_TAKEN' : i % 3 === 0 ? 'INVESTIGATING' : 'NEW',
    opened: days(-Math.min(150, Math.max(1, Math.floor((epoch-toMs(inv.due_date))/DAY)-1))),
    outstanding: amounts(balanceAt(inv, days(0))),
    recovered_amount: amounts(0), pending: balanceAt(inv,days(0)) > 0 && i % 5 === 1,
    owner: ['A. Rao','M. Iyer','S. Menon','Unassigned'][i%4],
    risk_score: inv.probability,
    invoice_obj: inv
  }));
  const reviewCases = cases.filter(c => c.pending).slice(0, 10);
  const reviews = reviewCases.map((c, i) => ({
    id: uuid(40000 + i), case_id: c.id, company: c.company,
    invoice: c.invoice, action: i % 3 === 0 ? 'ESCALATE_CASE' : 'SEND_EMAIL',
    created: days(-i - 2) + 'T11:35:00Z',
    outstanding: c.outstanding, days_overdue: Math.max(0, Math.floor((epoch - toMs(c.invoice_obj.due_date)) / DAY))
  }));
  const storageKey = 'invoiceiq-scenario-reviews-v1';
  const getDecisionState = () => {
    try { return JSON.parse(global.localStorage?.getItem(storageKey) || '{}') || {}; }
    catch { return {}; }
  };
  const recordDecision = (key, value) => {
    const state = getDecisionState();
    state[key] = value;
    global.localStorage?.setItem(storageKey, JSON.stringify(state));
  };
  const initialHistory = Array.from({length: 11}, (_, i) => {
    const caseItem = cases[(i * 3) % cases.length];
    return {
      id: uuid(50000 + i), action_id: uuid(50000 + i), case_id: caseItem.id,
      company_name: caseItem.company, action_type: 'SEND_EMAIL',
      decision: i % 4 === 0 ? 'Rejected' : 'Approved',
      decided_at: days(-2 - i * 3) + 'T14:45:00Z',
      review_hours: 4 + i * 1.7,
      description: 'Recorded scenario reviewer decision; no customer contacted.'
    };
  });
  const getReviews = () => {
    const store = getDecisionState();
    const additions = Object.values(store).filter(x => x?.type === 'new').map(x => x.review);
    return [...reviews, ...additions].filter(r => !store[r.id]?.decision);
  };
  const getHistory = () => {
    const store = getDecisionState();
    const additions = Object.values(store).filter(x => x?.decision).map(x => ({
      action_id: x.review?.id || x.id, case_id: x.review?.case_id || x.case_id,
      company_name: x.review?.company || x.company,
      action_type: x.review?.action || 'SEND_EMAIL', decision: x.decision,
      decided_at: x.decided_at, review_hours: 0.5,
      description: x.note + ' (scenario only)'
    }));
    return [...additions, ...initialHistory];
  };
  const keyByPeriod = (date, grain) => {
    const d = new Date(toMs(date));
    if (grain === 'year') d.setUTCMonth(0, 1);
    if (grain === 'quarter') d.setUTCMonth(Math.floor(d.getUTCMonth() / 3) * 3, 1);
    if (grain === 'month') d.setUTCDate(1);
    if (grain === 'week') d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
    return d.toISOString().slice(0, 10);
  };
  const sum = (rows, get) => rows.reduce((acc, row) => acc + get(row), 0);
  const count = (rows, fn) => rows.reduce((n, row) => n + Number(fn(row)), 0);
  const getView = (params, offset) => {
    const q = new URLSearchParams(params);
    const asOf = q.get('as_of') || days(0);
    if (toMs(asOf) > epoch || Number.isNaN(toMs(asOf))) return {error: 'Invalid as-of cutoff', status:422};
    const compareCutoff=q.get('compare_date');
    if (compareCutoff && (!/^\d{4}-\d{2}-\d{2}$/.test(compareCutoff) ||
       Number.isNaN(toMs(compareCutoff)) || compareCutoff >= asOf))
      return {error:'Comparison date must precede As of',status:422};
    const start = q.get('start_date') || '';
    const end = q.get('end_date') || '';
    if (start && end && start > end) return {error:'Start date exceeds end date',status:422};
    const currency = q.get('currency') || 'INR';
    if (currency !== 'INR') return {error:'Scenario portfolio uses INR only',status:422};
    const basis = q.get('date_basis') || 'posting';
    const status = q.get('status') || 'all';
    const risk = q.get('risk') || 'any';
    const query = (q.get('search') || '').toLowerCase();
    const company = q.get('company_id');
    const grain = q.get('grain') || 'month';
    const permitted = ['day','week','month','quarter','year'];
    if (!permitted.includes(grain)) return {error:'Unsupported date grouping',status:422};
    // Invoice status, company, search and risk constrain both invoice and payment populations.
    // Cash receipts still use PAYMENT dates, independently of posting/due cohort dates.
    const eligible = inv => {
      if (inv.posting_date > asOf) return false;
      if (company && inv.company_id !== company) return false;
      if (query && ![inv.invoice_number,inv.company,inv.id].some(v=>v.toLowerCase().includes(query))) return false;
      const paid = paidBy(inv,asOf), balance = balanceAt(inv,asOf);
      const overdue = balance>0 && inv.due_date<asOf;
      const s = inv.cancelled?'cancelled':inv.written_off?'written_off':balance<=0?'settled':paid>0?'partial':overdue?'overdue':'open';
      const statusMatch = status==='all' || (status==='outstanding' && balance>0) ||
        (status==='overdue' && overdue) || (status==='open' && balance>0 && paid<=0) || s===status;
      const riskMatch = risk==='any' || (risk==='unscored' && inv.probability==null) ||
        (risk==='low' && inv.probability<.3) ||
        (risk==='medium' && inv.probability>=.3 && inv.probability<.7) ||
        (risk==='high' && inv.probability>=.7);
      // All column filters are evaluated on the full scenario invoice cohort, before paging.
      if (q.get('invoice_number') && !inv.invoice_number.toLowerCase().includes(q.get('invoice_number').toLowerCase())) return false;
      if (q.get('company_search') && !inv.company.toLowerCase().includes(q.get('company_search').toLowerCase())) return false;
      if (q.get('posted_from') && inv.posting_date < q.get('posted_from')) return false;
      if (q.get('posted_to') && inv.posting_date > q.get('posted_to')) return false;
      if (q.get('due_from') && inv.due_date < q.get('due_from')) return false;
      if (q.get('due_to') && inv.due_date > q.get('due_to')) return false;
      const amountInr=inv.amount/100, paidInr=paid/100, balanceInr=balance/100;
      const overdueDays=Math.max(0,Math.round((toMs(asOf)-toMs(inv.due_date))/DAY));
      for (const [prefix,actual] of [['amount',amountInr],['paid',paidInr],['balance',balanceInr],
                                      ['days',overdueDays],['risk',inv.probability==null?null:inv.probability*100]]) {
        const lo=q.get(prefix+'_min'),hi=q.get(prefix+'_max');
        if (lo!==null && (actual===null || actual < Number(lo)-1e-9)) return false;
        if (hi!==null && (actual===null || actual > Number(hi)+1e-9)) return false;
      }
      const presence=q.get('case_presence')||'any';
      const hasCase=cases.some(c=>c.invoice_id===inv.id);
      if ((presence==='yes'&&!hasCase)||(presence==='no'&&hasCase)) return false;
      return statusMatch && riskMatch;
    };
    const eligibleInvoices = invoices.filter(eligible);
    let selected = eligibleInvoices.filter(inv => {
      if (basis === 'payment') return inv.payments.some(p =>
        p.date <= asOf && (!start || p.date >= start) && (!end || p.date <= end));
      const axis = basis === 'due' ? inv.due_date : inv.posting_date;
      return (!start || axis >= start) && (!end || axis <= end);
    });
    const getBalance = inv => balanceAt(inv,asOf);
    const open = selected.filter(inv=>getBalance(inv)>0);
    const overdue = open.filter(inv=>inv.due_date<asOf);
    const payments = eligibleInvoices.flatMap(inv => inv.payments.map(p=>({date:p.date,amount:p.paise,invoice:inv})))
      .filter(p=>p.date<=asOf&&(!start||p.date>=start)&&(!end||p.date<=end));
    const collected = sum(payments,p=>p.amount);
    const totals = {
      invoiced: amounts(sum(selected,inv=>inv.cancelled?0:inv.amount)),
      billed: amounts(sum(selected,inv=>inv.amount)),
      invoice_count: selected.length,
      company_count: new Set(selected.map(inv=>inv.company_id)).size,
      open: amounts(sum(open,getBalance)),open_count: open.length,
      overdue: amounts(sum(overdue,getBalance)), overdue_count:overdue.length,
      overdue_share: open.length && sum(open,getBalance)>0 ?
        Number((sum(overdue,getBalance)*100/sum(open,getBalance)).toFixed(2)): null,
      collected: amounts(collected),scored_open:open.filter(i=>i.probability!=null).length,
      partial_count:selected.filter(i=>getBalance(i)>0&&paidBy(i,asOf)>0).length,
      settled_count:selected.filter(i=>!i.cancelled&&!i.written_off&&getBalance(i)===0).length,
      cancelled_count:selected.filter(i=>i.cancelled).length,
      written_off_count:selected.filter(i=>i.written_off).length,
      median_invoice: selected.length?amounts(selected.map(i=>i.amount).sort((a,b)=>a-b)[Math.floor((selected.length-1)/2)]):null
    };
    const aging = ['not_due','1-30','31-60','61-90','90+'].map(key => {
      const subset = open.filter(inv=>{const age=Math.max(0,Math.round((toMs(asOf)-toMs(inv.due_date))/DAY));return key==='not_due'?inv.due_date>=asOf:key==='1-30'?age>=1&&age<=30:key==='31-60'?age>30&&age<=60:key==='61-90'?age>60&&age<=90:age>90;});
      return {key,count:subset.length,amount:amounts(sum(subset,getBalance))};
    });
    const riskBands = [
      ['0–20%',0,.2],['20–40',.2,.4],['40–60',.4,.6],['60–80',.6,.8],['80–100',.8,1.01]
    ].map(([key,lo,hi])=>{const subset=open.filter(i=>i.probability>=lo&&i.probability<hi);return {key,count:subset.length,amount:amounts(sum(subset,getBalance))};});
    riskBands.push({key:'Not scored',count:0,amount:'0.00'});
    const dueMap = new Map();
    for (const inv of open) {
      const key = inv.due_date<asOf?'Past due':keyByPeriod(inv.due_date,'week');
      const rec=dueMap.get(key)||{key,count:0,amount:0};rec.count++;rec.amount+=getBalance(inv);dueMap.set(key,rec);
    }
    const due = [...dueMap.values()].map(d=>({...d,amount:amounts(d.amount)}));
    const top = companies.map(co=>{const rows=selected.filter(i=>i.company_id===co.id),coOpen=rows.filter(i=>getBalance(i)>0),coOver=coOpen.filter(i=>i.due_date<asOf);return {
      id:co.id,name:co.name,count:coOpen.length,open:amounts(sum(coOpen,getBalance)),overdue:amounts(sum(coOver,getBalance)),
      oldest: coOpen.length?Math.max(0,...coOpen.map(i=>Math.round((toMs(asOf)-toMs(i.due_date))/DAY))):0,
      risk:coOpen.length?Number((sum(coOpen,i=>i.probability)/coOpen.length).toFixed(2)):null
    };}).filter(x=>x.count).sort((a,b)=>Number(b.overdue)-Number(a.overdue)).slice(0,7);
    const periods = new Map();
    for (const inv of selected) {const key=keyByPeriod(inv.posting_date,grain);if(!periods.has(key))periods.set(key,{period:key,invoiced:0,collected:0,open:0});periods.get(key).invoiced+=inv.cancelled?0:inv.amount;}
    for (const payment of payments) {const key=keyByPeriod(payment.date,grain);if(!periods.has(key))periods.set(key,{period:key,invoiced:0,collected:0,open:0});periods.get(key).collected+=payment.amount;}
    const bucketClose = period => {
      const dt=new Date(toMs(period));
      if (grain==='year')dt.setUTCFullYear(dt.getUTCFullYear()+1);
      else if(grain==='quarter')dt.setUTCMonth(dt.getUTCMonth()+3);
      else if(grain==='month')dt.setUTCMonth(dt.getUTCMonth()+1);
      else if(grain==='week')dt.setUTCDate(dt.getUTCDate()+7);
      else dt.setUTCDate(dt.getUTCDate()+1);
      dt.setUTCDate(dt.getUTCDate()-1);
      return iso(Math.min(epoch,dt.getTime()));
    };
    const series=[...periods.values()].sort((a,b)=>a.period.localeCompare(b.period)).slice(-400).map(v=>{
      const closing=bucketClose(v.period);
      return {period:v.period,invoiced:amounts(v.invoiced),collected:amounts(v.collected),
        open:amounts(sum(selected.filter(i=>i.posting_date<=closing),i=>balanceAt(i,closing)))};
    });
    const dailyMap=new Map();
    for(const p of payments){if(toMs(p.date)>=epoch-181*DAY)dailyMap.set(p.date,(dailyMap.get(p.date)||0)+p.amount);}
    const daily=[...dailyMap].map(([date,amount])=>({date,amount:amounts(amount)}));
    const sort = q.get('sort')||'dpd_desc';
    selected=selected.slice().sort((a,b)=>{
      const pa=paidBy(a,asOf),pb=paidBy(b,asOf);
      const ba=balanceAt(a,asOf),bb=balanceAt(b,asOf);
      const byNum=(va,vb)=>va-vb||a.id.localeCompare(b.id);
      const byText=(va,vb)=>va.localeCompare(vb)||a.id.localeCompare(b.id);
      if(sort==='invoice_asc')return byText(a.invoice_number,b.invoice_number);
      if(sort==='invoice_desc')return byText(b.invoice_number,a.invoice_number);
      if(sort==='company_asc')return byText(a.company,b.company);
      if(sort==='company_desc')return byText(b.company,a.company);
      if(sort==='due_asc'||sort==='dpd_desc')return byText(a.due_date,b.due_date);
      if(sort==='due_desc'||sort==='dpd_asc')return byText(b.due_date,a.due_date);
      if(sort==='paid_asc')return byNum(pa,pb);
      if(sort==='paid_desc')return byNum(pb,pa);
      if(sort==='balance_asc')return byNum(ba,bb);
      if(sort==='balance_desc')return byNum(bb,ba);
      if(sort==='risk_asc')return byNum(a.probability??Infinity,b.probability??Infinity);
      if(sort==='risk_desc')return byNum(b.probability??-Infinity,a.probability??-Infinity);
      if(sort==='amount_desc')return b.amount-a.amount;
      if(sort==='amount_asc')return a.amount-b.amount;
      if(sort==='posting_asc')return a.posting_date.localeCompare(b.posting_date);
      if(sort==='posting_desc')return b.posting_date.localeCompare(a.posting_date);
      return a.due_date.localeCompare(b.due_date);
    });
    const page=Math.max(1,Number(q.get('page')||1)), page_size=Math.min(100,Math.max(1,Number(q.get('page_size')||12)));
    const row = inv=>{
      const paid=paidBy(inv,asOf),balance=getBalance(inv);
      const s=inv.cancelled?'CANCELLED':inv.written_off?'WRITTEN_OFF':balance===0?'PAID':'OPEN';
      return {id:inv.id,invoice_number:inv.invoice_number,company_id:inv.company_id,company:inv.company,
        posting_date:inv.posting_date,due_date:inv.due_date,amount_total:amounts(inv.amount),amount_paid:amounts(paid),
        balance:amounts(balance),days_past_due:Math.max(0,Math.round((toMs(asOf)-toMs(inv.due_date))/DAY)),
        status:s,probability:inv.probability,payment_term:inv.payment_term,
        case_id:cases.find(c=>c.invoice_id===inv.id)?.id||null};
    };
    const settledSubset = selected.filter(i=>i.payments.length===1&&paidBy(i,asOf)>=i.amount);
    const histogram = [
      {key:'< −15',from:-1e9,to:-16},{key:'−15–−1',from:-15,to:-1},
      {key:'On due',from:0,to:0},{key:'1–7',from:1,to:7},
      {key:'8–15',from:8,to:15},{key:'16–30',from:16,to:30},
      {key:'31–60',from:31,to:60},{key:'60+',from:61,to:1e9}
    ].map(b=>({b:b.key,n:count(settledSubset,i=>{
      const lateness=Math.floor((toMs(i.payments[0].date)-toMs(i.due_date))/DAY);
      return lateness>=b.from&&lateness<=b.to;
    })}));
    const allocation={
      collected:sum(selected,inv=>paidBy(inv,asOf)),
      not_due:sum(open.filter(inv=>inv.due_date>=asOf),getBalance),
      overdue:sum(overdue,getBalance),
      written_off:sum(selected.filter(inv=>inv.written_off),inv=>inv.amount),
      cancelled:sum(selected.filter(inv=>inv.cancelled),inv=>inv.amount)
    };
    const allocSum=Object.values(allocation).reduce((a,b)=>a+b,0);
    const billedTotal=sum(selected,inv=>inv.amount);
    if(Math.abs(allocSum-billedTotal)>1)throw Error('Scenario financial allocation does not reconcile');
    const year = Number(asOf.slice(0,4)) - (asOf.slice(5) < '09-30' ? 1 : 0);
    const previous = `${year}-06-30`, quarterStart=`${year}-07-01`,quarterEnd=`${year}-09-30`;
    const inQuarter=inv=>inv.posting_date>=quarterStart&&inv.posting_date<=quarterEnd;
    const fullScope=eligibleInvoices;
    const bridge={
      openStart:sum(fullScope.filter(inv=>inv.posting_date<=previous),inv=>balanceAt(inv,previous)),
      invoiced:sum(fullScope.filter(inv=>inQuarter(inv)&&!inv.cancelled),inv=>inv.amount),
      collected:sum(fullScope.flatMap(inv=>inv.payments).filter(p=>p.date>=quarterStart&&p.date<=quarterEnd),p=>p.paise),
      writtenOff:sum(fullScope.filter(inv=>inQuarter(inv)&&inv.written_off),inv=>inv.amount),
      openEnd:sum(fullScope.filter(inv=>inv.posting_date<=quarterEnd),inv=>balanceAt(inv,quarterEnd))
    };
    if(bridge.openStart+bridge.invoiced-bridge.collected-bridge.writtenOff!==bridge.openEnd)
      throw Error('Scenario quarter bridge does not reconcile');
    const invoiceIds = new Set(selected.map(inv=>inv.id));
    const visibleCases = cases.filter(c=>invoiceIds.has(c.invoice_id));
    const visibleCaseIds = new Set(visibleCases.map(c=>c.id));
    const pipeline={
      new:visibleCases.filter(c=>c.status==='NEW').length,
      investigating:visibleCases.filter(c=>c.status==='INVESTIGATING').length,
      awaiting:getReviews().filter(r=>visibleCaseIds.has(r.case_id)).length,
      action:visibleCases.filter(c=>c.status==='ACTION_TAKEN').length,
      resolved:visibleCases.filter(c=>c.status==='RESOLVED').length,
      exposure:sum(visibleCases,c=>Number(c.outstanding)*100)
    };
    const response={generated_at:new Date().toISOString(),as_of:asOf,start_date:start||null,end_date:end||null,
      timezone:'UTC',workspace:'Scenario portfolio · simulated',currency:'INR',currency_verified:true,
      currency_note:'Simulated INR values. No real financial transactions; not merged into historical Neon records.',
      historical_available:true,historical_reason:'Simulated scenarios with dated payments; not historical observations.',
      metrics:totals,comparison:null,series,aging,risk:riskBands,daily,due,
      histogram,bridge:Object.fromEntries(Object.entries(bridge).map(([k,v])=>[k,amounts(v)])),
      allocation:Object.fromEntries(Object.entries(allocation).map(([k,v])=>[k,amounts(v)])),pipeline,
      companies,top,invoices:selected.slice((page-1)*page_size,page*page_size).map(row),
      total:selected.length,page,page_size,chart_notes:{
        open_line:'Scenario balance by end of reporting interval.',bridge:'Simulated Q3 bridge reconciles opening, invoices, receipts and write-offs to closing.',
        pipeline:'Linked simulated case statuses and review queue; never a real recovery claim.',calendar:'Scenario dates use contractual due dates; no bank-holiday adjustment.',
        settlement:'Simulated settlement dates; no real-world recovery outcome.',allocation:'Reconciled simulated invoice face amounts, payments, open balances and cancel/write-off allocations.'
      }
    };
    if(q.get('compare_date')&&!offset){const prior=new URLSearchParams(q);prior.delete('compare_date');prior.set('as_of',q.get('compare_date'));const comparison=getView(prior,true);if(!comparison.error)response.comparison=comparison.metrics;}
    return response;
  };
  const detailFor = id => {
    const item=cases.find(c=>c.id===id);
    if(!item)return null;
    const r=reviews.find(x=>x.case_id===id);
    const actions=[{id:r?.id||uuid(60000+cases.indexOf(item)),action_type:r?.action||'SEND_EMAIL',
      created_at:item.opened+'T09:15:00Z',status:r?'PENDING_APPROVAL':'EXECUTED',proposed_by:'scenario_policy_engine',
      policy_decisions:[{policy_name:'Review amount threshold',decision:r?'REQUIRES_HUMAN_REVIEW':'APPROVED',reason:'Scenario policy evaluation'}]}];
    const balance = Math.round(Number(item.outstanding) * 100);
    const part = Math.floor(balance / 3);
    const promises = item.pending && balance > 0 ? [
      {promised_date:days(7),promised_amount:amounts(part)},
      {promised_date:days(30),promised_amount:amounts(part)},
      {promised_date:days(60),promised_amount:amounts(balance-2*part)}
    ] : [];
    return {...item,actions,audit_logs:[{occurred_at:item.opened+'T09:15:00Z',display_event_type:'Case opened',description:'Simulated case history · no real customer contacted.'}],
      risk_score:item.risk_score,
      communication_logs:[{body:'Scenario draft only: please review the open invoice and propose a suitable settlement date. No message has been sent.'}],
      promises_to_pay:promises,
      invoice: {id:item.invoice_id,invoice_number:item.invoice,company:item.company,amount_total:amounts(item.invoice_obj.amount)},
      case_status:item.status,scenario:true};
  };
  const evidenceFor = id => {
    const inv=invoices.find(i=>i.id===id);if(!inv)return null;
    return {invoice:{id:inv.id,invoice_number:inv.invoice_number,
      payment_risk_model_version:'scenario_fixture_only',late_payment_probability:inv.probability,
      payment_risk_scored_at:days(0)+'T00:00:00Z'},
      trusted_prediction:false,explanation:null,
      historical_reason:'Scenario record; risk score is illustrative and not a model prediction.',
      payments:inv.payments.map(p=>({amount:amounts(p.paise),payment_date:p.date}))};
  };
  const csv = rows => '\uFEFF'+rows.map(r=>r.map(x=>{const v=String(x??'');return '"'+(/^[\s]*[=+@\-\t\r]/.test(v)?"'"+v:v).replaceAll('"','""')+'"';}).join(',')).join('\r\n');
  const respond = (data,status=200,headers={}) => new Response(typeof data==='string'?data:JSON.stringify(data),{status,headers:{'Cache-Control':'no-store','Content-Type':'application/json',...headers}});
  const route = async (url, options) => {
    const p=url.pathname;
    const q=url.searchParams;
    if(p==='/api/finance/overview') return null; // Unified adapter merges actual Neon analytics.
    if(p==='/api/finance/export' && !global.InvoiceIQUnified) {
      const params=new URLSearchParams(q);params.set('page_size','100');params.set('page','1');
      const view=getView(params);if(view.error)return respond({detail:view.error},view.status);
      const headers=['invoice_number','company','posting_date','due_date','amount_total','amount_paid','balance','days_past_due','status','probability','case_id'];
      const rows=[headers,...view.invoices.map(i=>headers.map(k=>i[k]??''))];
      for(let page=2;page<=Math.ceil(view.total/100);page++){
        params.set('page',String(page));
        const pageData=getView(params);
        rows.push(...pageData.invoices.map(i=>headers.map(k=>i[k]??'')));
      }
      return respond(csv(rows),200,{'Content-Type':'text/csv;charset=utf-8','Content-Disposition':'attachment; filename="invoiceiq-scenario.csv"'});
    }
    const evidenceMatch=p.match(/^\/api\/finance\/invoices\/([a-f0-9-]{36})\/evidence$/i);
    if(evidenceMatch){const detail=evidenceFor(evidenceMatch[1]);return detail?respond(detail):null;}
    if(p==='/api/workspace/cases') return null; // Merged with Neon cases by unified adapter.
    const caseMatch=p.match(/^\/api\/workspace\/cases\/([a-f0-9-]{36})$/i);
    if(caseMatch){const item=detailFor(caseMatch[1]);return item?respond(item):null;}
    if(p==='/api/workspace/reviews')return respond({reviews:getReviews()});
    if(p==='/api/reviewer/history')return respond({history:getHistory()});
    const reviewMatch=p.match(/^\/api\/reviewer\/reviews\/([a-f0-9-]{36})\/(approve|reject)$/i);
    if(reviewMatch){
      if((options?.method||'GET').toUpperCase()!=='POST')return respond({detail:'Method not allowed'},405);
      const r=getReviews().find(x=>x.id===reviewMatch[1]);
      if(!r)return respond({detail:'Review not found or previously decided'},409);
      let note='';try{note=String(JSON.parse(options?.body||'{}').note||'').trim();}catch{}
      if(note.length<3||note.length>2000)return respond({detail:'Decision reason required'},422);
      recordDecision(r.id,{id:r.id,review:r,decision:reviewMatch[2]==='approve'?'Approved':'Rejected',note,decided_at:new Date().toISOString()});
      return respond({ok:true,scenario:true,message:'Scenario review recorded in this browser only'});
    }
    if(p==='/api/review-requests'){
      if((options?.method||'GET').toUpperCase()==='GET')return respond({candidates:cases.filter(c=>Number(c.outstanding)>75000&&!c.pending).slice(0,8).map(c=>({case_id:c.id,company_name:c.company,invoice_number:c.invoice,outstanding_amount:c.outstanding,reason:'Scenario high-value payment follow-up',eligible:true}))});
      let d=null;try{d=JSON.parse(options?.body||'{}');}catch{}
      if(!cases.some(c=>c.id===d?.case_id)||typeof d.note!=='string'||d.note.trim().length<3)return respond({detail:'Invalid scenario request'},422);
      const c=cases.find(c=>c.id===d.case_id),id=uuid(70000+cases.indexOf(c));
      const review={id,case_id:c.id,company:c.company,invoice:c.invoice,action:'SEND_EMAIL',created:new Date().toISOString(),outstanding:c.outstanding,days_overdue:10};
      recordDecision('new-'+id,{type:'new',review});
      return respond({message:'Scenario review request created locally; no production action taken',id});
    }
    return null;
  };
  // Intercept only within an explicitly identified scenario iframe.
  if(active){
    const realFetch=global.fetch.bind(global);
    global.fetch=async function(resource, options){
      const src=typeof resource==='string'?resource:resource instanceof URL?resource.href:resource?.url;
      if(src){const url=new URL(src,global.location.origin);
        if(url.origin===global.location.origin){const result=await route(url,options);if(result)return result;}
      }
      return realFetch(resource,options);
    };
    // Keep the mode when navigation moves between existing four dashboard pages.
    global.document.addEventListener('click',event=>{
      const target=event.target?.closest?.('a[href^="/"]');
      if(!target)return;
      const href=target.getAttribute('href');
      if(!href||href.startsWith('/api/')||href.startsWith('/reference/'))return;
      const dest=new URL(href,global.location.origin);dest.searchParams.delete('scenario');
      target.href=dest.pathname+'?'+dest.searchParams.toString();
    },true);
  }
  // Make the original Invoice Intelligence table's disabled presentation controls useful
  // in scenario mode only. No backend writes or real-account bulk actions.
  if(active && global.location.pathname?.includes('/invoices/')) {
    const picked = new Set();
    const hidden = new Set();
    const all = selector => {
      const found = [];
      function walk(root) {
        found.push(...(root.querySelectorAll?.(selector) || []));
        for (const n of root.querySelectorAll?.('*') || []) if (n.shadowRoot) walk(n.shadowRoot);
      }
      walk(global.document);
      return found;
    };
    const table = () => all('table.t').find(t =>
      [...t.querySelectorAll('thead th')].some(h => h.textContent.trim() === 'Invoice'));
    const visibleRows = t => [...t.querySelectorAll('tbody tr')]
      .filter(tr => !!tr.querySelector('a[data-action="evidence"]'));
    function exportPicked() {
      const p = new URLSearchParams(global.location.search);
      p.set('page_size','100');
      const rows = [];
      if (global.InvoiceIQUnified) {
        const unified = global.InvoiceIQUnified.selectedRows([...picked]);
        if (!unified.length) return;
        const csvText = global.InvoiceIQUnified.csvBody(unified);
        const blob = new Blob([csvText],{type:'text/csv;charset=utf-8'});
        const url=URL.createObjectURL(blob);
        const a=global.document.createElement('a');a.href=url;a.download='invoiceiq-selected.csv';a.click();
        setTimeout(()=>URL.revokeObjectURL(url),1000);
        return;
      }
      const total = getView(p).total;
      for(let page=1;page<=Math.ceil(total/100);page++) {
        p.set('page',String(page));
        rows.push(...getView(p).invoices.filter(r=>picked.has(r.id)));
      }
      if(!rows.length) return;
      const keys=['invoice_number','company','posting_date','due_date','amount_total','amount_paid','balance','status','probability'];
      const blob = new Blob([csv([keys,...rows.map(r=>keys.map(k=>r[k]??''))])],{type:'text/csv;charset=utf-8'});
      const url = URL.createObjectURL(blob);
      const a = global.document.createElement('a');a.href=url;a.download='invoiceiq-selected-scenario.csv';a.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    function chooseColumns() {
      const t = table(); if(!t)return;
      const headers=[...t.querySelectorAll('thead th')];
      const dialog=global.document.createElement('dialog');
      dialog.style.cssText='background:#fff;color:#111;border:1px solid #ccc;border-radius:9px;padding:20px;max-width:460px;width:88%;font:13px system-ui';
      const heading=global.document.createElement('h3');heading.textContent='Visible invoice columns';dialog.append(heading);
      const desc=global.document.createElement('p');desc.textContent='Display preferences only · all invoice data is preserved.';dialog.append(desc);
      headers.forEach((header,index)=>{
        if(index===0 || index===1)return; // selection and invoice ID remain available
        const label=global.document.createElement('label');label.style.cssText='display:flex;gap:10px;align-items:center;padding:7px 0';
        const check=global.document.createElement('input');check.type='checkbox';check.checked=!hidden.has(index);
        check.addEventListener('change',()=>{check.checked?hidden.delete(index):hidden.add(index);paint();});
        label.append(check,global.document.createTextNode(header.textContent.trim()||('Column '+index)));dialog.append(label);
      });
      const close=global.document.createElement('button');close.type='button';close.textContent='Done';close.className='btn';close.addEventListener('click',()=>dialog.close());dialog.append(close);
      dialog.addEventListener('close',()=>dialog.remove());global.document.body.append(dialog);dialog.showModal();
    }
    function paint() {
      const t = table(); if(!t)return;
      [...t.querySelectorAll('tr')].forEach(tr=>[...tr.children].forEach((cell,index)=>{
        if(cell.tagName==='TD'||cell.tagName==='TH')cell.style.display=hidden.has(index)?'none':'';
      }));
      const checkboxes = visibleRows(t).map(tr=>{
        const check=tr.querySelector('input[type="checkbox"]');
        const id=tr.querySelector('a[data-action="evidence"]')?.dataset.id;
        if(!check||!id)return null;
        check.disabled=false;check.checked=picked.has(id);
        if(!check.__iqScenarioBound) {
          check.__iqScenarioBound=true;
          check.addEventListener('change',()=>{check.checked?picked.add(id):picked.delete(id);paint();});
        }
        return {check,id};
      }).filter(Boolean);
      const head = t.querySelector('thead input[type="checkbox"]');
      if(head) {
        head.disabled=false;
        head.checked=!!checkboxes.length && checkboxes.every(r=>picked.has(r.id));
        head.indeterminate=checkboxes.some(r=>picked.has(r.id))&&!head.checked;
        if(!head.__iqScenarioBound){head.__iqScenarioBound=true;
          head.addEventListener('change',()=>{for(const row of checkboxes){head.checked?picked.add(row.id):picked.delete(row.id);}paint();});
        }
      }
      const controls=all('button').filter(b=>b.textContent.trim()==='Columns');
      for(const button of controls){button.disabled=false;button.title='Choose visible columns';
        if(!button.__iqScenarioBound){button.__iqScenarioBound=true;button.addEventListener('click',chooseColumns);}
      }
      const footer=t.closest('section')?.querySelector('div[style*="justify-content: space-between"]');
      if(footer){let exportButton=footer.querySelector('[data-scenario-export-selected]');
        if(!exportButton){exportButton=global.document.createElement('button');exportButton.className='btn';exportButton.type='button';
          exportButton.dataset.scenarioExportSelected='1';exportButton.addEventListener('click',exportPicked);footer.append(exportButton);}
        exportButton.textContent='Export selected ('+picked.size+')';exportButton.disabled=picked.size===0;
      }
    }
    global.setInterval(paint,900);
    global.document.addEventListener('DOMContentLoaded',paint,{once:true});
  }
  global.InvoiceIQScenario={active,getView,detailFor,getReviews,getHistory,cases,invoices,companies};
})(window);
