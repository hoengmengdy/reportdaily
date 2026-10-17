"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { user: null, csrf: "", today: "", view: "daily", pages: [], report: null, dirty: false, filter: "all", busy: false, loading: false, setup: false, setupToken: "", viewerUsername: "" };
const number = (n) => new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(n);
const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const friendlyDate = (value) => new Date(`${value}T12:00:00+07:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Bangkok" });
let toastTimer;

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").classList.toggle("error", error);
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, error ? 7000 : 4000);
}

function showError(selector, message) {
  const el = $(selector);
  el.textContent = message || "";
  el.hidden = !message;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf, ...(options.headers || {}) },
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new Error(result.error || "The request could not be completed.");
  }
  return result;
}

function showLogin() {
  $("#app").hidden = true;
  $("#login-screen").hidden = false;
  $("#loading-screen").hidden = true;
  $$("dialog[open]").forEach((dialog) => dialog.close());
  state.user = null;
  state.csrf = "";
}

function setSetupMode(setup, token = "") {
  state.setup = setup;
  state.setupToken = token;
  $("#login-title").textContent = setup ? "Set up your workspace" : "Welcome back";
  $("#login-description").textContent = setup ? "Create your administrator account to get started." : "Sign in to your reporting workspace.";
  $("#login-submit").innerHTML = `${setup ? "Create account & open workspace" : "Sign in"} <span aria-hidden="true">→</span>`;
  $("#login-note").textContent = setup ? "Use at least 12 characters for your password. Your account is stored on this computer." : "Use the account created by your administrator.";
  $("#confirm-password-field").hidden = !setup;
  $("#confirm-password").required = setup;
  $("#password").minLength = setup ? 12 : 0;
  $("#password").autocomplete = setup ? "new-password" : "current-password";
  $("#viewer-login").hidden = setup || !state.viewerUsername;
}

async function openWorkspace(session) {
  Object.assign(state, { user: session.user, csrf: session.csrf, today: session.today, view: "daily", report: null, dirty: false, busy: false });
  $("#user-name").textContent = state.user.username;
  $("#user-role").textContent = state.user.role;
  $("#user-avatar").textContent = state.user.username.slice(0, 2).toUpperCase();
  $("#report-date").value = state.today;
  $("#report-date").max = state.today;
  $("#history-month").value = state.today.slice(0, 7);
  $("#history-month").max = state.today.slice(0, 7);
  $("#add-page").hidden = state.user.role !== "admin";
  $$(".admin-column").forEach((el) => { el.hidden = state.user.role !== "admin"; });
  $("#save-report").hidden = state.user.role === "viewer";
  $("#password").value = "";
  $("#login-screen").hidden = true;
  $("#app").hidden = false;
  $("#loading-screen").hidden = true;
  await changeView("daily", true);
}

function canLeave() {
  return !state.dirty || window.confirm("You have unsaved report changes. Discard them and continue?");
}

function validVisibleInputs() {
  const invalid = $$(".actual-input").find((input) => !input.validity.valid);
  if (invalid) {
    invalid.reportValidity();
    toast("Enter a whole number between 0 and 1,000,000 before filtering.", true);
    return false;
  }
  return true;
}

async function loadPages() {
  const result = await api("/api/pages");
  state.pages = result.pages;
  $("#nav-page-count").textContent = number(result.total_pages);
  $("#pages-count").textContent = number(result.total_pages);
  $("#pages-target").textContent = number(result.total_target);
  renderPages();
}

function viewError(error) {
  $("#view-error span").textContent = error.message || "Unable to reach the server. Check that it is running.";
  $("#view-error").hidden = false;
}

async function changeView(view, force = false) {
  if (state.busy || state.loading || (!force && !canLeave())) return;
  state.view = view;
  state.dirty = false;
  $$(".view").forEach((el) => { el.hidden = el.id !== `${view}-view`; });
  $$("[data-view]").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
    el.setAttribute("aria-current", el.dataset.view === view ? "page" : "false");
    el.setAttribute("aria-label", el.textContent.trim().replace(/\d+$/, ""));
  });
  $("#breadcrumb-current").textContent = ({ daily: "Daily overview", pages: "Facebook pages", history: "Report history" })[view];
  $("#view-error").hidden = true;
  state.loading = true;
  // Disable old report actions before a different date or view can load.
  if (view === "daily") {
    state.report = null;
    renderReport();
  }
  try {
    await loadPages();
    if (view === "daily") await loadReport();
    if (view === "history") await loadHistory();
  } catch (error) {
    viewError(error);
  } finally {
    state.loading = false;
    updateSaveState();
  }
}

function pageCell(page, id) {
  const initial = [...page.name.trim()][0] || "f";
  let profileId = "";
  try { profileId = new URL(page.url).searchParams.get("id") || ""; } catch { /* validated by server */ }
  return `<div class="page-cell"><span class="page-avatar tone-${id % 4}" aria-hidden="true">${esc(initial)}</span><div class="page-name">${esc(page.name)}<span class="page-profile">ID · ${esc(profileId)}</span></div></div>`;
}

function profileLink(page, full = false) {
  // Only server-validated Facebook profile URLs are accepted, still escaped here.
  return `<a class="${full ? "full-profile-link" : "profile-link"}" href="${esc(page.url)}" target="_blank" rel="noopener noreferrer" aria-label="Open Facebook profile for ${esc(page.name)}">${full ? esc(page.url) : `View on Facebook ${icon("arrow")}`}</a>`;
}

function statusBadge(row) {
  const reached = row.actual >= row.target;
  return `<span class="status-pill ${reached ? "reached" : "below"}">${reached ? "Reached" : "Below target"}</span>`;
}

function renderPages() {
  const query = $("#pages-search").value.trim().toLocaleLowerCase();
  const pages = state.pages.filter((p) => `${p.name} ${p.url}`.toLocaleLowerCase().includes(query));
  const admin = state.user?.role === "admin";
  $("#pages-body").innerHTML = pages.map((p) => `<tr><td>${pageCell(p, p.id)}</td><td>${profileLink(p, true)}</td><td class="number target-number">${number(p.target)}</td>${admin ? `<td><div class="row-actions"><button class="icon-button edit-page" data-id="${p.id}" aria-label="Edit ${esc(p.name)}" title="Edit page">${icon("edit")}</button><button class="icon-button delete-button" data-id="${p.id}" aria-label="Delete ${esc(p.name)}" title="Delete page">${icon("trash")}</button></div></td>` : ""}</tr>`).join("") || `<tr><td colspan="${admin ? 4 : 3}" class="empty-cell"><strong>No pages found</strong>${query ? "Try a different search." : "Add a Facebook page to start tracking posts."}</td></tr>`;
  $("#pages-visible-count").textContent = `Showing ${number(pages.length)} of ${number(state.pages.length)} pages`;
}

async function loadReport() {
  const report = await api(`/api/report?date=${encodeURIComponent($("#report-date").value)}`);
  state.report = report;
  state.dirty = false;
  renderReport();
}

function renderReport() {
  const report = state.report;
  if (!report) {
    $("#report-body").innerHTML = '<tr><td colspan="5" class="empty-cell">Loading report…</td></tr>';
    ["pages", "target", "actual", "remaining", "completion"].forEach((key) => { $(`#stat-${key}`).textContent = "—"; });
    $("#daily-progress").value = 0;
    $("#report-save-state").textContent = "Loading report…";
    updateSaveState();
    return;
  }
  $("#report-row-count").textContent = report.rows.length;
  $(".progress-copy h2").textContent = report.date === state.today ? "Today's progress" : `Progress for ${friendlyDate(report.date)}`;
  $("#actual-caption").textContent = report.date === state.today ? "Completed today" : `Completed on ${friendlyDate(report.date)}`;
  const query = $("#report-search").value.trim().toLocaleLowerCase();
  const rows = report.rows.filter((r) => `${r.name} ${r.url}`.toLocaleLowerCase().includes(query)
    && (state.filter === "all" || (state.filter === "reached") === (r.actual >= r.target)));
  $("#report-body").innerHTML = rows.map((r) => `<tr data-page="${r.page_id}"><td>${pageCell(r, r.page_id)}</td><td>${profileLink(r)}</td><td class="number target-number">${number(r.target)}</td><td class="number"><input class="actual-input" data-id="${r.page_id}" type="number" min="0" max="1000000" step="1" required value="${r.actual}" aria-label="Actual posts for ${esc(r.name)}" ${state.user?.role === "viewer" ? "disabled" : ""}></td><td class="row-status">${statusBadge(r)}</td></tr>`).join("") || '<tr><td colspan="5" class="empty-cell"><strong>No pages to display</strong>Try another search or status filter, or add a page.</td></tr>';
  $("#report-visible-count").textContent = `Showing ${number(rows.length)} of ${number(report.rows.length)} pages`;
  updateTotals();
  updateSaveState();
}

function updateTotals() {
  if (!state.report) return;
  const rows = state.report.rows;
  const target = rows.reduce((sum, r) => sum + r.target, 0);
  const actual = rows.reduce((sum, r) => sum + r.actual, 0);
  const remaining = rows.reduce((sum, r) => sum + Math.max(r.target - r.actual, 0), 0);
  const reached = rows.filter((r) => r.actual >= r.target).length;
  const completion = target ? Math.round(1000 * (target - remaining) / target) / 10 : 100;
  for (const [key, value] of Object.entries({ pages: rows.length, target, actual, remaining })) $(`#stat-${key}`).textContent = number(value);
  $("#stat-completion").textContent = `${number(completion)}%`;
  $("#daily-progress").value = completion;
  $("#daily-progress").textContent = `${number(completion)}%`;
  $("#progress-caption").textContent = `${number(reached)} of ${number(rows.length)} pages have reached their daily target.`;
  $("#total-target").textContent = number(target);
  $("#total-actual").textContent = number(actual);
  $("#total-reached").textContent = `${number(reached)} / ${number(rows.length)} reached`;
}

function updateSaveState() {
  const report = state.report;
  const invalid = $$(".actual-input").some((input) => !input.validity.valid);
  $("#report-date").disabled = state.busy || state.loading;
  $("#save-report").disabled = !report || state.busy || state.loading || invalid || (report.saved && !state.dirty);
  $("#save-report").innerHTML = `${icon("save")}${state.busy ? "Saving…" : "Save report"}`;
  $$(".export-button").forEach((button) => {
    button.disabled = !report?.saved || state.dirty || state.busy || state.loading;
    button.title = state.dirty || !report?.saved ? "Save the report before exporting" : "Export the saved report";
  });
  $("#report-save-state").classList.toggle("save-dirty", state.dirty);
  if (report) {
    const utcTime = report.updated_at ? new Date(report.updated_at.replace(" ", "T") + "Z").toLocaleString("en-GB", { timeZone: "Asia/Bangkok", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
    $("#report-save-state").textContent = state.dirty ? "Unsaved changes · save to preserve your progress" : report.saved ? `Saved · ${utcTime} (Bangkok)` : "New report · enter actual posts, then save";
  }
}

async function saveReport() {
  if (!state.report || state.busy || state.loading || state.user?.role === "viewer") return;
  for (const input of $$(".actual-input")) if (!input.reportValidity()) return;
  state.busy = true;
  updateSaveState();
  $$(".actual-input").forEach((input) => { input.disabled = true; });
  try {
    state.report = await api("/api/report", {
      method: "PUT",
      body: JSON.stringify({ date: state.report.date, revision: state.report.revision, rows: state.report.rows.map((r) => ({ page_id: r.page_id, actual: r.actual })) }),
    });
    state.dirty = false;
    toast(`Report for ${friendlyDate(state.report.date)} saved.`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.busy = false;
    renderReport();
  }
}

function editPage(page = null) {
  if (state.user?.role !== "admin") return;
  $("#page-dialog-title").textContent = page ? "Edit page" : "Add page";
  $("#page-id").value = page?.id || "";
  $("#page-name").value = page?.name || "";
  $("#page-url").value = page?.url || "";
  $("#page-target").value = page?.target ?? 3;
  showError("#page-error", "");
  $("#page-dialog").showModal();
  $("#page-name").focus();
}

async function loadHistory() {
  const month = $("#history-month").value;
  $("#history-body").innerHTML = '<tr><td colspan="6" class="empty-cell">Loading history…</td></tr>';
  const history = await api(`/api/history?month=${encodeURIComponent(month)}`);
  $("#history-days").textContent = number(history.days);
  $("#history-target").textContent = number(history.totals.target);
  $("#history-actual").textContent = number(history.totals.actual);
  $("#history-completion").textContent = `${number(history.totals.completion)}%`;
  $("#history-remaining").textContent = `${number(history.totals.remaining)} posts remaining`;
  $("#history-month-label").textContent = new Date(`${month}-01T12:00:00+07:00`).toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "Asia/Bangkok" });
  $("#history-body").innerHTML = history.reports.map((r) => `<tr><td class="history-date">${friendlyDate(r.date)}</td><td class="number">${number(r.pages)}</td><td class="number">${number(r.target)}</td><td class="number">${number(r.actual)}</td><td><div class="mini-progress"><progress max="100" value="${r.completion}" aria-label="Completion for ${r.date}"></progress>${number(r.completion)}%</div></td><td><button class="history-open" data-date="${r.date}">Open report →</button></td></tr>`).join("") || '<tr><td colspan="6" class="empty-cell"><strong>A fresh month, ready for progress</strong>Saved daily reports will appear here.</td></tr>';
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  showError("#login-error", "");
  try {
    const credentials = { username: $("#username").value, password: $("#password").value };
    if (state.setup) {
      if (credentials.password !== $("#confirm-password").value) throw new Error("Passwords do not match.");
      await api("/api/setup", { method: "POST", headers: { "X-Setup-Token": state.setupToken }, body: JSON.stringify(credentials) });
      $("#confirm-password").value = "";
      setSetupMode(false);
    }
    const result = await api("/api/login", { method: "POST", body: JSON.stringify(credentials) });
    await openWorkspace(result);
  } catch (error) {
    showError("#login-error", error.message);
  } finally {
    button.disabled = false;
  }
});

$("#viewer-login").addEventListener("click", async () => {
  if (!state.viewerUsername || state.setup) return;
  const button = $("#viewer-login");
  button.disabled = true;
  showError("#login-error", "");
  try {
    const session = await api("/api/login", { method: "POST", body: JSON.stringify({ username: state.viewerUsername }) });
    await openWorkspace(session);
  } catch (error) {
    showError("#login-error", error.message);
  } finally {
    button.disabled = false;
  }
});

$$("[data-view]").forEach((button) => button.addEventListener("click", () => changeView(button.dataset.view)));
$(".brand").addEventListener("click", (event) => { event.preventDefault(); changeView("daily"); });
$("#retry-load").addEventListener("click", () => changeView(state.view));
$("#report-date").addEventListener("change", async () => {
  if (state.busy || state.loading || !$("#report-date").validity.valid || !canLeave()) {
    $("#report-date").value = state.report?.date || state.today;
    return;
  }
  await changeView("daily", true);
});
$("#report-search").addEventListener("input", () => { if (validVisibleInputs()) renderReport(); });
$("#pages-search").addEventListener("input", renderPages);
$$("[data-filter]").forEach((button) => button.addEventListener("click", () => {
  if (!validVisibleInputs()) return;
  state.filter = button.dataset.filter;
  $$("[data-filter]").forEach((b) => b.classList.toggle("active", b === button));
  renderReport();
}));
$("#report-body").addEventListener("input", (event) => {
  const input = event.target.closest(".actual-input");
  if (!input || !state.report || state.busy) return;
  const value = input.valueAsNumber;
  const valid = input.validity.valid && Number.isInteger(value) && value >= 0 && value <= 1000000;
  input.setAttribute("aria-invalid", String(!valid));
  state.dirty = true;
  if (valid) {
    const row = state.report.rows.find((r) => r.page_id === Number(input.dataset.id));
    row.actual = value;
    input.closest("tr").querySelector(".row-status").innerHTML = statusBadge(row);
    updateTotals();
  }
  updateSaveState();
});
// Keep a focused row in place while typing; apply status filters on blur.
$("#report-body").addEventListener("focusout", (event) => {
  if (state.filter !== "all" && event.target.matches(".actual-input") && event.target.validity.valid) {
    setTimeout(() => { if (!$("#report-body").contains(document.activeElement)) renderReport(); }, 0);
  }
});
$("#save-report").addEventListener("click", saveReport);
$$(".export-button").forEach((button) => button.addEventListener("click", async () => {
  if (!state.report?.saved || state.dirty || state.busy) return;
  const format = button.dataset.format;
  button.disabled = true;
  try {
    const response = await fetch(`/api/export.${format}?date=${state.report.date}`, { credentials: "same-origin" });
    if (!response.ok) {
      const result = await response.json();
      if (response.status === 401) showLogin();
      throw new Error(result.error);
    }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `facebook-report-${state.report.date}.${format}`;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`${format === "xlsx" ? "Excel" : "CSV"} report exported.`);
  } catch (error) { toast(error.message, true); }
  finally { updateSaveState(); }
}));
$("#add-page").addEventListener("click", () => editPage());
$$(".close-dialog").forEach((button) => button.addEventListener("click", () => $("#page-dialog").close()));
$("#pages-body").addEventListener("click", (event) => {
  const edit = event.target.closest(".edit-page");
  const remove = event.target.closest(".delete-button");
  if (edit) editPage(state.pages.find((p) => p.id === Number(edit.dataset.id)));
  if (remove) {
    const page = state.pages.find((p) => p.id === Number(remove.dataset.id));
    $("#delete-name").textContent = page.name;
    $("#confirm-delete").dataset.id = page.id;
    showError("#delete-error", "");
    $("#delete-dialog").showModal();
  }
});
$("#page-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#page-submit");
  const id = $("#page-id").value;
  button.disabled = true;
  showError("#page-error", "");
  try {
    await api(id ? `/api/pages/${id}` : "/api/pages", { method: id ? "PUT" : "POST", body: JSON.stringify({ name: $("#page-name").value, url: $("#page-url").value, target: $("#page-target").valueAsNumber }) });
    $("#page-dialog").close();
    await loadPages();
    toast(id ? "Page updated. Unsaved reports will use the new details." : "Page added to your workspace.");
  } catch (error) {
    if ($("#page-dialog").open) showError("#page-error", error.message);
    else viewError(error);
  } finally { button.disabled = false; }
});
$$(".close-delete").forEach((button) => button.addEventListener("click", () => $("#delete-dialog").close()));
$("#confirm-delete").addEventListener("click", async () => {
  const button = $("#confirm-delete");
  button.disabled = true;
  try {
    await api(`/api/pages/${button.dataset.id}`, { method: "DELETE" });
    $("#delete-dialog").close();
    await loadPages();
    toast("Page deleted. Saved report history is preserved.");
  } catch (error) {
    if ($("#delete-dialog").open) showError("#delete-error", error.message);
    else viewError(error);
  } finally { button.disabled = false; }
});
$("#history-month").addEventListener("change", async () => {
  if (!$("#history-month").validity.valid) return;
  $("#view-error").hidden = true;
  try { await loadHistory(); } catch (error) { viewError(error); }
});
$("#history-body").addEventListener("click", (event) => {
  const button = event.target.closest(".history-open");
  if (!button) return;
  $("#report-date").value = button.dataset.date;
  changeView("daily");
});
$("#logout").addEventListener("click", async () => {
  if (state.busy || !canLeave()) return;
  try {
    await api("/api/logout", { method: "POST", body: "{}" });
    state.dirty = false;
    showLogin();
  } catch (error) { toast(error.message, true); }
});
window.addEventListener("beforeunload", (event) => {
  if (state.dirty) { event.preventDefault(); event.returnValue = ""; }
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && state.user && state.view === "daily" && !$("dialog[open]")) {
    event.preventDefault();
    if (!$("#save-report").disabled) saveReport();
  }
});

(async function boot() {
  if (location.protocol === "file:") {
    showLogin();
    showError("#login-error", "Start the server with python server.py, then open http://127.0.0.1:8000. This app needs its database server.");
    $("#login-form button").disabled = true;
    return;
  }
  try {
    const setup = await api("/api/setup");
    state.viewerUsername = setup.viewer || "";
    if (setup.needed) {
      showLogin();
      if (setup.enabled) setSetupMode(true, setup.token);
      else showError("#login-error", "Create your administrator on the server with python server.py --create-user.");
      return;
    }
    setSetupMode(false);
    await openWorkspace(await api("/api/session"));
  }
  catch (error) {
    showLogin();
    if (!/sign in|session expired/i.test(error.message)) showError("#login-error", error.message);
  }
})();
