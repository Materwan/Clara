// Administration (for users flagged administrator): users, the server, what Clara knows about people, a console.

import { api } from "./api.js";
import {
  ago, clear, confirmDialog, dateTime, duration, h, openDialog, popupMenu, promptDialog, secretDialog, toast,
} from "./ui.js";

const TABS = [["users", "Users"], ["server", "Server"], ["people", "People & memory"], ["console", "Console"]];

export function mountAdmin(container, me, tab = "users") {
  const body = h("div", {});
  let current = tab;
  let timer = null;

  const tabs = h("div", { class: "subtabs", role: "tablist" });
  const show = (name) => {
    current = name;
    location.hash = `#/admin/${name}`;
    clearInterval(timer);
    timer = null;
    clear(tabs).append(...TABS.map(([id, label]) =>
      h("button", { role: "tab", class: id === current ? "active" : "", "aria-selected": id === current, onclick: () => show(id) }, label)));
    clear(body);
    ({ users, server, people, console: consoleTab })[current](body, me, (interval) => { timer = interval; });
  };

  container.append(h("div", { class: "container wide" }, h("h2", {}, "Administration"), tabs, body));
  show(TABS.some(([id]) => id === tab) ? tab : "users");
  return { destroy() { clearInterval(timer); } };
}

const fail = (error) => toast(error.detail || String(error), true);

/** Run a server console command (`/provider cloud`...) and give its text. */
async function command(line) {
  return (await api.post("/v1/admin/command", { line })).output;
}

// ---- users ------------------------------------------------------------------------------------------------

function users(box, me) {
  const list = h("div", { class: "table-wrap" });

  async function load() {
    let found;
    try { found = (await api.get("/v1/admin/users")).users; } catch (error) { return fail(error); }
    clear(list).append(h("table", { class: "grid" },
      h("thead", {}, h("tr", {}, ["User", "Role", "Person", "Devices", "Last sign-in", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, found.map(row))));
  }

  function row(user) {
    const more = h("button", { class: "icon", "aria-label": `Actions for ${user.name}` }, "⋯");
    more.onclick = (event) => {
      event.stopPropagation();
      popupMenu(more, [
        { label: "Generate a new password", run: () => reset(user, true) },
        { label: "Set a password…", run: () => reset(user, false) },
        { label: user.is_admin ? "Remove administrator rights" : "Make administrator", run: () => edit(user, { admin: !user.is_admin }) },
        { label: user.disabled ? "Enable" : "Disable", run: () => edit(user, { disabled: !user.disabled }) },
        { label: "Sign out everywhere", run: () => signOut(user) },
        { label: "Remove user", danger: true, run: () => remove(user) },
      ]);
    };
    return h("tr", {},
      h("td", {}, h("strong", {}, user.name), user.name === me.name && h("span", { class: "muted" }, " (you)")),
      h("td", {}, user.disabled ? h("span", { class: "badge off" }, "disabled") : user.is_admin ? h("span", { class: "badge admin" }, "administrator") : h("span", { class: "badge" }, "user")),
      h("td", {}, user.person ? `${user.person.name} (#${user.person.id})` : "—"),
      h("td", { title: user.surfaces.join(", ") }, String(user.sessions)),
      h("td", { title: dateTime(user.last_login_at) }, ago(user.last_login_at)),
      h("td", {}, more));
  }

  async function edit(user, change) {
    try { await api.patch(`/v1/admin/users/${encodeURIComponent(user.name)}`, change); toast("Done."); load(); } catch (error) { fail(error); }
  }

  async function reset(user, generate) {
    let change;
    if (generate) {
      if (!await confirmDialog("New password", `Generate a new password for ${user.name}? They are signed out everywhere.`, "Generate")) return;
      change = { generate_password: true };
    } else {
      const value = await promptDialog(`Password for ${user.name}`, "New password (10 characters or more)", "", "Set", { type: "password", hint: "They are signed out everywhere." });
      if (!value) return;
      change = { password: value };
    }
    try {
      const done = await api.patch(`/v1/admin/users/${encodeURIComponent(user.name)}`, change);
      if (generate) await secretDialog(`Password for ${user.name}`, "Give it to them; they can change it on the Account page.", done.password);
      else toast("Password set.");
      load();
    } catch (error) { fail(error); }
  }

  async function signOut(user) {
    try {
      const done = await api.post(`/v1/admin/users/${encodeURIComponent(user.name)}/sign-out`, {});
      toast(`${done.signed_out} device(s) signed out.`);
      load();
    } catch (error) { fail(error); }
  }

  async function remove(user) {
    if (!await confirmDialog("Remove user", `${user.name} will no longer be able to log in. Their memories and conversations are kept (erase them under People & memory).`, "Remove", true)) return;
    try { await api.delete(`/v1/admin/users/${encodeURIComponent(user.name)}`); toast("User removed."); load(); } catch (error) { fail(error); }
  }

  async function add() {
    const result = await openDialog((close) => {
      const name = h("input", { type: "text", autocomplete: "off", required: true, pattern: "[a-zA-Z0-9][a-zA-Z0-9_.\\-]{0,31}", autofocus: true });
      const password = h("input", { type: "text", autocomplete: "off", placeholder: "leave empty to generate one" });
      const admin = h("input", { type: "checkbox" });
      return h("form", { onsubmit: (event) => { event.preventDefault(); close({ name: name.value, password: password.value, admin: admin.checked }); } },
        h("h3", {}, "Add a user"),
        h("div", { class: "stack" },
          h("label", { class: "field" }, "User name (letters, digits, . _ -)", name),
          h("label", { class: "field" }, "Password (10 characters or more)", password),
          h("label", { class: "check" }, admin, "Administrator")),
        h("p", { class: "muted small" }, "If Clara already knows an account with this name (cli:name, app:name…), the user takes it over with its memories."),
        h("div", { class: "actions" }, h("button", { type: "button", onclick: () => close(null) }, "Cancel"), h("button", { class: "primary", type: "submit" }, "Create")));
    });
    if (!result) return;
    try {
      const done = await api.post("/v1/admin/users", { name: result.name, password: result.password || null, admin: result.admin });
      await secretDialog(`User ${done.user.name} created`, "Give them this password; they can change it on the Account page.", done.password);
      load();
    } catch (error) { fail(error); }
  }

  box.append(h("div", { class: "row", style: undefined }, h("p", { class: "muted grow" }, "People who can sign in with a password."), h("button", { class: "primary", onclick: add }, "+ Add user")), list);
  load();
}

// ---- server -----------------------------------------------------------------------------------------------

function server(box, me, setTimer) {
  const stats = h("div", { class: "stats" });
  const controls = h("div", { class: "card stack" });
  box.append(stats, controls);

  const stat = (label, value) => h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value));

  async function load() {
    let status;
    try { status = await api.get("/v1/admin/status"); } catch { return; }
    clear(stats).append(
      stat("Provider", `${status.provider.label} (${status.provider.id})`),
      stat("Model", status.model),
      stat("Running", status.stopping ? "stopping…" : duration(status.uptime_seconds)),
      stat("Turns", `${status.turns.running} running · ${status.turns.since_start} since start`),
      stat("Tokens", `${status.tokens.prompt.toLocaleString()} in · ${status.tokens.completion.toLocaleString()} out`),
      stat("Memory", `${status.people} people · ${status.facts} facts`),
      stat("Listening", status.listen),
      stat("Tailscale", status.tailscale.mode === "off" ? "off" : status.tailscale.url || `unavailable: ${status.tailscale.problem || "starting…"}`));
    drawControls(status);
  }

  let drawn = "";
  async function drawControls(status) {
    const key = status.provider.id + status.model;
    if (key === drawn) return;
    drawn = key;
    let models = { models: [], model: status.model };
    try { models = await api.get("/v1/admin/models"); } catch { /* the select stays empty */ }
    const provider = h("select", { "aria-label": "Provider", onchange: async () => { await run(`/provider ${provider.value}`); } },
      status.providers.map((p) => h("option", { value: p.id, selected: p.id === status.provider.id, disabled: !p.usable }, `${p.label} (${p.id})${p.usable ? "" : " — no key"}`)));
    const names = models.models.includes(status.model) ? models.models : [status.model, ...models.models];
    const model = h("select", { "aria-label": "Model", onchange: async () => { await run(`/model ${model.value}`); } },
      names.map((name) => h("option", { value: name, selected: name === status.model }, name)));
    clear(controls).append(
      h("h3", {}, "Language model"),
      h("p", { class: "muted small" }, "Changing these switches every client at once, without a restart. Everybody is notified."),
      h("div", { class: "row wrap" }, h("label", { class: "field" }, "Provider", provider), h("label", { class: "field" }, "Model", model)),
      models.error && h("p", { class: "error small" }, models.error),
      h("hr", {}),
      h("div", {}, h("button", { class: "danger", onclick: stop }, "Stop the server…"),
        h("p", { class: "muted small" }, "Waits for the running answers to finish, tells every client, then exits. If a service manager restarts it, it comes back.")));
  }

  async function run(line) {
    try {
      const output = await command(line);
      toast(output.split("\n")[0]);
    } catch (error) { fail(error); }
    drawn = "";
    load();
  }

  async function stop() {
    if (!await confirmDialog("Stop the server", "Running answers finish first; new questions are refused. Nobody can use Clara until it is started again.", "Stop", true)) return;
    try { toast((await command("/stop")).split("\n")[0]); } catch (error) { fail(error); }
  }

  load();
  setTimer(setInterval(load, 5000));
}

// ---- people and memory -------------------------------------------------------------------------------------

function people(box) {
  const listBox = h("div", { class: "card" });
  const detail = h("div", {});
  box.append(h("div", { class: "people" }, listBox, detail));
  let selected = null;

  async function load() {
    let found;
    try { found = (await api.get("/v1/admin/people")).people; } catch (error) { return fail(error); }
    clear(listBox).append(h("h3", {}, "People"),
      ...found.map((p) => h("div", { class: "person" + (p.id === selected ? " active" : ""), tabindex: 0, onclick: () => { selected = p.id; load(); show(p); }, onkeydown: (e) => { if (e.key === "Enter") { selected = p.id; load(); show(p); } } },
        h("strong", {}, p.name), " ", p.user && h("span", { class: "badge admin" }, "login"),
        h("div", { class: "muted small" }, `${p.facts} fact(s) · ${p.accounts.join(", ") || "no account"}`))));
    if (selected !== null) {
      const again = found.find((p) => p.id === selected);
      if (again && !detail.firstChild) show(again);
    }
  }

  async function show(person) {
    let body;
    try { body = await api.get(`/v1/admin/people/${person.id}/facts`); } catch (error) { return fail(error); }
    const text = h("input", { type: "text", placeholder: "Add a fact", maxLength: 300, "aria-label": "New fact" });
    clear(detail).append(h("div", { class: "card" },
      h("h3", {}, person.name, " ", h("span", { class: "muted small" }, `#${person.id}`)),
      h("p", { class: "muted small" }, "Accounts: " + (person.accounts.join(", ") || "none")),
      h("ul", { class: "facts" }, body.facts.length ? body.facts.map((fact) => h("li", {}, h("span", { class: "grow" }, fact.text),
        h("button", { class: "ghost icon danger", "aria-label": "Delete fact", onclick: async () => {
          try { await api.delete(`/v1/admin/people/${person.id}/facts/${fact.id}`); show(person); load(); } catch (error) { fail(error); }
        } }, "✕"))) : h("li", { class: "muted" }, "No facts.")),
      h("form", { class: "row", onsubmit: async (event) => {
        event.preventDefault();
        if (!text.value.trim()) return;
        try { await api.post(`/v1/admin/people/${person.id}/facts`, { text: text.value }); show(person); load(); } catch (error) { fail(error); }
      } }, h("div", { class: "grow row" }, text), h("button", { type: "submit" }, "Add")),
      h("hr", {}),
      h("div", { class: "row wrap" },
        h("button", { onclick: () => link(person) }, "Link an account…"),
        h("button", { class: "danger", onclick: () => erase(person) }, "Erase this person…"))));
  }

  async function link(person) {
    const value = await promptDialog(`Link an account to ${person.name}`, "Account (surface:user, e.g. discord:1234)", "", "Link", {
      hint: "If that account already has its own memories they are merged into this person, which cannot be undone." });
    if (!value) return;
    try {
      toast((await command(`/link ${value.trim()} ${person.id}`)).split("\n")[0]);
      selected = person.id;
      load();
    } catch (error) { fail(error); }
  }

  async function erase(person) {
    let found;
    try { found = await api.get(`/v1/admin/people/${person.id}/footprint`); } catch (error) { return fail(error); }
    const detailText = `${found.accounts} account(s), ${found.facts} fact(s), ${found.messages} message(s) in ${found.conversations} conversation(s)`;
    if (!await confirmDialog(`Erase ${person.name}?`, `This erases ${detailText}, and their login if any. There is no undo. (The traffic log is not erased.)`, "Erase for good", true)) return;
    try {
      toast((await command(`/forget-person ${person.id} confirm`)).split("\n")[0]);
      selected = null;
      clear(detail);
      load();
    } catch (error) { fail(error); }
  }

  load();
}

// ---- console -----------------------------------------------------------------------------------------------

function consoleTab(box) {
  const out = h("div", { class: "console-out", role: "log", tabindex: 0 });
  const input = h("input", { type: "text", class: "grow", placeholder: "/help", autocomplete: "off", spellcheck: false, list: "console-commands", "aria-label": "Command" });
  const datalist = h("datalist", { id: "console-commands" });
  const history = [];
  let at = 0;

  const print = (text, kind = "") => { out.append(h("div", { class: kind }, text)); out.scrollTop = out.scrollHeight; };

  async function run(event) {
    event.preventDefault();
    const line = input.value.trim();
    if (!line) return;
    history.push(line);
    at = history.length;
    input.value = "";
    print("> " + line, "cmd");
    try {
      const result = await api.post("/v1/admin/command", { line });
      if (result.output) print(result.output, result.output.startsWith("!") ? "err" : "");
      if (result.quit) print("(this console closes only the page; use /stop to stop the server)");
    } catch (error) { print(error.detail || String(error), "err"); }
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowUp" && history.length) { at = Math.max(0, at - 1); input.value = history[at]; event.preventDefault(); }
    if (event.key === "ArrowDown" && history.length) { at = Math.min(history.length, at + 1); input.value = history[at] || ""; event.preventDefault(); }
  });

  box.append(
    h("p", { class: "muted" }, "The same commands as the server's own console and clara-admin. Type /help. Passwords made by /user are shown here only once."),
    out, h("form", { class: "row", onsubmit: run }, input, datalist, h("button", { class: "primary", type: "submit" }, "Run")));
  api.get("/v1/admin/commands").then((commands) => {
    datalist.append(...commands.map((c) => h("option", { value: "/" + c.name }, c.summary)));
  }).catch(() => {});
  print("Type /help for the commands.");
  input.focus();
}
