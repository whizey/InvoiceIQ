/* One public read-only dashboard. Pseudonymized Neon invoices + clearly attributed
 * local illustrative operational scenarios. Does not alter Neon or authorize writes. */
(function (global) {
  'use strict';
  const scenario = global.InvoiceIQScenario;
  if (!scenario) throw Error('InvoiceIQ scenario adapter missing');
  const upstream = global.fetch.bind(global);
  const SOURCE_CUTOFF = '2016-03-17';
  const numeric = value => Number(value || 0);
  const decimal = cents => (cents / 100).toFixed(2);
  const addAmounts = (a, b) => decimal(Math.round(numeric(a)*100)+Math.round(numeric(b)*100));
  const sumFields = ['invoiced','billed','open','overdue','collected'];
  const countFields = ['invoice_count','company_count','open_count','overdue_count',
    'scored_open','partial_count','settled_count','cancelled_count','written_off_count'];
  const asResponse = (payload, status=200, type='application/json') =>
    new Response(type==='application/json' ? JSON.stringify(payload) : payload,
      {status,headers:{'Content-Type':type,'Cache-Control':'no-store'}});
  const COLUMN_FIELDS = ['invoice_number','company_search','posted_from','posted_to',
    'due_from','due_to','amount_min','amount_max','paid_min','paid_max','balance_min','balance_max',
    'days_min','days_max','risk_min','risk_max','case_presence'];
  const advancedFiltersUsed = q => COLUMN_FIELDS.some(key => {
    const value=q.get(key); return value!==null && value!=='' && !(key==='case_presence'&&value==='any');
  });
  const sourceQuery = q => {
    const p=new URLSearchParams(q);
    for(const key of ['preset','case_id','scenario']) p.delete(key);
    if (!p.has('as_of') || p.get('as_of')>SOURCE_CUTOFF) p.set('as_of',SOURCE_CUTOFF);
    // Comparison is fetched explicitly at its own cutoff below; never let the backend
    // silently drop a date that is newer than the historical 2016 source cutoff.
    p.delete('compare_date');
    // Latest Neon import is historical. A future local 'as of' date cannot extend it.
    return p;
  };
  const periodAdd = (first,second) => {
    const merged=new Map();
    for(const row of [...(first||[]),...(second||[])]) {
      const previous=merged.get(row.period)||{period:row.period,invoiced:'0',collected:'0',open:null};
      for(const key of ['invoiced','collected']) previous[key]=addAmounts(previous[key],row[key]);
      if(row.open!=null) previous.open=addAmounts(previous.open||'0',row.open);
      merged.set(row.period,previous);
    }
    return [...merged.values()].sort((a,b)=>a.period.localeCompare(b.period));
  };
  const groupAdd = (first,second,keys) => {
    const merged=new Map();
    for(const row of [...(first||[]),...(second||[])]) {
      const key=String(row[keys]);
      const existing=merged.get(key)||{key,count:0,amount:'0'};
      existing.count+=numeric(row.count);
      existing.amount=addAmounts(existing.amount,row.amount);
      merged.set(key,existing);
    }
    return [...merged.values()];
  };
  const dailyAdd = (first,second) => {
    const map=new Map();
    for(const row of [...(first||[]),...(second||[])])
      map.set(row.date,addAmounts(map.get(row.date)||'0',row.amount));
    return [...map].map(([date,amount])=>({date,amount})).sort((a,b)=>a.date.localeCompare(b.date));
  };
  const combineMetrics = (real,simulated) => {
    const m={...real,...simulated};
    for(const key of sumFields) m[key]=addAmounts(real?.[key],simulated?.[key]);
    for(const key of countFields) m[key]=numeric(real?.[key])+numeric(simulated?.[key]);
    m.overdue_share=numeric(m.open)>0 ? Number((100*numeric(m.overdue)/numeric(m.open)).toFixed(2)):null;
    // Medians cannot be added; a combined median requires the full distribution.
    m.median_invoice=numeric(real?.invoice_count)>0 && numeric(simulated?.invoice_count)>0
      ? null : numeric(real?.invoice_count)>0 ? real.median_invoice : simulated?.median_invoice;
    return m;
  };
  const sortRows = (a,b,sort) => {
    const n=key=>numeric(key);
    const byNum=(aVal,bVal)=>n(aVal)-n(bVal)||a.id.localeCompare(b.id);
    const byText=(aVal,bVal)=>String(aVal||'').localeCompare(String(bVal||''))||a.id.localeCompare(b.id);
    if(sort==='invoice_asc')return byText(a.invoice_number,b.invoice_number);
    if(sort==='invoice_desc')return byText(b.invoice_number,a.invoice_number);
    if(sort==='company_asc')return byText(a.company,b.company);
    if(sort==='company_desc')return byText(b.company,a.company);
    if(sort==='due_asc'||sort==='dpd_desc')return byText(a.due_date,b.due_date);
    if(sort==='due_desc'||sort==='dpd_asc')return byText(b.due_date,a.due_date);
    if(sort==='paid_asc')return byNum(a.amount_paid,b.amount_paid);
    if(sort==='paid_desc')return byNum(b.amount_paid,a.amount_paid);
    if(sort==='balance_asc')return byNum(a.balance,b.balance);
    if(sort==='balance_desc')return byNum(b.balance,a.balance);
    if(sort==='risk_asc')return byNum(a.probability??Infinity,b.probability??Infinity);
    if(sort==='risk_desc')return byNum(b.probability??-Infinity,a.probability??-Infinity);
    if(sort==='amount_desc') return n(b.amount_total)-n(a.amount_total)||a.id.localeCompare(b.id);
    if(sort==='amount_asc') return n(a.amount_total)-n(b.amount_total)||a.id.localeCompare(b.id);
    if(sort==='posting_desc') return b.posting_date.localeCompare(a.posting_date)||a.id.localeCompare(b.id);
    if(sort==='posting_asc') return a.posting_date.localeCompare(b.posting_date)||a.id.localeCompare(b.id);
    return a.due_date.localeCompare(b.due_date)||a.id.localeCompare(b.id);
  };
  const seen = new Map();
  const historyCache = new Map();
  const HISTORY_CACHE_MS = 120000;
  const loadScenarioRows = (q, limit, count) => {
    const all=[];let page=1;
    while(all.length<Math.min(count,limit)) {
      const p=new URLSearchParams(q);p.set('page',String(page++));p.set('page_size','100');
      const chunk=scenario.getView(p);
      if(chunk.error)throw Error(chunk.error);
      all.push(...chunk.invoices.map(r=>({...r,record_origin:'illustrative_scenario'})));
      if(!chunk.invoices.length||page>6)break;
    }
    return all;
  };
  const historicLatestByAxis = Object.freeze({
    posting:'2016-02-25', due:'2016-04-03', payment:'2016-03-17'
  });
  const emptyHistoricalPeriod = q => {
    const start=q.get('start_date');
    const axis=q.get('date_basis')||'posting';
    return Boolean(start && historicLatestByAxis[axis] && start>historicLatestByAxis[axis]);
  };
  const emptyHistoricalResponse = () => ({
    metrics:Object.fromEntries([...sumFields,...countFields].map(k=>[k,0])),
    invoices:[],series:[],aging:[],risk:[],due:[],daily:[],top:[],companies:[],
    total:0,page:1,page_size:100,historical_available:true,as_of:SOURCE_CUTOFF
  });
  const fetchReal = async (q,page,signal) => {
    if (emptyHistoricalPeriod(q)) return emptyHistoricalResponse();
    if(signal?.aborted) throw Error('Request aborted');
    const query=sourceQuery(q);query.set('page_size','100');query.set('page',String(page));
    const key=query.toString(), prior=historyCache.get(key);
    if(prior && Date.now()-prior.at<HISTORY_CACHE_MS) return prior.body;
    if(prior) historyCache.delete(key);
    const response=await upstream('/api/finance/overview?'+key,{cache:'no-store',signal});
    const body=await response.json().catch(()=>({}));
    if(!response.ok)throw Error('Neon historical endpoint returned HTTP '+response.status+(body.detail?' · '+body.detail:''));
    if(!body.metrics||!Array.isArray(body.invoices)||!Array.isArray(body.series))throw Error('Unexpected Neon dashboard response');
    // FastAPI ignores unknown query parameters on old deployments. Reject the
    // response until server support is live, rather than displaying false totals.
    if(advancedFiltersUsed(q) && body.column_filters_version!==1)
      throw Error('Historical column filters require deploying the updated InvoiceIQ backend.');
    if(!signal?.aborted){
      historyCache.set(key,{body,at:Date.now()});
      if(historyCache.size>40) historyCache.delete(historyCache.keys().next().value);
    }
    return body;
  };
  const merge = (real,sim,rows,q,sourceError,priorReal) => {
    const m=combineMetrics(real?.metrics,sim.metrics);
    const bands=groupAdd(real?.aging,sim.aging,'key');
    const risk=groupAdd(real?.risk,sim.risk,'key');
    const due=groupAdd(real?.due,sim.due,'key');
    const tops=[...(real?.top||[]),...(sim.top||[])].sort((a,b)=>numeric(b.overdue)-numeric(a.overdue)).slice(0,7);
    const companies=new Map();
    for(const c of [...(real?.companies||[]),...(sim.companies||[])])companies.set(c.id,c);
    // No fallback to current metrics: a missing prior snapshot is NOT a valid comparison.
    const compare = q.get('compare_date') && sim.comparison
      ? combineMetrics(priorReal,sim.comparison) : null;
    const sourceMessage=sourceError
      ? 'Historical Neon records could not load: '+sourceError+'. Only illustrative workflow data is shown.'
      : 'Includes '+numeric(real?.metrics?.invoice_count).toLocaleString('en-IN')+' reconciled historical B2B invoices and clearly identified illustrative operational scenarios. Scenario activity is not a verified recovery outcome.';
    return {
      ...sim,
      generated_at:new Date().toISOString(),
      workspace: 'InvoiceIQ · public read-only',
      historical_available:!!real && real.historical_available!==false,
      historical_reason:sourceMessage,
      historical_source_connected:!sourceError,
      historical_source_count:numeric(real?.metrics?.invoice_count),
      illustrative_source_count:numeric(sim?.metrics?.invoice_count),
      contains_scenarios:true,
      scenario_billed:sim.metrics.billed,
      metrics:m,comparison:compare,
      comparison_as_of:q.get('compare_date')||null,
      comparison_coverage:compare ? (sourceError ? 'illustrative_only' : 'unified') : 'none',
      currency_verified:!(real?.metrics?.invoice_count>0) && sim.currency_verified,
      currency_note:!sourceError && numeric(real?.metrics?.invoice_count)>0
        ? '₹ values shown for display; historic B2B CSV has no verified currency field. Illustrative operational invoices use explicit INR. No conversion performed.'
        : sim.currency_note,
      series:periodAdd(real?.series,sim.series),aging:bands,risk,due,
      daily:dailyAdd(real?.daily,sim.daily),
      companies:[...companies.values()].sort((a,b)=>a.name.localeCompare(b.name)),
      top:tops,
      invoices:rows,
      total:numeric(real?.total)+numeric(sim.total),
      page:Math.max(1,numeric(q.get('page'))||1),
      page_size:Math.min(100,Math.max(1,numeric(q.get('page_size'))||12)),
      chart_notes:{...sim.chart_notes,
        bridge:'This Q3 reconciliation covers illustrative operational invoices in the current quarter; historic B2B import is from 2011–2016.',
        pipeline:'Illustrative operational case stages. The historic Neon import contains no recovery cases.',
        settlement:'Illustrative settlement outcomes only; no historical recovery attribution is asserted.',
        allocation:'Illustrative operating-period invoice allocation; historical B2B source lacks a loss ledger.'}
    };
  };
  const requestOverview = async (q,opts={}) => {
    const sim=scenario.getView(q);
    if(sim.error) return asResponse({detail:sim.error},sim.status||422);
    const compareDate = q.get('compare_date');
    const effectiveAsOf = q.get('as_of') || sim.as_of;
    if (compareDate && (!/^\d{4}-\d{2}-\d{2}$/.test(compareDate) ||
        Number.isNaN(Date.parse(compareDate+'T00:00:00Z')) || compareDate >= effectiveAsOf)) {
      return asResponse({detail:'Comparison date must be earlier than the selected As of date.'},422);
    }
    const page=Math.max(1,numeric(q.get('page'))||1);
    const size=Math.min(100,Math.max(1,numeric(q.get('page_size'))||12));
    const requested=page*size;
    let real=null,sourceError=null,priorReal=null;
    let allReal=[];
    try {
      real=await fetchReal(q,1,opts.signal);
      allReal.push(...real.invoices.map(r=>({...r,record_origin:'historical_b2b'})));
      const pages=Math.ceil(Math.min(numeric(real.total),requested)/100);
      for(let p=2;p<=pages;p++){
        const r=await fetchReal(q,p,opts.signal);
        allReal.push(...r.invoices.map(row=>({...row,record_origin:'historical_b2b'})));
      }
    }catch(error){
      if(opts.signal?.aborted)throw error;
      sourceError=String(error?.message||error);
      // Refuse to display plausible-looking incomplete totals on historical outages.
      if (!emptyHistoricalPeriod(q)) return asResponse({
        detail:sourceError?.includes('column filters require deploying') ? 'Historical column filters need the updated FastAPI backend on Render before querying 2011–2016 invoices.' : 'Historical records could not be verified. Combined totals are unavailable; retry shortly.'
      },503);
      real=null;allReal=[];
    }
    if (compareDate && real) {
      // Historic imports do not change after their original reconciled cutoff.
      // A 2026 comparison still uses the same 2016 history; only the genuinely
      // dated illustrative operating portfolio can change after that date.
      const nowRealCutoff=sourceQuery(q).get('as_of');
      const priorCutoff=compareDate > SOURCE_CUTOFF ? SOURCE_CUTOFF : compareDate;
      if (priorCutoff === nowRealCutoff) priorReal=real.metrics;
      else {
        const priorQuery=new URLSearchParams(q);
        priorQuery.delete('compare_date');
        priorQuery.set('as_of',priorCutoff);
        try { priorReal=(await fetchReal(priorQuery,1,opts.signal)).metrics; }
        catch(error) {
          if(opts.signal?.aborted)throw error;
          return asResponse({detail:'Cannot verify the historical comparison at '+priorCutoff+': '+
            String(error?.message||error)},503);
        }
      }
    }
    const allScenario=loadScenarioRows(q,requested,sim.total);
    const sort=q.get('sort')||'posting_desc';
    const ordered=[...allReal,...allScenario].sort((a,b)=>sortRows(a,b,sort));
    for(const r of ordered)seen.set(r.id,r);
    const slice=ordered.slice((page-1)*size,requested);
    return asResponse(merge(real,sim,slice,q,sourceError,priorReal));
  };
  const escapeCSV = value => {
    const t=String(value??'');
    return '"'+(/^[\s]*[=+@\-\t\r]/.test(t)?"'"+t:t).replaceAll('"','""')+'"';
  };
  const csvHeaders=['invoice_number','company','posting_date','due_date','amount_total',
    'amount_paid','balance','days_past_due','status','probability','case_id','record_origin'];
  const csvBody = rows => '\ufeff'+[csvHeaders,...rows.map(r=>csvHeaders.map(k=>r[k]??''))]
    .map(row=>row.map(escapeCSV).join(',')).join('\r\n');
  const exportResponse = async (q,opts) => {
    const counter=await requestOverview(q,opts);
    if(!counter.ok)return counter;
    const result=await counter.json();
    if(!result.historical_source_connected)return asResponse({detail:'Neon historical data unavailable. Export blocked to prevent an incomplete CSV.'},503);
    if(result.total>10000)return asResponse({detail:'More than 10,000 records. Narrow date, company or status filters for complete CSV export.'},422);
    const list=[]; const pages=Math.ceil(result.total/100);
    for(let page=1;page<=pages;page++){
      const query=new URLSearchParams(q);query.set('page_size','100');query.set('page',String(page));
      const response=await requestOverview(query,opts);
      if(!response.ok)return response;
      const data=await response.json();
      if(!data.historical_source_connected)return asResponse({detail:'Neon historical data unavailable; export blocked.'},503);
      list.push(...data.invoices);
    }
    return asResponse(csvBody(list),200,'text/csv;charset=utf-8');
  };
  const requestCases = async (opts) => {
    const ops=scenario.cases.map(({invoice_obj,...row})=>({...row,record_origin:'illustrative_scenario'}));
    let neon=[],warning='';
    try {
      const response=await upstream('/api/workspace/cases',{cache:'no-store',...opts});
      if(!response.ok)throw Error('HTTP '+response.status);
      const result=await response.json();
      if(!Array.isArray(result.cases))throw Error('Unexpected cases response');
      neon=result.cases;
    }catch(error){warning='Historical case source unavailable: '+String(error?.message||error);}
    return asResponse({cases:[...ops,...neon],source_note:warning || 'Includes illustrative operational cases.'});
  };
  global.fetch=async function(resource,options){
    const src=typeof resource==='string'?resource:resource instanceof URL?resource.href:resource?.url;
    if(src){
      const url=new URL(src,global.location.origin);
      if(url.origin===global.location.origin){
        if(url.pathname==='/api/finance/overview')return requestOverview(url.searchParams,options||{});
        if(url.pathname==='/api/finance/export')return exportResponse(url.searchParams,options||{});
        if(url.pathname==='/api/workspace/cases')return requestCases(options||{});
      }
    }
    return upstream(resource,options);
  };
  global.InvoiceIQUnified={sourceQuery,combineMetrics,sortRows,requestOverview,seen,
    selectedRows: ids => ids.map(id=>seen.get(id)).filter(Boolean),
    csvBody};
})(window);
