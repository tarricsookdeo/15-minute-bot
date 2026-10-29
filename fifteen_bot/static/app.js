"use strict";

const $ = (sel) => document.querySelector(sel);
const TRADES_POLL_MS = 5000;
const TOKEN_KEY = "fifteen-bot-session";

let state = null;          // last dashboard snapshot (pushed over the WebSocket)
let clockOffsetMs = 0;     // server time - browser time, so countdowns match the engine
let available = [];        // markets from /api/markets/available
let editingSeries = [];    // markets the strategy dialog will save to
let timers = [];
let socket = null;         // live connection: pushes state, and keeps an idle session alive
let sessionToken = null;   // this tab's session; see getToken
let savesSessions = false; // the server keeps sessions (encrypted) through restarts; from /api/info
let socketRetries = 0;

// --- helpers ----------------------------------------------------------------

// The session token is saved in localStorage, so coming back later (even after closing the
// browser) rejoins the running bot without the key. Never in a cookie, so other sites can't
// send requests as this user. Each tab reads it once and keeps its own copy, so connecting a
// different key in another tab can't redirect this tab's requests to that other session.
function getToken() {
  if (sessionToken === null) {
    try { sessionToken = localStorage.getItem(TOKEN_KEY); } catch { /* storage blocked */ }
  }
  return sessionToken;
}
function setToken(token) {
  sessionToken = token;
  try { localStorage.setItem(TOKEN_KEY, token); } catch { /* storage blocked: lasts until refresh */ }
}
function clearToken() {
  try {
    if (localStorage.getItem(TOKEN_KEY) === sessionToken) localStorage.removeItem(TOKEN_KEY);
  } catch { /* nothing stored */ }
  sessionToken = "";  // not null: don't read back a token another tab saved since
}

async function api(method, path, body) {
  const token = getToken();
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (token) headers["X-Session"] = token;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ")
      : data.detail;
    if (res.status === 401 && token) sessionEnded(detail);
    throw Object.assign(new Error(detail || `Request failed (${res.status})`), { status: res.status });
  }
  return data;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const num = (v) => (v === null || v === undefined || v === "" ? null : Number(v));
const cents = (v) => (num(v) === null ? "—" : `${Math.round(num(v) * 100)}¢`);
const pct = (v) => `${Math.round(num(v) * 100)}%`;

function money(v, signed = true) {
  const n = num(v) ?? 0;
  const s = `$${Math.abs(n).toFixed(2)}`;
  if (!signed) return s;
  return n > 0.004 ? `+${s}` : n < -0.004 ? `-${s}` : s;
}
const pnlClass = (v) => ((num(v) ?? 0) > 0.004 ? "pos" : (num(v) ?? 0) < -0.004 ? "neg" : "");

function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function sideText(side) {
  return { EITHER: "YES or NO", YES: "YES", NO: "NO" }[side];
}

function exitText(s) {
  const parts = [];
  if (s.take_profit !== null && s.take_profit !== undefined) parts.push(`take profit at +${cents(s.take_profit)}`);
  if (s.stop_loss !== null && s.stop_loss !== undefined) parts.push(`stop loss at −${cents(s.stop_loss)}`);
  return parts.join(" · ");
}

function strategyText(s) {
  const exits = exitText(s);
  const limit = num(s.daily_loss_limit) === null ? "" : ` · daily loss limit ${money(s.daily_loss_limit, false)}`;
  return `With ≤ ${clock(s.entry_seconds)} left, buy ${sideText(s.side)} priced ${cents(s.min_price)}–${cents(s.max_price)} · ${num(s.quantity)} contract${num(s.quantity) === 1 ? "" : "s"}${exits ? ` · ${exits}` : ""}${limit}`;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `show${isError ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.className = ""), 3500);
}

function confirmAction(title, body, okLabel) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  $("#confirm-ok").textContent = okLabel;
  dialog.returnValue = "";
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener("close", () => resolve(dialog.returnValue === "ok"), { once: true }));
}

// --- strategy form (shared by the setup wizard and the edit dialog) -------------

const DEFAULT_STRATEGY = {
  entry_seconds: 120, cutoff_seconds: 10, min_price: "0.80", max_price: "0.90", side: "EITHER", quantity: "1",
  take_profit: null, stop_loss: null, daily_loss_limit: null,
};

function strategyFields() {
  const field = (label, control, hint) =>
    `<label class="field"><span>${label}</span>${control}<small class="hint">${hint}</small></label>`;
  return `<div class="form-grid">
    ${field("Start buying with this much time left", `<div class="inline"><input type="number" name="entry_seconds" min="5" max="900" step="1" required><em>seconds</em></div>`, "120 = the last 2 minutes of each window")}
    ${field("Stop trying with this much time left", `<div class="inline"><input type="number" name="cutoff_seconds" min="0" max="900" step="1" required><em>seconds</em></div>`, "Leaves time for the order to fill before the window closes")}
    ${field("Lowest chance to buy at", `<div class="inline"><input type="number" name="min_price" min="1" max="99" step="1" required><em>%</em></div>`, "A contract's price in cents is the market's chance it wins")}
    ${field("Highest chance to buy at", `<div class="inline"><input type="number" name="max_price" min="1" max="99" step="1" required><em>%</em></div>`, "Pricier contracts win more often but pay less")}
    ${field("Which side", `<select name="side"><option value="EITHER">Either YES or NO</option><option value="YES">YES only</option><option value="NO">NO only</option></select>`, "“Either” buys whichever side is in your range")}
    ${field("Contracts per trade", `<input type="number" name="quantity" min="0.01" step="0.01" required>`, "Each winning contract pays out $1.00")}
    ${field("Take profit at (optional)", `<div class="inline"><input type="number" name="take_profit" min="1" max="98" step="1" placeholder="Off"><em>¢ per contract</em></div>`, "Sells early once each contract is up this much after fees")}
    ${field("Stop loss at (optional)", `<div class="inline"><input type="number" name="stop_loss" min="1" max="98" step="1" placeholder="Off"><em>¢ per contract</em></div>`, "Sells early once each contract is down this much after fees")}
    ${field("Daily loss limit for this market (optional)", limitInput("daily_loss_limit"), "Stops buying it before the day's loss could pass this")}
  </div>`;
}

/** A dollar amount field for a daily loss limit; blank means no limit. */
function limitInput(name, id = "") {
  return `<div class="inline"><em>$</em><input type="number" name="${name}"${id ? ` id="${id}"` : ""} min="0.01" step="0.01" placeholder="Off"></div>`;
}

function fillStrategy(form, s) {
  const f = form.elements;
  f.entry_seconds.value = s.entry_seconds;
  f.cutoff_seconds.value = s.cutoff_seconds;
  f.min_price.value = Math.round(num(s.min_price) * 100);
  f.max_price.value = Math.round(num(s.max_price) * 100);
  f.side.value = s.side;
  f.quantity.value = num(s.quantity);
  f.take_profit.value = num(s.take_profit) === null ? "" : Math.round(num(s.take_profit) * 100);
  f.stop_loss.value = num(s.stop_loss) === null ? "" : Math.round(num(s.stop_loss) * 100);
  f.daily_loss_limit.value = num(s.daily_loss_limit) ?? "";
}

/** A blank exit field means off (null); otherwise cents in the form, dollars in the API. */
const readTarget = (input) => (input.value.trim() === "" ? null : String(Number(input.value) / 100));
/** A blank loss limit means off (null); otherwise dollars. */
const readLimit = (input) => (input.value.trim() === "" ? null : input.value.trim());

function limitProblem(limit, name) {
  if (limit === null) return null;
  const c = Math.round(num(limit) * 100 * 1e6) / 1e6;
  if (Number.isInteger(c) && c >= 1 && c <= 1e8) return null;
  return `${name} must be between $0.01 and $1,000,000 in whole cents, or blank for off.`;
}

function readStrategy(form) {
  const f = form.elements;
  return {
    entry_seconds: Number(f.entry_seconds.value),
    cutoff_seconds: Number(f.cutoff_seconds.value),
    min_price: (Number(f.min_price.value) / 100).toFixed(2),
    max_price: (Number(f.max_price.value) / 100).toFixed(2),
    side: f.side.value,
    quantity: f.quantity.value,
    take_profit: readTarget(f.take_profit),
    stop_loss: readTarget(f.stop_loss),
    daily_loss_limit: readLimit(f.daily_loss_limit),
  };
}

/** A plain-language reason the rule can't be saved, or null if it's fine. */
function strategyProblem(s) {
  if (!(s.entry_seconds >= 5 && s.entry_seconds <= 900)) return "Start time must be between 5 and 900 seconds.";
  if (!(s.cutoff_seconds >= 0 && s.cutoff_seconds < s.entry_seconds)) return "The stop time must be less than the start time.";
  const min = num(s.min_price), max = num(s.max_price);
  if (!(min >= 0.01 && max <= 0.99)) return "Chances must be between 1% and 99%.";
  if (min > max) return "The lowest chance can't be above the highest chance.";
  if (!(num(s.quantity) > 0)) return "Buy at least one contract per trade.";
  for (const [target, name] of [[s.take_profit, "Take profit"], [s.stop_loss, "Stop loss"]]) {
    const c = target === null ? null : Math.round(num(target) * 100 * 1e6) / 1e6;
    if (c !== null && !(Number.isInteger(c) && c >= 1 && c <= 98)) {
      return `${name} must be a whole number of cents from 1 to 98, or blank for off.`;
    }
  }
  return limitProblem(s.daily_loss_limit, "The daily loss limit");
}

function strategyPreview(s) {
  if (strategyProblem(s)) return `<span class="neg">${esc(strategyProblem(s))}</span>`;
  const q = num(s.quantity);
  const example = Math.round(((num(s.min_price) + num(s.max_price)) / 2) * 100) / 100;
  const exampleSide = s.side === "NO" ? "NO" : "YES";
  const exampleLeft = Math.round((s.entry_seconds + s.cutoff_seconds) / 2);
  return `Between <b>${clock(s.entry_seconds)}</b> and <b>${clock(s.cutoff_seconds)}</b> before each window closes, the bot buys
    <b>${sideText(s.side)}</b> once it's priced <b>${cents(s.min_price)}–${cents(s.max_price)}</b>. At most one trade per window.
    <div class="example"><b>Example:</b> with ${clock(exampleLeft)} left, ${exampleSide} is at ${cents(example)}. The bot buys ${q} ${exampleSide}
    for ${money(q * example, false)}. If ${exampleSide} wins you get ${money(q, false)} back
    (<span class="pos">${money(q * (1 - example))}</span>); if it loses you lose <span class="neg">${money(q * example, false)}</span>. Fees not included.</div>
    ${exitPreview(s)}${limitPreview(s)}`;
}

function limitPreview(s) {
  if (s.daily_loss_limit === null) return "";
  return `<div class="example"><b>Daily loss limit:</b> before each buy, the bot checks that even if this buy and every open trade
    in this market lost, the market's loss today couldn't pass <b class="neg">${money(s.daily_loss_limit, false)}</b>. If it could, the bot
    stops buying this market until you press Resume on its card. Days start at midnight US Eastern.</div>`;
}

function exitPreview(s) {
  const lines = [];
  if (s.take_profit !== null) {
    lines.push(`once each contract is up <b class="pos">${cents(s.take_profit)}</b> or more after fees, it sells to lock in the profit`);
  }
  if (s.stop_loss !== null) {
    lines.push(`once each contract is down <b class="neg">${cents(s.stop_loss)}</b> or more after fees, it sells to limit the loss`);
  }
  if (!lines.length) return `<div class="example">The bot holds every position until the window settles.</div>`;
  return `<div class="example"><b>Selling early:</b> while the window is open, ${lines.join("; ")}.
    It sells at the current bid, and re-prices every few seconds until the order fills. If it can't sell in time, the position settles as normal.</div>`;
}

// --- setup wizard ------------------------------------------------------------------

const API_SETTINGS_URL = "https://public.com/settings/security/api";
const STEPS = [
  ["welcome", "Welcome"],
  ["connect", "Connect"],
  ["account", "Account"],
  ["markets", "Markets"],
  ["strategy", "Your rule"],
  ["review", "Review"],
];
const wiz = {
  step: "welcome",
  session: null,
  available: null,
  selected: new Set(),
  strategy: { ...DEFAULT_STRATEGY },
};

async function boot() {
  try {
    savesSessions = (await api("GET", "/api/info")).saves_sessions;
  } catch { /* keep the cautious default wording */ }
  if (!getToken()) return showOnboarding("welcome");
  let session;
  try {
    session = await api("GET", "/api/session");
  } catch (err) {
    if (err.status === 503 || err.status === undefined) {
      // The app is restarting: the session is kept, so wait and try again.
      $("#conn-banner").classList.remove("hidden");
      return setTimeout(() => boot().catch((e) => toast(e.message, true)), 3000);
    }
    if (getToken()) toast(err.message, true);  // a 401 already returned to the start
    return;
  }
  $("#conn-banner").classList.add("hidden");
  wiz.session = session;
  openSocket();
  if (!session.account_id) return showOnboarding("account");
  if (!session.onboarded) return showOnboarding("markets");
  showDashboard();
}

/** Back to the start after the server ended this session (or it expired). */
function sessionEnded(reason) {
  resetLocalSession();
  showOnboarding("connect");
  if (reason) $("#connect-error").textContent = reason;
}

function resetLocalSession() {
  clearToken();
  $("#conn-banner").classList.add("hidden");
  const ws = socket;
  socket = null;
  ws?.close();
  stopPolling();
  state = null;
  Object.assign(wiz, { session: null, available: null, selected: new Set(), strategy: { ...DEFAULT_STRATEGY } });
}

async function endSession() {
  try {
    await api("DELETE", "/api/session");
  } catch {
    /* already ended on the server */
  }
  resetLocalSession();
}

function openSocket() {
  const token = getToken();
  if (socket || !token) return;
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  socket = ws;
  ws.addEventListener("open", () => ws.send(JSON.stringify({ session: token })));
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "state") {
      socketRetries = 0;
      $("#conn-banner").classList.add("hidden");
      if (!$("#dash-view").classList.contains("hidden")) setState(message.state);
    } else if (message.type === "ended") {
      sessionEnded(message.reason);
    }
  });
  ws.addEventListener("close", () => {
    if (socket !== ws) return;  // closed on purpose
    socket = null;
    if (!getToken()) return;
    // The bot keeps trading on the server without a tab, so just reconnect.
    $("#conn-banner").classList.remove("hidden");
    setTimeout(openSocket, Math.min(1000 * 2 ** socketRetries++, 10000));
  });
}

function showOnboarding(step) {
  stopPolling();
  $("#dash-view").classList.add("hidden");
  $("#onboard-view").classList.remove("hidden");
  goTo(step);
}

function goTo(step) {
  wiz.step = step;
  const index = STEPS.findIndex(([key]) => key === step);
  const stepper = $("#stepper");
  stepper.innerHTML = STEPS.map(([, label], i) => {
    const status = i < index ? "done" : i === index ? "current" : "";
    return `<li class="${status}" ${i === index ? 'aria-current="step"' : ""}><span class="n">${i < index ? "✓" : i + 1}</span><span class="label">${label}</span></li>`;
  }).join("");
  $("#onboard-body").innerHTML = WIZARD_STEPS[step]();
  window.scrollTo(0, 0);
  $("#onboard-body").querySelector("h1")?.focus();
  WIZARD_AFTER[step]?.();  // may move focus to the step's main control
}

const backButton = (step, label = "Back") => `<button type="button" class="btn ghost" data-go="${step}">← ${label}</button>`;

const WIZARD_STEPS = {
  welcome: () => `
    <h1 tabindex="-1">Let's set up your trading bot</h1>
    <p class="lead">In a few minutes you'll have a bot that watches Public's 15-minute prediction markets and trades them using rules you choose.</p>
    <div class="how">
      <div class="how-item"><span class="badge">1</span><div><b>Connect your Public account</b><p>Using an API key from Public. ${savesSessions
        ? "It's stored encrypted, so your bot carries on after the app restarts, and deleted when you disconnect."
        : "It's held in the server's memory only while your bot exists, never written to disk."}</p></div></div>
      <div class="how-item"><span class="badge">2</span><div><b>Pick the markets to trade</b><p>BTC, gold, EUR/USD and more. Each one asks a new yes/no question every 15 minutes.</p></div></div>
      <div class="how-item"><span class="badge">3</span><div><b>Set your rule</b><p>For example: “with 2 minutes left, buy whichever side is priced 80–90¢”.</p></div></div>
    </div>
    <div class="callout warn"><b>This bot trades with real money.</b> It places real orders in your Public account. Nothing is traded until you press <b>Start</b>, and you can stop it at any time.
      Once started, it keeps trading even after you close this page or turn off your computer.</div>
    <div class="wizard-actions"><span></span><button type="button" class="btn primary" data-go="connect">Get started →</button></div>`,

  connect: () => `
    <h1 tabindex="-1">Connect your Public account</h1>
    <p class="lead">The bot uses a Public API key to read prices and place orders for you. Here's how to get one:</p>
    <ol class="steps-list">
      <li>
        <b>Open Public's API settings</b>
        <span>It opens in a new tab. Log in if Public asks you to.</span>
        <a class="btn" href="${API_SETTINGS_URL}" target="_blank" rel="noopener noreferrer">Open Public API settings ↗</a>
      </li>
      <li>
        <b>Generate a secret key and copy it</b>
        <span>Treat it like a password: anyone who has it can trade in your account.</span>
      </li>
      <li>
        <b>Paste it here</b>
        <form id="connect-form" autocomplete="off">
          <div class="key-input">
            <input type="password" id="api-key" spellcheck="false" placeholder="Paste your secret key" aria-label="Public API secret key">
            <button type="button" class="btn ghost" data-action="toggle-key">Show</button>
          </div>
          <button class="btn primary" type="submit" id="connect-btn">Connect</button>
        </form>
      </li>
    </ol>
    <p class="error" id="connect-error" role="alert"></p>
    <details class="faq">
      <summary>How is my key kept safe?</summary>
      <ul>
        <li>It travels over an encrypted connection to this app's server, and from there only to Public.</li>
        <li>${savesSessions
          ? "The app stores it encrypted in its database, so your bot carries on after the app restarts. It's never written to logs, and never shown on this page again."
          : "The server keeps it in memory only. It's never written to disk or to logs, and never shown on this page again."}</li>
        <li>This browser remembers your session, not your key, so you can come back to your running bot without pasting the key again.</li>
        <li>${savesSessions
          ? "The key is deleted when you click Disconnect, or after a day with the bot stopped and no page open."
          : "The key is forgotten when you click Disconnect, after a day with the bot stopped and no page open, or when the app stops."}</li>
        <li>You can delete the key in Public's API settings at any time, which stops it working immediately.</li>
      </ul>
    </details>
    <div class="wizard-actions">${backButton("welcome")}<span></span></div>`,

  account: () => {
    const accounts = wiz.session.accounts;
    const preferred = wiz.session.account_id
      ?? (accounts.find((a) => a.type === "BROKERAGE") ?? accounts[0])?.id;
    return `
    <h1 tabindex="-1">Choose the account to trade in</h1>
    <p class="lead"><span class="pos">✓ Connected.</span> Your key works.
      ${accounts.length === 1 ? "We found one account:" : `We found ${accounts.length} accounts. Pick the one the bot should use:`}</p>
    <div class="choice-list">
      ${accounts.map((a) => `
        <label class="choice">
          <input type="radio" name="account" value="${esc(a.id)}" ${a.id === preferred ? "checked" : ""}>
          <div><b>${esc(a.id)}</b><span class="muted small">${esc(accountTypeLabel(a.type))}</span></div>
        </label>`).join("")}
    </div>
    <p class="muted small">Prediction markets are traded in a brokerage account.</p>
    <p class="error" id="account-error" role="alert"></p>
    <div class="wizard-actions">
      <button type="button" class="btn ghost" data-action="different-key">← Use a different key</button>
      <button type="button" class="btn primary" data-action="choose-account">Continue →</button>
    </div>`;
  },

  markets: () => `
    <h1 tabindex="-1">Pick the markets to trade</h1>
    <p class="lead">Each market asks a yes/no question about a price that resets every 15 minutes, and you can bet on either answer.
      Start with one or two. You can add more later from the dashboard.</p>
    <input type="search" id="wiz-search" placeholder="Search BTC, gold, EUR/USD…" autocomplete="off" aria-label="Search markets">
    <div id="wiz-markets" class="tile-list"><p class="muted">Loading markets from Public…</p></div>
    <div class="wizard-actions">
      ${backButton("account")}
      <div class="actions"><span class="muted small" id="wiz-count"></span>
      <button type="button" class="btn primary" data-action="markets-next" id="markets-next" disabled>Continue →</button></div>
    </div>`,

  strategy: () => `
    <h1 tabindex="-1">Set your trading rule</h1>
    <p class="lead">This rule applies to <b>${esc(selectedNames())}</b>. We've filled in a starting point based on your idea. Adjust anything you like.
      You can give each market its own rule later.</p>
    <form id="wiz-strategy">${strategyFields()}</form>
    <div class="preview" id="wiz-preview"></div>
    <div class="wizard-actions">
      ${backButton("markets")}
      <button type="button" class="btn primary" data-action="strategy-next">Continue →</button>
    </div>`,

  review: () => {
    const s = wiz.strategy;
    return `
    <h1 tabindex="-1">You're ready to go</h1>
    <p class="lead">Here's what you set up:</p>
    <dl class="review">
      <dt>Account</dt><dd>${esc(wiz.session.account_id)}</dd>
      <dt>Markets</dt><dd>${esc(selectedNames())}</dd>
      <dt>Rule</dt><dd>${esc(strategyText(s))}</dd>
      <dt>Most per trade</dt><dd>${money(num(s.quantity) * num(s.max_price), false)} plus fees, per market, per 15-minute window</dd>
    </dl>
    <label class="field review-limit"><span>Daily loss limit for the whole account (optional)</span>${limitInput("account_limit", "wiz-account-limit")}
      <small class="hint">Stops buying on every market before the day's loss across all of them could pass this. You can change it on the dashboard.</small></label>
    <div class="callout">
      <b>What happens next</b>
      <ol>
        <li>The bot watches the current 15-minute window of each market you picked.</li>
        <li>Once ${clock(s.entry_seconds)} or less is left and a side is priced ${cents(s.min_price)}–${cents(s.max_price)}, it places a real buy order. That's at most one per window.</li>
        <li>A few minutes after the window closes, Public settles it, and the dashboard shows the win or loss.</li>
      </ol>
    </div>
    <p class="muted small">You can stop the bot at any time with the button in the top bar.
      <b>The bot keeps trading after you close this page.</b> Come back in this browser to check on it, or paste
      the same key on another device. To end it, click <b>Disconnect</b>: the bot stops, unfilled orders are cancelled,
      and your settings and results here are cleared. Contracts you already bought stay in your Public account.</p>
    <p class="error" id="finish-error" role="alert"></p>
    <div class="wizard-actions">
      ${backButton("strategy")}
      <div class="actions">
        <button type="button" class="btn ghost" data-action="finish">Finish without starting</button>
        <button type="button" class="btn primary" data-action="finish-start">Start trading</button>
      </div>
    </div>`;
  },
};

const WIZARD_AFTER = {
  connect: () => ($("#saved-btn") ?? $("#api-key")).focus(),
  markets: loadWizardMarkets,
  strategy: () => {
    const form = $("#wiz-strategy");
    fillStrategy(form, wiz.strategy);
    const update = () => ($("#wiz-preview").innerHTML = strategyPreview(readStrategy(form)));
    form.addEventListener("input", update);
    update();
  },
};

const ACCOUNT_TYPES = {
  BROKERAGE: "Brokerage account",
  HIGH_YIELD: "High-yield cash account",
  BOND_ACCOUNT: "Bond account",
  TREASURY: "Treasury account",
  TRADITIONAL_IRA: "Traditional IRA",
  ROTH_IRA: "Roth IRA",
  JOINT: "Joint account",
  ENTITY: "Entity account",
};
const accountTypeLabel = (type) => ACCOUNT_TYPES[type] ?? `${type} account`;

function selectedNames() {
  const names = (wiz.available ?? []).filter((m) => wiz.selected.has(m.series)).map((m) => m.name);
  return names.length > 2 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names.join(" and ");
}

async function loadWizardMarkets() {
  if (!wiz.available) {
    try {
      wiz.available = await api("GET", "/api/markets/available");
    } catch (err) {
      $("#wiz-markets").innerHTML = `<p class="error">Couldn't load markets: ${esc(err.message)}</p>
        <button type="button" class="btn" data-go="markets">Try again</button>`;
      return;
    }
  }
  $("#wiz-search").addEventListener("input", renderWizardMarkets);
  renderWizardMarkets();
}

function renderWizardMarkets() {
  const q = $("#wiz-search").value.trim().toLowerCase();
  const popular = new Set(
    [...wiz.available].sort((a, b) => num(b.volume) - num(a.volume)).slice(0, 3).map((m) => m.series),
  );
  const matches = wiz.available.filter(
    (m) => !q || `${m.name} ${m.series} ${m.category} ${m.subcategories.join(" ")}`.toLowerCase().includes(q),
  );
  const groups = {};
  matches.forEach((m) => (groups[m.category || "Other"] ??= []).push(m));
  $("#wiz-markets").innerHTML = Object.entries(groups).map(([category, markets]) => `
    <h3>${esc(category)}</h3>
    <div class="tile-grid">
      ${markets.map((m) => {
        const detail = m.title?.split(" · ")[1];
        return `
        <label class="tile">
          <input type="checkbox" data-series="${esc(m.series)}" ${wiz.selected.has(m.series) ? "checked" : ""}>
          <div>
            <div class="tile-name">${esc(m.name)} ${popular.has(m.series) ? '<span class="pill popular">Popular</span>' : ""}</div>
            <div class="muted small">${m.close_time
              ? `Now trading${detail ? ` · ${esc(detail)}` : ""} · closes ${new Date(m.close_time).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`
              : "Between windows right now"}</div>
          </div>
        </label>`;
      }).join("")}
    </div>`).join("") || `<p class="muted">No markets match “${esc(q)}”.</p>`;
  updateWizardCount();
}

function updateWizardCount() {
  const n = wiz.selected.size;
  $("#wiz-count").textContent = n ? `${n} selected` : "Select at least one";
  $("#markets-next").disabled = n === 0;
}

async function connectFromWizard(apiKey) {
  const error = $("#connect-error");
  const button = $("#connect-btn");
  const label = button.textContent;
  error.textContent = "";
  if (!apiKey.trim()) {
    error.textContent = "Paste your secret key first.";
    return;
  }
  button.disabled = true;
  button.textContent = "Checking your key with Public…";
  let result;
  try {
    result = await api("POST", "/api/connect", { api_key: apiKey });
  } catch (err) {
    error.textContent = err.message;
    button.disabled = false;
    button.textContent = label;
    return;
  }
  const { token, ...session } = result;
  setToken(token);
  wiz.session = session;
  openSocket();
  if (session.account_id && session.onboarded) {
    // The same key is already running in another tab: this tab joins that session.
    showDashboard();
    return toast("Joined your running session.");
  }
  goTo("account");
}

async function finishWizard(start) {
  $("#finish-error").textContent = "";
  const limit = readLimit($("#wiz-account-limit"));
  const problem = limitProblem(limit, "The account's daily loss limit");
  if (problem) return ($("#finish-error").textContent = problem);
  $("#onboard-body").querySelectorAll(".wizard-actions button").forEach((b) => (b.disabled = true));
  try {
    await api("POST", "/api/onboarding", { series: [...wiz.selected], strategy: wiz.strategy, daily_loss_limit: limit, start });
  } catch (err) {
    $("#finish-error").textContent = err.message;
    $("#onboard-body").querySelectorAll(".wizard-actions button").forEach((b) => (b.disabled = false));
    return;
  }
  showDashboard();
  toast(start ? "You're all set. The bot is trading." : "You're all set. Press Start when you're ready.");
}

$("#onboard-body").addEventListener("click", async (e) => {
  const go = e.target.closest("[data-go]")?.dataset.go;
  if (go) return goTo(go);
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "toggle-key") {
    const input = $("#api-key");
    input.type = input.type === "password" ? "text" : "password";
    e.target.textContent = input.type === "password" ? "Show" : "Hide";
  }
  if (action === "different-key") {
    await endSession();
    goTo("connect");
  }
  if (action === "choose-account") {
    const accountId = $("#onboard-body").querySelector('input[name="account"]:checked')?.value;
    if (!accountId) return ($("#account-error").textContent = "Pick an account to continue.");
    try {
      wiz.session = await api("POST", "/api/account", { account_id: accountId });
    } catch (err) {
      return ($("#account-error").textContent = err.message);
    }
    if (wiz.session.onboarded) showDashboard();
    else goTo("markets");
  }
  if (action === "markets-next") goTo("strategy");
  if (action === "strategy-next") {
    const strategy = readStrategy($("#wiz-strategy"));
    if (strategyProblem(strategy)) return;  // the preview already explains what to fix
    wiz.strategy = strategy;
    goTo("review");
  }
  if (action === "finish") finishWizard(false);
  if (action === "finish-start") finishWizard(true);
});

$("#onboard-body").addEventListener("change", (e) => {
  const series = e.target.dataset?.series;
  if (!series) return;
  if (e.target.checked) wiz.selected.add(series);
  else wiz.selected.delete(series);
  updateWizardCount();
});

$("#onboard-body").addEventListener("submit", (e) => {
  e.preventDefault();
  if (e.target.id !== "connect-form") return;
  connectFromWizard($("#api-key").value);
});

// --- dashboard ----------------------------------------------------------------

function showDashboard() {
  $("#onboard-view").classList.add("hidden");
  $("#dash-view").classList.remove("hidden");
  stopPolling();
  openSocket();
  refreshState();  // render now rather than waiting for the next push
  refreshTrades();
  timers = [setInterval(refreshTrades, TRADES_POLL_MS), setInterval(tickCountdowns, 1000)];
}

function stopPolling() {
  timers.forEach(clearInterval);
  timers = [];
}

async function refreshState() {
  try {
    setState(await api("GET", "/api/state"));
  } catch (err) {
    if (getToken()) toast(`Couldn't reach the bot: ${err.message}`, true);
  }
}

function setState(next) {
  state = next;
  clockOffsetMs = new Date(next.server_time) - Date.now();
  renderTopbar();
  renderSummary();
  renderMarkets();
  renderPerformance();
  renderLog();
  renderTradeFilter();
}

function renderTopbar() {
  const btn = $("#engine-btn");
  const halted = state.running && Boolean(state.loss_limit.halted_at);
  btn.classList.toggle("on", state.running && !halted);
  btn.classList.toggle("halted", halted);
  btn.innerHTML = `<span class="dot"></span>${halted ? "Loss limit reached — Resume" : state.running ? "Engine running — Stop" : "Engine stopped — Start"}`;
  $("#account-label").textContent = `Account ${state.account_id}`;
  $("#limit-banner").classList.toggle("hidden", !state.loss_limit.halted_at);
}

/** The card's live view of a trade that hasn't settled yet. */
const EXIT_LABELS = { TAKE_PROFIT: "take-profit", STOP_LOSS: "stop-loss" };

function positionBlock(p) {
  const filled = num(p.filled), quantity = num(p.quantity), sold = num(p.sold) ?? 0;
  const what = `${esc(p.side)} ×${filled}${filled !== quantity ? ` of ${quantity}` : ""} @ ${cents(p.avg_price)}`
    + (sold > 0 ? ` · ${sold} sold` : "");
  const selling = p.selling ? ` · selling (${EXIT_LABELS[p.exit_reason] ?? "early exit"})` : "";
  if (filled === 0) {
    return `<div class="position"><div class="position-head"><span>Order open</span><span class="muted">${esc(p.side)} ×${quantity} @ ${cents(p.avg_price)}, waiting to fill</span></div></div>`;
  }
  const outcomes = `<div class="muted small">If ${esc(p.side)} wins <span class="pos">${money(p.if_win)}</span> · if it loses <span class="neg">${money(p.if_lose)}</span></div>`;
  if (!p.trading) {
    return `<div class="position"><div class="position-head"><span>Your position</span><span class="muted">${what}</span></div>
      <div class="position-pnl"><span class="muted">Window closed, waiting for Public to settle it</span></div>${outcomes}</div>`;
  }
  const pnl = p.pnl === null
    ? `<b class="muted">—</b><span class="muted small">no bid right now</span>`
    : `<b class="${pnlClass(p.pnl)}">${money(p.pnl)}</b><span class="muted small">at ${cents(p.bid)} bid${p.order_open ? " · order still filling" : ""}${selling}</span>`;
  return `<div class="position"><div class="position-head"><span>Your position</span><span class="muted">${what}</span></div>
    <div class="position-pnl"><span class="muted small">Live P/L</span>${pnl}</div>${outcomes}</div>`;
}

function renderSummary() {
  const t = state.totals;
  const settled = t.wins + t.losses;
  const cost = num(t.cost);
  $("#summary").innerHTML = [
    ["Total P&L", `<span class="${pnlClass(t.pnl)}">${money(t.pnl)}</span>`],
    ["Open P/L", t.open_pnl === null ? "—" : `<span class="${pnlClass(t.open_pnl)}">${money(t.open_pnl)}</span>`],
    ["Win rate", settled ? pct(t.wins / settled) : "—"],
    ["Settled trades", `${settled} <span class="muted small">(${t.wins}W · ${t.losses}L)</span>`],
    ["Pending", t.pending],
    ["Return on stake", cost ? `<span class="${pnlClass(t.pnl)}">${((num(t.pnl) / cost) * 100).toFixed(1)}%</span>` : "—"],
    ["Today, settled", todayStat(state.loss_limit)],
  ]
    .map(([label, value]) => `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div></div>`)
    .join("");
}

/** Today's settled P&L, with the account's daily loss limit and how much of it is left. */
function todayStat(l) {
  const today = `<span class="${pnlClass(l.today_pnl)}">${money(l.today_pnl)}</span>`;
  if (l.limit === null) {
    return `${today}<div class="sub">No daily loss limit · <button class="btn link" type="button" data-action="edit-limit">Set one</button></div>`;
  }
  const room = Math.max(0, num(l.limit) + num(l.today_pnl) - num(l.at_risk));
  return `${today}<div class="sub">Limit ${money(l.limit, false)} · ${money(room, false)} left
    · <button class="btn link" type="button" data-action="edit-limit">Edit</button></div>`;
}

$("#summary").addEventListener("click", (e) => {
  if (e.target.closest("[data-action]")?.dataset.action === "edit-limit") openLimitDialog();
});

function secondsLeft(closeTime) {
  return (new Date(closeTime) - (Date.now() + clockOffsetMs)) / 1000;
}

function quoteInRange(market, side, ask) {
  const s = market.strategy;
  if (ask === null || (s.side !== "EITHER" && s.side !== side)) return false;
  const price = Math.ceil(ask * 100 - 1e-9) / 100;
  return price >= num(s.min_price) && price <= num(s.max_price);
}

function renderMarkets() {
  const el = $("#markets");
  if (!state.markets.length) {
    el.innerHTML = `<div class="empty">No markets yet. Click <b>Add markets</b> to pick the 15-minute markets you want to trade.</div>`;
    return;
  }
  el.innerHTML = state.markets.map((m) => {
    const w = m.window;
    const st = m.stats;
    const target = w?.title?.split(" · ")[1] ?? "";
    const yes = num(w?.yes_ask), no = num(w?.no_ask);
    return `
      <article class="market ${m.enabled ? "" : "disabled"}" data-series="${esc(m.series)}">
        <div class="market-head">
          <div>
            <div class="market-name">${esc(m.name)}</div>
            <div class="muted small">${esc(m.series)}${m.category ? " · " + esc(m.category) : ""}${target ? " · " + esc(target) : ""}</div>
          </div>
          <label class="switch" title="Trade this market when the engine is running">
            <input type="checkbox" data-action="toggle" ${m.enabled ? "checked" : ""}>
            <span class="track"></span>
          </label>
        </div>
        <div class="status" data-status>${statusHTML(cardStatus(m))}</div>
        ${w && !w.multi_outcome ? `
          <div class="quotes">
            <div class="quote ${quoteInRange(m, "YES", yes) ? "in-range" : ""}"><span>YES ask</span><b>${cents(yes)}</b></div>
            <div class="quote ${quoteInRange(m, "NO", no) ? "in-range" : ""}"><span>NO ask</span><b>${cents(no)}</b></div>
          </div>` : ""}
        ${m.position ? positionBlock(m.position) : ""}
        <div class="strategy-line">${esc(strategyText(m.strategy))}${num(m.strategy.daily_loss_limit) === null ? "" : `
          <div class="muted small">Today ${money(m.today.pnl)} settled · ${money(m.today.at_risk, false)} at risk</div>`}</div>
        <div class="market-foot">
          <div><span class="${pnlClass(st.pnl)}"><b>${money(st.pnl)}</b></span> <span class="muted">· ${st.wins}W–${st.losses}L${st.pending ? ` · ${st.pending} pending` : ""}</span></div>
          <div>
            ${m.enabled && m.halted_at ? `<button class="btn link" data-action="resume" type="button">Resume</button>` : ""}
            <button class="btn link" data-action="edit" type="button">Edit</button>
            <button class="btn link" data-action="remove" type="button">Remove</button>
          </div>
        </div>
      </article>`;
  }).join("");
  tickCountdowns();
}

/**
 * Where a market stands in the current window under its own rule. All 15-minute
 * markets share one clock (shown in the top bar), so cards show this instead of a timer.
 */
function cardStatus(m) {
  const w = m.window, s = m.strategy, t = m.window_trade;
  if (m.enabled && !t) {
    if (m.halted_at) {
      return { tone: "bad", label: "Stopped by its daily loss limit", detail: "No new buys. Take-profit and stop-loss sells carry on. Resume to buy again." };
    }
    if (state.running && state.loss_limit.halted_at) {
      return { tone: "bad", label: "Paused by the account's loss limit", detail: "No new buys. Press Resume in the top bar to buy again." };
    }
  }
  if (!w) {
    return state.markets.some((other) => other.window)
      ? { tone: "idle", label: "No window right now", detail: "This market isn't trading at the moment." }
      : { tone: "idle", label: "Between windows", detail: "The next window opens shortly." };
  }
  if (w.multi_outcome) {
    return { tone: "bad", label: "Not supported", detail: "Markets with more than two outcomes can't be traded yet." };
  }
  if (t) {
    const order = `${t.side} ×${num(t.quantity)} @ ${cents(t.price)}`;
    if (t.result === "REJECTED" || t.result === "ERROR") return { tone: "bad", label: "Order rejected", detail: t.note || order };
    if (t.result === "UNFILLED") return { tone: "idle", label: "Order didn't fill", detail: order };
    if (num(t.filled) === 0) return { tone: "active", label: "Order placed, waiting to fill", detail: order };
    const sold = num(t.sold) ?? 0, reason = EXIT_LABELS[t.exit_reason] ?? "early exit";
    if (sold > 0 && sold >= num(t.filled)) {
      return { tone: "good", label: `Sold ${t.side} ×${sold} early (${reason})`, detail: "Done for this window" };
    }
    return {
      tone: "good",
      label: `Bought ${t.side} ×${num(t.filled)} @ ${cents(t.price)}`,
      detail: sold > 0 ? `${sold} sold early (${reason}), the rest held` : "Done for this window",
    };
  }
  if (!m.enabled) return { tone: "idle", label: "Off", detail: "Switch on to trade this market." };
  if (!state.running) return { tone: "idle", label: "Engine stopped", detail: "Press Start in the top bar to trade." };
  const left = secondsLeft(w.close_time);
  if (left > s.entry_seconds) {
    return { tone: "idle", label: `Buy window opens in ${clock(left - s.entry_seconds)}`, detail: `Looks for a buy with ${clock(s.entry_seconds)} left` };
  }
  if (left > s.cutoff_seconds) {
    return {
      tone: "active",
      hot: true,
      label: `Buying now · ${clock(left - s.cutoff_seconds)} left to buy`,
      detail: w.note ?? `Waiting for a side priced ${cents(s.min_price)}–${cents(s.max_price)}`,
    };
  }
  return { tone: "idle", label: "Skipped this window", detail: "No side was priced in range in time." };
}

function statusHTML(status) {
  return `<span class="status-dot ${status.tone}"></span><div><div class="status-label">${esc(status.label)}</div>
    <div class="muted small">${esc(status.detail)}</div></div>`;
}

/** The shared window countdown in the top bar, plus each card's status, once a second. */
function tickCountdowns() {
  const closes = state.markets
    .map((m) => m.window?.close_time)
    .filter((c) => c && secondsLeft(c) > 0)
    .sort((a, b) => new Date(a) - new Date(b));
  const clockEl = $("#window-clock");
  if (!state.markets.length) clockEl.innerHTML = "";
  else if (!closes.length) clockEl.innerHTML = `<span class="muted">Between windows, the next one opens shortly</span>`;
  else {
    const at = new Date(closes[0]).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    clockEl.innerHTML = `<span class="muted">Window closes in</span> <b>${clock(secondsLeft(closes[0]))}</b> <span class="muted">· ${at}</span>`;
  }
  document.querySelectorAll(".market").forEach((card) => {
    const market = state.markets.find((m) => m.series === card.dataset.series);
    if (!market) return;
    const status = cardStatus(market);
    card.querySelector("[data-status]").innerHTML = statusHTML(status);
    card.classList.toggle("hot", Boolean(status.hot));
  });
}

$("#markets").addEventListener("click", async (e) => {
  const action = e.target.closest("[data-action]")?.dataset.action;
  const series = e.target.closest(".market")?.dataset.series;
  if (!action || !series || action === "toggle") return;
  const market = state.markets.find((m) => m.series === series);
  if (action === "edit") openStrategyDialog([series], market.strategy, market.name);
  if (action === "resume") {
    const ok = await confirmAction(`Resume buying ${market.name}?`, RESUME_WARNING, "Resume");
    if (ok) setState(await api("PUT", `/api/markets/${encodeURIComponent(series)}`, { enabled: true }));
  }
  if (action === "remove") {
    const ok = await confirmAction(`Remove ${market.name}?`, "The bot stops trading it. Its trade history is kept.", "Remove");
    if (ok) setState(await api("DELETE", `/api/markets/${encodeURIComponent(series)}`));
  }
});

$("#markets").addEventListener("change", async (e) => {
  if (e.target.dataset.action !== "toggle") return;
  const series = e.target.closest(".market").dataset.series;
  try {
    setState(await api("PUT", `/api/markets/${encodeURIComponent(series)}`, { enabled: e.target.checked }));
  } catch (err) {
    toast(err.message, true);
  }
});

function renderPerformance() {
  const rows = state.markets.map((m) => {
    const s = m.stats;
    const settled = s.wins + s.losses;
    return `<tr>
      <td>${esc(m.name)}</td>
      <td class="num">${s.trades}</td>
      <td class="num">${s.wins}</td>
      <td class="num">${s.losses}</td>
      <td class="num">${settled ? pct(s.wins / settled) : "—"}</td>
      <td class="num">${s.pending}</td>
      <td class="num">${money(s.cost, false)}</td>
      <td class="num ${pnlClass(s.pnl)}">${money(s.pnl)}</td>
    </tr>`;
  });
  $("#perf-body").innerHTML = rows.join("") || `<tr><td colspan="8" class="muted">Add a market to see its performance.</td></tr>`;
}

function renderLog() {
  $("#log").innerHTML = state.log.map((entry) => `
    <li><time>${new Date(entry.time).toLocaleTimeString()}</time>
    <span class="${esc(entry.level)}">${entry.series ? `<b>${esc(entry.series)}</b> ` : ""}${esc(entry.message)}</span></li>`).join("")
    || `<li class="muted">Nothing yet.</li>`;
}

function renderTradeFilter() {
  const select = $("#trade-filter");
  const current = select.value;
  const options = [["", "All markets"], ...state.markets.map((m) => [m.series, m.name])];
  const html = options.map(([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
  if (select.dataset.html !== html) {
    select.innerHTML = html;
    select.dataset.html = html;
    select.value = options.some(([v]) => v === current) ? current : "";
  }
}

async function refreshTrades() {
  const series = $("#trade-filter").value;
  try {
    const trades = await api("GET", `/api/trades${series ? `?series=${encodeURIComponent(series)}` : ""}`);
    const names = Object.fromEntries((state?.markets ?? []).map((m) => [m.series, m.name]));
    $("#trades-body").innerHTML = trades.map((t) => {
      const filled = num(t.filled_quantity);
      const qty = filled && filled !== num(t.quantity) ? `${filled}/${num(t.quantity)}` : num(t.quantity);
      return `<tr title="${esc(t.note ?? t.event_symbol)}">
        <td>${new Date(t.placed_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</td>
        <td>${esc(names[t.series] ?? t.series)}</td>
        <td>${esc(t.side)}</td>
        <td class="num">${qty}</td>
        <td class="num">${cents(num(t.avg_price) > 0 ? t.avg_price : t.limit_price)}</td>
        <td class="num">${t.seconds_left ?? "—"}s</td>
        <td><span class="pill ${esc(t.result)}">${esc(t.result.toLowerCase())}</span>${num(t.sold_quantity) > 0 && t.result !== "SOLD"
          ? ` <span class="muted small">${num(t.sold_quantity)} sold early</span>` : ""}${t.exit_reason && num(t.sold_quantity) > 0
          ? ` <span class="muted small">(${esc(EXIT_LABELS[t.exit_reason] ?? t.exit_reason)})</span>` : ""}</td>
        <td class="num ${pnlClass(t.pnl)}">${t.pnl === null ? "—" : money(t.pnl)}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="8" class="muted">No trades yet.</td></tr>`;
  } catch {
    /* the state poll reports connection problems */
  }
}
$("#trade-filter").addEventListener("change", refreshTrades);

// --- top bar actions ------------------------------------------------------------

const RESUME_WARNING = "If you resume with the same limit today, the bot stops again before any buy that could pass it. "
  + "To trade more today, raise the limit first.";

$("#engine-btn").addEventListener("click", async () => {
  const halted = state.running && Boolean(state.loss_limit.halted_at);
  const running = halted || !state.running;
  if (halted) {
    if (!(await confirmAction("Resume buying?", RESUME_WARNING, "Resume"))) return;
  } else if (running) {
    const enabled = state.markets.filter((m) => m.enabled).length;
    const ok = await confirmAction(
      "Start trading?",
      `The bot will place real orders on account ${state.account_id} for ${enabled} enabled market${enabled === 1 ? "" : "s"}: buys as soon as a market's rule matches, and early sells where a rule sets a take-profit or stop-loss. You can lose the full amount you pay for each contract.`,
      "Start",
    );
    if (!ok) return;
  }
  try {
    setState(await api("POST", "/api/engine", { running }));
  } catch (err) {
    toast(err.message, true);
  }
});

$("#disconnect-btn").addEventListener("click", async () => {
  const ok = await confirmAction(
    "Disconnect?",
    "This ends your session: the bot stops, unfilled orders are cancelled, and your markets, rules and results here are cleared. Contracts you already bought stay in your Public account.",
    "Disconnect",
  );
  if (!ok) return;
  await endSession();
  showOnboarding("welcome");
});

// --- strategy dialog ------------------------------------------------------------

const strategyForm = $("#strategy-form");
strategyForm.querySelector(".strategy-fields").innerHTML = strategyFields();

function openStrategyDialog(seriesList, strategy, label) {
  editingSeries = seriesList;
  $("#strategy-title").textContent = seriesList.length > 1 ? "Edit all strategies" : `Edit strategy — ${label}`;
  $("#strategy-subtitle").textContent = seriesList.length > 1
    ? `Saves the same strategy to all ${seriesList.length} of your markets. You can still edit each one afterwards.`
    : "Applies to every 15-minute window of this market, at most one trade per window.";
  fillStrategy(strategyForm, strategy);
  $("#strategy-error").textContent = "";
  updatePreview();
  $("#strategy-dialog").showModal();
}

function updatePreview() {
  $("#strategy-preview").innerHTML = strategyPreview(readStrategy(strategyForm));
}
strategyForm.addEventListener("input", updatePreview);

strategyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const strategy = readStrategy(strategyForm);
  const problem = strategyProblem(strategy);
  if (problem) return ($("#strategy-error").textContent = problem);
  try {
    const next = editingSeries.length === 1
      ? await api("PUT", `/api/markets/${encodeURIComponent(editingSeries[0])}`, { strategy })
      : await api("POST", "/api/markets/strategy", { series: editingSeries, strategy });
    setState(next);
    $("#strategy-dialog").close();
    toast("Strategy saved");
  } catch (err) {
    $("#strategy-error").textContent = err.message;
  }
});

$("#edit-all-btn").addEventListener("click", () => {
  if (!state.markets.length) return toast("Add a market first");
  openStrategyDialog(state.markets.map((m) => m.series), state.markets[0].strategy);
});

document.querySelectorAll("dialog [data-close]").forEach((btn) => btn.addEventListener("click", () => btn.closest("dialog").close()));

// --- daily loss limit dialog ----------------------------------------------------------

const limitForm = $("#limit-form");

function openLimitDialog() {
  limitForm.elements.daily_loss_limit.value = num(state.loss_limit.limit) ?? "";
  $("#limit-error").textContent = "";
  $("#limit-dialog").showModal();
}

limitForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const limit = readLimit(limitForm.elements.daily_loss_limit);
  const problem = limitProblem(limit, "The daily loss limit");
  if (problem) return ($("#limit-error").textContent = problem);
  try {
    setState(await api("PUT", "/api/loss-limit", { daily_loss_limit: limit }));
    $("#limit-dialog").close();
    toast(limit === null ? "Daily loss limit turned off" : "Daily loss limit saved");
  } catch (err) {
    $("#limit-error").textContent = err.message;
  }
});

// --- market browser -----------------------------------------------------------------

$("#add-btn").addEventListener("click", async () => {
  $("#add-dialog").showModal();
  $("#market-search").value = "";
  $("#available-list").innerHTML = `<p class="muted">Loading 15-minute markets from Public…</p>`;
  try {
    available = await api("GET", "/api/markets/available");
    renderAvailable();
    $("#market-search").focus();
  } catch (err) {
    $("#available-list").innerHTML = `<p class="error">${esc(err.message)}</p>`;
  }
});

function renderAvailable() {
  const q = $("#market-search").value.trim().toLowerCase();
  const selected = new Set(state.markets.map((m) => m.series));
  const matches = available.filter((m) => !q || `${m.name} ${m.series} ${m.category} ${m.subcategories.join(" ")}`.toLowerCase().includes(q));
  const groups = {};
  matches.forEach((m) => (groups[m.category || "Other"] ??= []).push(m));
  $("#available-list").innerHTML = Object.entries(groups).map(([category, markets]) => `
    <h3>${esc(category)}</h3>
    ${markets.map((m) => `
      <div class="avail">
        <div>
          <div>${esc(m.name)}</div>
          <div class="meta">${esc(m.series)} · ${m.close_time ? `current window closes ${new Date(m.close_time).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "no open window right now"}${m.volume ? ` · vol ${Math.round(num(m.volume)).toLocaleString()}` : ""}</div>
        </div>
        ${selected.has(m.series)
          ? `<span class="muted small">Added</span>`
          : `<button class="btn" type="button" data-add="${esc(m.series)}">Add</button>`}
      </div>`).join("")}`).join("") || `<p class="muted">No 15-minute markets match.</p>`;
}

$("#market-search").addEventListener("input", renderAvailable);
$("#available-list").addEventListener("click", async (e) => {
  const series = e.target.closest("[data-add]")?.dataset.add;
  if (!series) return;
  try {
    setState(await api("POST", "/api/markets", { series }));
    renderAvailable();
  } catch (err) {
    toast(err.message, true);
  }
});

boot().catch((err) => toast(err.message, true));
