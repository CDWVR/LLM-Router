/* Loaded independently; screenshot renderer is fetched only when reporting. */
(() => {
  const byId = (id) => document.getElementById(id);
  const dialog = byId("report-dialog"), trigger = byId("report-bug");
  const description = byId("report-description"), error = byId("report-error");
  const preview = byId("report-image"), status = byId("report-capture-status");
  const submit = byId("report-submit"), remove = byId("report-remove");
  const redact = byId("report-redact"), canvas = document.createElement("canvas");
  canvas.className="report-redaction";canvas.hidden=true;canvas.setAttribute("aria-label","Drag across the screenshot to cover sensitive areas");
  byId("report-preview").append(canvas);
  let origin=null;
  const point=(event)=>{const r=canvas.getBoundingClientRect();return [(event.clientX-r.left)*canvas.width/r.width,(event.clientY-r.top)*canvas.height/r.height];};
  canvas.addEventListener("pointerdown",event=>{origin=point(event);canvas.setPointerCapture(event.pointerId);event.preventDefault();});
  canvas.addEventListener("pointerup",event=>{
    if(!origin)return;const end=point(event),ctx=canvas.getContext("2d");ctx.fillStyle="#111111";ctx.fillRect(Math.min(origin[0],end[0]),Math.min(origin[1],end[1]),Math.max(8,Math.abs(end[0]-origin[0])),Math.max(8,Math.abs(end[1]-origin[1])));origin=null;
    const url=canvas.toDataURL("image/png");screenshot=url.split(",")[1];preview.src=url;
  });
  redact.addEventListener("click",()=>{
    if(!screenshot)return;
    const opening=canvas.hidden;
    if(opening){canvas.width=preview.naturalWidth;canvas.height=preview.naturalHeight;canvas.getContext("2d").drawImage(preview,0,0);}
    canvas.hidden=!opening;preview.hidden=opening;redact.setAttribute("aria-pressed",String(opening));redact.textContent=opening?"Done redacting":"Redact areas";
  });
  let screenshot = null, loading, generation = 0, saving = false;
  let previousOverflow = "";

  function renderer() {
    if (!loading) loading = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/vendor/html2canvas.min.js";
      const failed = () => { clearTimeout(timeout); loading = null; script.remove(); reject(new Error("capture_unavailable")); };
      const timeout = setTimeout(failed, 10000);
      script.onload = () => { clearTimeout(timeout); resolve(window.html2canvas); };
      script.onerror = failed;
      document.head.append(script);
    });
    return loading;
  }

  trigger.addEventListener("click", async () => {
    const version = ++generation;
    trigger.disabled = true;
    screenshot = null;
    canvas.hidden=true;redact.disabled=true;redact.textContent="Redact areas";redact.setAttribute("aria-pressed","false");
    preview.hidden = true;
    preview.removeAttribute("src");
    status.hidden = false;
    status.textContent = "Capturing this view…";
    remove.disabled = true;
    error.hidden = true;
    byId("report-result").hidden = true;
    submit.disabled = true;
    submit.textContent = "Save report ↗";
    description.disabled = false;
    const flash = byId("capture-flash");
    flash.classList.remove("capture-active");
    requestAnimationFrame(() => flash.classList.add("capture-active"));
    previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.showModal();
    description.focus();
    try {
      const capture = await renderer();
      const canvas = await capture(document.body, {
        backgroundColor: "#09090b", logging: false, scale: Math.min(1, 1600 / innerWidth, 1200 / innerHeight),
        width: innerWidth, height: innerHeight, x: scrollX, y: scrollY,
        windowWidth: innerWidth, windowHeight: innerHeight,
        ignoreElements: (node) => node.tagName === "CANVAS" || node.id === "gate" || node.id === "toasts",
        onclone: (doc) => {
          doc.querySelectorAll("input, textarea").forEach((node) => { node.value = ""; node.setAttribute("value", ""); node.textContent = ""; });
          // DOM capture cannot reproduce clipped gradient text reliably.
          doc.querySelectorAll(".hero h1 span").forEach((node) => { node.style.background = "none"; node.style.color = "#b5baff"; node.style.webkitTextFillColor = "#b5baff"; });
        },
      });
      if (version !== generation || !dialog.open) return;
      const url = canvas.toDataURL("image/png");
      if (url.length > 2_600_000) throw new Error("capture_too_large");
      screenshot = url.split(",")[1];
      preview.src = url;
      preview.hidden = false;
      status.hidden = true;
      remove.disabled = false;
      redact.disabled = false;
    } catch {
      if (version === generation) status.textContent = "Preview unavailable. You can still describe and save the issue.";
    } finally {
      if (version === generation) submit.disabled = false;
      trigger.disabled = false;
    }
  });

  remove.addEventListener("click", () => {
    canvas.hidden=true;redact.disabled=true;
    screenshot = null;
    preview.hidden = true;
    preview.removeAttribute("src");
    status.hidden = false;
    status.textContent = "Screenshot removed. Only your description will be saved.";
    remove.disabled = true;
  });
  description.addEventListener("input", () => {
    byId("report-count").textContent = `${description.value.length} / 4000`;
    error.hidden = true;
    description.removeAttribute("aria-invalid");
  });
  byId("report-close").addEventListener("click", () => { if (!saving) dialog.close(); });
  dialog.addEventListener("cancel", (event) => { if (saving) event.preventDefault(); });
  dialog.addEventListener("close", () => {
    ++generation;
    screenshot = null;
    preview.removeAttribute("src");
    document.body.style.overflow = previousOverflow;
    trigger.focus();
  });
  byId("report-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving || submit.disabled) return;
    if (description.value.trim().length < 10) {
      error.textContent = "Add a little more detail (at least 10 characters).";
      error.hidden = false;
      description.setAttribute("aria-invalid", "true");
      description.focus();
      return;
    }
    saving = true;
    canvas.hidden=true;preview.hidden=!screenshot;redact.disabled=true;
    submit.disabled = true;
    remove.disabled = true;
    description.disabled = true;
    byId("report-close").disabled = true;
    submit.textContent = "Saving…";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch("/reports", {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content },
        body: JSON.stringify({ description: description.value.trim(), screenshot }), signal: controller.signal,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "report_unavailable");
      byId("report-result").textContent = `Report saved. Reference: ${result.id}`;
      byId("report-result").hidden = false;
      submit.textContent = "Saved ✓";
      description.value = "";
      byId("report-count").textContent = "0 / 4000";
    } catch (failure) {
      const messages = { report_rate_limited: "Five reports per hour are allowed. Please try again later.", forbidden: "Your session expired. Reload the page before reporting.", locked: "Unlock Router before saving a report.", invalid_screenshot: "The screenshot could not be saved. Remove it and try again." };
      error.textContent = messages[failure.message] || "Could not confirm the report was saved. Your draft is still here; please try again.";
      error.hidden = false;
      submit.disabled = false;
      submit.textContent = "Try again";
      description.disabled = false;
      remove.disabled = !screenshot;
      redact.disabled = !screenshot;
    } finally {
      clearTimeout(timeout);
      saving = false;
      byId("report-close").disabled = false;
    }
  });
})();
