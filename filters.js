/* filters.js — shared faceted filter bar for Find jobs + Dashboard.
   Only exposes filters backed by real data in the jobs index; degree level /
   max experience / industry are intentionally absent (see note in the app),
   because ATS feeds don't provide them structured and faking them would return
   wrong results. Self-contained: injects its own CSS, no dependencies. */
window.TailorFilters = (function () {
  const DATE = [["", "Any time"], ["360", "Last 6 hours"], ["1440", "Last 24 hours"],
    ["10080", "Last 7 days"], ["20160", "Last 14 days"], ["43200", "Last 30 days"]];
  const WORKPLACE = ["Remote", "On-site"];
  const EMPTYPE = [["FullTime", "Full-time"], ["PartTime", "Part-time"], ["Contract", "Contract"], ["Intern", "Internship"]];
  const JOBTYPE = [["intern", "Internship"], ["entry", "Entry level"], ["mid", "Mid level"], ["experienced", "Experienced"]];
  const SPON = [["", "Any sponsorship"], ["yes", "Sponsors visa"], ["hide", "Hide “no sponsorship”"]];
  const APPLYABLE = [["", "All jobs"], ["auto", "Auto-applyable only"], ["applyable", "Auto + assisted"]];

  const S = { mins: "", countries: [], workplace: [], companies: [], etypes: [], jobtypes: [], sponsor: "", only: "" };
  let facets = { countries: [], companies: [] }, onChange = function () { }, host = null;

  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function buildParams(base) {
    const p = new URLSearchParams(base || "");
    if (S.mins) p.set("mins", S.mins);
    if (S.countries.length) p.set("country", S.countries.join("|"));
    if (S.companies.length) p.set("company", S.companies.join("|"));
    if (S.etypes.length) p.set("etype", S.etypes.join("|"));
    if (S.jobtypes.length) p.set("jobtype", S.jobtypes.join(","));
    if (S.workplace.length) p.set("workplace", S.workplace.join(",").toLowerCase());
    if (S.sponsor) p.set("sponsor", S.sponsor);
    if (S.only) p.set("only", S.only);
    return p;
  }
  function setFacets(f) { facets = Object.assign(facets, f || {}); render(); }
  function clearAll() { S.mins = ""; S.countries = []; S.workplace = []; S.companies = []; S.etypes = []; S.jobtypes = []; S.sponsor = ""; S.only = ""; render(); onChange(); }
  function active() { return (S.mins ? 1 : 0) + S.countries.length + S.workplace.length + S.companies.length + S.etypes.length + S.jobtypes.length + (S.sponsor ? 1 : 0) + (S.only ? 1 : 0); }

  function closeAll() { host && host.querySelectorAll(".tf-drop.open").forEach(d => d.classList.remove("open")); }
  document.addEventListener("click", e => { if (host && !e.target.closest(".tf-chip-wrap")) closeAll(); });

  function toggle(arr, v) { const i = arr.indexOf(v); if (i < 0) arr.push(v); else arr.splice(i, 1); }

  // a multiselect dropdown: label + count chip → panel of checkboxes (+ optional search)
  function multiChip(key, label, options, searchable) {
    const sel = S[key], n = sel.length;
    const rows = (q) => options
      .filter(o => !q || (o[1] || o[0]).toLowerCase().includes(q))
      .map(o => `<label class="tf-opt"><input type="checkbox" data-k="${key}" value="${esc(o[0])}"${sel.includes(o[0]) ? " checked" : ""}> ${esc(o[1] || o[0])}</label>`).join("") || `<div class="tf-empty">No options</div>`;
    return `<div class="tf-chip-wrap"><button class="tf-chip${n ? " on" : ""}" data-drop>${esc(label)}${n ? ` <b>${n}</b>` : ""} <span class="tf-car">▾</span></button>
      <div class="tf-drop">${searchable ? `<input class="tf-search" placeholder="Search ${esc(label.toLowerCase())}…" data-search="${key}">` : ""}<div class="tf-opts" data-opts="${key}">${rows("")}</div></div></div>`;
  }
  function radioChip(key, label, options) {
    const cur = S[key];
    const rows = options.map(o => `<label class="tf-opt"><input type="radio" name="tf-${key}" data-rk="${key}" value="${esc(o[0])}"${cur === o[0] ? " checked" : ""}> ${esc(o[1])}</label>`).join("");
    const lbl = cur ? (options.find(o => o[0] === cur) || [, label])[1] : label;
    return `<div class="tf-chip-wrap"><button class="tf-chip${cur ? " on" : ""}" data-drop>${esc(lbl)} <span class="tf-car">▾</span></button><div class="tf-drop">${rows}</div></div>`;
  }

  function render() {
    if (!host) return;
    const countryOpts = (facets.countries || []).map(c => [c, c]);
    const companyOpts = (facets.companies || []).map(c => [c, c]);
    host.innerHTML =
      radioChip("mins", "Date", DATE) +
      multiChip("countries", "Country", countryOpts, true) +
      multiChip("workplace", "Workplace", WORKPLACE.map(w => [w, w]), false) +
      multiChip("companies", "Companies", companyOpts, true) +
      multiChip("jobtypes", "Job type", JOBTYPE, false) +
      multiChip("etypes", "Employment", EMPTYPE, false) +
      radioChip("sponsor", "Sponsorship", SPON) +
      radioChip("only", "Auto-apply", APPLYABLE) +
      (active() ? `<button class="tf-clear" data-clear>Clear all (${active()})</button>` : "");
    wire();
  }

  function wire() {
    host.querySelectorAll("[data-drop]").forEach(b => b.onclick = e => {
      e.stopPropagation(); const d = b.nextElementSibling, was = d.classList.contains("open"); closeAll(); if (!was) d.classList.add("open");
    });
    host.querySelectorAll("input[data-k]").forEach(cb2 => cb2.onchange = () => { toggle(S[cb2.dataset.k], cb2.value); syncCount(cb2.dataset.k); onChange(); });
    host.querySelectorAll("input[data-rk]").forEach(r => r.onchange = () => { S[r.dataset.rk] = r.value; render(); onChange(); });
    host.querySelectorAll("input[data-search]").forEach(inp => inp.oninput = () => {
      const key = inp.dataset.search, box = host.querySelector(`[data-opts="${key}"]`), q = inp.value.toLowerCase();
      const opts = key === "countries" ? (facets.countries || []) : (facets.companies || []);
      box.innerHTML = opts.filter(o => o.toLowerCase().includes(q)).map(o => `<label class="tf-opt"><input type="checkbox" data-k="${key}" value="${esc(o)}"${S[key].includes(o) ? " checked" : ""}> ${esc(o)}</label>`).join("") || `<div class="tf-empty">No matches</div>`;
      box.querySelectorAll("input[data-k]").forEach(c => c.onchange = () => { toggle(S[key], c.value); syncCount(key); onChange(); });
    });
    const cl = host.querySelector("[data-clear]"); if (cl) cl.onclick = clearAll;
  }
  // update just the chip's count badge without closing the open panel
  function syncCount(key) {
    const wrap = [...host.querySelectorAll(".tf-chip-wrap")].find(w => w.querySelector(`[data-opts="${key}"], [data-k="${key}"]`) || (w.innerHTML.includes(`data-opts="${key}"`)));
    render(); // simplest: re-render (panel state lost but counts correct)
  }

  function injectCSS() {
    if (document.getElementById("tf-css")) return;
    const st = document.createElement("style"); st.id = "tf-css";
    st.textContent = `
    .tf-bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    .tf-chip-wrap{position:relative}
    .tf-chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line,#e3e3ea);background:var(--card,#fff);color:var(--ink,#1a2420);font:600 12.5px "Inter",system-ui,sans-serif;padding:7px 12px;border-radius:100px;cursor:pointer;white-space:nowrap}
    .tf-chip:hover{border-color:var(--ink-soft,#8a8a99)}
    .tf-chip.on{background:var(--indigo,#183527);border-color:var(--indigo,#183527);color:#fff}
    .tf-chip b{background:rgba(255,255,255,.25);border-radius:100px;padding:0 6px;font-size:11px}
    .tf-chip .tf-car{opacity:.6;font-size:9px}
    .tf-clear{border:none;background:transparent;color:var(--fair,#a1554a);font:600 12px "Inter";cursor:pointer;padding:6px 4px;text-decoration:underline}
    .tf-drop{display:none;position:absolute;top:calc(100% + 6px);left:0;z-index:60;min-width:220px;max-width:280px;background:var(--card,#fff);border:1px solid var(--line,#e3e3ea);border-radius:12px;box-shadow:0 14px 40px rgba(20,28,24,.16);padding:8px}
    .tf-drop.open{display:block}
    .tf-search{width:100%;box-sizing:border-box;border:1px solid var(--line,#e3e3ea);border-radius:8px;padding:7px 9px;font-size:12.5px;margin-bottom:6px;background:var(--card-2,#f7f7f2)}
    .tf-opts{max-height:230px;overflow:auto}
    .tf-opt{display:flex;align-items:center;gap:8px;padding:7px 8px;border-radius:8px;font-size:13px;color:var(--ink,#1a2420);cursor:pointer}
    .tf-opt:hover{background:var(--card-2,#f4f4ef)}
    .tf-opt input{accent-color:var(--indigo,#183527);margin:0}
    .tf-empty{padding:10px;color:var(--ink-faint,#9a9aa6);font-size:12.5px}`;
    document.head.appendChild(st);
  }

  return {
    mount(container, onChangeCb) {
      injectCSS();
      host = typeof container === "string" ? document.getElementById(container) : container;
      if (host) host.classList.add("tf-bar");
      onChange = onChangeCb || function () { };
      render();
    },
    buildParams, setFacets, clearAll, state: S
  };
})();
