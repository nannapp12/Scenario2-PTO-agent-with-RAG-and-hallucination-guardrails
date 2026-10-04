// Set window.API_BASE before this script if the API is hosted on another origin.
const API_BASE = window.API_BASE || "";
const $ = (id) => document.getElementById(id);
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

function setStatus(msg, isError = false, link = null) {
  const el = $("status");
  el.replaceChildren(document.createTextNode(msg));
  if (link) {
    const a = document.createElement("a");
    a.href = link.href; a.textContent = link.text;
    el.append(" ", a);
  }
  el.classList.toggle("error", isError);
}

async function lookup(employeeId) {
  employeeId = employeeId.trim();
  $("result").hidden = true;
  if (!ID_RE.test(employeeId)) {
    setStatus("Employee not found", true);  // same answer the API gives; no guessing
    return;
  }
  $("lookup").disabled = true;
  setStatus("Looking up…");
  try {
    const r = await fetch(`${API_BASE}/pto/${encodeURIComponent(employeeId)}`, {
      credentials: "include",
      headers: { Accept: "application/json" },
    });
    const data = await r.json().catch(() => ({}));
    if (r.status === 401) {
      const back = encodeURIComponent(location.pathname);
      setStatus(data.detail || "Sign in required.", true,
        { href: `/.auth/login/aad?post_login_redirect_uri=${back}`, text: "Sign in" });
      return;
    }
    if (!r.ok) throw new Error(data.detail || "Request failed.");
    render(data);
    setStatus("");
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    $("lookup").disabled = false;
  }
}

const num = (v) => Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const TYPES = { FULL_TIME: "Full-time", PART_TIME: "Part-time", TEMPORARY: "Temporary" };

function render(d) {
  $("result").hidden = false;
  $("who-name").textContent = d.name_masked || d.employee_id;
  $("who-meta").textContent = [d.employee_id, d.email_masked, TYPES[d.employment_type] || d.employment_type]
    .filter(Boolean).join(" · ");
  $("available-days").textContent = num(d.available_days);
  $("available-hours").textContent = `${num(d.available_hours)} hours`;
  $("explanation").textContent = d.explanation;

  const warnings = $("warnings");
  warnings.replaceChildren(...d.warnings.map((w) => { const li = document.createElement("li"); li.textContent = w; return li; }));

  const rows = [
    ["Start date", d.start_date],
    ["As of", d.as_of],
    ["Length of service", `${d.tenure.years} yr ${d.tenure.months} mo`],
    ["Eligible for vacation", d.eligible ? "Yes" : "No (regular full-time only)"],
    ["Biweekly periods completed", d.periods_completed],
    ["Current accrual", d.eligible ? `${d.hours_per_period} h biweekly, up to ${num(d.annual_allotment_days)} days/yr` : "—"],
    ["Accrued", `${num(d.accrued_days)} days (${num(d.accrued_hours)} h)`],
    ["Balance cap", d.cap_days == null ? "—" : `${num(d.cap_days)} days${d.at_cap ? " (reached)" : ""}`],
    ["Waiting period", d.in_waiting_period ? `Until ${d.waiting_period_ends}` : "Completed"],
    ["Policy version", d.policy_version],
  ];
  $("facts").replaceChildren(...rows.map(([k, v]) => {
    const tr = document.createElement("tr");
    const th = document.createElement("th"); th.textContent = k;
    const td = document.createElement("td"); td.textContent = v;
    tr.append(th, td);
    return tr;
  }));

  const cites = $("citations");
  cites.replaceChildren();
  d.citations.forEach((c) => {
    const h = document.createElement("strong"); h.textContent = c.section;
    const pre = document.createElement("pre"); pre.textContent = c.excerpt;
    cites.append(h, pre);
  });
  $("citations-details").hidden = !d.citations.length;
}

$("pto-form").addEventListener("submit", (e) => { e.preventDefault(); lookup($("employee-id").value); });
