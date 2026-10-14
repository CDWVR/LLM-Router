"use strict";

const $ = (id) => document.getElementById(id);
const root = document.documentElement;
const body = document.body;
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const bar = $("bar");
const orb = $("orb");
const heroSlot = $("hero-slot");
const brandSlot = $("brand-slot");
const composerSlot = $("composer-slot");
const thread = $("thread");
const form = $("composer");
const message = $("message");
const sendButton = $("send");
const file = $("file");
const attachment = $("attachment");
const attachmentThumb = $("attachment-thumb");
const attachmentName = $("attachment-name");
const verify = $("verify");
const reuse = $("reuse");
const reset = $("reset");
const hint = $("hint");
const toBottom = $("to-bottom");
const panel = $("panel");
const panelBody = $("panel-body");
const scrim = $("scrim");
const toasts = $("toasts");

const IMAGE_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const IMAGE_CAP = 8 * 1024 * 1024;
const MODEL_COLORS = ["var(--m0)", "var(--m1)", "var(--m2)"];
const TASKS = { chat: "Chat", code: "Code", extract: "Extraction", plan: "Planning", image: "Image" };
const REJECTIONS = {
  modality: "Can't read this kind of input",
  context_window: "Conversation is longer than its context",
  latency_ceiling: "Too slow for the time limit",
  residency: "Not available in the allowed region",
  denylist: "Blocked by policy",
  cost_ceiling: "Cannot fit within the remaining budget",
  provider_not_configured: "Provider key is missing or the connection is disabled",
};
const FAILURES = {
  rate_limited: "rate-limited",
  latency_budget_exceeded: "ran out of time",
  call_failed: "call failed",
};
const ERRORS = {
  router_busy: { title: "Router is finishing another request", text: "Wait for it to finish, then try again.", action: "retry" },
  provider_not_configured: { title: "Provider needs setup", text: "Open Models & connections to check which providers are configured.", action: "edit" },
  provider_auth_failed: { title: "Provider rejected its credentials", text: "Check the provider key on the server, then try again.", action: "retry" },
  cost_budget_exceeded: { title: "The request exhausted its budget", text: "No further model calls were made. Shorten the conversation or review the server budget.", action: "edit" },
  output_validation_failed: { title: "The answer failed its format check", text: "No accepted answer was returned.", action: "retry" },
  verification_rejected: { title: "The second opinion found a problem", text: "The proposed answer was withheld. Try rephrasing or requesting more context.", action: "edit" },
  verification_unavailable: { title: "The answer could not be double-checked", text: "A different eligible model, time, and budget are needed. The unchecked answer was withheld.", action: "edit" },
  network: { title: "Can't reach Router", text: "The local server isn't responding. Check that app.py is still running.", action: "retry" },
  rate_limited: { title: "The provider is rate-limiting requests", text: "Wait a few seconds, then try again.", action: "retry" },
  latency_budget_exceeded: { title: "That took too long", text: "The reply ran past the time limit.", action: "retry" },
  all_models_failed: { title: "No model finished the reply", text: "Every candidate failed to answer.", action: "retry" },
  model_call_failed: { title: "The model didn't answer", text: "Something went wrong while calling the model.", action: "retry" },
  no_eligible_model: { title: "No model can take this request", text: "Every model was ruled out:", action: "edit" },
  message_too_long: { title: "That message is too long", text: "Keep it under 100,000 characters.", action: "edit" },
  image_too_large: { title: "That image is too large", text: "Use an image under 8 MB.", action: "edit" },
  payload_too_large: { title: "That request is too large", text: "Use a smaller image or a shorter message.", action: "edit" },
  invalid_image: { title: "That file isn't a supported image", text: "Use a PNG, JPEG, GIF or WebP file.", action: "edit" },
  empty_message: { title: "The message was empty", text: "Write something, then send.", action: "edit" },
  forbidden: { title: "Your session expired", text: "Reload the page to start a new session.", action: "reload" },
  locked: { title: "This router is locked", text: "Enter the passphrase to continue.", action: "unlock" },
};
const ICONS = {
  chevron: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M8 5.5l4.5 4.5L8 14.5"/></svg>',
  copy: '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6.5" y="6.5" width="9" height="9" rx="2"/><path d="M13.5 6.5V5.5a2 2 0 0 0-2-2h-6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h1"/></svg>',
  check: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4.5 10.5l3.5 3.5 7.5-8"/></svg>',
  up: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 9l3-5.5c1.1 0 2 .9 2 2V8h3.2a1.5 1.5 0 0 1 1.5 1.7l-.8 5A1.5 1.5 0 0 1 14.4 16H7M7 9H4.5v7H7M7 9v7"/></svg>',
  down: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M13 11l-3 5.5c-1.1 0-2-.9-2-2V12H4.8a1.5 1.5 0 0 1-1.5-1.7l.8-5A1.5 1.5 0 0 1 5.6 4H13M13 11h2.5V4H13M13 11V4"/></svg>',
  alert: '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 6.5v4.5M10 13.8v.2"/><circle cx="10" cy="10" r="7"/></svg>',
  image: '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3.5" y="4.5" width="13" height="11" rx="2"/><path d="M3.5 13l4-4 3 3 2-2 4 4"/></svg>',
};

let mode = "hero";
let busy = false;
let chosenFile = null;
let chosenUrl = "";
let hasLastImage = false;
let pendingReset = null;
let panelTrigger = null;

function emit(phase, model = -1) {
  window.dispatchEvent(new CustomEvent("router:phase", { detail: { phase, model } }));
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function icon(name) {
  const span = document.createElement("span");
  span.innerHTML = ICONS[name];
  return span.firstElementChild;
}

function modelColor(index) {
  if (index == null || index < 0) return "var(--accent)";
  return MODEL_COLORS[index % MODEL_COLORS.length];
}

function percent(value) {
  return value == null ? null : Math.round(Number(value) * 100);
}

function duration(ms) {
  if (ms == null) return "—";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function unbreakable(name) {
  return String(name || "").replaceAll("-", "\u2011");
}

function normalize(view) {
  view.model_name = unbreakable(view.model_name || view.model_id);
  for (const key of ["candidates", "rejected", "attempts"]) {
    for (const row of view[key] || []) row.name = unbreakable(row.name || row.model_id);
  }
  return view;
}

function taskLabel(task) {
  return TASKS[task] || "Chat";
}

function sourceSentence(view) {
  if (view.used_fallback) return `${view.routed_model_name} was the first choice. ${view.model_name} answered after recovery.`;
  const pct = percent(view.confidence);
  switch (view.analysis_source) {
    case "user_selected": return "You selected this model. It passed the eligibility checks.";
    case "jev":
      return pct == null ? "Jev chose this model for the question." : `Jev chose this model with ${pct}% confidence.`;
    case "jev_low_confidence_policy":
      return "Jev wasn't confident enough, so the ranking policy made the call.";
    case "jev_low_confidence_stickiness":
      return "Jev wasn't confident, and this is a short follow-up, so the previous model continued.";
    case "fallback_heuristic":
      return "The local task classifier and ranking policy made the call.";
    case "single_eligible":
      return "Only one model could take this request.";
    case "modality":
      return "An image was attached, so the vision model answered.";
    default:
      return "The ranking policy made the call.";
  }
}

/* Layout */

function layout(instant = false) {
  if (instant) orb.classList.add("instant");
  root.style.setProperty("--composer-h", `${form.offsetHeight}px`);
  const size = orb.offsetWidth || 1;
  if (mode === "hero") {
    const composerAt = composerSlot.getBoundingClientRect();
    root.style.setProperty("--composer-top", `${Math.round(composerAt.top + window.scrollY)}px`);
    const orbAt = heroSlot.getBoundingClientRect();
    orb.style.transform = `translate(${orbAt.left + window.scrollX}px, ${orbAt.top + window.scrollY}px) scale(${orbAt.width / size})`;
  } else {
    const orbAt = brandSlot.getBoundingClientRect();
    orb.style.transform = `translate(${orbAt.left}px, ${orbAt.top}px) scale(${orbAt.height / size})`;
  }
  if (instant) {
    void orb.offsetWidth;
    requestAnimationFrame(() => orb.classList.remove("instant"));
  }
}

function setMode(next, instant = false) {
  if (next === mode) return;
  const animate = !instant && !reduced;
  const before = form.getBoundingClientRect();
  mode = next;
  reset.hidden = next === "hero";
  body.dataset.dock = next;
  if (next === "chat" && animate) {
    body.classList.add("leaving-hero");
    window.setTimeout(() => {
      body.dataset.mode = "chat";
      body.classList.remove("leaving-hero");
      onScroll();
    }, 200);
  } else {
    body.dataset.mode = next;
  }
  if (next === "hero") window.scrollTo(0, 0);
  layout(!animate);
  onScroll();
  if (animate) {
    const shift = before.top - form.getBoundingClientRect().top;
    if (Math.abs(shift) > 1) {
      form.animate(
        [{ transform: `translate(-50%, ${shift}px)` }, { transform: "translate(-50%, 0)" }],
        { duration: 700, easing: "cubic-bezier(0.2, 0.8, 0.2, 1)" },
      );
    }
  }
}

function scrollToEnd(smooth = true) {
  window.scrollTo({ top: document.documentElement.scrollHeight, behavior: smooth && !reduced ? "smooth" : "auto" });
}

function onScroll() {
  const scrolled = mode === "chat" && window.scrollY > 4;
  bar.classList.toggle("scrolled", scrolled);
  const distance = document.documentElement.scrollHeight - window.scrollY - window.innerHeight;
  toBottom.hidden = mode !== "chat" || distance < 240;
}

function syncKeyboard() {
  const viewport = window.visualViewport;
  if (!viewport) return;
  const offset = Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop);
  root.style.setProperty("--kb", `${Math.round(offset)}px`);
}

/* Composer */

function updateSend() {
  sendButton.disabled = !busy && !message.value.trim();
}

function autosize() {
  const before = form.offsetHeight;
  message.style.height = "auto";
  message.style.height = `${Math.min(message.scrollHeight, 240)}px`;
  if (form.offsetHeight !== before) layout(true);
  updateSend();
}

function showAttachment(name, url) {
  attachmentName.textContent = name;
  if (url) attachmentThumb.src = url;
  else attachmentThumb.removeAttribute("src");
  attachment.hidden = false;
  layout(true);
}

function clearAttachment() {
  if (chosenUrl) URL.revokeObjectURL(chosenUrl);
  chosenUrl = "";
  chosenFile = null;
  file.value = "";
  attachmentThumb.removeAttribute("src");
  attachment.hidden = true;
  reuse.setAttribute("aria-pressed", "false");
  layout(true);
}

function chooseFile(candidate) {
  if (!candidate) return;
  if (!IMAGE_TYPES.includes(candidate.type)) {
    toast("Use a PNG, JPEG, GIF or WebP image.");
    return;
  }
  if (candidate.size > IMAGE_CAP) {
    toast("That image is over 8 MB.");
    return;
  }
  clearAttachment();
  chosenFile = candidate;
  chosenUrl = URL.createObjectURL(candidate);
  showAttachment(candidate.name || "Pasted image", chosenUrl);
  message.focus();
}

function dismissHint() {
  hint.classList.add("gone");
  try { localStorage.setItem("router.hint", "1"); } catch { /* storage unavailable */ }
}

function setBusy(on) {
  busy = on;
  sendButton.classList.toggle("busy", on);
  sendButton.setAttribute("aria-label", on ? "Stop" : "Send");
  if (on) sendButton.disabled = false;
  else updateSend();
}

/* Thread */

function addUser(text, imageAttached) {
  const article = el("article", "msg msg-user");
  const bubble = el("div", "bubble", text);
  article.append(bubble);
  if (imageAttached) {
    const note = el("span", "bubble-image");
    note.append(icon("image"), document.createTextNode("Image attached"));
    article.append(note);
  }
  article.dataset.text = text;
  thread.append(article);
  return article;
}

function addPending() {
  const article = el("article", "msg msg-ai pending");
  article.setAttribute("aria-label", "Router is working");
  const steps = el("div", "pending-steps");
  const labels = ["Checking models", "Choosing", "Writing"];
  const nodes = labels.map((label) => el("span", "step", label));
  nodes.forEach((node, index) => {
    if (index) steps.append(el("span", "step-sep"));
    steps.append(node);
  });
  const shimmer = el("div", "shimmer");
  shimmer.append(el("span"), el("span"), el("span"));
  article.append(steps, shimmer);
  thread.append(article);
  let current = 0;
  nodes[0].classList.add("active");
  const advance = (to) => {
    nodes[current].classList.replace("active", "complete");
    current = to;
    nodes[current].classList.add("active");
  };
  article.phase = (phase) => {
    const index = { eligibility: 0, analysis: 1, writing: 2 }[phase];
    if (index != null && index !== current) advance(index);
    document.title = `${phase === "verification" ? "Checking answer" : labels[index] || "Working"}… · Router`;
  };
  article.stop = () => {};
  return article;
}

function enhanceCode(reading) {
  reading.querySelectorAll("figure.code").forEach((figure) => {
    const caption = figure.querySelector("figcaption");
    const code = figure.querySelector("code");
    if (!caption || !code) return;
    const button = el("button", "copy-code");
    button.type = "button";
    button.append(icon("copy"), document.createTextNode("Copy"));
    button.addEventListener("click", () => copyText(code.textContent, button, "Copied"));
    caption.append(button);
  });
}

async function copyText(text, button, done) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    toast("Copy isn't available here.");
    return;
  }
  const original = [...button.childNodes];
  button.replaceChildren(icon("check"), ...(done ? [document.createTextNode(done)] : []));
  button.classList.add("done");
  window.setTimeout(() => {
    button.replaceChildren(...original);
    button.classList.remove("done");
  }, 1400);
}

function actionButton(name, label) {
  const button = el("button", "action");
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.title = label;
  button.append(icon(name));
  return button;
}

function markLatest(article) {
  thread.querySelectorAll(".msg-ai.latest").forEach((node) => {
    node.classList.remove("latest");
    node.querySelectorAll(".rate, .action-note").forEach((item) => item.remove());
  });
  article.classList.add("latest");
}

function addAssistant(view, reveal = false) {
  normalize(view);
  const article = el("article", "msg msg-ai");
  article.style.setProperty("--model", modelColor(view.model_index));

  const why = el("button", "why");
  why.type = "button";
  why.setAttribute("aria-haspopup", "dialog");
  why.setAttribute("aria-label", `Why ${view.model_name} was chosen`);
  const pct = percent(view.confidence);
  const meta = pct == null ? taskLabel(view.task) : `${taskLabel(view.task)} · Route ${pct}%`;
  why.append(el("span", "dot"), el("span", "why-name", view.model_name), el("span", "why-meta", meta), icon("chevron"));
  why.lastElementChild.classList.add("why-chevron");
  why.addEventListener("click", () => openPanel(view, why));

  const reading = el("div", "reading");
  reading.innerHTML = view.html;
  enhanceCode(reading);
  if (reveal && !reduced) {
    [...reading.children].forEach((child, index) => child.style.setProperty("--i", String(Math.min(index, 12))));
    reading.classList.add("reveal");
  }

  article.append(why, reading, answerStatus(view), buildActions(reading, view.request_id));
  thread.append(article);
  markLatest(article);
  return article;
}

function answerStatus(view) {
  const detail = [duration(view.elapsed_ms)];
  if (view.reserved_usd != null) detail.push(`Budget reserved $${Number(view.reserved_usd).toFixed(4)}`);
  if (view.verification === "accept") detail.push("Second opinion accepted");
  if (view.truncated) detail.push("Output limit reached — the reply may be incomplete");
  return el("p", "answer-status", detail.join(" · "));
}

function buildActions(reading, requestId) {
  const actions = el("div", "msg-actions");
  const copy = actionButton("copy", "Copy reply");
  copy.addEventListener("click", () => copyText(reading.innerText, copy));
  const up = actionButton("up", "Good answer");
  const down = actionButton("down", "Bad answer");
  up.classList.add("rate");
  down.classList.add("rate");
  const note = el("span", "action-note");
  for (const [button, label] of [[up, "pass"], [down, "fail"]]) {
    button.addEventListener("click", async () => {
      up.disabled = true;
      down.disabled = true;
      try {
        await api("/label", { label, request_id: requestId });
        button.setAttribute("aria-pressed", "true");
        note.textContent = "Thanks — logged for routing quality";
      } catch {
        up.disabled = false;
        down.disabled = false;
        note.textContent = "Couldn't save that rating";
      }
      note.classList.add("show");
    });
  }
  actions.append(copy, up, down, note);
  return actions;
}

function addError(error, payload, userArticle) {
  const code = error && error.error in ERRORS ? error.error : "model_call_failed";
  const spec = ERRORS[code];
  const article = el("article", "msg msg-ai");
  const callout = el("div", "callout");
  callout.setAttribute("role", "alert");
  const badge = el("span", "callout-icon");
  badge.append(icon("alert"));
  const content = el("div");
  content.append(el("h3", null, spec.title), el("p", null, spec.text));
  const rejected = (error && error.rejected) || [];
  if (rejected.length) {
    const list = el("ul");
    for (const row of rejected) list.append(el("li", null, `${unbreakable(row.name || row.model_id)} — ${REJECTIONS[row.detail] || row.reason}`));
    content.append(list);
  }
  const actions = el("div", "callout-actions");
  const primary = el("button", "button primary");
  primary.type = "button";
  if (spec.action === "retry") {
    primary.textContent = "Try again";
    primary.addEventListener("click", () => {
      article.remove();
      send(payload, userArticle);
    });
  } else if (spec.action === "reload") {
    primary.textContent = "Reload";
    primary.addEventListener("click", () => window.location.reload());
  } else {
    primary.textContent = "Edit message";
    primary.addEventListener("click", () => {
      message.value = userArticle.dataset.text || "";
      userArticle.remove();
      article.remove();
      if (!thread.children.length) setMode("hero");
      autosize();
      message.focus();
    });
  }
  actions.append(primary);
  content.append(actions);
  callout.append(badge, content);
  article.append(callout);
  thread.append(article);
  return article;
}

/* Network */

async function api(path, payload) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(payload),
    });
  } catch {
    throw { error: "network" };
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw { error: "model_call_failed" };
  }
  if (!response.ok) throw data;
  return data;
}

function nearBottom() {
  return document.documentElement.scrollHeight - window.scrollY - window.innerHeight < 180;
}

function followStream() {
  if (nearBottom()) scrollToEnd(false);
}

function openLive() {
  const article = el("article", "msg msg-ai");
  const why = el("button", "why");
  why.type = "button";
  const reading = el("div", "reading live");
  article.append(why, reading);
  thread.append(article);
  return { article, why, reading, view: null };
}

function paintLiveModel(live, preview) {
  const name = unbreakable(preview.model_name || preview.model_id);
  const pct = percent(preview.confidence);
  const meta = pct == null ? taskLabel(preview.task) : `${taskLabel(preview.task)} · Route ${pct}%`;
  live.article.style.setProperty("--model", modelColor(preview.model_index));
  const chevron = icon("chevron");
  chevron.classList.add("why-chevron");
  live.why.replaceChildren(el("span", "dot"), el("span", "why-name", name), el("span", "why-meta", meta), chevron);
  live.why.setAttribute("aria-label", `Why ${name} was chosen`);
}

function finishLive(live, view) {
  normalize(view);
  paintLiveModel(live, view);
  live.reading.classList.remove("live");
  live.reading.innerHTML = view.html;
  enhanceCode(live.reading);
  if (!live.why.dataset.bound) {
    live.why.dataset.bound = "1";
    live.why.addEventListener("click", () => openPanel(live.view, live.why));
  }
  live.view = view;
  live.article.querySelector(".live-status")?.remove();
  live.article.append(answerStatus(view), buildActions(live.reading, view.request_id));
  markLatest(live.article);
}

async function readSse(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut !== -1) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let name = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (data.length) onEvent(name, JSON.parse(data.join("\n")));
      cut = buffer.indexOf("\n\n");
    }
  }
}

// Batch incoming tokens to avoid a DOM write for every token without delaying reading.
function createReveal(live) {
  let pending = "";
  let timer = 0;
  let halted = false;

  function step() {
    timer = 0;
    if (!pending) return;
    const count = Math.min(pending.length, 8192);
    live.reading.append(document.createTextNode(pending.slice(0, count)));
    pending = pending.slice(count);
    followStream();
    if (pending) timer = window.setTimeout(step, 16);
  }

  return {
    add(text) {
      if (!text || halted) return;
      if (reduced) {
        live.reading.append(document.createTextNode(text));
        followStream();
        return;
      }
      pending += text;
      if (!timer) step();
    },
    clear() {
      pending = "";
      if (timer) window.clearTimeout(timer);
      timer = 0;
      live.reading.replaceChildren();
    },
    cancel() {
      halted = true;
      pending = "";
      if (timer) window.clearTimeout(timer);
      timer = 0;
    },
    drain() {
      if (reduced || halted || !pending) return Promise.resolve();
      return new Promise((resolve) => {
        const wait = () => {
          if (halted || !pending) resolve();
          else window.setTimeout(wait, 40);
        };
        wait();
      });
    },
  };
}

let turnAbort = null;

function settleStopped(live, payload, userArticle) {
  if (!live) return;
  live.reading.classList.remove("live");
  const row = el("div", "stopped");
  const retry = el("button", "button", "Try again");
  retry.type = "button";
  retry.addEventListener("click", () => {
    row.remove();
    send(payload, userArticle);
  });
  row.append(el("span", null, "Stopped"), retry);
  live.article.append(row);
}

async function send(payload, userArticle) {
  if (busy) return;
  payload.client_request_id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  setBusy(true);
  emit("routing");
  const pending = addPending();
  let live = null;
  let reveal = null;
  const abort = new AbortController();
  turnAbort = abort;
  abort.clientRequestId = payload.client_request_id;
  scrollToEnd();
  try {
    let response;
    try {
      response = await fetch("/turn", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, "Accept": "text/event-stream" },
        body: JSON.stringify(payload),
        signal: abort.signal,
      });
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      throw { error: "network" };
    }
    const type = response.headers.get("content-type") || "";
    if (!response.ok || !type.includes("text/event-stream")) {
      let data;
      try { data = await response.json(); } catch { data = { error: "model_call_failed" }; }
      throw data.error ? data : { error: "model_call_failed" };
    }
    let failed = null;
    let finished = null;
    await readSse(response, (name, data) => {
      if (name === "phase") {
        pending.phase(data.phase);
        $("progress-status").textContent = { eligibility: "Checking eligible models", analysis: "Choosing a model", writing: "Generating an answer", verification: "Checking the answer" }[data.phase] || "Working";
        if (live && data.phase === "verification") {
          live.article.querySelector(".live-status")?.remove();
          live.article.append(el("p", "live-status", "Checking the answer with another model…"));
        }
      } else if (name === "model") {
        pending.stop();
        pending.remove();
        live = live || openLive();
        reveal = reveal || createReveal(live);
        paintLiveModel(live, data);
        document.title = "Writing… · Router";
        emit("writing");
        followStream();
      } else if (name === "delta" && reveal) {
        reveal.add(data.text || "");
      } else if (name === "reset" && reveal) {
        reveal.clear();
      } else if (name === "done") {
        pending.stop();
        pending.remove();
        finished = data;
      } else if (name === "error") {
        failed = data;
      }
    });
    if (abort.signal.aborted) throw new DOMException("Stopped", "AbortError");
    if (failed) {
      if (reveal) reveal.cancel();
      throw failed;
    }
    if (!finished) throw { error: "model_call_failed" };
    if (live && reveal) {
      await reveal.drain();
      if (abort.signal.aborted) throw new DOMException("Stopped", "AbortError");
      finishLive(live, finished);
    } else {
      live = { article: addAssistant(finished, true) };
    }
    if (finished.image_attached) {
      hasLastImage = true;
      reuse.hidden = false;
      layout(true);
    }
    emit("done", finished.model_index);
    $("progress-status").textContent = `Answer ready from ${finished.model_name}${finished.verification === "accept" ? ", second opinion accepted" : ""}.`;
    followStream();
  } catch (error) {
    pending.stop();
    pending.remove();
    if (error && error.name === "AbortError") {
      if (reveal) reveal.cancel();
      if (live && live.reading.textContent) settleStopped(live, payload, userArticle);
      emit("idle");
    } else {
      if (reveal) reveal.cancel();
      if (live) live.article.remove();
      if (error && error.error === "locked") {
        showGate();
        emit("idle");
      } else {
        addError(error, payload, userArticle);
        emit("error");
        scrollToEnd();
      }
    }
  } finally {
    if (turnAbort === abort) turnAbort = null;
    setBusy(false);
    document.title = "Router";
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) {
    if (turnAbort) {
      api("/cancel", { client_request_id: turnAbort.clientRequestId }).catch(() => {});
      turnAbort.abort();
    }
    return;
  }
  const text = message.value.trim();
  if (!text) return;
  if (pendingReset && !await commitReset()) return;
  const payload = { message: text, verify: verify.getAttribute("aria-pressed") === "true" };
  let imageAttached = false;
  if (chosenFile) {
    try {
      payload.image_base64 = await fileToBase64(chosenFile);
    } catch {
      toast("That image couldn't be read.");
      return;
    }
    payload.image_type = chosenFile.type;
    imageAttached = true;
  } else if (reuse.getAttribute("aria-pressed") === "true") {
    payload.reuse_image = true;
    imageAttached = true;
  }
  message.value = "";
  clearAttachment();
  autosize();
  dismissHint();
  setMode("chat");
  const userArticle = addUser(text, imageAttached);
  send(payload, userArticle);
});

function fileToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("read_failed"));
    reader.readAsDataURL(blob);
  });
}

/* Panel */

function meter(label, fraction, value) {
  const row = el("div", "meter");
  const track = el("span", "meter-track");
  const fill = el("span", "meter-fill");
  fill.dataset.width = `${Math.max(2, Math.min(100, fraction * 100)).toFixed(1)}%`;
  track.append(fill);
  row.append(el("span", null, label), track, el("span", "meter-value", value));
  return row;
}

function buildPanel(view) {
  const fragment = document.createDocumentFragment();

  const verdict = el("section", "verdict");
  verdict.style.setProperty("--model", modelColor(view.model_index));
  const name = el("div", "verdict-model");
  name.append(el("span", "dot"), document.createTextNode(view.model_name));
  verdict.append(name, el("p", null, sourceSentence(view)));
  const stats = el("dl", "stats");
  const pct = percent(view.confidence);
  for (const [term, value] of [["Task", taskLabel(view.task)], ["Confidence", pct == null ? "—" : `${pct}%`], ["Time", duration(view.elapsed_ms)]]) {
    const stat = el("div", "stat");
    stat.append(el("dt", null, term), el("dd", null, value));
    stats.append(stat);
  }
  verdict.append(stats);
  fragment.append(verdict);

  const candidates = view.candidates || [];
  const rejected = view.rejected || [];
  const attempts = view.attempts || [];

  const steps = el("section", "panel-section");
  steps.append(el("h3", null, "How it was decided"));
  const timeline = el("ol", "timeline");
  const step = (title, text, extra, className) => {
    const item = el("li", className);
    item.append(el("strong", null, title), document.createTextNode(text));
    if (extra && extra.length) {
      const list = el("ul");
      extra.forEach((line) => list.append(el("li", null, line)));
      item.append(list);
    }
    timeline.append(item);
  };
  const catalog = view.catalog_size || candidates.length + rejected.length;
  step(
    "Eligibility",
    `${candidates.length} of ${catalog} models could take this request.`,
    rejected.map((row) => `${row.name} — ${REJECTIONS[row.detail] || row.reason}`),
  );
  step("Analysis", sourceSentence(view));
  if (candidates.length) {
    step("Ranking", `Compared ${taskLabel(view.task).toLowerCase()} quality, expected speed, and estimated cost. ${candidates[0].name} was the first execution choice; a confident router preference can override the numerical score.`);
  }
  const failed = attempts.filter((row) => row.error);
  if (failed.length) {
    step(
      "Recovery",
      "Not every call succeeded the first time.",
      attempts.map((row) => `${row.name} — ${row.error ? FAILURES[row.error] || "call failed" : "answered"}`),
      "warn",
    );
  }
  const checks = [];
  if (view.validation && !["ok", "not_required"].includes(view.validation)) checks.push(`Format check: ${view.validation.replaceAll("_", " ")}`);
  if (view.verification && view.verification !== "not_run") checks.push(`Second opinion: ${view.verification.replaceAll("_", " ")}`);
  if (checks.length) step("Checks", "The answer was reviewed after it was written.", checks);
  step("Answer", `${view.model_name} replied in ${duration(view.elapsed_ms)}.`, null, "final");
  const final = timeline.lastElementChild;
  final.style.setProperty("--model", modelColor(view.model_index));
  steps.append(timeline);
  fragment.append(steps);

  if (candidates.length) {
    const section = el("section", "panel-section");
    section.append(el("h3", null, "Candidates"));
    const list = el("ul", "candidates");
    const fastest = Math.min(...candidates.map((row) => row.latency_ms || Infinity));
    for (const row of candidates) {
      const item = el("li", row.selected ? "candidate selected" : "candidate");
      item.style.setProperty("--model", modelColor(row.index));
      const head = el("div", "candidate-head");
      head.append(el("span", "dot"), el("span", null, row.name));
      if (row.selected) head.append(el("span", "badge", "Selected"));
      head.append(el("span", "score", `score ${Number(row.score).toFixed(2)}`));
      item.append(
        head,
        meter(row.quality_source === "measured" ? "Measured" : "Estimate", row.quality, `${Math.round(row.quality * 100)}%`),
        meter("Speed", fastest / (row.latency_ms || fastest), duration(row.latency_ms)),
      );
      list.append(item);
    }
    section.append(list);
    fragment.append(section);
  }

  fragment.append(el("p", "footnote", "Estimate is catalog quality; Measured is the observed pass rate after at least 20 ratings. Route confidence describes model choice, not answer correctness. Scores combine quality, cost, and speed. Budget reservations use configured prices and conservative token estimates; they are not an invoice."));
  return fragment;
}

function setInert(on) {
  for (const node of [bar, document.querySelector(".main"), form, toBottom]) node.inert = on;
}

function openPanel(view, trigger) {
  openDrawer(buildPanel(view), "Why this model", trigger);
}

let panelCloseTimer;
function openDrawer(content, title, trigger) {
  clearTimeout(panelCloseTimer);
  panelTrigger = trigger;
  $("panel-title").textContent = title;
  panelBody.replaceChildren(content);
  panelBody.scrollTop = 0;
  scrim.hidden = false;
  panel.hidden = false;
  setInert(true);
  requestAnimationFrame(() => {
    scrim.classList.add("open");
    panel.classList.add("open");
    requestAnimationFrame(() => {
      panelBody.querySelectorAll(".meter-fill").forEach((fill) => { fill.style.width = fill.dataset.width; });
    });
  });
  $("panel-close").focus();
}

function closePanel() {
  if (panel.hidden) return;
  scrim.classList.remove("open");
  panel.classList.remove("open");
  setInert(false);
  panelCloseTimer = window.setTimeout(() => {
    scrim.hidden = true;
    panel.hidden = true;
  }, reduced ? 0 : 250);
  if (panelTrigger) panelTrigger.focus();
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !panel.hidden) {
    event.preventDefault();
    closePanel();
  }
});

panel.addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const focusable = [...panel.querySelectorAll("button, [href], [tabindex]:not([tabindex='-1'])")].filter((node) => !node.disabled);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

$("panel-close").addEventListener("click", closePanel);
scrim.addEventListener("click", closePanel);

$("connections").addEventListener("click", async () => {
  try {
    const response = await fetch("/state");
    if (!response.ok) throw new Error("state");
    const state = await response.json();
    const content = el("div", "connections-content");
    content.append(el("h3", null, "Bring your own models"), el("p", "connection-copy", "Connect OpenAI, Anthropic, Groq, OpenRouter, or your own compatible endpoint. Keys stay on this server."));
    for (const provider of state.providers || []) {
      const row = el("section", "connection-row");
      row.append(el("strong", null, provider.name), el("span", provider.configured ? "connection-ready" : "connection-missing", provider.configured ? "Configured" : provider.enabled ? "Key needed" : "Disabled"));
      const models = (state.models || []).filter((model) => model.provider === provider.name);
      row.append(el("p", "connection-copy", models.length ? models.map((m) => `${m.name} (${m.modalities.join(", ")})`).join(" · ") : "No enabled models in the catalog"));
      content.append(row);
    }
    content.append(el("p", "connection-copy", "To connect: set the provider key in .env, then add its model to catalog.json. Custom endpoints go in providers.json. See INTEGRATIONS.md for examples and API access. Configured means credentials are present; it does not confirm account access or model health."));
    if (state.catalog_error) content.append(el("p", "connection-missing", "The catalog has an error. The previous valid models are still in use."));
    openDrawer(content, "Models & connections", $("connections"));
  } catch { toast("Couldn't load connections. Check that Router is running."); }
});

/* Toasts and reset */

function toast(text, actionLabel, onAction, ttl = 3200) {
  const node = el("div", "toast");
  node.append(el("span", null, text));
  const dismiss = () => {
    if (!node.isConnected) return;
    node.classList.add("leaving");
    window.setTimeout(() => node.remove(), 250);
  };
  if (actionLabel) {
    const button = el("button", null, actionLabel);
    button.type = "button";
    button.addEventListener("click", () => {
      dismiss();
      onAction();
    });
    node.append(button);
  }
  toasts.append(node);
  window.setTimeout(dismiss, ttl);
  return dismiss;
}

async function commitReset() {
  if (!pendingReset) return true;
  const current = pendingReset;
  pendingReset = null;
  window.clearTimeout(current.timer);
  current.dismiss();
  setBusy(true);
  try {
    await api("/reset", {});
    verify.setAttribute("aria-pressed", "false");
    return true;
  } catch {
    thread.append(...current.nodes);
    hasLastImage = current.hadImage;
    reuse.hidden = !hasLastImage;
    setMode("chat", true);
    toast("Couldn't clear the thread. The previous conversation was restored.");
    return false;
  } finally { setBusy(false); }
}

reset.addEventListener("click", () => {
  if (busy || !thread.children.length) return;
  const nodes = [...thread.children];
  const hadImage = hasLastImage;
  nodes.forEach((node) => node.remove());
  hasLastImage = false;
  reuse.hidden = true;
  clearAttachment();
  closePanel();
  setMode("hero");
  emit("idle");
  const undo = () => {
    if (!pendingReset) return;
    window.clearTimeout(pendingReset.timer);
    pendingReset = null;
    thread.append(...nodes);
    hasLastImage = hadImage;
    reuse.hidden = !hadImage;
    setMode("chat", true);
    scrollToEnd(false);
  };
  const dismiss = toast("Thread cleared", "Undo", undo, 5000);
  pendingReset = { dismiss, nodes, hadImage, timer: window.setTimeout(commitReset, 5000) };
  message.focus({ preventScroll: true });
});

window.addEventListener("pagehide", () => {
  if (!pendingReset) return;
  fetch("/reset", {
    method: "POST",
    keepalive: true,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: "{}",
  });
});

/* Inputs */

message.addEventListener("keydown", (event) => {
  if (busy) return;
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

message.addEventListener("input", autosize);

message.addEventListener("paste", (event) => {
  const item = [...(event.clipboardData?.items || [])].find((entry) => entry.kind === "file" && IMAGE_TYPES.includes(entry.type));
  if (!item) return;
  event.preventDefault();
  chooseFile(item.getAsFile());
});

file.addEventListener("change", () => chooseFile(file.files[0]));

$("attachment-remove").addEventListener("click", () => {
  clearAttachment();
  message.focus();
});

verify.addEventListener("click", () => {
  verify.setAttribute("aria-pressed", String(verify.getAttribute("aria-pressed") !== "true"));
});

reuse.addEventListener("click", () => {
  if (!hasLastImage) return;
  const next = reuse.getAttribute("aria-pressed") !== "true";
  clearAttachment();
  if (next) {
    reuse.setAttribute("aria-pressed", "true");
    showAttachment("Last image", "");
  }
});

document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => {
    message.value = button.dataset.prompt;
    autosize();
    message.focus();
    message.setSelectionRange(message.value.length, message.value.length);
  });
});

for (const type of ["dragenter", "dragover"]) {
  window.addEventListener(type, (event) => {
    if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
    event.preventDefault();
    form.classList.add("dragging");
  });
}
window.addEventListener("dragleave", (event) => {
  if (event.relatedTarget == null) form.classList.remove("dragging");
});
window.addEventListener("drop", (event) => {
  event.preventDefault();
  form.classList.remove("dragging");
  chooseFile(event.dataTransfer?.files?.[0]);
});

toBottom.addEventListener("click", () => scrollToEnd());
window.addEventListener("scroll", onScroll, { passive: true });
window.addEventListener("resize", () => layout(true));
window.visualViewport?.addEventListener("resize", () => {
  syncKeyboard();
  layout(true);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement !== message && panel.hidden && gate.hidden && !document.querySelector("dialog[open]")) {
    event.preventDefault();
    message.focus();
  }
});

/* Start */

try {
  if (localStorage.getItem("router.hint")) hint.classList.add("gone");
} catch { /* storage unavailable */ }

layout(true);
requestAnimationFrame(() => orb.classList.add("ready"));
document.fonts?.ready.then(() => layout(true));

const gate = document.getElementById("gate");
const gateForm = document.getElementById("gate-form");
const gateError = document.getElementById("gate-error");
const gatePassword = document.getElementById("gate-password");

function showGate() {
  gate.hidden = false;
  setInert(true);
  gatePassword.focus();
}

function hideGate() {
  gate.hidden = true;
  setInert(false);
  gateError.hidden = true;
  gatePassword.value = "";
}

gateForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!gatePassword.value.trim()) {
    gateError.textContent = "Enter your passphrase to continue.";
    gateError.hidden = false;
    gatePassword.focus();
    return;
  }
  gateError.textContent = "That passphrase doesn't match.";
  gateError.hidden = true;
  try {
    await api("/unlock", { password: gatePassword.value });
  } catch (error) {
    if (error && error.error === "unlock_rate_limited") {
      gateError.textContent = "Too many attempts. Wait five minutes and try again.";
      gateError.hidden = false;
      return;
    }
    if (error && error.error === "locked") {
      gateError.hidden = false;
      gatePassword.select();
      return;
    }
    gateError.hidden = false;
    return;
  }
  hideGate();
  const restored = await fetch("/state").then((response) => response.json()).catch(() => null);
  if (restored && !thread.children.length) restoreState(restored);
  message.focus({ preventScroll: true });
});

function restoreState(state) {
  if (state.external_login) {
    showGate();
    gateForm.replaceChildren(el("h2", null, "Your models. Your workspace."), el("p", "gate-copy", "Sign in to your account to continue."));
    const signIn = el("a", "gate-continue", "Sign in securely");
    signIn.href = "/login";
    gateForm.append(signIn);
    signIn.focus();
    return;
  }
  if (state.catalog_error) toast("catalog.json could not be read. The previous models are still in use.");
  if (state.locked) showGate();
  if (!state.turns || !state.turns.length) {
    if (!state.locked) message.focus({ preventScroll: true });
    return;
  }
    hasLastImage = Boolean(state.has_image);
    reuse.hidden = !hasLastImage;
    if (state.verify) verify.setAttribute("aria-pressed", "true");
    let lastModel = -1;
    for (const turn of state.turns) {
      if (turn.role === "user") addUser(turn.text, turn.image_attached);
      else {
        addAssistant(turn);
        lastModel = turn.model_index;
      }
    }
    setMode("chat", true);
    emit("done", lastModel);
    scrollToEnd(false);
}

fetch("/state")
  .then((response) => response.json())
  .then((state) => restoreState(state))
  .catch(() => message.focus({ preventScroll: true }));
