/* Data adapter for the retained HTML templates. No runtime or layout replacement. */
(function (global) {
  'use strict';
  const colors = ['#D4D4D1', '#8A8A87', '#3F3F3F', '#F0F0EE'];
  const names = ['Total billed', 'Still owed to you', 'Overdue', 'Share that is late', 'Payments received', 'Risk scores'];
  function money(value, currency, verified, compact) {
    if (value === null || value === undefined || typeof value === 'boolean' || String(value).trim() === '' || !Number.isFinite(Number(value))) return 'Unavailable';
    const amount = Number(value);
    if (!verified && currency !== 'INR') return new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(amount) + ' ' + currency + '*';
    if (currency !== 'INR') return new Intl.NumberFormat('en-IN', { style: 'currency', currency, maximumFractionDigits: 2 }).format(amount);
    const negative = amount < 0 ? '−' : '';
    const absolute = Math.abs(amount);
    let suffix = '', scaled = absolute;
    if (absolute >= 10000000) { scaled /= 10000000; suffix = 'Cr'; }
    else if (absolute >= 100000) { scaled /= 100000; suffix = 'L'; }
    else if (absolute >= 1000) { scaled /= 1000; suffix = 'k'; }
    const valueText = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(scaled);
    return negative + '₹' + valueText + (verified ? suffix : suffix ? ' ' + suffix : '') + (verified ? '' : '*');
  }
  function readableDate(value) {
    if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return value || '—';
    return new Date(value + 'T00:00:00Z').toLocaleDateString('en-IN',
      { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
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
    if(logic.query.get('portal')==='1' && (!path || path==='/invoices')) q.set('view','analytics');
    return (path || '/invoices') + '?' + q;
  }
  function vals(logic) {
    logic.query ||= new URLSearchParams(global.location?.search || '');
    const d = logic.state.data, m = d ? d.metrics : {}, accent = logic.props.accent || '#C2410C';
    const pending = !d && !logic.state.error;
    const missing = '—';
    const fmt = value => value == null ? missing : money(value, d?.currency || 'INR', d?.currency_verified ?? true, true);
    const n = value => value == null || !Number.isFinite(Number(value)) ? missing : Number(value).toLocaleString('en-IN');
    const status = logic.state.error || (logic.state.loading ? (d ? 'Updating source data… Previous results shown.' : 'Loading source data…') : d?.historical_source_connected === false
      ? 'Partial results · historical source unavailable'
      : d ? 'Report generated ' + new Date(d.generated_at).toLocaleString('en-IN',
        { day: 'numeric', month: 'short', year: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'Connecting…');
    const base = [
      ['THIS PERIOD',fmt(m.invoiced),n(m.invoice_count)+' invoices · '+n(m.company_count)+' customers','Everything invoiced in the selected period. Cancelled invoices are left out.',{status:'all'},'View invoices'],
      ['AS OF DATE',fmt(m.open),n(m.open_count)+' unpaid invoices','What customers still have to pay, as of '+(d?.as_of || 'the selected date')+'.',{status:'outstanding'},'View open'],
      ['AS OF DATE',fmt(m.overdue),n(m.overdue_count)+' invoices','Still owed and already past the agreed due date.',{status:'overdue'},'View overdue'],
      ['AS OF DATE',m.overdue_share==null?missing:Number(m.overdue_share).toFixed(1)+'%','of the money owed to you','How much of what you are owed is already late.',{status:'overdue'},'See aging'],
      ['THIS PERIOD',fmt(m.collected),'money received in the period','Payments that came in during the selected period, for any invoice.',{date_basis:'payment'},'View payments'],
      ['UNPAID',m.scored_open==null?missing:n(m.scored_open)+' / '+n(m.open_count),'unpaid invoices with a late-payment risk score',m.open_count==null||m.scored_open==null?'Risk scores unavailable.':'A model estimates how likely each unpaid invoice is to be paid late. '+n(m.open_count-m.scored_open)+' not scored yet; never shown as 0% risk.',{status:'outstanding',risk:'any'},'View invoices']
    ];
    const comparedDate=logic.query.get('compare_date');
    const previous=d?.comparison;
    const deltaMoney=(key)=>{
      if(m[key]==null||previous?.[key]==null)return 'Comparison unavailable';
      const movement=Number(m[key])-Number(previous[key]);
      const sign=movement>0?'+':movement<0?'−':'';
      const amount=fmt(Math.abs(movement));
      const pct=Number(previous[key]) ? ' · '+(100*movement/Math.abs(Number(previous[key]))).toFixed(1)+'%' :
        movement ? ' · new since cutoff' : ' · unchanged';
      return sign+amount+pct;
    };
    const deltaPoints=(now,prior,label)=>{
      if(now==null||prior==null)return 'Not comparable at this cutoff';
      const movement=Number(now)-Number(prior);
      return (movement>0?'+':'')+movement.toFixed(1)+' '+label;
    };
    const ratio=(a,b)=>Number(b)>0?100*Number(a)/Number(b):null;
    const comparisonDeltas=previous?[
      deltaMoney('invoiced'),deltaMoney('open'),deltaMoney('overdue'),
      deltaPoints(m.overdue_share,previous.overdue_share,'percentage points'),
      deltaMoney('collected'),
      deltaPoints(ratio(m.scored_open,m.open_count),ratio(previous.scored_open,previous.open_count),'percentage points')
    ]:[];
    const kpis = base.map((r,i)=>({label:names[i],basis:r[0],value:r[1],sub:r[2],def:r[3],spark:'',color:i===2?accent:'#0B0B0B',href:link(logic,r[4]),link:r[5],
      compared:!!previous,delta:previous?'vs '+comparedDate+' · '+comparisonDeltas[i]:''}));
    const series = d?.series || [], maximum = Math.max(1,...series.flatMap(p=>[Number(p.invoiced),Number(p.collected),Number(p.open||0)]));
    const openSegments=[];let segment=[];
    series.forEach((p,i)=>{if(p.open==null){if(segment.length)openSegments.push({points:segment.join(' ')});segment=[];}else segment.push(((i+.5)*1400/series.length).toFixed(1)+','+(220-Number(p.open)/maximum*220).toFixed(1));});
    if(segment.length)openSegments.push({points:segment.join(' ')});
    const spark=values=>{if(values.length<2||values.some(v=>v==null))return '';const max=Math.max(...values.map(Number)),min=Math.min(...values.map(Number));return values.map((v,i)=>(i*120/(values.length-1)).toFixed(1)+','+(29-(Number(v)-min)/(max-min||1)*26).toFixed(1)).join(' ');};
    kpis[0].spark=spark(series.map(p=>p.invoiced));kpis[1].spark=spark(series.map(p=>p.open));kpis[4].spark=spark(series.map(p=>p.collected));
    const bars=series.map(p=>({m:p.period,label:bucketLabel(p.period,logic.query.get('grain')||'month'),ih:Math.max(0,Number(p.invoiced)/maximum*220),ch:Math.max(0,Number(p.collected)/maximum*220),tip:p.period+' · Invoiced '+fmt(p.invoiced)+' · Collected '+fmt(p.collected)}));
    const agingKeys=['not_due','1-30','31-60','61-90','90+'], agingLabels=['Not due','1–30 days','31–60 days','61–90 days','90+ days'];
    const aging=agingKeys.map((key,i)=>{const a=d?.aging.find(r=>r.key===key),available=d?.historical_available;return{band:agingLabels[i],n:available?n(a?.count):'—',amt:available?fmt(a?.amount):missing,share:available&&Number(m.open)>0?((Number(a?.amount||0)/Number(m.open))*100).toFixed(1)+'%':'—',w:available&&Number(m.open)>0?Number(a?.amount||0)/Number(m.open)*100:0,c:colors[i]||accent};});
    const scenario = global.InvoiceIQScenario?.active && d?.contains_scenarios === true;
    const scenarioData = scenario ? d : null;
    const allocation = scenarioData?.allocation || null;
    const parts = ['collected','not_due','overdue','written_off','cancelled'];
    // Scenario-only allocation: the heading must not include historical billed.
    const billed = scenarioData ? Number(scenarioData.scenario_billed ?? 0) : Number(m.billed ?? 0);
    const allocationPct = Object.fromEntries(parts.map(key=>[key,allocation&&billed>0?100*Number(allocation[key])/billed:0]));
    const histogram = (scenarioData?.histogram||[]).map(h=>({...h,h:Math.round((h.n/Math.max(1,...scenarioData.histogram.map(x=>x.n)))*132),c:h.b==='On due'?'#525252':'#0B0B0B'}));
    const portalRec = logic.query.get('portal')==='1' ? logic.portalRecovery : null;
    const pipeline = scenarioData?.pipeline || (portalRec ? {awaiting:portalRec.kpis.awaiting_review,escalated:portalRec.kpis.escalated,approved:portalRec.kpis.approved,rejected:portalRec.kpis.rejected,exposure:Number(portalRec.kpis.case_exposure)*100,real:true} : undefined);
    const bridgeKeys=['openStart','invoiced','collected','writtenOff','openEnd'];
    const portalBridge=(!scenarioData&&d?.bridge?.start_date&&d.bridge.end_date&&d.bridge.opening!==undefined)?{openStart:Number(d.bridge.opening),invoiced:Number(d.bridge.invoiced),collected:Number(d.bridge.collected),writtenOff:Number(d.bridge.written_off),openEnd:Number(d.bridge.closing)}:null;
    const bridge=scenarioData?.bridge||portalBridge;
    const bridgeValues=bridgeKeys.map(key=>Number(bridge?.[key]||0));
    const bridgeMax=Math.max(1,...bridgeValues);
    const bridgeHeight=bridgeValues.map(n=>bridge?Math.max(3,100*n/bridgeMax):0);
    const bridgeLabelBottom=bridgeHeight.map((height,i)=>([0,52,78,0,0][i]+height+8));
    const bridgeText=bridgeValues.map(n=>bridge?money(n,'INR',true,true):missing);
    const dayLabel=iso=>iso?new Date(iso+'T00:00:00Z').toLocaleDateString('en-IN',{day:'numeric',month:'short',year:'2-digit',timeZone:'UTC'}):'';
    const bridgeStartLabel=portalBridge?'Open '+dayLabel(new Date(Date.parse(d.bridge.start_date+'T00:00:00Z')-86400000).toISOString().slice(0,10)):'Open 30 Jun';
    const bridgeEndLabel=portalBridge?'Open '+dayLabel(d.bridge.end_date):'Open 30 Sep';
    const bridgeNote=portalBridge?'Opening balance + invoices posted − receipts = closing. No write-off ledger is held, so write-offs are zero.':d?.chart_notes?.bridge||'Bridge unavailable';
    const funnelNames=pipeline?.real?['Awaiting review','Escalated','Approved','Rejected']:['New','Investigating','Awaiting approval','Action taken','Resolved, 30 days'];
    const funnelCounts=pipeline?(pipeline.real?[pipeline.awaiting,pipeline.escalated,pipeline.approved,pipeline.rejected]:[pipeline.new,pipeline.investigating,pipeline.awaiting,pipeline.action,pipeline.resolved]):[];
    const funnelMax=Math.max(1,...funnelCounts);
    const riskKeys=['0–20%','20–40','40–60','60–80','80–100','Not scored'];
    const riskMax=Math.max(1,...(d?.risk||[]).map(r=>r.count));
    const risk=riskKeys.map((b,i)=>{const a=d?.risk.find(r=>r.key===b);return{b,n:d?.historical_available?n(a?.count):'—',h:d?.historical_available?(a?.count||0)/riskMax*120:0,style:i===5?'background: repeating-linear-gradient(135deg,#BDBDBA 0 2px,#FFFFFF 2px 6px); border: 1px dashed #8A8A87':'background: '+(i===4?accent:i===3?'#3F3F3F':'#8A8A87')};});
    const top=(d?.top||[]).map(t=>({name:t.name,id:t.id,inv:t.count+' open',open:fmt(t.open),od:fmt(t.overdue),share:Number(m.overdue)?Number(t.overdue)/Number(m.overdue)*100:0,dpd:t.oldest+' d',risk:t.risk==null?'Not scored':Number(t.risk).toFixed(2),case:'See invoices',href:link(logic,logic.query.get('portal')==='1'?{company_search:t.name}:{company_id:t.id})}));
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
    const rows=(d?.invoices||[]).map(r=>{const scored=r.probability!=null;const open=Number(r.balance)>0;const overdue=open&&r.days_past_due>0;const label=r.status==='CANCELLED'?'Canceled':r.status==='WRITTEN_OFF'?'Written off':r.balance===null?'Unverified':open?(Number(r.amount_paid)>0?'Part-paid'+(overdue?' · overdue':''):overdue?'Overdue':'Open'):'Settled';return{id:r.invoice_number,uuid:r.id,co:r.company,cid:r.company_id,terms:r.payment_term==null?'Terms not recorded':'Net '+r.payment_term,post:r.posting_date,due:r.due_date,amt:fmt(r.amount_total),paid:fmt(r.amount_paid),bal:fmt(r.balance),dpd:r.days_past_due==null?missing:open?(r.days_past_due?r.days_past_due+' d':'Not due'):'—',dpdColor:overdue?accent:'#0B0B0B',status:label,statusStyle:'font-size:12px;border:1px solid #D9D9D6;border-radius:999px;padding:3px 9px',scored,unscored:!scored,risk:scored?(Number(r.probability)*100).toFixed(0)+'%':'Not scored',riskW:scored?Number(r.probability)*100:0,riskC:scored&&Number(r.probability)>=.8?accent:'#0B0B0B',hasCase:!!r.case_id,noCase:!r.case_id,case:r.case_id,caseHref:link(logic,{case_id:r.case_id},'/cases')};});
    return{loading:pending,empty:!!d && !logic.state.loading && !logic.state.error && d.total===0,accent,kpis,bars,openSegments,openLine:'',aging,risk,top,weeks,due,rows,hist:[],
      funnel:funnelNames.map((s,i)=>({s,v:pipeline?funnelCounts[i]:missing,w:pipeline?(100*funnelCounts[i]/funnelMax):0,c:colors[i]||'#0B0B0B'})),
      hist:histogram,riskProvenance:scenario?'Illustrative scenario scores · not model predictions':'Stored scores with verified model provenance',
      caseExposure:pipeline?money(pipeline.exposure/100,'INR',true,true):missing,
      bridgeStartLabel,bridgeEndLabel,bridgeNote:bridgeNote,
      bridgeStart:bridgeText[0],bridgeInvoiced:bridgeText[1],bridgeCollected:bridgeText[2],bridgeWriteOff:bridgeText[3],bridgeEnd:bridgeText[4],
      bridgeStartHeight:bridgeHeight[0],bridgeInvoicedHeight:bridgeHeight[1],bridgeCollectedHeight:bridgeHeight[2],bridgeWriteOffHeight:bridgeHeight[3],bridgeEndHeight:bridgeHeight[4],
      bridgeStartLabelBottom:bridgeLabelBottom[0],bridgeInvoicedLabelBottom:bridgeLabelBottom[1],bridgeCollectedLabelBottom:bridgeLabelBottom[2],bridgeWriteOffLabelBottom:bridgeLabelBottom[3],bridgeEndLabelBottom:bridgeLabelBottom[4],
      approvalsWaiting:pipeline?String(pipeline.awaiting)+' awaiting review · View →':'Unavailable · Review →',
      allocationCollectedPct:allocationPct.collected,allocationNotDuePct:allocationPct.not_due,
      allocationOverduePct:allocationPct.overdue,allocationWriteOffPct:allocationPct.written_off,
      allocationCanceledPct:allocationPct.cancelled,
      allocationCollected:allocation?money(allocation.collected,'INR',true,true):missing,
      allocationOverdue:allocation?money(allocation.overdue,'INR',true,true):missing,
      allocationNotDue:allocation?money(allocation.not_due,'INR',true,true):missing,
      allocationWrittenOff:allocation?money(allocation.written_off,'INR',true,true):missing,
      allocationCanceled:allocation?money(allocation.cancelled,'INR',true,true):missing,
      statusRole:logic.state.error?'alert':'status',status,workspace:d?.workspace||'Not connected',scopeLabel:d?n(m.company_count)+' companies · '+d.currency:'Authorized scope required',
      hasFilters:!!(['status','company_id','risk','search'].some(key=>{
        const value=logic.query.get(key);
        return value && !['all','any'].includes(value);
      })),
      filterSummary:[
        logic.query.get('status') && logic.query.get('status')!=='all' ? 'Status: '+logic.query.get('status').replaceAll('_',' ') : '',
        logic.query.get('company_id') ? 'Company: '+(d?.companies?.find(c=>c.id===logic.query.get('company_id'))?.name||'selected') : '',
        logic.query.get('risk') && logic.query.get('risk')!=='any' ? 'Risk: '+logic.query.get('risk') : '',
        logic.query.get('search') ? 'Search: '+logic.query.get('search') : ''
      ].filter(Boolean).join(' · '),
      rangeLabel:logic.query.get('start_date') && logic.query.get('end_date') ?
        logic.query.get('start_date')+' → '+logic.query.get('end_date') : 'All dates',
      comparisonActive:!!comparedDate,
      comparisonReady:!!previous,
      comparisonTitle:previous?'Compared with '+comparedDate:'Comparison unavailable for '+comparedDate,
      comparisonDetail:previous?'As-of '+(d?.as_of||'—')+' against '+comparedDate+' · same selected filters; balances are reconstructed at each cutoff.':
        'Unable to verify the earlier cutoff. Current values are not substituted for historical results.',
      comparisonOpen:previous?fmt(previous.open):missing,
      comparisonOverdue:previous?fmt(previous.overdue):missing,
      comparisonCollected:previous?fmt(previous.collected):missing,
      comparisonInvoiced:previous?fmt(previous.invoiced):missing,
      comparisonCoverage: d?.comparison_coverage === 'illustrative_only'
        ? 'Neon history unavailable; comparison uses illustrative records only.'
        : 'Historic B2B records end in 2016; later cutoff movement is from clearly identified illustrative operations.',
      sourceDegraded:d?.historical_source_connected===false,
      sourceAlert:'Historical Neon data is temporarily unavailable; figures shown only include illustrative operating records. Do not treat this as the complete dataset.',
      asOf:readableDate(d?.as_of||new Date().toISOString().slice(0,10)),
      sourceNote:d?.fixed_as_of?'Historical ledger: this view is fixed at '+readableDate(d.as_of)+' and receives no new transactions.':
        d?.workspace_kind==='showcase'?'Sample workspace: reopening the dashboard does not create invoices or payments. Figures change only with recorded sample actions or as invoices age.':
        d?.workspace_kind==='customer'?'Figures come from stored invoices and payments. They update when records are imported or synced; reopening the dashboard alone creates no new activity.':
        'This report uses stored records. Reopening it does not create new activity.',
      summary:d?(!logic.state.loading&&m.invoice_count===0?'No data for this selection. ':'')+n(m.invoice_count)+' invoices from '+n(m.company_count)+' companies. '+d.historical_reason:logic.state.error||'Loading original source records…',
      currencyNote:d?.currency_note||'No currency conversion performed.',calendarNote:d?.chart_notes.calendar||'Calendar policy not yet loaded.',
      openTotal:fmt(m.open),overdueTotal:fmt(m.overdue),billedTotal:allocation?money(billed,'INR',true,true):fmt(m.billed),invoiceCount:n(m.invoice_count),companyCount:n(m.company_count),medianInvoice:fmt(m.median_invoice),partialCount:n(m.partial_count),openCount:n(m.open_count),unpaidCount:m.open_count==null?missing:n(m.open_count-m.partial_count),overdueCount:n(m.overdue_count),settledCount:n(m.settled_count),cancelledCount:n(m.cancelled_count),writtenOffCount:n(m.written_off_count),
      riskNote:d?.historical_available?n(m.scored_open)+' of '+n(m.open_count)+' open invoices scored. '+n(m.open_count-m.scored_open)+' not scored.':'Open-invoice risk population unavailable without historical balances.',
      chartGrainText:'Invoiced by posting '+(logic.query.get('grain')||'month')+' · Collected by payment '+(logic.query.get('grain')||'month')+' · Line is open balance at period end · '+(d?.chart_notes.open_line||'Historical balance unavailable.'),lineNote:d?.chart_notes.open_line||'Historical month-end balance unavailable.',pipelineNote:d?.chart_notes.pipeline||'Stage mapping unavailable.',settlementNote:d?.chart_notes.settlement||'Settlement policy unavailable.',allocationNote:d?.chart_notes.allocation||'Adjustment evidence unavailable.',
      axis:[1,.75,.5,.25,0].map(f=>fmt(maximum*f)),heatMax:fmt(heatMax),heatNote:d?'Cash receipts by payment date through '+d.as_of+'. Each square is one day.':'Loading dated cash receipts…',dueNote:'Contractual dates, not a cash forecast. '+(d?.chart_notes.calendar||''),
      topNote:'Top companies by overdue balance in the selected cohort.',rowLabel:d?'Rows '+(d.total?((d.page-1)*d.page_size+1):0)+'–'+Math.min(d.page*d.page_size,d.total)+' of '+d.total+' · combined dataset sorting':logic.state.error?'Data unavailable': 'Loading rows…',
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
           const now = logic.state.data?.as_of || new Date().toISOString().slice(0,10);
           const start = logic.query.get('start_date');
           const end = logic.query.get('end_date') || now;
           const span = start ? (Date.parse(end)-Date.parse(start))/86400000 : Infinity;
           if(grain==='day' && span>89) change(logic,{...range('90D',now),preset:'90D',grain});
           else if(grain==='week' && span>365) change(logic,{...range('12M',now),preset:'12M',grain});
           else change(logic,{grain});
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
      return ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][month - 1]
        + ' ' + String(year).slice(-2);
    }

    return String(period);
  }

  function change(logic, values){for(const[k,v]of Object.entries(values)){if(v)logic.query.set(k,v);else logic.query.delete(k);}if(!('page'in values))logic.query.delete('page');const query=logic.query.toString();global.history.replaceState(null,'','?'+query);if(global.parent!==global){const topQuery=new URLSearchParams(query);topQuery.delete('portal');global.parent.history.replaceState(null,'',global.parent.location.pathname+'?'+topQuery);}load(logic);}
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
      for (const key of ['preset','case_id','portal']) params.delete(key);
      const response = await fetch('/api/finance/overview?' + params, {signal:controller.signal,cache:'no-store'});
      const data = await response.json().catch(() => null);
      if (!response.ok) throw Object.assign(new Error(typeof data?.detail === 'string' ? data.detail : 'HTTP ' + response.status), {status:response.status});
      if (!data?.metrics || !Array.isArray(data.invoices) || !Array.isArray(data.series)) throw new Error('Invalid dashboard response');
      if (controller.signal.aborted) return;
      logic.loadedQuery = query;
      logic.loadedScope = currentScope;
      logic.lastFetchAt = Date.now();
      if (logic.query.get('portal') === '1') {
        try { const rec = await fetch('/api/portal/recovery-summary', {signal:controller.signal,cache:'no-store'}); logic.portalRecovery = rec.ok ? await rec.json() : null; } catch { logic.portalRecovery = null; }
      }
      logic.setState({data,loading:false,error:''});
    } catch (error) {
      if (!controller.signal.aborted) {
        // Never display the previous selection's numbers under a newly selected
        // date, company, status or comparison when its request has failed.
        const retained = logic.loadedQuery === query &&
          ![401,403].includes(error.status) && logic.loadedScope === currentScope
          ? logic.state.data : null;
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
  let activeInvoiceLogic=null;
  function bind(logic,page){logic.query=new URLSearchParams(global.location.search);logic.page=page;
    if(page==='invoices') activeInvoiceLogic=logic;
    logic.click=async e=>{const element=controlFor(e,'[data-action],a');if(!element)return;if(element.matches('a')&&!element.hasAttribute('data-action')){const href=element.getAttribute('href');if(href?.startsWith('/')&&(!href.includes('?')||href.endsWith('?')))element.href=link(logic,{},href.split('?')[0]);return;}
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
      if(action==='range') {
         const now = logic.state.data?.as_of || new Date().toISOString().slice(0,10);
         const all = value==='All data';
         const narrow = all && ['day','week'].includes(logic.query.get('grain'));
         change(logic,{...range(value,now),preset:value,...(narrow?{grain:'month'}:{})});
       }
      if(action==='grain') {
         const now = logic.state.data?.as_of || new Date().toISOString().slice(0,10);
         const start = logic.query.get('start_date');
         const end = logic.query.get('end_date') || now;
         const daySpan = start ? (Date.parse(end)-Date.parse(start))/86400000 : Infinity;
         // All historical data spans 2011–2016 plus present-day scenarios.
         // Auto-select a supported window for fine time grains.
         if(value==='day' && daySpan>89) change(logic,{...range('90D',now),preset:'90D',grain:value});
         else if(value==='week' && daySpan>365) change(logic,{...range('12M',now),preset:'12M',grain:value});
         else change(logic,{grain:value});
       }
      if(action==='basis')change(logic,{date_basis:value});
      if(action==='status')change(logic,{status:value});
      if(action==='compare') {
        const asOf=logic.state.data?.as_of||new Date().toISOString().slice(0,10);
        const el=dialog('Compare as-of cutoffs');
        const help=document.createElement('p');help.textContent='Compare the selected invoice cohort at two actual dates. The current as-of date is '+asOf+'. Future invoices and payments are not backdated.';el.append(help);
        const controls=document.createElement('div');controls.style.cssText='display:flex;flex-wrap:wrap;gap:8px;margin:12px 0';
        const input=document.createElement('input');input.type='date';input.setAttribute('aria-label','Earlier comparison date');input.min='1900-01-01';
        const yesterday=new Date(asOf+'T00:00:00Z');yesterday.setUTCDate(yesterday.getUTCDate()-1);
        input.max=yesterday.toISOString().slice(0,10);
        const preset=(days,label)=>{const button=document.createElement('button');button.type='button';button.className='btn';button.textContent=label;
          button.onclick=()=>{const dt=new Date(asOf+'T00:00:00Z');dt.setUTCDate(dt.getUTCDate()-days);input.value=dt.toISOString().slice(0,10);};controls.append(button);};
        preset(7,'7 days earlier');preset(30,'30 days earlier');preset(90,'90 days earlier');preset(365,'1 year earlier');
        const previousDate=logic.query.get('compare_date');
        const suggested=new Date(asOf+'T00:00:00Z');suggested.setUTCDate(suggested.getUTCDate()-30);
        input.value=previousDate||suggested.toISOString().slice(0,10);
        const error=document.createElement('p');error.setAttribute('role','alert');error.style.color='#9A3412';
        const actions=document.createElement('div');actions.style.cssText='display:flex;flex-wrap:wrap;gap:8px;margin-top:14px';
        const apply=document.createElement('button');apply.type='button';apply.className='btn dark';apply.textContent='Apply comparison';
        apply.onclick=()=>{if(!input.value||input.value>=asOf||input.value<'1900-01-01'||Number.isNaN(Date.parse(input.value+'T00:00:00Z'))){error.textContent='Choose an earlier valid date, before '+asOf+'.';return;}change(logic,{compare_date:input.value});el.close();};
        const clear=document.createElement('button');clear.type='button';clear.className='btn';clear.textContent='Remove comparison';clear.onclick=()=>{change(logic,{compare_date:null});el.close();};
        controls.append(input);actions.append(apply,clear);el.append(controls,error,actions);
        input.focus();
      }
      if(action==='clear-compare')change(logic,{compare_date:null});
      if(action==='custom-range') {
        const el=dialog('Invoice date range');
        const help=document.createElement('p');help.textContent='Filter posting, due or payment dates using the selected Date basis. Both endpoints are inclusive.';
        const row=document.createElement('div');row.style.cssText='display:flex;flex-wrap:wrap;gap:12px;margin:12px 0';
        const start=document.createElement('input');start.type='date';start.setAttribute('aria-label','Range start date');start.value=logic.query.get('start_date')||'';
        const end=document.createElement('input');end.type='date';end.setAttribute('aria-label','Range end date');end.value=logic.query.get('end_date')||'';
        for(const [label,input] of [['From',start],['Through',end]]){const field=document.createElement('label');field.style.cssText='display:flex;flex-direction:column;gap:5px';field.append(document.createTextNode(label),input);row.append(field);}
        const error=document.createElement('p');error.setAttribute('role','alert');error.style.color='#9A3412';
        const controls=document.createElement('div');controls.style.cssText='display:flex;gap:8px';
        const apply=document.createElement('button');apply.type='button';apply.className='btn dark';apply.textContent='Apply range';
        apply.onclick=()=>{if(!start.value||!end.value||start.value>end.value){error.textContent='Choose both dates in chronological order.';return;}
          change(logic,{start_date:start.value,end_date:end.value,preset:null});el.close();};
        const clear=document.createElement('button');clear.type='button';clear.className='btn';clear.textContent='All dates';clear.onclick=()=>{change(logic,{start_date:null,end_date:null,preset:'All data'});el.close();};
        controls.append(apply,clear);el.append(help,row,error,controls);start.focus();
      }
      if(action==='reset-filters')change(logic,{search:null,status:null,company_id:null,risk:null,sort:'posting_desc'});
      if(action==='asof'){const el=dialog('As of');const input=document.createElement('input');input.type='date';input.setAttribute('aria-label','As of date');input.value=logic.state.data?.as_of||'';input.min='1900-01-01';input.max=new Date().toISOString().slice(0,10);
        const error=document.createElement('p');error.setAttribute('role','alert');error.style.color='#9A3412';
        const apply=document.createElement('button');apply.className='btn dark';apply.textContent='Apply cutoff';
        apply.onclick=()=>{if(!input.value||input.value>input.max||input.value<'1900-01-01'){error.textContent='Choose a valid cutoff through '+input.max;return;}
          const compare=logic.query.get('compare_date');change(logic,{as_of:input.value,...(compare&&compare>=input.value?{compare_date:null}:{})});el.close();};el.append(input,error,apply);}

      if(action==='refresh')load(logic);
      if(action==='export'){element.disabled=true;try{const response=await fetch('/api/finance/export?'+logic.query);if(!response.ok)throw new Error((await response.json()).detail||'Export failed');const url=URL.createObjectURL(await response.blob());const a=document.createElement('a');a.href=url;a.download='invoiceiq.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(err){logic.setState({error:err.message});}finally{element.disabled=false;}}
      if(action==='company')choose(logic,'company_id',[['','All'],...(logic.state.data?.companies||[]).map(c=>[c.id,c.name])]);
      if(action==='risk')choose(logic,'risk',[['any','Any'],['low','Low'],['medium','Medium'],['high','High'],['unscored','Not scored']]);
      if(action==='amount')choose(logic,'sort',[['posting_desc','Newest first'],['posting_asc','Oldest first'],['amount_desc','Amount: high to low'],['amount_asc','Amount: low to high'],['dpd_desc','Days past due: high to low']]);
      if(action==='evidence')evidence(element.dataset.id);
      if(action==='access'){const el=dialog('Workspace access');const input=document.createElement('input');input.type='password';input.autocomplete='off';input.setAttribute('aria-label','Analytics credential');const submit=document.createElement('button');submit.className='btn';submit.textContent='Connect';submit.onclick=async()=>{const response=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:input.value})});if(response.ok){localStorage.setItem('invoiceiq-session-change',crypto.randomUUID());global.location.reload();}else{const p=document.createElement('p');p.textContent='Credential could not be saved';el.append(p);}};
        const disconnect=document.createElement('button');disconnect.className='btn';disconnect.textContent='Disconnect';disconnect.onclick=async()=>{disconnect.disabled=true;try{const response=await fetch('/api/session',{method:'DELETE'});if(!response.ok)throw new Error('Could not disconnect');localStorage.setItem('invoiceiq-session-change',crypto.randomUUID());(global.parent!==global?global.parent:global).location.assign('/connect');}catch(error){disconnect.disabled=false;const p=document.createElement('p');p.textContent=error.message;el.append(p);}};
        el.append(input,submit,disconnect);}
    };
    logic.input=e=>{const target=controlFor(e,'input[type=search]');if(target){clearTimeout(logic.debounce);logic.debounce=setTimeout(()=>change(logic,{search:target.value}),350);}};
    logic.keydown=e=>{if(e.key!=='Enter'&&e.key!==' ')return;const target=controlFor(e,'[data-action=chart-period],[data-action=period-scope]');if(target){e.preventDefault();target.click();}};
    document.addEventListener('click',logic.click);document.addEventListener('input',logic.input);wireGroupButtons(logic);document.addEventListener('keydown',logic.keydown);
    logic.visible=()=>{if(!document.hidden&&!global.parent.document.hidden&&!logic.state.loading&&Date.now()-(logic.lastFetchAt||0)>300000)load(logic);};
    document.addEventListener('visibilitychange',logic.visible);
    logic.poll=setInterval(logic.visible,300000);load(logic);
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
    if(logic.page==='invoices') global.InvoiceIQColumnFilters?.sync(logic.query);
    controls('input[data-action=compare]').forEach(el=>{el.checked=!!logic.query.get('compare_date');});
    controls('button[data-action=company]').forEach(el=>{
      const id=logic.query.get('company_id');const name=logic.state.data?.companies?.find(c=>c.id===id)?.name;
      el.textContent='Company: '+(id?(name||'Selected'):'All')+' ▾';
    });
    controls('button[data-action=risk]').forEach(el=>{const risk=logic.query.get('risk')||'any';el.textContent='Risk: '+(risk==='any'?'Any':risk[0].toUpperCase()+risk.slice(1))+' ▾';});
    controls('button[data-action=amount]').forEach(el=>{const sort=logic.query.get('sort')||'posting_desc';const label={posting_desc:'Newest',posting_asc:'Oldest',amount_desc:'Amount ↓',amount_asc:'Amount ↑',dpd_desc:'Overdue days ↓'}[sort]||'Sort';el.textContent=label+' ▾';});
  }
  function unbind(logic){if(activeInvoiceLogic===logic)activeInvoiceLogic=null;logic.controller?.abort();clearInterval(logic.poll);document.removeEventListener('visibilitychange',logic.visible);clearTimeout(logic.debounce);document.removeEventListener('click',logic.click);document.removeEventListener('input',logic.input);document.removeEventListener('keydown',logic.keydown);}
  global.InvoiceIQ={money,range,vals,bind,unbind,sync,
    applyColumnFilters(values){
      if(!activeInvoiceLogic)throw Error('Invoice table is not mounted');
      change(activeInvoiceLogic,values);
    }
  };
})(window);
