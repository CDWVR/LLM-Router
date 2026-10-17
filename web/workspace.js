/* Workspace UI: all user/provider text is inserted as text, never HTML. */
(() => {
  const launch = document.getElementById("workspace-open");
  let snapshot, section = "Overview", content, notice;
  const node = (tag, cls, value) => { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = value; return n; };
  const button = (label, action, cls = "ws-button") => { const n = node("button", cls, label); n.type = "button"; n.addEventListener("click", async () => { n.disabled = true; try { await action(); } catch (e) { notify(e.message || "Something went wrong. Please try again.", true); if(e.code==="configuration_conflict")notice.append(button("Reload latest (discard draft)",refresh)); } finally { n.disabled = false; } }); return n; };
  const money = (value) => `$${Number(value || 0).toFixed(4)}`;
  const errors = { unauthorized: "Sign in again to continue.", configuration_conflict: "Another administrator saved changes first. Your edits have not been saved. Reload the latest version before editing again.", admin_required: "Only workspace administrators can change this.", provider_discovery_failed: "Could not list models. Check the credential and endpoint, or enter a model ID manually.", api_key_required: "Enter a provider API key.", provider_host_not_approved: "This endpoint must be approved by the server operator.", project_budget_exceeded: "This workspace has reached its 24-hour budget.", no_eligible_model: "Connect and enable a model that fits your routing limits.", invalid_number: "Check the numeric limits and pricing values.", request_in_progress: "This request is already running.", invalid_invitation: "This invitation is invalid, used, or expired." };
  async function call(path, data) {
    const response = await fetch(path, data === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json", "X-CSRF-Token":document.querySelector('meta[name="csrf-token"]').content}, body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) { const code = typeof result.error === "string" ? result.error : result.error?.code; const error=new Error(errors[code] || (code || "Request failed").replaceAll("_", " ")); error.code=code; throw error; }
    return result;
  }
  const change = (action, data={}) => call("/workspace", {action,...data});
  function notify(message, bad=false) { notice.textContent = message; notice.className = "ws-notice"+(bad?" error":""); notice.hidden = false; notice.scrollIntoView({block:"nearest", behavior:matchMedia("(prefers-reduced-motion: reduce)").matches?"instant":"smooth"}); }
  function field(label, value="", type="text", hint="") {
    const wrap = node("label", "ws-field"), input = node(type === "textarea" ? "textarea" : "input");
    if (type !== "textarea") input.type = type;
    input.value = value; input.autocomplete = type === "password" ? "new-password" : "off";
    wrap.append(node("span", null, label), input); if (hint) wrap.append(node("small", null, hint));
    return {wrap,input};
  }
  function heading(title, copy) { content.append(node("h2", "ws-title", title), node("p", "ws-copy", copy)); }
  function row(title, copy, actions=[]) { const item=node("div","ws-row"), left=node("div"); left.append(node("strong",null,title),node("p",null,copy)); item.append(left,...actions); return item; }
  function link(label, href) { const a=node("a","ws-link",label); a.href=href; return a; }
  function empty(copy) { content.append(node("p", "ws-empty", copy)); }
  async function refresh() { snapshot=await call("/workspace"); render(); }
  function render() {
    const root=node("div","workspace");
    const tabs=node("nav","ws-tabs"); tabs.setAttribute("aria-label","Workspace sections");
    const admin=["owner","admin"].includes(snapshot.role);
    for(const name of ["Overview","Conversations","Models","Routing","Compare","Reports","Account"]) {
      if ((name==="Reports" || name==="Routing")&&!admin) continue;
      const tab=button(name,async()=>{section=name;await refresh();},"ws-tab"+(section===name?" active":"")); tab.setAttribute("aria-current",section===name?"page":"false"); tabs.append(tab);
    }
    notice=node("p","ws-notice"); notice.hidden=true; notice.setAttribute("role","status");
    content=node("section","ws-content"); root.append(tabs,notice,content);
    document.getElementById("panel-body").replaceChildren(root);
    document.getElementById("panel-body").scrollTop=0;
    document.getElementById("panel").classList.add("workspace-panel");
    ({Overview:overview,Conversations:conversations,Models:models,Routing:routing,Compare:compare,Reports:reports,Account:account})[section]();
    tabs.querySelector(".active")?.focus({preventScroll:true});
  }
  function overview() {
    const project=snapshot.projects.find(p=>p.id===snapshot.project);
    heading(project?.name || "Your workspace", "Your models, conversations and usage in one place.");
    const usage=snapshot.usage, requests=usage.requests;
    const stats=node("div","ws-stats");
    const completed=requests.filter(r=>r.status==="completed");
    const times=completed.map(r=>r.telemetry.elapsed_ms).filter(Number.isFinite).sort((a,b)=>a-b);
    for(const [title,value] of [["Reserved · last 24h",money(usage.reserved_24h_usd)],["24-hour limit",money(usage.daily_limit_usd)],["Completed",String(completed.length)],["p95 response",times.length?`${(times[Math.min(times.length-1,Math.ceil(times.length*.95)-1)]/1000).toFixed(1)}s`:"—"]]) {
      const card=node("div","ws-stat"); card.append(node("span",null,title),node("strong",null,value));stats.append(card);
    }
    content.append(stats,node("p","ws-copy","Reservations include retry allowances and may exceed billed usage. Request metrics show the most recent 500 requests."));
    if(usage.reserved_24h_usd>=usage.daily_limit_usd*.8)content.append(node("p","ws-notice","Your workspace has used at least 80% of its 24-hour budget."));
    content.append(node("h3",null,"Provider health"));
    if(!snapshot.providers.length)empty("Connect your first provider in Models to get started.");
    for(const p of snapshot.providers)content.append(row(p.name,`${p.configured?"Configured":"Needs a credential"} · ${p.health?.status||"untested"}`));
    content.append(node("h3",null,"Recent requests"));
    if(!requests.length)empty("Usage appears here after your first request.");
    for(const r of requests.slice(0,12))content.append(row(r.telemetry.returned_model_id||r.telemetry.error||"Request",`${r.status} · ${money(r.charge_micro/1e6)} reserved · ${new Date(r.created*1000).toLocaleString()}`));
  }
  function conversations() {
    heading("A place for every conversation", "Search, revisit, rename, archive or export your workspace threads.");
    const search=field("Search conversations", "", "search"), list=node("div"); content.append(search.wrap,list);
    const draw=()=>{
      list.replaceChildren();
      const matches=snapshot.threads.filter(t=>t.title.toLowerCase().includes(search.input.value.toLowerCase()));
      if(!matches.length)list.append(node("p","ws-empty","No conversations yet. Send a message to start one."));
      for(const thread of matches){
        const item=row(thread.title,thread.archived?"Archived":new Date(thread.updated*1000).toLocaleString());
        const actions=node("div","ws-actions");
        actions.append(button("Open",async()=>{await change("thread_open",{id:thread.id});location.reload();}),button("Rename",()=>{
          const f=field("Conversation name",thread.title);item.replaceChildren(f.wrap,button("Save",async()=>{await change("thread_rename",{id:thread.id,title:f.input.value});await refresh();}));f.input.focus();
        }),button(thread.archived?"Restore":"Archive",async()=>{await change("thread_archive",{id:thread.id,archived:!thread.archived});await refresh();}),link("Export",`/export/thread/${encodeURIComponent(thread.id)}`));
        item.append(actions);list.append(item);
      }
    }; search.input.addEventListener("input",draw); draw();
  }
  function models() {
    heading("Bring your own models", "Connect a provider, check access, then review and enable the models you want Router to use.");
    if(["owner","admin"].includes(snapshot.role))content.append(button("+ Connect provider",()=>connect()));
    for(const p of snapshot.providers)content.append(row(p.name,`${p.adapter} · ${p.health?.status||"untested"}`, [button("Manage",()=>connect(p))]));
    content.append(node("h3",null,"Model catalog"));
    for(const m of snapshot.models)content.append(row(m.display_name,`${m.provider} · ${m.enabled?"Enabled":"Disabled"} · ${m.context_window.toLocaleString()} context`,["owner","admin"].includes(snapshot.role)?[button("Edit",()=>modelForm(m.provider,m.upstream_model_id,m))]:[]));
    if(!snapshot.models.length)empty("No models enabled. Add a connection above to begin.");
  }
  function connect(existing) {
    content.replaceChildren();heading("01 / Connect", "Credentials are encrypted on the server. Leave the key empty when editing to keep the existing credential.");
    let template=existing ? ({"https://api.openai.com/v1":"openai","https://api.anthropic.com/v1":"anthropic","https://api.groq.com/openai/v1":"groq","https://openrouter.ai/api/v1":"openrouter"}[existing.base_url]||"custom") : "openai";
    const choices=node("div","ws-choices");
    const name=field("Connection name",existing?.name||"openai"), key=field("API key","","password"), url=field("Endpoint URL",existing?.base_url||"https://api.openai.com/v1");
    name.input.readOnly=Boolean(existing);
    const templates={openai:"https://api.openai.com/v1",anthropic:"https://api.anthropic.com/v1",groq:"https://api.groq.com/openai/v1",openrouter:"https://openrouter.ai/api/v1",custom:"https://your-approved-host.example/v1",local:"http://127.0.0.1:11434/v1"};
    for(const choice of Object.keys(templates)){
      if(choice==="local"&&snapshot.saas)continue;
      const b=button(choice,()=>{template=choice==="local"?"custom":choice;if(!existing)name.input.value=choice;url.input.value=templates[choice];choices.querySelectorAll("button").forEach(x=>x.classList.remove("selected"));b.classList.add("selected");},"ws-choice"+(choice===template?" selected":""));choices.append(b);
    }
    const enabled=node("input");enabled.type="checkbox";enabled.checked=existing?.enabled!==false;
    const enabledLabel=node("label","ws-check");enabledLabel.append(enabled,document.createTextNode("Connection enabled"));
    content.append(choices,name.wrap,url.wrap,key.wrap,enabledLabel,button("Save connection",async()=>{
      await change("connection_save",{revision:existing?.revision||0,name:name.input.value,template,base_url:url.input.value,api_key:key.input.value,enabled:enabled.checked});key.input.value="";snapshot=await call("/workspace"); discover(name.input.value);
    },"ws-button primary"));
  }
  function discover(provider) {
    content.replaceChildren();heading("02 / Discover", "Check credentials and list available models. This step does not generate an answer.");
    const manual=field("Model ID", "", "text", "Use the exact model ID supplied by your provider.");
    content.append(button("Check connection & discover models",async()=>{
      const result=await change("connection_test",{name:provider});const list=node("div","ws-discovery");
      for(const model of result.models)list.append(button(model.name,()=>modelForm(provider,model.id),"ws-model-option"));
      content.append(node("p","ws-notice","Connection verified. Choose a model below."),list);
    }),manual.wrap,button("Configure model manually",()=>modelForm(provider,manual.input.value)));
  }
  function modelForm(provider, upstream, existing) {
    content.replaceChildren();heading("03 / Review & enable", "Verify capabilities and prices against your provider's documentation. Quality and latency are routing estimates.");
    const fields={model_id:field("Router model ID",existing?.model_id||`${provider}/${upstream}`),upstream_model_id:field("Provider model ID",upstream),display_name:field("Display name",existing?.display_name||upstream),context_window:field("Context window (tokens)",existing?.context_window||128000,"number"),max_output_tokens:field("Maximum output tokens",existing?.max_output_tokens||4096,"number"),input_usd_per_million:field("Input USD / million tokens",existing?.input_usd_per_million??"","number"),output_usd_per_million:field("Output USD / million tokens",existing?.output_usd_per_million??"","number"),expected_latency_ms:field("Expected latency (ms)",existing?.expected_latency_ms||2000,"number"),quality:field("Estimated quality (0–1)",existing?.quality?.chat??.7,"number"),criteria:field("Best suited for",existing?.criteria||"General conversation and reasoning")};
    fields.model_id.input.readOnly=Boolean(existing);
    if(!existing){
      // "Configure model manually" opens this form before the provider ID is
      // known, so the default Router ID would be saved as a bare "provider/".
      // Follow the provider ID until the operator renames the model themselves.
      let renamed=false;
      fields.model_id.input.addEventListener("input",()=>{renamed=true;});
      fields.upstream_model_id.input.addEventListener("input",()=>{
        if(!renamed){const value=fields.upstream_model_id.input.value.trim();
          fields.model_id.input.value=value?`${provider}/${value}`:"";}
      });
    }
    const grid=node("div","ws-grid");for(const f of Object.values(fields)){f.input.step="any";grid.append(f.wrap);}
    const image=node("input");image.type="checkbox";image.checked=existing?.modalities?.includes("image")||false;
    const imageLabel=node("label","ws-check");imageLabel.append(image,document.createTextNode("Supports image input"));
    const enable=node("input");enable.type="checkbox";enable.checked=existing?.enabled!==false;
    const enableLabel=node("label","ws-check");enableLabel.append(enable,document.createTextNode("Enable for routing"));
    const tools=node("input");tools.type="checkbox";tools.checked=existing?.supports_tools===true;
    const toolsLabel=node("label","ws-check");toolsLabel.append(tools,document.createTextNode("Supports function tools (API)"));
    content.append(grid,imageLabel,toolsLabel,enableLabel,button("Save model",async()=>{
      const m={revision:existing?.revision||0,provider,regions:["us"],modalities:image.checked?["text","image"]:["text"],supports_tools:tools.checked,enabled:enable.checked,output_token_cap:1024};
      for(const [name,f] of Object.entries(fields)){
        if(!f.input.value.trim())throw new Error("Fill in every model field, including verified pricing.");
        m[name]=f.input.type==="number"?Number(f.input.value):f.input.value;
      }
      const q=m.quality;m.quality={chat:q,code:q,plan:q,extract:q,image:q};await change("model_save",{model:m});await refresh();notify("Model saved. It is available according to your routing settings.");
    },"ws-button primary"));
  }
  function routing() {
    heading("Choose your tradeoffs", "Project settings apply to every member and API key. Requests may narrow these limits.");
    let selected=snapshot.settings.profile;const choices=node("div","ws-choices");
    for(const [key,title,copy] of [["balanced","Balanced","Quality, cost and speed"],["fast","Fast","Prioritize lower latency"],["quality","Best quality","Prioritize quality estimates"],["private","Private / local","Loopback models only"]]){
      const b=button("",()=>{selected=key;choices.querySelectorAll("button").forEach(x=>x.classList.remove("selected"));b.classList.add("selected");},"ws-choice"+(selected===key?" selected":""));b.append(node("strong",null,title),node("small",null,copy));choices.append(b);
    }
    const cost=field("Maximum USD per request",snapshot.settings.max_cost_usd,"number"),latency=field("Maximum latency (seconds)",snapshot.settings.max_latency_ms/1000,"number"),daily=field("Rolling 24-hour budget (USD)",snapshot.usage.daily_limit_usd,"number");
    cost.input.step="0.01";daily.input.step="0.01";content.append(choices,cost.wrap,latency.wrap,daily.wrap,node("h3",null,"Allowed connections"),node("p","ws-copy","Leave all unchecked to allow every enabled connection."));
    const checks=[];for(const p of snapshot.providers){const c=node("input");c.type="checkbox";c.checked=snapshot.settings.allowed_providers.includes(p.name);const l=node("label","ws-check");l.append(c,document.createTextNode(p.name));content.append(l);checks.push([p.name,c]);}
    content.append(button("Save routing policy",async()=>{await change("settings",{revision:snapshot.settings.revision||0,profile:selected,max_cost_usd:Number(cost.input.value),max_latency_ms:Number(latency.input.value)*1000,daily_limit_usd:Number(daily.input.value),allowed_providers:checks.filter(([,c])=>c.checked).map(([name])=>name)});await refresh();notify("Routing policy saved.");},"ws-button primary"));
  }
  function compare() {
    heading("Compare before you commit", "Run the same prompt on two or three models. The total cost ceiling is split evenly between them.");
    const prompt=field("Prompt","","textarea"),ceiling=field("Total cost ceiling (USD)",.1,"number"),checks=[];ceiling.input.step=".01";content.append(prompt.wrap,ceiling.wrap);
    for(const m of snapshot.models.filter(m=>m.enabled)){const c=node("input");c.type="checkbox";const l=node("label","ws-check");l.append(c,document.createTextNode(m.display_name));content.append(l);checks.push([m.model_id,c]);}
    const results=node("div","ws-comparison");content.append(button("Run comparison · uses provider credits",async()=>{
      results.replaceChildren(node("p","ws-notice","Running selected models…"));
      const result=await call("/compare",{prompt:prompt.input.value,models:checks.filter(([,c])=>c.checked).map(([id])=>id),max_cost_usd:Number(ceiling.input.value),request_id:crypto.randomUUID()});results.replaceChildren();
      for(const r of result.results){const card=node("article","ws-result");card.append(node("h3",null,r.model_id),node("p","ws-copy",r.error||`${(r.elapsed_ms/1000).toFixed(1)}s · ${money(r.reserved_usd)} reserved`),node("pre",null,r.text||""));results.append(card);}
    },"ws-button primary"),button("Save prompt to evaluation set",async()=>{await change("save_evaluation",{prompt:prompt.input.value});notify("Evaluation prompt saved.");}),results);
    for(const e of snapshot.evaluations)content.append(row(e.prompt.slice(0,80),"Saved evaluation prompt",[button("Use",()=>{prompt.input.value=e.prompt;}),link("Export",`/export/evaluation/${e.id}`)]));
  }
  function reports() {
    heading("Close the feedback loop", "Reports and screenshots are visible only to workspace administrators.");
    if(!snapshot.reports.length)empty("No reports yet. The bug button captures issues as they happen.");
    for(const r of snapshot.reports){const card=node("article","ws-result");card.append(node("strong",null,r.id),node("p",null,r.description),node("p","ws-copy",r.status));
      const tags=field("Tags",r.tags||""), actions=node("div","ws-actions");
      for(const state of ["new","investigating","resolved"])actions.append(button(state,async()=>{await change("report_status",{revision:r.revision||0,id:r.id,status:state,tags:tags.input.value});await refresh();}));
      actions.append(link("Export",`/export/report/${r.id}`),button("View screenshot",()=>{const img=node("img","ws-report-image");img.alt="Submitted bug report screenshot";img.src=`/export/report/${r.id}?image=1`;img.addEventListener("error",()=>{img.remove();notify("This report has no screenshot.");},{once:true});card.append(img);}));card.append(tags.wrap,actions);content.append(card);
    }
  }
  function account() {
    heading("Workspace & access", `Your role: ${snapshot.role}. API keys are project-scoped, expire in 90 days, and are shown once.`);
    for(const p of snapshot.projects)content.append(row(p.name,p.role,[button(p.id===snapshot.project?"Current":"Switch",async()=>{if(p.id!==snapshot.project){await change("switch_project",{id:p.id});sessionStorage.removeItem("router.draft");location.reload();}})]));
    const project=field("New workspace name");content.append(project.wrap,button("Create workspace",async()=>{await change("new_project",{name:project.input.value});location.reload();}));
    const invite=field("Invitation code");content.append(invite.wrap,button("Join workspace",async()=>{await change("join",{invitation:invite.input.value});location.reload();}));
    if(["owner","admin"].includes(snapshot.role)){
      content.append(node("h3",null,"Members"),button("Create single-use member invitation",async()=>{const result=await change("invite");const code=field("Invitation · expires in 24 hours",result.invitation);code.input.readOnly=true;content.append(code.wrap);code.input.select();}));
      for(const member of snapshot.members)content.append(row(member.user_id,member.role,member.role==="owner"?[]:[button("Remove access",async()=>{await change("remove_member",{user_id:member.user_id});await refresh();})]));
      content.append(node("h3",null,"API keys"));const name=field("Key name");content.append(name.wrap,button("Create API key",async()=>{const result=await change("key_create",{name:name.input.value});const secret=field("Copy this key now",result.key);secret.input.readOnly=true;content.append(secret.wrap);secret.input.select();}));
      for(const k of snapshot.keys)content.append(row(k.name,`Expires ${new Date(k.expires*1000).toLocaleDateString()}`,[button("Revoke",async()=>{await change("key_revoke",{id:k.id});await refresh();})]));
    }
    content.append(button("Sign out",async()=>{await change("logout");sessionStorage.removeItem("router.draft");location.reload();}));
  }
  launch?.addEventListener("click",async()=>{
    try { snapshot=await call("/workspace");openDrawer(node("div"),"Workspace",launch);render(); }
    catch(error){toast(error.message);}
  });
  document.getElementById("panel-close").addEventListener("click",()=>document.getElementById("panel").classList.remove("workspace-panel"));
  document.getElementById("connections").addEventListener("click",()=>document.getElementById("panel").classList.remove("workspace-panel"));
  // Conversation drafts stay in memory; never restore them into another signed-in session.
})();
