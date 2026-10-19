/* Invoice Intelligence column filters. Query-driven and never page-slice filtering.
 * Historical filters require backend column_filters_version=1, or fail closed.
 */
(function (global) {
  'use strict';
  const groups = Object.freeze({
    invoice: {label:'Invoice',fields:[['invoice_number','Invoice ID contains','text']]},
    company: {label:'Company',fields:[['company_search','Company name contains','text']]},
    posted: {label:'Posted',fields:[['posted_from','From','date'],['posted_to','To','date']]},
    due: {label:'Due',fields:[['due_from','From','date'],['due_to','To','date']]},
    amount: {label:'Amount',fields:[['amount_min','Minimum ₹','number'],['amount_max','Maximum ₹','number']]},
    paid: {label:'Paid',fields:[['paid_min','Minimum ₹','number'],['paid_max','Maximum ₹','number']]},
    balance: {label:'Balance',fields:[['balance_min','Minimum ₹','number'],['balance_max','Maximum ₹','number']]},
    days: {label:'Days past due',fields:[['days_min','Minimum days','integer'],['days_max','Maximum days','integer']]},
    status: {label:'Status',fields:[['status','Invoice status','status']]},
    risk: {label:'Late-payment risk',fields:[['risk','Risk band','risk'],['risk_min','Minimum %','percentage'],['risk_max','Maximum %','percentage']]},
    case: {label:'Case',fields:[['case_presence','Case presence','case']]}
  });
  const SORT = Object.freeze({
    invoice:['invoice_asc','invoice_desc'], company:['company_asc','company_desc'],
    posted:['posting_asc','posting_desc'],due:['due_asc','due_desc'],
    amount:['amount_asc','amount_desc'],paid:['paid_asc','paid_desc'],
    balance:['balance_asc','balance_desc'],days:['dpd_asc','dpd_desc'],
    risk:['risk_asc','risk_desc']
  });
  const options = Object.freeze({
    status:[['all','All'],['open','Open'],['partial','Part-paid'],['overdue','Overdue'],
      ['settled','Settled'],['cancelled','Cancelled'],['written_off','Written off']],
    risk:[['any','Any'],['low','Low (<30%)'],['medium','Medium (30–69%)'],
      ['high','High (70%+)'],['unscored','Not scored']],
    case:[['any','Any'],['yes','Has case'],['no','No case']]
  });
  const defaults = {status:'all',risk:'any',case_presence:'any'};
  const ALL_KEYS = [...new Set(Object.values(groups).flatMap(g => g.fields.map(row=>row[0])))];
  const getRoot = node => node?.getRootNode?.() || document;
  const currentQuery = () => new URLSearchParams(global.location.search);
  const popup = title => {
    const d=document.createElement('dialog');
    d.setAttribute('aria-label',title);
    d.style.cssText='border:1px solid #D9D9D6;border-radius:9px;min-width:min(370px,95vw);max-width:95vw;padding:22px 24px;box-shadow:0 12px 40px #0002;color:#0B0B0B;background:white;font:13px Geist,system-ui';
    const h=document.createElement('h2');h.textContent=title;h.style.cssText='margin:0 0 15px;font-size:17px';d.append(h);
    document.body.append(d);
    d.addEventListener('close',()=>d.remove(),{once:true});
    d.showModal();return d;
  };
  const btn = (text,action,dark=false) => {
    const b=document.createElement('button');b.type='button';b.textContent=text;
    b.style.cssText='padding:9px 13px;border-radius:6px;border:1px solid #D9D9D6;cursor:pointer;font:inherit;'+
      (dark?'background:#0B0B0B;color:white':'background:white;color:#0B0B0B');
    b.addEventListener('click',action);return b;
  };
  const show = key => {
    const group=groups[key];if(!group)return;
    const q=currentQuery();const dialog=popup('Filter '+group.label);
    const form=document.createElement('form');form.method='dialog';
    form.style.cssText='display:flex;flex-direction:column;gap:12px';
    const controls = new Map();
    for(const [name,label,type] of group.fields){
      const wrapper=document.createElement('label');wrapper.textContent=label;
      wrapper.style.cssText='display:flex;flex-direction:column;gap:5px;color:#3F3F3F;font-weight:500';
      let input;
      if(type==='status'||type==='risk'||type==='case'){
        input=document.createElement('select');
        for(const [value,labelText] of options[type]){
          const opt=document.createElement('option');opt.value=value;opt.textContent=labelText;input.append(opt);
        }
      }else{
        input=document.createElement('input');
        input.type=type==='integer'||type==='number'||type==='percentage'?'number':type;
        if(input.type==='number'){
          input.min='0';input.step=type==='integer'?'1':type==='percentage'?'0.1':'0.01';
          if(type==='percentage')input.max='100';
        }
        if(input.type==='text')input.maxLength=100;
      }
      input.name=name;input.value=q.get(name)??defaults[name]??'';
      input.setAttribute('aria-label',label);
      input.style.cssText='background:#FFF;border:1px solid #D9D9D6;color:#0B0B0B;border-radius:6px;padding:9px 10px;min-width:0;font:inherit';
      wrapper.append(input);form.append(wrapper);controls.set(name,input);
    }
    const error=document.createElement('p');error.setAttribute('role','alert');
    error.style.cssText='color:#B91C1C;margin:0;font-size:12px';form.append(error);
    const actions=document.createElement('div');actions.style.cssText='display:flex;flex-wrap:wrap;gap:7px;margin-top:5px';
    const apply=()=>{
      const values={};
      for(const [name,input] of controls){
        if(!input.checkValidity()) {input.reportValidity();return;}
        const value=input.value.trim();
        if(value && input.type==='number' && (!Number.isFinite(Number(value))||Number(value)<0)){
          error.textContent='Enter a valid non-negative number.';return;
        }
        values[name]=value||defaults[name]||null;
      }
      const comparisons=[['posted_from','posted_to'],['due_from','due_to'],
        ['amount_min','amount_max'],['paid_min','paid_max'],['balance_min','balance_max'],
        ['days_min','days_max'],['risk_min','risk_max']];
      for(const [lo,hi] of comparisons){
        const l=values[lo],h=values[hi];
        if(l!==undefined&&h!==undefined&&l!=null&&h!=null &&
          (lo.includes('from')?l>h:Number(l)>Number(h))){
          error.textContent='Minimum cannot exceed maximum.';return;
        }
      }
      global.InvoiceIQ.applyColumnFilters(values);dialog.close();
    };
    form.addEventListener('submit',event=>{event.preventDefault();apply();});
    actions.append(btn('Apply',apply,true));
    actions.append(btn('Clear',()=>{
      global.InvoiceIQ.applyColumnFilters(Object.fromEntries(group.fields.map(([name])=>[name,defaults[name]||null])));
      dialog.close();
    }));
    actions.append(btn('Close',()=>dialog.close()));
    form.append(actions);
    if(SORT[key]){
      const sorting=document.createElement('div');sorting.style.cssText='display:flex;gap:8px;margin-top:14px;border-top:1px solid #E5E5E2;padding-top:14px';
      sorting.append(btn('Sort ascending ↑',()=>{global.InvoiceIQ.applyColumnFilters({sort:SORT[key][0]});dialog.close();}));
      sorting.append(btn('Sort descending ↓',()=>{global.InvoiceIQ.applyColumnFilters({sort:SORT[key][1]});dialog.close();}));
      form.append(sorting);
    }
    if(key==='risk'){
      const note=document.createElement('small');
      note.textContent='Historical scores are stored ML predictions. Illustrative scores are sample values.';
      note.style.cssText='color:#6B6B6B;line-height:1.5';form.append(note);
    }
    dialog.append(form);
    const first=controls.values().next().value;first?.focus();
  };
  function scan(root,fn){
    for(const element of root.querySelectorAll('[data-column-filter]'))fn(element);
    for(const element of root.querySelectorAll('*'))if(element.shadowRoot)scan(element.shadowRoot,fn);
  }
  const sync = query => {
    scan(document,button=>{
      const group=groups[button.dataset.columnFilter];if(!group)return;
      const count=group.fields.reduce((n,[name])=>{
        const value=query.get(name);return n+Number(value!=null&&value!==''&&value!==(defaults[name]||''));
      },0);
      button.style.background=count?'#0B0B0B':'transparent';
      button.style.color=count?'#FFF':'#525252';
      button.style.borderColor=count?'#0B0B0B':'transparent';
      button.setAttribute('aria-pressed',count?'true':'false');
      button.title=(count?count+' active filter(s) · ':'')+'Filter / sort '+group.label;
    });
  };
  // The reference framework uses nested shadow roots: use composedPath to find clicked headers.
  document.addEventListener('click',event=>{
    const button=(event.composedPath?.()||[]).find(
      node=>node?.matches?.('[data-column-filter]'));
    if(!button)return;
    event.preventDefault();show(button.dataset.columnFilter);
  });
  global.InvoiceIQColumnFilters={sync,groups,ALL_KEYS};
})(window);
