/* InvoiceIQ: live data for the user's ORIGINAL Approvals HTML, no sample numbers. */
(() => {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clean = s => String(s ?? '—').replaceAll('_',' ').toLowerCase().replace(/\b\w/g,c=>c.toUpperCase());
  const date = s => s && Number.isFinite(Date.parse(s)) ? new Date(s).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'}) : '—';
  const dateTime = s => s && Number.isFinite(Date.parse(s)) ? new Date(s).toLocaleString('en-IN',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}) : '—';
  const age = (s,now) => s && Number.isFinite(Date.parse(s)) ? Math.max(0, Math.floor((now-Date.parse(s))/86400000)) : null;
  const shortId = s => s ? String(s).slice(0,8) : '—';
  const toPaise = v => { const m=/^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(String(v??'').trim()); if(!m)return null; const n=BigInt(m[2])*100n+BigInt((m[3]||'').padEnd(2,'0')); return m[1]?-n:n; };
  const money = v => {
    const raw=toPaise(v); if(raw===null)return '—';
    const abs=raw<0n?-raw:raw, sign=raw<0n?'−':'';
    const units=[[1000000000n,'Cr'],[10000000n,'L'],[100000n,'k']];
    for(const [scale,suffix] of units) if(abs>=scale){
      const n=(abs*100n+scale/2n)/scale;
      const frac=String(n%100n).padStart(2,'0').replace(/0+$/,'');
      return sign+'₹'+(n/100n).toString()+(frac?'.'+frac:'')+suffix;
    }
    const paise=String(abs%100n).padStart(2,'0').replace(/0+$/,'');
    return sign+'₹'+(abs/100n).toString()+(paise?'.'+paise:'');
  };
  const sumMoney = rows => { let n=0n; const seen=new Set(); for(const r of rows){const key=r.caseId+'|'+r.invoice;if(seen.has(key))continue;seen.add(key);const x=toPaise(r.outstanding_amount);if(x===null)return null;n+=x;}return (n/100n).toString()+'.'+String(n%100n).padStart(2,'0'); };
  const reviewerHeader = {'X-InvoiceIQ-Reviewer-Key': ''};
  const state={key:'',reviews:[],history:null,chosen:'',detail:null,now:Date.now(),loading:false,authorized:false};
  let listController, detailController;
  let main,kpis,queue,details,historySection,connection,notice;

  const findMain = root => {
    const candidate=root.querySelector?.('main');
    if(candidate?.querySelector('[aria-label="Approval totals"]')&&candidate.querySelector('[aria-label="Queue"]'))return candidate;
    for(const n of root.querySelectorAll?.('*')||[])if(n.shadowRoot){const found=findMain(n.shadowRoot);if(found)return found;}
    return null;
  };
  const display = msg => {notice.textContent=msg;notice.hidden=!msg;};
  const fetchJSON = async (path, options={}) => {
    const target = path === 'review-history' ? '/api/reviewer/history'
      : /^reviews\/[0-9a-f-]{36}\/(approve|reject)$/i.test(path) ? '/api/reviewer/' + path
      : '/api/workspace/' + path;
    const response=await fetch(target,{cache:'no-store',...options,headers:{...reviewerHeader,...options.headers}});
    let result={};try{result=await response.json();}catch{}
    if(!response.ok){const error=Error(typeof result.detail==='string'?result.detail:`HTTP ${response.status}`);error.status=response.status;throw error;}
    return result;
  };
  const normalized = r => ({
    id:String(r.action_id??r.id??''),caseId:String(r.case_id??''),
    company:r.company_name??r.company??'Company unavailable',
    invoice:r.invoice_number??r.invoice??'—',
    action:r.action_type??'Unknown action',
    created:r.created_at??null,
    outstanding_amount:r.outstanding_amount??r.outstanding??null,
    days:r.days_overdue??null
  });
  function updateKpis(){
    const slots=[...kpis.children];if(slots.length!==4)throw Error('Original four Approvals KPI cards missing');
    const ages=state.reviews.map(r=>({row:r,days:age(r.created,state.now)})).filter(x=>x.days!==null).sort((a,b)=>b.days-a.days);
    const recent=Array.isArray(state.history)?state.history.filter(h=>h.decided_at&&age(h.decided_at,state.now)!==null&&age(h.decided_at,state.now)<=30):null;
    const hours=recent?.map(h=>Number(h.review_hours)).filter(n=>Number.isFinite(n)&&n>=0).sort((a,b)=>a-b)||[];
    const mid=hours.length?(hours[Math.floor((hours.length-1)/2)]+hours[Math.floor(hours.length/2)])/2:null;
    const values=[
      [state.authorized?String(state.reviews.length):'—',state.authorized?money(sumMoney(state.reviews))+' balance affected':'Connect authorized reviewer to load queue'],
      [state.authorized&&ages.length?ages[0].days+' d':'—',ages.length?shortId(ages[0].row.id)+' · since '+date(ages[0].row.created):'No dated waiting actions'],
      [recent?String(recent.length):'—',recent?`${recent.filter(x=>x.decision==='Approved').length} approved · ${recent.filter(x=>x.decision==='Rejected').length} rejected`:'Decision history unavailable'],
      [mid===null?'—':Number(mid.toFixed(1))+' h',mid===null?'No recorded decision intervals':'from hold to decision']
    ];
    slots.forEach((card,i)=>{const number=card.querySelector('.num');if(number)number.textContent=values[i][0];const lines=[...card.children];if(lines.length>2)lines[2].textContent=values[i][1];});
  }
  function renderQueue(){
    const heading=queue.firstElementChild;
    const note='<div style="padding:14px 18px;font-size:12px;color:#6B6B6B">Only actions authorized for your reviewer scope are shown.</div>';
    queue.innerHTML='';if(heading)queue.append(heading);
    if(!state.authorized && state.loading){queue.insertAdjacentHTML('beforeend','<div role="status" style="padding:18px;color:#666">Loading authorized approvals…</div>');return;}
    if(!state.authorized){queue.insertAdjacentHTML('beforeend','<div style="padding:18px;color:#525252;font-size:13px">Connect an authorized reviewer to see the actual waiting queue.</div>');return;}
    if(!state.reviews.length){queue.insertAdjacentHTML('beforeend','<div style="padding:18px;color:#525252;font-size:13px">No pending approvals in your authorized scope.</div>'+note);return;}
    for(const r of state.reviews){
      const chosen=r.id===state.chosen;
      const button=document.createElement('button');button.type='button';button.dataset.approval=r.id;
      button.style.cssText='display:block;width:100%;padding:16px 18px;border:0;border-bottom:1px solid #F0F0EE;text-align:left;font:inherit;cursor:pointer;color:#0B0B0B;'+(chosen?'background:#FAFAF9;box-shadow:inset 3px 0 0 #0B0B0B':'background:#FFFFFF');
      button.innerHTML=`<div style="display:flex;justify-content:space-between"><span class="mono" title="${esc(r.id)}" style="font-size:12.5px;font-weight:500">${esc(shortId(r.id))}</span><span class="mono" style="font-size:11px;color:#6B6B6B">${age(r.created,state.now)??'—'} d</span></div>
        <div style="font-size:14px;font-weight:600;margin-top:6px">${esc(clean(r.action))}</div>
        <div style="font-size:12.5px;color:#525252;margin-top:2px">${esc(r.company)} · ${esc(r.invoice)} · ${money(r.outstanding_amount)}</div>
        <div style="margin-top:8px"><span class="pill">Pending reviewer decision</span></div>`;
      button.addEventListener('click',()=>{state.chosen=r.id;state.detail=null;renderQueue();renderDetail();void loadDetail(r);});
      queue.append(button);
    }
    queue.insertAdjacentHTML('beforeend',note);
  }
  function renderDetail(){
    const r=state.reviews.find(x=>x.id===state.chosen);
    if(!r){details.innerHTML='<div style="padding:24px;color:#525252">Select an approval in the waiting queue to view its recorded evidence and decide.</div>';return;}
    const d=state.detail;
    if (!d) {details.innerHTML='<div role="status" style="padding:24px;color:#666">Loading recorded policy evidence…</div>';return;}
    if (state.detailError) {details.innerHTML='<div role="alert" style="padding:24px">Case evidence unavailable: '+esc(state.detailError)+'</div>';return;}
    const action=d?.actions?.find(a=>a.id===r.id);
    const decisions=Array.isArray(action?.policy_decisions)?action.policy_decisions:[];
    const proof=d?.risk_score==null?'—':esc(String(d.risk_score));
    const discussions=(d?.communication_logs||[]).filter(a=>a.body);
    const promises=Array.isArray(d?.promises_to_pay)?d.promises_to_pay:[];
    const evidence=[['Recorded case risk score',proof],['Days past due',r.days==null?'—':String(r.days)],['Recorded action status',clean(action?.status)],['Open invoice',r.invoice]];
    details.innerHTML=`
      <div style="padding:20px 24px;border-bottom:1px solid #E5E5E2;display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px"><div>
        <div style="display:flex;gap:10px;align-items:center"><h2 class="mono" style="margin:0;font-size:17px;font-weight:500" title="${esc(r.id)}">${esc(shortId(r.id))}</h2><span class="pill">Held for review</span></div>
        <div style="font-size:18px;font-weight:600;margin-top:8px">${esc(clean(r.action))}</div>
        <div style="font-size:13px;color:#525252;margin-top:4px">Proposed ${dateTime(r.created)} · ${esc(r.company)} · ${esc(r.invoice)} · ${money(r.outstanding_amount)} · <a href="/cases" target="_top">View cases</a></div>
      </div></div>
      <div style="display:flex;flex-wrap:wrap">
        <div style="flex:1 1 340px;min-width:0;padding:20px 24px;border-right:1px solid #F0F0EE;display:flex;flex-direction:column;gap:18px">
          <div><div class="lbl" style="margin-bottom:8px">Why it's held</div><div style="border:1px solid #D9D9D6;border-radius:8px;padding:12px 14px;font-size:13px;line-height:1.5">${!d?'Loading recorded policy evidence…':decisions.length?decisions.map(p=>`<div><span class="mono">${esc(p.policy_name)}</span> · ${esc(p.reason||p.rule||clean(p.decision))}</div>`).join(''):'No specific policy decision recorded for this action.'}</div></div>
          <div><div class="lbl" style="margin-bottom:8px">Evidence</div><div style="display:flex;flex-direction:column;gap:8px;font-size:13px">${evidence.map(([k,v])=>`<div style="display:flex;justify-content:space-between;gap:10px"><span>${esc(k)}</span><span class="mono">${v}</span></div>`).join('')}</div></div>
          <div><div class="lbl" style="margin-bottom:8px">Proposed plan</div>${promises.length?`<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px">${promises.map(p=>`<div style="border:1px solid #E5E5E2;border-radius:8px;padding:10px"><div class="mono" style="font-size:11px;color:#6B6B6B">${date(p.promised_date)}</div><div class="num" style="font-weight:600">${money(p.promised_amount)}</div></div>`).join('')}</div>`:'<div style="font-size:13px;color:#6B6B6B">No payment plan in the recorded case data.</div>'}</div>
        </div>
        <div style="flex:1 1 340px;min-width:0;padding:20px 24px;display:flex;flex-direction:column;gap:16px">
          <div><div class="lbl" style="margin-bottom:8px">Message draft</div><div style="border:1px solid #E5E5E2;border-radius:8px;padding:14px;font-size:13px;line-height:1.6;background:#FAFAF9;overflow-wrap:anywhere">${discussions.length?esc(discussions[discussions.length-1].body):'No message draft recorded. Nothing will be sent by opening this review.'}</div></div>
          <label style="display:flex;flex-direction:column;gap:6px;font-size:12.5px;color:#3F3F3F">Reason for your decision (required for audit log)<textarea data-review-note rows="3" style="border:1px solid #D9D9D6;border-radius:8px;padding:10px 12px;font:inherit;font-size:13px;resize:vertical" placeholder="Explain your decision"></textarea></label>
          <div style="display:flex;gap:10px"><button class="btn dark" data-decision="approve" style="flex:1">Approve</button><button class="btn" data-decision="reject" style="flex:1">Reject</button></div>
          <div style="font-size:11.5px;color:#6B6B6B;line-height:1.5">Reviewer permission, company scope and action status must be checked by the backend. A decision is not successful until the server confirms it. External messages are not sent by this page.</div>
        </div>
      </div>`;
    details.querySelectorAll('[data-decision]').forEach(btn=>btn.addEventListener('click',()=>void decide(btn.dataset.decision)));
  }
  async function loadDetail(r){
    detailController?.abort();
    const controller = detailController = new AbortController();
    state.detailError = '';
    try{const d=await fetchJSON('cases/'+encodeURIComponent(r.caseId), {signal:controller.signal});
      if(!controller.signal.aborted && state.chosen===r.id){state.detail=d;renderDetail();}
    }catch(e){if(!controller.signal.aborted && state.chosen===r.id){state.detail={};state.detailError=e.message;renderDetail();}}
  }
  async function decide(decision){
    const r=state.reviews.find(x=>x.id===state.chosen);
    const note=details.querySelector('[data-review-note]')?.value.trim()||'';
    if(!r||!state.key)return;
    if(!note){display('A decision reason is required.');details.querySelector('[data-review-note]')?.focus();return;}
    if(!window.confirm(`${decision==='approve'?'Approve':'Reject'} ${window.InvoiceIQScenario?.active?'this simulated review (browser-only; no backend write)':'the real review'} for ${r.company}?`))return;
    const controls=[...details.querySelectorAll('[data-decision]')];controls.forEach(b=>b.disabled=true);
    display('Submitting reviewer-authorized decision…');
    try{
      await fetchJSON('reviews/'+encodeURIComponent(r.id)+'/'+decision,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({note})});
      display(window.InvoiceIQScenario?.active ? 'Scenario decision saved in this browser. Refreshing review queue…' : 'Decision saved by backend. Reloading authorized records…');await load();
    }catch(e){display('Decision not saved: '+e.message);controls.forEach(b=>b.disabled=false);}
  }
  function csvField(x){let v=String(x??'');if(/^[\s]*[=+@\-\t\r]/.test(v))v="'"+v;return '"'+v.replaceAll('"','""')+'"';}
  function renderHistory(){
    const body=historySection.querySelector('tbody');if(!body)return;
    const records=state.history;
    if(!Array.isArray(records)){body.innerHTML=`<tr><td colspan="8" style="padding:18px">${state.loading?'Loading decision history…':'Decision history unavailable until supported by the reviewer API.'}</td></tr>`;return;}
    if(!records.length){body.innerHTML='<tr><td colspan="8" style="padding:18px">No reviewer decisions in the authorized history.</td></tr>';return;}
    body.innerHTML=records.map(h=>`<tr><td class="mono" title="${esc(h.action_id||h.id)}">${esc(shortId(h.action_id||h.id))}</td><td>${esc(clean(h.action_type))}</td><td>${esc(h.company_name)}</td><td class="mono">—</td><td><span class="pill">${esc(h.decision)}</span></td><td>—</td><td class="mono">${dateTime(h.decided_at)}</td><td style="color:#525252">${esc(h.description)}</td></tr>`).join('');
  }
  function exportHistory(){
    if(!Array.isArray(state.history)){display('Connect your reviewer before exporting the authorized history.');return;}
    const keys=['action_id','case_id','company_name','action_type','decision','decided_at','review_hours','description'];
    const csv='\ufeff'+[keys,...state.history.map(r=>keys.map(k=>r[k]??''))].map(line=>line.map(csvField).join(',')).join('\r\n');
    const url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));
    const a=document.createElement('a');a.href=url;a.download='invoiceiq-review-history.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function load(){
    listController?.abort();detailController?.abort();
    const controller = listController = new AbortController();
    const current = () => !controller.signal.aborted;
    state.now=Date.now();state.loading=true;
    queue.setAttribute('aria-busy','true');
    display('Loading authorized approvals…');updateKpis();renderQueue();renderHistory();
    try{
      const result=await fetchJSON('reviews',{signal:controller.signal});
      if(!current())return;
      if(!Array.isArray(result.reviews))throw Error('Unexpected review API response');
      state.reviews=result.reviews.map(normalized).filter(r=>r.id&&r.caseId);state.authorized=true;
      let historyError='';
      try{
        const h=await fetchJSON('review-history',{signal:controller.signal});
        if(!current())return;
        if(!Array.isArray(h.history))throw Error('Unexpected history API response');
        state.history=h.history;
      }catch(e){if(!current())return;if(e.status===401||e.status===403)throw e;historyError='Decision history refresh failed: '+e.message;}
      state.loading=false;
      if(!state.reviews.some(r=>r.id===state.chosen)){state.chosen=state.reviews[0]?.id||'';state.detail=null;}
      updateKpis();renderQueue();renderHistory();renderDetail();
      if(state.chosen)void loadDetail(state.reviews.find(r=>r.id===state.chosen));
      display(historyError);
      const badge=main.parentElement?.querySelector('a[href="/approvals"] span.mono:last-child');
      if(badge)badge.textContent=String(state.reviews.length);
    }catch(e){
      if(!current())return;
      state.loading=false;
      if(!state.authorized || [401,403].includes(e.status)){state.authorized=false;state.reviews=[];state.history=null;state.chosen='';state.detail=null;}
      updateKpis();renderQueue();renderHistory();renderDetail();display('Approvals unavailable: '+e.message);
    }finally{if(current()){state.loading=false;queue.setAttribute('aria-busy','false');}}
  }
  function init(){
    main=findMain(document);if(!main)return false;
    kpis=main.querySelector('[aria-label="Approval totals"]');queue=main.querySelector('[aria-label="Queue"]');
    const pair=queue?.parentElement;
    details=pair?.querySelectorAll(':scope > section')[1];
    historySection=pair?.nextElementSibling;
    const header=main.querySelector('header');
    if(!kpis||kpis.children.length!==4||!queue||!details||!historySection?.querySelector('tbody')||!header)return false;
    connection=header.querySelector('span.pill');if(!connection)return false;
    connection.outerHTML='<form id="iq-reviewer-login" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center"><input required type="password" autocomplete="off" aria-label="Reviewer credential" placeholder="Reviewer credential" style="height:36px;border:1px solid #D9D9D6;border-radius:8px;padding:0 11px;font:inherit;font-size:12px"/><button class="btn dark" type="submit" style="height:36px">Connect reviewer</button></form>';
    const form=header.querySelector('#iq-reviewer-login');
    form.addEventListener('submit',e=>{e.preventDefault();const input=form.querySelector('input');const key=input.value.trim();input.value='';if(!key)return;if(state.key!==key){listController?.abort();detailController?.abort();state.authorized=false;state.reviews=[];state.history=null;state.chosen='';state.detail=null;state.detailError='';renderDetail();}state.key=key;reviewerHeader['X-InvoiceIQ-Reviewer-Key']=key;void load();});
    notice=document.createElement('div');notice.setAttribute('role','status');notice.style.cssText='border:1px solid #E5E5E2;padding:11px 14px;background:#fff;color:#525252;border-radius:8px;font-size:12px';
    kpis.before(notice);display('Connect a reviewer credential to load live approvals.');
    const exportButton=[...historySection.querySelectorAll('button')].find(b=>b.textContent.includes('Export CSV'));
    if(exportButton)exportButton.addEventListener('click',exportHistory);
    updateKpis();renderQueue();renderDetail();renderHistory();
    const sidebar=main.parentElement;for(const href of ['/cases','/approvals']){const badge=sidebar?.querySelector('a[href="'+href+'"] span.mono:last-child');if(badge)badge.textContent='—';}
    document.getElementById('iq-initial-loading')?.remove();
    document.getElementById('dc-root')?.style.setProperty('visibility','visible');
    main.closest('x-dc')?.style.setProperty('visibility','visible');document.querySelector('x-dc')?.style.setProperty('visibility','visible');
    if (window.InvoiceIQScenario?.active) {
      // Scenario approvals require no privileged reviewer credentials and cannot affect Neon.
      form.style.display = 'none';
      state.key = 'scenario-reviewer';
      reviewerHeader['X-InvoiceIQ-Reviewer-Key'] = state.key;
      display('Simulated approval workflow · decisions stored in this browser only.');
      void load();
    }
    return true;
  }
  let attempts=0;const timer=setInterval(()=>{
    try{if(init()){clearInterval(timer);return;}}catch(e){clearInterval(timer);document.body.insertAdjacentHTML('afterbegin',`<p role="alert" style="padding:12px;background:#fff;color:#B91C1C">Approvals initialization failed: ${esc(e.message)}</p>`);return;}
    if(++attempts>=150){clearInterval(timer);document.body.insertAdjacentHTML('afterbegin','<p role="alert">Original Approvals template unavailable. Sample data must not be used for decisions.</p>');}
  },100);
})();
