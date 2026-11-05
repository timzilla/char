/* Char web app: submit a module form, watch the job, preview a slip, reprint. */
const Char = (() => {
  const $ = (s, r = document) => r.querySelector(s);
  let toastT = 0;
  const toast = (msg, bad) => { const t = $("#toast"); t.textContent = msg; t.className = bad ? "bad" : ""; t.hidden = false; clearTimeout(toastT); toastT = setTimeout(() => (t.hidden = true), bad ? 6000 : 3500); };
  const params = (form) => { const o = {}; for (const el of form.elements) { if (!el.name) continue; o[el.name] = el.type === "checkbox" ? (el.checked ? "on" : "") : el.value; } return o; };

  async function watch(id) {
    toast(`Slip ${id} queued…`);
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 1500));
      const j = await fetch(`/api/jobs/${id}`).then((r) => r.json()).catch(() => null);
      if (!j) return;
      if (j.status === "printed") { toast(`Printed.`); refreshJobs(); return; }
      if (j.status === "failed") { toast(`Failed: ${j.error}`, true); refreshJobs(); return; }
      if (j.status === "waiting") toast(`Waiting: ${j.error}. It will keep trying for half an hour.`, true);
    }
  }
  async function refreshJobs() {
    const list = $(".jobs"); if (!list) return;
    const html = await fetch(location.pathname === "/jobs" ? "/jobs" : "/").then((r) => r.text()).catch(() => "");
    const doc = new DOMParser().parseFromString(html, "text/html"); const fresh = doc.querySelector(".jobs"); if (fresh) list.replaceWith(fresh);
    const p = doc.querySelector("#printer"); if (p) $("#printer").replaceWith(p);
  }
  async function submit(form) {
    const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
    try {
      const body = new FormData(form);
      const r = await fetch(`/m/${form.dataset.module}/print`, { method: "POST", body });
      const j = await r.json();
      if (j.error) toast(j.error, true); else { if (form.id === "quick") form.reset(); watch(j.job); }
    } catch (e) { toast("Could not reach Char.", true); }
    btn.disabled = false;
  }
  async function preview(form) {
    const pre = $("#preview"); if (!pre) return;
    const r = await fetch("/api/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ module: form.dataset.module, params: params(form) }) });
    const j = await r.json();
    if (j.error) { pre.textContent = "⚠ " + j.error; return; }
    pre.replaceChildren(); const imgs = j.images || [];   // "[image]" lines in the text become the pictures themselves
    (j.text || "(blank)").split("[image]").forEach((part, i) => {
      if (i > 0) { const im = imgs[i - 1]; if (im && im.b64) { const el = document.createElement("img"); el.src = "data:image/png;base64," + im.b64; el.alt = "";
        el.style.width = Math.min(100, (im.width / 576) * 100) + "%"; el.className = "slipimg"; pre.append(el); } else pre.append("[image]"); }
      pre.append(part.replace(/^\n/, ""));
    });
  }

  document.addEventListener("submit", (e) => { const f = e.target.closest("form[data-module]"); if (!f) return; e.preventDefault(); submit(f); });
  document.addEventListener("click", (e) => {
    const re = e.target.closest("[data-reprint]"); if (re) { fetch(`/api/jobs/${re.dataset.reprint}/reprint`, { method: "POST" }).then((r) => r.json()).then((j) => j.job && watch(j.job)); }
    if (e.target.id === "previewbtn") preview($("#mform"));
  });
  let pt = 0; document.addEventListener("input", (e) => { const f = e.target.closest("#mform"); if (!f) return; clearTimeout(pt); pt = setTimeout(() => preview(f), 500); });
  document.addEventListener("DOMContentLoaded", () => { const f = $("#mform"); if (f) preview(f); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  return { watch, toast };
})();
