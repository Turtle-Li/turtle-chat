(() => {
  "use strict";
  const API = "/api/v1/turtle/project-api";
  const token = () => {
    let value = localStorage.getItem("token") || "";
    try { value = JSON.parse(value); } catch (_) {}
    return typeof value === "string" ? value : "";
  };
  const esc = (value) => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
  const number = (value) => new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
  const usd = (micro) => micro == null ? "—" : `$${(Number(micro || 0) / 1_000_000).toFixed(6)}`;
  const usageSource = (source) => source === "upstream_reported" ? "上游 usage" : source === "locally_estimated" ? "本地估算" : source === "not_charged" ? "未计费" : "请求兜底";
  const time = (value) => value ? new Date(Number(value) * 1000).toLocaleString("zh-CN", { hour12:false }) : "尚未调用";
  const duration = (value) => `${number(value)} ms`;
  const request = async (path, options={}) => {
    const response = await fetch(`${API}${path}`, { ...options, headers:{Authorization:`Bearer ${token()}`,...(options.headers||{})} });
    let payload={}; try { payload=await response.json(); } catch (_) {}
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : payload?.error?.message || "请求失败");
    return payload;
  };
  const state={bundle:null,usage:null,offset:0,limit:100,revokeId:null};
  document.querySelector("#api-base").textContent=`${window.location.origin}/api/project/v1`;
  const summary=document.querySelector("#summary"),keys=document.querySelector("#keys"),records=document.querySelector("#records"),notice=document.querySelector("#notice"),filters=document.querySelector("#filters"),createForm=document.querySelector("#create-key");
  const renderSummary=()=>{
    const totals=state.usage?.totals||{};
    const lifetime=(state.bundle?.keys||[]).reduce((sum,key)=>({tokens:sum.tokens+Number(key.total_tokens||0),requests:sum.requests+Number(key.request_count||0),official:sum.official+Number(key.total_official_cost_microusd||0),actual:sum.actual+Number(key.total_actual_cost_microusd||0)}),{tokens:0,requests:0,official:0,actual:0});
    summary.innerHTML=[
      ["可用 API 额度",state.bundle?.balance_microusd==null?"未启用预付额度":usd(state.bundle.balance_microusd),state.bundle?.balance_microusd==null?"历史兼容模式":`调用中占用 ${usd(state.bundle.reserved_microusd)}`],
      ["累计实际消耗",usd(lifetime.actual),`官方参考 ${usd(lifetime.official)}`],
      ["记录 Token",number(lifetime.tokens),`${number(totals.locally_estimated_requests)} 条本地估算`],
      ["累计请求",number(lifetime.requests),`${number(totals.errors)} 条当前筛选异常`],
    ].map(([label,value,copy])=>`<article class="stat"><small>${label}</small><strong>${value}</strong><em>${copy}</em></article>`).join("");
  };
  const renderKeys=()=>{
    const items=state.bundle?.keys||[];
    const activeCount=items.filter((item)=>item.status==="active").length,maxKeys=Number(state.bundle?.max_keys)||5,atLimit=activeCount>=maxKeys;
    keys.innerHTML=items.length?items.map(item=>`<div class="key" data-status="${esc(item.status)}"><div><strong>${esc(item.name)}</strong><code>${esc(item.key_prefix)}••••••••</code><small class="muted">${number(item.request_count)} 次 · ${usd(item.total_actual_cost_microusd)} · ${time(item.last_used_at)}</small></div>${item.status==="active"?`<button data-revoke="${esc(item.id)}">撤销</button>`:"<small>已撤销</small>"}</div>`).join(""):'<div class="empty">还没有项目密钥</div>';
    document.querySelector("#key-limit").textContent=`${number(activeCount)} / ${number(maxKeys)} 个有效密钥`;
    createForm.querySelector("button[type=submit]").disabled=atLimit;
    createForm.querySelector("button[type=submit]").textContent=atLimit?"已达上限":"创建密钥";
    createForm.querySelector("input").disabled=atLimit;
    const select=filters.elements.key_id,selected=select.value;
    select.innerHTML='<option value="">全部项目</option>'+items.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    select.value=selected;
  };
  const renderRecords=()=>{
    const items=state.usage?.recent||[];
    records.innerHTML=items.length?items.map(item=>`<tr><td><strong>${esc(time(item.created_at))}</strong><small>${esc(item.project_name)} · ${esc(item.request_id)}</small></td><td><strong>${esc(item.pricing_profile||item.model)}</strong><small>${esc(item.route||"默认")}</small></td><td class="${item.outcome==="success"?"ok":"bad"}">${esc(item.outcome)}<small>HTTP ${number(item.status_code)}</small></td><td><strong>入 ${item.prompt_tokens==null?"—":number(item.prompt_tokens)} · 出 ${item.completion_tokens==null?"—":number(item.completion_tokens)}</strong><small>缓存 ${number(item.cached_tokens)} · ${usageSource(item.usage_source)}</small></td><td><strong>${usd(item.actual_cost_microusd)}</strong><small>官方 ${usd(item.official_cost_microusd)} × ${Number(item.cost_multiplier??1).toFixed(2)}</small></td><td>${duration(item.latency_ms)}</td></tr>`).join(""):'<tr><td colspan="6" class="empty">当前筛选范围没有调用记录</td></tr>';
    const page=state.usage?.pagination||{},size=Number(page.limit)||state.limit,current=Math.floor((Number(page.offset)||0)/size)+1,total=Math.max(1,Math.ceil((Number(page.total)||0)/size));
    document.querySelector("#page-state").textContent=`第 ${current} / ${total} 页 · ${number(page.total)} 条`;
    document.querySelector("#previous-page").disabled=state.offset<=0;
    document.querySelector("#next-page").disabled=!page.has_more;
  };
  const usagePath=()=>{const query=new URLSearchParams(new FormData(filters));for(const [key,value] of [...query])if(!value)query.delete(key);query.set("model","gpt-5-web");query.set("limit",String(state.limit));query.set("offset",String(state.offset));return `/usage?${query}`};
  const load=async()=>{
    try{
      const hours=Number(filters.elements.hours.value)||24;
      const bundle=await request(`/me?hours=${hours}`);
      state.bundle=bundle;
      if(!bundle.enabled){notice.hidden=false;notice.textContent="管理员尚未为当前账号开通项目 API 权限。";summary.innerHTML="";document.querySelector(".layout").hidden=true;return}
      document.querySelector(".layout").hidden=false;notice.hidden=true;state.usage=await request(usagePath());renderSummary();renderKeys();renderRecords();
    }catch(error){notice.hidden=false;notice.textContent=error.message}
  };
  filters.addEventListener("change",async()=>{state.offset=0;try{state.usage=await request(usagePath());renderSummary();renderRecords()}catch(error){notice.hidden=false;notice.textContent=error.message}});
  createForm.addEventListener("submit",async(event)=>{event.preventDefault();const form=event.currentTarget,name=String(new FormData(form).get("name")||"").trim();if(!name)return;try{const result=await request("/keys",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});document.querySelector("#secret-value").textContent=result.api_key;document.querySelector("#secret-dialog").showModal();form.reset();await load()}catch(error){notice.hidden=false;notice.textContent=error.message}});
  keys.addEventListener("click",(event)=>{const button=event.target.closest("[data-revoke]");if(!button)return;state.revokeId=button.dataset.revoke;document.querySelector("#revoke-dialog").showModal()});
  document.querySelector("#confirm-revoke").addEventListener("click",async(event)=>{if(!state.revokeId)return;event.currentTarget.disabled=true;try{await request(`/keys/${encodeURIComponent(state.revokeId)}`,{method:"DELETE"});document.querySelector("#revoke-dialog").close();state.revokeId=null;await load()}catch(error){notice.hidden=false;notice.textContent=error.message}finally{event.currentTarget.disabled=false}});
  document.querySelector("#copy-secret").addEventListener("click",async()=>{await navigator.clipboard.writeText(document.querySelector("#secret-value").textContent);document.querySelector("#copy-secret").textContent="已复制"});
  document.querySelector("#refresh").addEventListener("click",load);
  const changePage=async(delta)=>{state.offset=Math.max(0,state.offset+delta*state.limit);state.usage=await request(usagePath());renderSummary();renderRecords()};
  document.querySelector("#previous-page").addEventListener("click",()=>void changePage(-1));
  document.querySelector("#next-page").addEventListener("click",()=>void changePage(1));
  load();
})();
