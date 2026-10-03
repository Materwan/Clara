// What Clara remembers about you, and your account: password, devices, linking accounts.

import { api } from "./api.js";
import { ago, clear, confirmDialog, dateTime, h, secretDialog, toast } from "./ui.js";

const who = (user) => ({ surface: "web", user_id: user.name });

// ---- memory ------------------------------------------------------------------------------------------------

export function mountMemory(container, user) {
  const list = h("ul", { class: "facts" });
  const filter = h("input", { type: "search", placeholder: "Filter", "aria-label": "Filter facts" });
  const text = h("input", { type: "text", placeholder: "Something Clara should remember about you", maxLength: 300, "aria-label": "New fact" });
  let facts = [];

  const draw = () => {
    clear(list);
    const needle = filter.value.trim().toLowerCase();
    const shown = facts.filter((fact) => fact.text.toLowerCase().includes(needle));
    if (!shown.length) list.append(h("li", { class: "muted" }, facts.length ? "No fact matches." : "Clara does not know anything about you yet."));
    for (const fact of shown) {
      list.append(h("li", {}, h("span", { class: "grow" }, fact.text),
        h("button", { class: "ghost icon danger", title: "Forget this", "aria-label": `Forget: ${fact.text}`, onclick: async () => {
          try {
            await api.delete(`/v1/memory/facts/${fact.id}`, who(user));
            facts = facts.filter((f) => f.id !== fact.id);
            draw();
          } catch (error) { toast(error.detail, true); }
        } }, "✕")));
    }
  };

  const add = async (event) => {
    event.preventDefault();
    const value = text.value.trim();
    if (!value) return;
    try {
      const body = await api.post("/v1/memory/facts", { ...who(user), text: value });
      text.value = "";
      if (body.stored === false) toast("Clara already knows that.");
      await load();
    } catch (error) { toast(error.detail, true); }
  };

  async function load() {
    try {
      facts = (await api.get("/v1/memory/facts", who(user))).facts;
    } catch (error) {
      if (error.status !== 404) toast(error.detail, true);
      facts = [];
    }
    draw();
  }

  container.append(h("div", { class: "container" },
    h("h2", {}, "What Clara remembers"),
    h("p", { class: "muted" }, "Clara saves facts about you during conversations and uses them in every chat, on all your devices. You can add or remove them here."),
    h("form", { class: "row", onsubmit: add }, h("div", { class: "grow row" }, text), h("button", { class: "primary", type: "submit" }, "Add")),
    h("div", { class: "card" }, h("div", { class: "row", style: undefined }, filter), list)));
  load();
  return { destroy() {} };
}

// ---- account -----------------------------------------------------------------------------------------------

export function mountAccount(container, user, onSignOut) {
  const page = h("div", { class: "container" });
  container.append(page);

  async function draw() {
    let me, devices;
    try {
      [me, devices] = await Promise.all([api.get("/v1/auth/me"), api.get("/v1/auth/sessions")]);
    } catch (error) {
      return void toast(error.detail, true);
    }
    clear(page);
    page.append(
      h("h2", {}, "Account"),
      profileCard(me),
      passwordCard(),
      devicesCard(devices.sessions),
      linkCard(me),
    );
  }

  const profileCard = (me) => h("div", { class: "card" },
    h("h3", {}, me.name, " ", me.is_admin && h("span", { class: "badge admin" }, "administrator")),
    h("p", { class: "muted" }, `Clara knows you as ${me.person?.name || me.name}. Your accounts: ${me.accounts.join(", ") || "none yet"}.`),
    h("button", { onclick: onSignOut }, "Sign out"));

  function passwordCard() {
    const current = h("input", { type: "password", autocomplete: "current-password", required: true });
    const next = h("input", { type: "password", autocomplete: "new-password", required: true, minLength: 10 });
    const again = h("input", { type: "password", autocomplete: "new-password", required: true });
    const note = h("p", { class: "small" });
    return h("form", { class: "card stack", onsubmit: async (event) => {
      event.preventDefault();
      note.className = "small";
      if (next.value !== again.value) { note.className = "small error"; note.textContent = "The two new passwords differ."; return; }
      try {
        const done = await api.post("/v1/auth/password", { current_password: current.value, new_password: next.value });
        current.value = next.value = again.value = "";
        toast(done.signed_out_elsewhere ? `Password changed; ${done.signed_out_elsewhere} other device(s) signed out.` : "Password changed.");
        draw();
      } catch (error) { note.className = "small error"; note.textContent = error.detail; }
    } },
    h("h3", {}, "Change password"),
    h("label", { class: "field" }, "Current password", current),
    h("label", { class: "field" }, "New password (10 characters or more)", next),
    h("label", { class: "field" }, "New password again", again),
    note,
    h("div", {}, h("button", { class: "primary", type: "submit" }, "Change password")));
  }

  function devicesCard(sessions) {
    const others = sessions.filter((s) => !s.current);
    return h("div", { class: "card" },
      h("h3", {}, "Your devices"),
      h("p", { class: "muted small" }, "Each place you are signed in. A device you do not use for 90 days is signed out."),
      h("div", { class: "table-wrap" }, h("table", { class: "grid" },
        h("thead", {}, h("tr", {}, ["Where", "Last used", "Signed in", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, sessions.map((s) => h("tr", {},
          h("td", {}, h("strong", {}, s.surface), " ", s.current && h("span", { class: "badge ok" }, "this device"), h("div", { class: "muted small" }, (s.device || "").slice(0, 60), s.address && ` · ${s.address}`)),
          h("td", { title: dateTime(s.last_used_at) }, ago(s.last_used_at)),
          h("td", {}, dateTime(s.created_at)),
          h("td", {}, !s.current && h("button", { onclick: async () => { await signOut(s.id); } }, "Sign out"))))))),
      others.length > 0 && h("p", {}, h("button", { class: "danger", onclick: async () => {
        if (await confirmDialog("Sign out other devices", `Sign out ${others.length} other device(s)?`, "Sign out", true)) for (const s of others) await signOut(s.id, true);
        draw();
      } }, "Sign out all other devices")));
  }

  async function signOut(id, quiet) {
    try { await api.delete(`/v1/auth/sessions/${id}`); } catch (error) { toast(error.detail, true); }
    if (!quiet) draw();
  }

  function linkCard(me) {
    const code = h("div", {});
    const surface = h("input", { type: "text", placeholder: "discord", pattern: "[a-z0-9_-]{1,32}", "aria-label": "Surface", required: true });
    const account = h("input", { type: "text", placeholder: "1234", "aria-label": "Account id", required: true });
    const secret = h("input", { type: "text", placeholder: "code", "aria-label": "Link code", required: true, autocomplete: "off" });
    return h("div", { class: "card stack" },
      h("h3", {}, "Link another account"),
      h("p", { class: "muted small" }, "Signing in already makes the web, the desktop app and the terminal one person. This is for accounts that do not log in with a password, such as Discord."),
      h("div", {}, h("strong", {}, "Add an account to you"),
        h("p", { class: "muted small" }, "On the other account's client ask for its link code (for example /linkcode), then type it here."),
        h("form", { class: "row wrap", onsubmit: async (event) => {
          event.preventDefault();
          try {
            const done = await api.post("/v1/accounts/link", {
              surface: surface.value.trim().toLowerCase(), user_id: account.value.trim(), code: secret.value.trim(),
              to_surface: "web", to_user_id: me.name,
            });
            toast(`Linked. Your accounts: ${done.accounts.join(", ")}`);
            surface.value = account.value = secret.value = "";
            draw();
          } catch (error) { toast(error.detail, true); }
        } }, surface, account, secret, h("button", { class: "primary", type: "submit" }, "Link"))),
      h("div", {}, h("strong", {}, "Let another account join you from its side"),
        h("p", { class: "muted small" }, `Get a code for ${me.name} on the web (valid 10 minutes, usable once), then give it to the other client: /link web ${me.name} <code>.`),
        h("button", { onclick: async () => {
          try {
            const done = await api.post("/v1/accounts/link-code", who(me));
            clear(code).append(h("div", { class: "secret-box" }, done.code), h("p", { class: "muted small" }, `Valid ${Math.round(done.expires_in / 60)} minutes.`));
          } catch (error) { toast(error.detail, true); }
        } }, "Get a link code"), code));
  }

  draw();
  return { destroy() {} };
}

export { secretDialog };
