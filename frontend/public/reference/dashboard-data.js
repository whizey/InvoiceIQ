/* Data adapter for the retained HTML templates. No runtime or layout replacement. */
(function (global) {
  'use strict';
  const colors = ['#D4D4D1', '#8A8A87', '#3F3F3F', '#F0F0EE'];
  const names = ['Invoiced', 'Open receivables', 'Overdue exposure', 'Overdue share', 'Collected', 'Score coverage'];
  function money(value, currency, verified, compact) {
    if (value === null || value === undefined || typeof value === 'boolean' || String(value).trim() === '' || !Number.isFinite(Number(value))) return 'Unavailable';
    const amount = Number(value);
    if (!verified) return new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(amount) + ' ' + currency + '*';
    if (currency !== 'INR') return new Intl.NumberFormat('en-IN', { style: 'currency', currency, maximumFractionDigits: 2 }).format(amount);
    const negative = amount < 0 ? '−' : '';
    const absolute = Math.abs(amount);
    let suffix = '', scaled = absolute;
    if (absolute >= 10000000) { scaled /= 10000000; suffix = 'Cr'; }
    else if (absolute >= 100000) { scaled /= 100000; suffix = 'L'; }
    else if (absolute >= 1000) { scaled /= 1000; suffix = 'k'; }
    const valueText = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(scaled);
    return negative + '₹' + valueText + suffix;
  }
  function range(preset, asOf) {
    const end = new Date(asOf + 'T00:00:00Z'), start = new Date(end);
    if (preset === 'All data') return { start_date: '', end_date: '' };
    if (preset === 'MTD') start.setUTCDate(1);
    if (preset === 'QTD') start.setUTCMonth(Math.floor(start.getUTCMonth() / 3) * 3, 1);
    if (preset === 'YTD') start.setUTCMonth(0, 1);
    if (preset === '90D') start.setUTCDate(start.getUTCDate() - 89);
    if (preset === '12M') { start.setUTCDate(start.getUTCDate()+1);const day=start.getUTCDate();start.setUTCDate(1);start.setUTCFullYear(start.getUTCFullYear()-1);const last=new Date(Date.UTC(start.getUTCFullYear(),start.getUTCMonth()+1,0)).getUTCDate();start.setUTCDate(Math.min(day,last)); }
    return { start_date: start.toISOString().slice(0,10), end_date: asOf };
  }
  function link(logic, extra, path) {
    const q = new URLSearchParams(logic.query);q.delete('page');
    for (const [k,v] of Object.entries(extra || {})) { if(v)q.set(k,v);else q.delete(k); }
    return (path || '/invoices') + '?' + q;
  }
  function vals(logic) {
    logic.query ||= new URLSearchParams(global.location?.search || '');
    const d = logic.state.data, m = d ? d.metrics : {}, accent = logic.props.accent || '#C2410C';
    const pending = !d && !logic.state.error;
    const missing = '—';
    const fmt = value => value == null ? missing : money(value, d?.currency || 'INR', d?.currency_verified ?? true, true);
    const n = value => value == null || !Number.isFinite(Number(value)) ? missing : Number(value).toLocaleString('en-IN');
    const status = logic.state.error || (logic.state.loading ? (d ? 'Updating source data… Previous results shown.' : 'Loading source data…') : d ? 'Synced ' + new Date(d.generated_at).toLocaleString('en-IN') : 'Connecting…');
    const base = [
      ['POSTING',fmt(m.invoiced),n(m.invoice_count)+' invoices · '+n(m.company_count)+' companies','Posting date in range. Canceled excluded.',{status:'all'},'View invoices'],
      ['AS OF',fmt(m.open),n(m.open_count)+' invoices','Unpaid balance at '+(d?.as_of || 'selected cutoff')+'.',{status:'outstanding'},'View open'],
      ['AS OF',fmt(m.overdue),n(m.overdue_count)+' invoices','Open and past due date.',{status:'overdue'},'View overdue'],
      ['AS OF',m.overdue_share==null?missing:Number(m.overdue_share).toFixed(1)+'%','of open balance','Overdue ÷ open, same cohort and date.',{status:'overdue'},'See aging'],
      ['PAYMENT',fmt(m.collected),'cash receipts in range','Not the same as recovery of this cohort.',{date_basis:'payment'},'View payments'],
      ['OPEN',m.scored_open==null?missing:n(m.scored_open)+' / '+n(m.open_count),'open invoices with a stored score',m.open_count==null||m.scored_open==null?'Score coverage unavailable.':n(m.open_count-m.scored_open)+' not scored. Never shown as 0% risk.',{status:'outstanding',risk:'any'},'View invoices']
    ];
    if(d?.comparison){for(const [index,key] of [[1,'open'],[2,'overdue']]){if(m[key]!=null&&d.comparison[key]!=null)base[index][2]+=' · '+fmt(Number(m[key])-Number(d.comparison[key]))+' vs '+logic.query.get('compare_date');}}
    const kpis = base.map((r,i)=>({label:names[i],basis:r[0],value:r[1],sub:r[2],def:r[3],spark:'',color:i===2?accent:'#0B0B0B',href:link(logic,r[4]),link:r[5]}));
    const series = d?.series || [], maximum = Math.max(1,...series.flatMap(p=>[Number(p.invoiced),Number(p.collected),Number(p.open||0)]));
    const openSegments=[];let segment=[];
    series.forEach((p,i)=>{if(p.open==null){if(segment.length)openSegments.push({points:segment.join(' ')});segment=[];}else segment.push(((i+.5)*1400/series.length).toFixed(1)+','+(220-Number(p.open)/maximum*220).toFixed(1));});
    if(segment.length)openSegments.push({points:segment.join(' ')});
    const spark=values=>{if(values.length<2||values.some(v=>v==null))return '';const max=Math.max(...values.map(Number)),min=Math.min(...values.map(Number));return values.map((v,i)=>(i*120/(values.length-1)).toFixed(1)+','+(29-(Number(v)-min)/(max-min||1)*26).toFixed(1)).join(' ');};
    kpis[0].spark=spark(series.map(p=>p.invoiced));kpis[1].spark=spark(series.map(p=>p.open));kpis[4].spark=spark(series.map(p=>p.collected));
    const bars=series.map(p=>({m:p.period,label:bucketLabel(p.period,logic.query.get('grain')||'month'),ih:Math.max(0,Number(p.invoiced)/maximum*220),ch:Math.max(0,Number(p.collected)/maximum*220),tip:p.period+' · Invoiced '+fmt(p.invoiced)+' · Collected '+fmt(p.collected)}));
    const agingKeys=['not_due','1-30','31-60','61-90','90+'], agingLabels=['Not due','1–30 days','31–60 days','61–90 days','90+ days'];
    const aging=agingKeys.map((key,i)=>{const a=d?.aging.find(r=>r.key===key),available=d?.historical_available;return{band:agingLabels[i],n:available?n(a?.count):'—',amt:available?fmt(a?.amount):missing,share:available&&Number(m.open)>0?((Number(a?.amount||0)/Number(m.open))*100).toFixed(1)+'%':'—',w:available&&Number(m.open)>0?Number(a?.amount||0)/Number(m.open)*100:0,c:colors[i]||accent};});
    const riskKeys=['0–20%','20–40','40–60','60–80','80–100','Not scored'];
    const riskMax=Math.max(1,...(d?.risk||[]).map(r=>r.count));
    const risk=riskKeys.map((b,i)=>{const a=d?.risk.find(r=>r.key===b);return{b,n:d?.historical_available?n(a?.count):'—',h:d?.historical_available?(a?.count||0)/riskMax*120:0,style:i===5?'background: repeating-linear-gradient(135deg,#BDBDBA 0 2px,#FFFFFF 2px 6px); border: 1px dashed #8A8A87':'background: '+(i===4?accent:i===3?'#3F3F3F':'#8A8A87')};});
    const top=(d?.top||[]).map(t=>({name:t.name,id:t.id,inv:t.count+' open',open:fmt(t.open),od:fmt(t.overdue),share:Number(m.overdue)?Number(t.overdue)/Number(m.overdue)*100:0,dpd:t.oldest+' d',risk:t.risk==null?'Not scored':Number(t.risk).toFixed(2),case:'See invoices',href:link(logic,{company_id:t.id})}));
    const weeks=[];const dayEnd=new Date((d?.as_of||'2000-01-01')+'T00:00:00Z');dayEnd.setUTCDate(dayEnd.getUTCDate()-((dayEnd.getUTCDay()+6)%7)+6);
    const first=new Date(dayEnd);first.setUTCDate(first.getUTCDate()-181);
    const heatMax=Math.max(1,...(d?.daily||[]).map(r=>Math.abs(Number(r.amount))));const shades=['#EDEDEB','#C9C9C6','#8A8A87','#3F3F3F','#0B0B0B'];
    const dailyByDate=new Map();
    for(const r of d?.daily||[]){
      if(!dailyByDate.has(r.date))dailyByDate.set(r.date,r);
    }
    for(let w=0;w<26;w++){const days=[];let month='';for(let i=0;i<7;i++){const dt=new Date(first);dt.setUTCDate(first.getUTCDate()+w*7+i);const key=dt.toISOString().slice(0,10),row=dailyByDate.get(key),amount=Number(row?.amount||0);if(i===0&&dt.getUTCDate()<=7)month=dt.toLocaleString('en',{month:'short',timeZone:'UTC'});days.push({c:!d?'#EDEDEB':shades[amount?Math.max(1,Math.ceil(Math.abs(amount)/heatMax*4)):0],t:key+' · '+(d?fmt(amount):missing),href:link(logic,{date_basis:'payment',start_date:key,end_date:key})});}weeks.push({m:month,days});}
    const dueRows=[...(d?.due||[])].sort((a,b)=>a.key==='Past due'?-1:b.key==='Past due'?1:a.key.localeCompare(b.key));const dueMax=Math.max(1,...dueRows.map(r=>Number(r.amount)));
    const due=dueRows.map(r=>({v:fmt(r.amount),h:Number(r.amount)/dueMax*140,l:r.key,c:r.key==='Past due'?accent:'#0B0B0B'}));
    const rows=(d?.invoices||[]).map(r=>{const scored=r.probability!=null;const open=Number(r.balance)>0;const overdue=open&&r.days_past_due>0;const label=r.status==='CANCELLED'?'Canceled':r.status==='WRITTEN_OFF'?'Written off':r.balance===null?'Unverified':open?(Number(r.amount_paid)>0?'Part-paid'+(overdue?' · overdue':''):overdue?'Overdue':'Open'):'Settled';return{id:r.invoice_number,uuid:r.id,co:r.company,cid:r.company_id,terms:r.payment_term==null?'Terms not recorded':'Net '+r.payment_term,post:r.posting_date,due:r.due_date,amt:fmt(r.amount_total),paid:fmt(r.amount_paid),bal:fmt(r.balance),dpd:r.days_past_due==null?missing:open?(r.days_past_due?r.days_past_due+' d':'Not due'):'—',dpdColor:overdue?accent:'#0B0B0B',status:label,statusStyle:'font-size:12px;border:1px solid #D9D9D6;border-radius:999px;padding:3px 9px',scored,unscored:!scored,risk:scored?Number(r.probability).toFixed(2):'Not scored',riskW:scored?Number(r.probability)*100:0,riskC:scored&&Number(r.probability)>=.8?accent:'#0B0B0B',hasCase:!!r.case_id,noCase:!r.case_id,case:r.case_id,caseHref:link(logic,{case_id:r.case_id},'/cases')};});
    return{loading:pending,empty:!!d && !logic.state.loading && !logic.state.error && d.total===0,accent,kpis,bars,openSegments,openLine:'',aging,risk,top,weeks,due,rows,hist:[],
      funnel:['New','Investigating','Awaiting approval','Action taken','Resolved, 30 days'].map((s,i)=>({s,v:missing,w:0,c:colors[i]||'#0B0B0B'})),
      statusRole:logic.state.error?'alert':'status',status,workspace:d?.workspace||'Not connected',scopeLabel:d?n(m.company_count)+' companies · '+d.currency:'Authorized scope required',
      asOf:d?.as_of||'Select date',summary:d?(!logic.state.loading&&m.invoice_count===0?'No data for this selection. ':'')+n(m.invoice_count)+' invoices from '+n(m.company_count)+' companies. '+d.historical_reason:logic.state.error||'Loading original source records…',
      currencyNote:d?.currency_note||'No currency conversion performed.',calendarNote:d?.chart_notes.calendar||'Calendar policy not yet loaded.',
      openTotal:fmt(m.open),overdueTotal:fmt(m.overdue),billedTotal:fmt(m.billed),invoiceCount:n(m.invoice_count),companyCount:n(m.company_count),medianInvoice:fmt(m.median_invoice),partialCount:n(m.partial_count),openCount:n(m.open_count),unpaidCount:m.open_count==null?missing:n(m.open_count-m.partial_count),overdueCount:n(m.overdue_count),settledCount:n(m.settled_count),cancelledCount:n(m.cancelled_count),writtenOffCount:n(m.written_off_count),
      riskNote:d?.historical_available?n(m.scored_open)+' of '+n(m.open_count)+' open invoices scored. '+n(m.open_count-m.scored_open)+' not scored.':'Open-invoice risk population unavailable without historical balances.',
      chartGrainText:'Invoiced by posting '+(logic.query.get('grain')||'month')+' · Collected by payment '+(logic.query.get('grain')||'month')+' · Line is open balance at period end · '+(d?.chart_notes.open_line||'Historical balance unavailable.'),lineNote:d?.chart_notes.open_line||'Historical month-end balance unavailable.',bridgeNote:d?.chart_notes.bridge||'No dated adjustment ledger.',pipelineNote:d?.chart_notes.pipeline||'Stage mapping unavailable.',settlementNote:d?.chart_notes.settlement||'Settlement policy unavailable.',allocationNote:d?.chart_notes.allocation||'Adjustment evidence unavailable.',
      axis:[1,.75,.5,.25,0].map(f=>fmt(maximum*f)),heatMax:fmt(heatMax),heatNote:d?'Cash receipts by payment date through '+d.as_of+'. Each square is one day.':'Loading dated cash receipts…',dueNote:'Contractual dates, not a cash forecast. '+(d?.chart_notes.calendar||''),
      topNote:'Top companies by overdue balance in the selected cohort.',rowLabel:d?'Rows '+(d.total?((d.page-1)*d.page_size+1):0)+'–'+Math.min(d.page*d.page_size,d.total)+' of '+d.total+' · server-side sorting':logic.state.error?'Data unavailable': 'Loading rows…',
      page:d?.page||1,previousDisabled:!d||d.page<=1,nextDisabled:!d||d.page*d.page_size>=d.total,previous:()=>change(logic,{page:String(Math.max(1,(d?.page||1)-1))}),next:()=>change(logic,{page:String((d?.page||1)+1)}),
      unavailable:missing,zero:0
    };
  }
  // Find controls even when events cross a Shadow DOM boundary.
  function controlFor(event, selector) {
    for (const node of (event.composedPath?.() || [])) {
      if (node instanceof Element && node.matches(selector)) return node;
    }
    return event.target instanceof Element ? event.target.closest(selector) : null;
  }
  function controls(selector) {
    const found = [];
    function scan(root) {
      found.push(...root.querySelectorAll(selector));
      for (const n of root.querySelectorAll('*')) if (n.shadowRoot) scan(n.shadowRoot);
    }
    scan(document);
    return found;
  }
  function chartDates(period, grain) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(period)) return null;
    const start = new Date(period+'T00:00:00Z'), end = new Date(start);
    if (Number.isNaN(start.getTime())) return null;
    if (grain==='day') end.setUTCDate(end.getUTCDate()+1);
    else if (grain==='week') end.setUTCDate(end.getUTCDate()+7);
    else if (grain==='quarter') end.setUTCMonth(end.getUTCMonth()+3);
    else if (grain==='year') end.setUTCFullYear(end.getUTCFullYear()+1);
    else end.setUTCMonth(end.getUTCMonth()+1);
    end.setUTCDate(end.getUTCDate()-1);
    return {
      start_date:start.toISOString().slice(0,10),
      end_date:end.toISOString().slice(0,10)
    };
  }

  function groupButtons() {
    const found = [];

    function walk(root) {
      found.push(...root.querySelectorAll('[data-action="grain"]'));
      for (const node of root.querySelectorAll('*')) {
        if (node.shadowRoot) walk(node.shadowRoot);
      }
    }

    walk(document);
    return found;
  }

  function wireGroupButtons(logic) {
    for (const button of groupButtons()) {
      if (button.__iqGrainLogic === logic) continue;

      button.__iqGrainLogic = logic;

      button.addEventListener('click', event => {
        const grain = button.dataset.value;

        if (!['day', 'week', 'month', 'quarter', 'year'].includes(grain)) {
          return;
        }

        event.preventDefault();
        event.stopImmediatePropagation();

        if ((logic.query.get('grain') || 'month') !== grain) {
          change(logic, {grain});
        }
      });
    }
  }

  function bucketLabel(period, grain) {
    const [year, month] = String(period).split('-').map(Number);

    if (!year || !month) return String(period);

    if (grain === 'year') return String(year);
    if (grain === 'quarter') {
      return 'Q' + Math.ceil(month / 3) + ' ' + year;
    }
    if (grain === 'month') {
      return new Date(Date.UTC(year, month - 1, 1))
        .toLocaleDateString('en-GB', {
          month: 'short',
          year: '2-digit',
          timeZone: 'UTC'
        });
    }

    return String(period);
  }

  function change(logic, values){for(const[k,v]of Object.entries(values)){if(v)logic.query.set(k,v);else logic.query.delete(k);}if(!('page'in values))logic.query.delete('page');const query=logic.query.toString();global.history.replaceState(null,'','?'+query);if(global.parent!==global)global.parent.history.replaceState(null,'',global.parent.location.pathname+'?'+query);load(logic);}
  // Only query filters share data. Unknown parameters are treated as scope boundaries.
  function scope(query) {
    const filters = new Set(['start_date','end_date','as_of','compare_date','grain','date_basis','status','risk','search','sort','page','page_size','preset','case_id']);
    return [...query].filter(([key]) => !filters.has(key)).sort().toString();
  }
  async function load(logic) {
    const query = logic.query.toString();
    if (logic.state.loading && logic.pendingQuery === query) return;
    logic.controller?.abort();
    const controller = new AbortController();
    logic.controller = controller;
    logic.pendingQuery = query;
    const currentScope = scope(logic.query);
    logic.setState({loading:true,error:'',data:logic.loadedScope === currentScope ? logic.state.data : null});
    try {
      const params = new URLSearchParams(query);
      for (const key of ['preset','case_id']) params.delete(key);
      const response = await fetch('/api/finance/overview?' + params, {signal:controller.signal,cache:'no-store'});
      const data = await response.json().catch(() => null);
      if (!response.ok) throw Object.assign(new Error(typeof data?.detail === 'string' ? data.detail : 'HTTP ' + response.status), {status:response.status});
      if (!data?.metrics || !Array.isArray(data.invoices) || !Array.isArray(data.series)) throw new Error('Invalid dashboard response');
      if (controller.signal.aborted) return;
      logic.loadedQuery = query;
      logic.loadedScope = currentScope;
      logic.setState({data,loading:false,error:''});
    } catch (error) {
      if (!controller.signal.aborted) {
        const retained = ![401,403].includes(error.status) && logic.loadedScope === currentScope ? logic.state.data : null;
        logic.setState({data:retained,error:error.message,loading:false});
      }
    }
  }
  function dialog(title){const el=document.createElement('dialog');el.style.cssText='border:1px solid #D9D9D6;border-radius:10px;padding:24px;max-width:760px;width:90vw;max-height:85vh;font:13px Geist,system-ui';const h=document.createElement('h2');h.textContent=title;el.append(h);const close=document.createElement('button');close.className='btn';close.textContent='Close';close.onclick=()=>el.close();el.append(close);el.addEventListener('close',()=>el.remove());document.body.append(el);el.showModal();return el;}
  function choose(logic,key,options){const el=dialog(key==='company_id'?'Company':key==='risk'?'Risk':'Sort amount');for(const[value,label]of options){const button=document.createElement('button');button.className='btn';button.textContent=label;button.onclick=()=>{change(logic,{[key]:value});el.close();};el.append(button);}}
  async function evidence(id){const el=dialog('Invoice evidence');try{const response=await fetch('/api/finance/invoices/'+encodeURIComponent(id)+'/evidence');const data=await response.json();if(!response.ok)throw new Error(data.detail||'Evidence unavailable');const p=document.createElement('p');p.textContent=data.invoice.invoice_number+' · '+data.historical_reason;el.append(p);
    const drivers = data.trusted_prediction && Array.isArray(data.explanation?.all_contributions)
      ? data.explanation.all_contributions.filter(item => Number.isFinite(Number(item.shap_value)))
          .sort((a,b) => Math.abs(Number(b.shap_value))-Math.abs(Number(a.shap_value))).slice(0,5) : [];
    if (drivers.length) {
      const chart=document.createElement('section');chart.setAttribute('aria-label','Verified SHAP explanation');
      const title=document.createElement('h3');title.textContent='B2B late-payment prediction · SHAP';chart.append(title);
      const probability=Number(data.invoice.late_payment_probability);
      const summary=document.createElement('p');summary.textContent=
        (Number.isFinite(probability)?`Late-payment probability ${(probability*100).toFixed(1)}% · `:'')+
        `Model ${data.invoice.payment_risk_model_version || 'unknown'}`;chart.append(summary);
      const max=Math.max(...drivers.map(item => Math.abs(Number(item.shap_value))),0.000001);
      for(const item of drivers){
        const row=document.createElement('div');row.style.cssText='margin:12px 0';
        const label=document.createElement('div');label.textContent=
          `${item.label || item.feature}: ${Number(item.shap_value).toFixed(3)} log-odds`;row.append(label);
        const track=document.createElement('div');track.style.cssText='height:8px;background:#EDEDEB;border-radius:4px';
        const bar=document.createElement('div');bar.style.cssText=`height:8px;border-radius:4px;width:${Math.max(4,Math.abs(Number(item.shap_value))/max*100)}%;background:${Number(item.shap_value)>=0?'#C2410C':'#3F3F3F'}`;
        track.append(bar);row.append(track);chart.append(row);
      }
      const note=document.createElement('p');note.textContent='SHAP values are log-odds contributions, not percentage-point changes or permission to contact a customer.';chart.append(note);el.append(chart);
    }
    const pre=document.createElement('pre');pre.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere';pre.textContent=JSON.stringify({invoice:data.invoice.invoice_number,payments:data.payments,prediction_verified:data.trusted_prediction,model_version:data.invoice.payment_risk_model_version,scored_at:data.invoice.payment_risk_scored_at,explanation:data.explanation},null,2);el.append(pre);}catch(e){const p=document.createElement('p');p.textContent=e.message;el.append(p);}}
  function bind(logic,page){logic.query=new URLSearchParams(global.location.search);logic.page=page;
    logic.click=async e=>{const element=controlFor(e,'[data-action],a');if(!element)return;if(element.matches('a')&&!element.hasAttribute('data-action')){if(element.getAttribute('href')?.startsWith('/')&&!element.getAttribute('href').includes('?'))element.href=link(logic,{},element.getAttribute('href'));return;}
      const action=element.dataset.action,value=element.dataset.value;e.preventDefault();
      if(action==='chart-period'){
        const dates=chartDates(value,logic.query.get('grain')||'month');
        const basis=element.dataset.basis;
        if(dates&&(basis==='posting'||basis==='payment')){
          const url=link(logic,{
            ...dates,
            date_basis:basis,
            status:'all',
            preset:null,
            compare_date:null
          },'/invoices');
          (global.parent!==global?global.parent:global).location.assign(url);
        }
        return;
      }
      if(action==='period-scope'){
        const dates = chartDates(
          value,
          logic.query.get('grain') || 'month'
        );

        if(dates){
          const cutoff = logic.state.data?.as_of;

          if(cutoff && dates.end_date > cutoff){
            dates.end_date = cutoff;
          }

          if(dates.start_date <= dates.end_date){
            change(logic,{
              ...dates,
              date_basis:'posting',
              preset:null,
              compare_date:null
            });
          }
        }
        return;
      }
      if(action==='range')change(logic,{...range(value,logic.state.data?.as_of||new Date().toISOString().slice(0,10)),preset:value});
      if(action==='grain')change(logic,{grain:value});
      if(action==='basis')change(logic,{date_basis:value});
      if(action==='status')change(logic,{status:value});
      if(action==='compare'){const el=dialog('Comparison cutoff');const input=document.createElement('input');input.type='date';input.max=logic.state.data?.as_of||'';const apply=document.createElement('button');apply.className='btn';apply.textContent='Compare';apply.onclick=()=>{if(input.value){change(logic,{compare_date:input.value});el.close();}};el.append(input,apply);}
      if(action==='asof'){const el=dialog('As of');const input=document.createElement('input');input.type='date';input.value=logic.state.data?.as_of||'';input.max=logic.state.data?.as_of||'';const apply=document.createElement('button');apply.className='btn';apply.textContent='Apply';apply.onclick=()=>{if(input.value){change(logic,{as_of:input.value});el.close();}};el.append(input,apply);}
      if(action==='refresh')load(logic);
      if(action==='export'){element.disabled=true;try{const response=await fetch('/api/finance/export?'+logic.query);if(!response.ok)throw new Error((await response.json()).detail||'Export failed');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='invoiceiq.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(err){logic.setState({error:err.message});}finally{element.disabled=false;}}
      if(action==='company')choose(logic,'company_id',[['','All'],...(logic.state.data?.companies||[]).map(c=>[c.id,c.name])]);
      if(action==='risk')choose(logic,'risk',[['any','Any'],['low','Low'],['medium','Medium'],['high','High'],['unscored','Not scored']]);
      if(action==='amount')choose(logic,'sort',[['amount_desc','Amount descending'],['amount_asc','Amount ascending'],['dpd_desc','Days past due descending']]);
      if(action==='evidence')evidence(element.dataset.id);
      if(action==='access'){const el=dialog('Workspace access');const input=document.createElement('input');input.type='password';input.autocomplete='off';input.setAttribute('aria-label','Analytics credential');const submit=document.createElement('button');submit.className='btn';submit.textContent='Connect';submit.onclick=async()=>{const response=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:input.value})});if(response.ok){localStorage.setItem('invoiceiq-session-change',crypto.randomUUID());global.location.reload();}else{const p=document.createElement('p');p.textContent='Credential could not be saved';el.append(p);}};
        const disconnect=document.createElement('button');disconnect.className='btn';disconnect.textContent='Disconnect';disconnect.onclick=async()=>{disconnect.disabled=true;try{const response=await fetch('/api/session',{method:'DELETE'});if(!response.ok)throw new Error('Could not disconnect');localStorage.setItem('invoiceiq-session-change',crypto.randomUUID());(global.parent!==global?global.parent:global).location.assign('/connect');}catch(error){disconnect.disabled=false;const p=document.createElement('p');p.textContent=error.message;el.append(p);}};
        el.append(input,submit,disconnect);}
    };
    logic.input=e=>{const target=controlFor(e,'input[type=search]');if(target){clearTimeout(logic.debounce);logic.debounce=setTimeout(()=>change(logic,{search:target.value}),350);}};
    logic.keydown=e=>{if(e.key!=='Enter'&&e.key!==' ')return;const target=controlFor(e,'[data-action=chart-period],[data-action=period-scope]');if(target){e.preventDefault();target.click();}};
    document.addEventListener('click',logic.click);document.addEventListener('input',logic.input);wireGroupButtons(logic);document.addEventListener('keydown',logic.keydown);
    logic.visible=()=>{if(!document.hidden&&!global.parent.document.hidden)load(logic);};
    document.addEventListener('visibilitychange',logic.visible);
    logic.poll=setInterval(logic.visible,60000);load(logic);
  }
  function sync(logic){
    wireGroupButtons(logic);
    for (const b of groupButtons()) {
      b.classList.toggle(
        'on',
        b.dataset.value === (logic.query.get('grain') || 'month')
      );
    }
    const defaults={basis:'posting',grain:'month',range:'All data',status:'all'};
    const keys={basis:'date_basis',grain:'grain',range:'preset',status:'status'};
    controls('[data-action][data-value]').forEach(el=>{if(keys[el.dataset.action])el.classList.toggle('on',el.dataset.value===(logic.query.get(keys[el.dataset.action])||defaults[el.dataset.action]));});
    controls('input[type=search]').forEach(el=>{if(el!==document.activeElement)el.value=logic.query.get('search')||'';});
  }
  function unbind(logic){logic.controller?.abort();clearInterval(logic.poll);document.removeEventListener('visibilitychange',logic.visible);clearTimeout(logic.debounce);document.removeEventListener('click',logic.click);document.removeEventListener('input',logic.input);document.removeEventListener('keydown',logic.keydown);}
  global.InvoiceIQ={money,range,vals,bind,unbind,sync};
})(window);
