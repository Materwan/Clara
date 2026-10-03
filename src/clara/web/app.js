// Clara's web site: sign in, then a chat, what Clara remembers, your account, and (for administrators) the
// administration. Everything talks to the same HTTP API as the other clients, as the surface "web".

import { mountAccount, mountMemory } from "./account.js";
import { mountAdmin } from "./admin.js";
import { ApiError, api, events } from "./api.js";
import { mountChat } from "./chat.js";
import { clear, h, toast } from "./ui.js";

const app = document.getElementById("app");
let user = null; // who is signed in: the answer of /v1/auth/me
let page = null; // what is mounted: {destroy}
let healthTimer = null;

// ---- signing in ----------------------------------------------------------------------------------------

function showLogin(message = "") {
  stop();
  user = null;
  const name = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: false, required: true, autofocus: true });
  const password = h("input", { type: "password", autocomplete: "current-password", required: true });
  const note = h("p", { class: message ? "notice" : "muted small" }, message);
  const button = h("button", { class: "primary", type: "submit" }, "Sign in");
  const form = h("form", { class: "login stack", onsubmit: async (event) => {
    event.preventDefault();
    button.disabled = true;
    note.className = "muted small";
    note.textContent = "Signing in…";
    try {
      await api.post("/v1/auth/login", { username: name.value.trim(), password: password.value, surface: "web" }, { quiet401: true });
      password.value = "";
      await boot();
    } catch (error) {
      note.className = "error small";
      note.textContent = error.detail || String(error);
      button.disabled = false;
      password.select();
    }
  } },
  h("h1", {}, h("span", { class: "logo" }), "Clara"),
  h("p", { class: "muted" }, "Sign in to talk to Clara."),
  note,
  h("label", { class: "field" }, "User name", name),
  h("label", { class: "field" }, "Password", password),
  button,
  h("p", { class: "muted small" }, "Ask the person who runs this server for an account."));
  clear(app).append(h("div", { class: "login-wrap" }, form));
  name.focus();
}

async function signOut() {
  try { await api.post("/v1/auth/logout", {}); } catch { /* the cookie is dropped anyway */ }
  showLogin();
}

events.addEventListener("signed-out", () => { if (user) showLogin("Your session ended. Please sign in again."); });

// ---- the shell and the pages -----------------------------------------------------------------------------

const PAGES = { chat: "Chat", memory: "Memory", account: "Account", admin: "Admin" };

function shell() {
  const status = h("div", { class: "status", title: "Server status" }, h("span", { class: "dot" }), h("span", { class: "text" }, "…"));
  const tabs = h("nav", { class: "tabs", "aria-label": "Pages" },
    Object.entries(PAGES).filter(([id]) => id !== "admin" || user.is_admin).map(([id, label]) =>
      h("a", { href: `#/${id}`, "data-page": id }, label)));
  const body = h("main", { class: "page", id: "page" });
  clear(app).append(h("div", { class: "shell" },
    h("header", { class: "topbar" },
      h("a", { class: "brand", href: "#/chat" }, h("span", { class: "logo" }), "Clara"),
      tabs, h("div", { class: "grow" }), status,
      h("span", { class: "muted small", title: user.is_admin ? "Administrator" : "" }, user.name),
      h("button", { class: "ghost", onclick: signOut }, "Sign out")),
    body));
  healthTimer = setInterval(() => health(status), 20000);
  health(status);
  return body;
}

async function health(status) {
  const dot = status.querySelector(".dot");
  const text = status.querySelector(".text");
  try {
    const info = await (await fetch("/health", { cache: "no-store" })).json();
    dot.className = "dot up";
    text.textContent = `${info.model} · ${info.provider}`;
  } catch {
    dot.className = "dot down";
    text.textContent = "Clara is not running";
  }
}

function stop() {
  clearInterval(healthTimer);
  healthTimer = null;
  page?.destroy();
  page = null;
}

function route() {
  if (!user) return;
  const [, id = "chat", sub] = location.hash.split("/");
  const name = Object.hasOwn(PAGES, id) && (id !== "admin" || user.is_admin) ? id : "chat";
  for (const link of document.querySelectorAll(".tabs a")) link.classList.toggle("active", link.dataset.page === name);
  const body = document.getElementById("page");
  if (!body) return;
  page?.destroy();
  clear(body);
  page = name === "chat" ? mountChat(body, user)
    : name === "memory" ? mountMemory(body, user)
    : name === "account" ? mountAccount(body, user, signOut)
    : mountAdmin(body, user, sub);
}

window.addEventListener("hashchange", () => {
  // switching between the sub-tabs of the admin page sets the hash itself: do not rebuild the page for it
  if (location.hash.startsWith("#/admin/") && document.querySelector('.tabs a[data-page="admin"].active')) return;
  route();
});

// ---- start ------------------------------------------------------------------------------------------------------

async function boot() {
  try {
    user = await api.get("/v1/auth/me", undefined, { quiet401: true });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return showLogin();
    clear(app).append(h("div", { class: "login-wrap" }, h("div", { class: "login stack" },
      h("h1", {}, h("span", { class: "logo" }), "Clara"),
      h("p", { class: "error" }, error.detail || String(error)),
      h("button", { onclick: boot }, "Try again"))));
    return;
  }
  stop();
  shell();
  if (!location.hash) location.hash = "#/chat";
  route();
}

boot().catch((error) => toast(String(error), true));
