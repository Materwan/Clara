// The chat page: conversations at the side, the conversation, documents, the context meter.

import { ApiError, api, conversationPath, streamChat } from "./api.js";
import { DocumentError, MAX_TOTAL_CHARS, compose, readDocument, splitMessage, totalChars } from "./documents.js";
import { renderMarkdown } from "./markdown.js";
import { clear, confirmDialog, h, parseDate, popupMenu, promptDialog, randomId, toast } from "./ui.js";

const SURFACE = "web";
const INSTRUCTIONS =
  "You are talking through Clara's web site, in a chat window. Markdown is displayed, but keep answers " +
  "short and conversational. The user can attach files (PDF, code, Markdown, text): their content comes in the " +
  'message, each inside <document name="..." type="..."> tags. Refer to them by name.';

export function mountChat(container, user) {
  const who = { surface: SURFACE, user_id: user.name };
  const lastKey = `clara.last.${user.name}`;
  const state = {
    list: [], query: "", current: null, messages: [], summary: "", earlier: false,
    busy: false, abort: null, docs: [], context: null, live: null, stick: true,
  };

  // ---- the page ---------------------------------------------------------------------------------------
  const search = h("input", { type: "search", placeholder: "Search conversations", "aria-label": "Search conversations" });
  const listBox = h("div", { class: "convos", role: "list" });
  const sidebar = h("aside", { class: "sidebar" },
    h("div", { class: "sidebar-head" }, h("button", { class: "primary", onclick: () => { newChat(); closeSidebar(); } }, "+ New chat"), search),
    listBox);

  const title = h("div", { class: "title grow" }, "New chat");
  const bar = h("i", {});
  const meter = h("div", { class: "meter", role: "img", "aria-label": "Context used" }, bar);
  const compactButton = h("button", { class: "ghost small", onclick: compact, title: "Replace the older messages by a summary" }, "Summarise");
  const messagesInner = h("div", { class: "messages-inner" });
  const messagesBox = h("div", { class: "messages", role: "log", "aria-live": "polite" }, messagesInner);
  const input = h("textarea", { rows: 1, placeholder: "Message Clara…  (Enter to send, Shift+Enter for a new line)", "aria-label": "Message" });
  const chips = h("div", { class: "chips" });
  const picker = h("input", { type: "file", multiple: true, hidden: true, onchange: () => { addFiles([...picker.files]); picker.value = ""; } });
  const attach = h("button", { class: "ghost icon", title: "Attach documents (PDF, code, text)", "aria-label": "Attach documents", onclick: () => picker.click() }, "📎");
  const sendButton = h("button", { class: "primary", onclick: send }, "Send");
  const composer = h("div", { class: "composer" },
    h("div", { class: "composer-inner" }, chips, input, h("div", { class: "composer-bar" }, attach, picker, h("span", { class: "grow muted small", id: "docinfo" }), sendButton)));
  const menuButton = h("button", { class: "menu-btn ghost icon", "aria-label": "Conversations", onclick: () => root.classList.toggle("open") }, "☰");
  const main = h("section", { class: "main" },
    h("div", { class: "chat-head" }, menuButton, title, meter, compactButton), messagesBox, composer);
  const root = h("div", { class: "chat" }, sidebar, main);
  container.append(root);

  const closeSidebar = () => root.classList.remove("open");

  // ---- the list of conversations ----------------------------------------------------------------------
  let searchTimer = null;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.query = search.value.trim(); loadList(); }, 250);
  });

  async function loadList() {
    try {
      state.list = (await api.get("/v1/conversations", { ...who, q: state.query })).conversations;
    } catch (error) {
      if (error.status !== 401) toast(error.detail || String(error), true);
      return;
    }
    renderList();
  }

  function groupOf(info) {
    if (info.pinned) return "Pinned";
    const date = parseDate(info.updated_at);
    if (!date) return "Older";
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.floor((today - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
    return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days < 7 ? "Previous 7 days" : "Older";
  }

  function renderList() {
    clear(listBox);
    if (!state.list.length) {
      listBox.append(h("p", { class: "muted small", style: undefined }, state.query ? "Nothing found." : "No conversation yet."));
      return;
    }
    const order = ["Pinned", "Today", "Yesterday", "Previous 7 days", "Older"];
    const groups = new Map(order.map((name) => [name, []]));
    for (const info of state.list) groups.get(groupOf(info)).push(info);
    for (const [name, items] of groups) {
      if (!items.length) continue;
      listBox.append(h("div", { class: "group-title" }, name));
      for (const info of items) listBox.append(convoRow(info));
    }
  }

  function convoRow(info) {
    const label = info.title || (info.preview ? splitMessage(info.preview).text || info.preview : "") || "New chat";
    const more = h("button", { class: "ghost icon more", "aria-label": "Conversation menu", title: "More",
      onclick: (event) => {
        event.stopPropagation();
        popupMenu(more, [
          { label: "Rename", run: () => rename(info) },
          { label: info.pinned ? "Unpin" : "Pin", run: () => pin(info) },
          { label: "Delete", danger: true, run: () => remove(info) },
        ]);
      } }, "⋯");
    return h("div", {
      class: "convo" + (info.id === state.current ? " active" : ""), role: "listitem", tabindex: 0,
      onclick: () => { open(info.id); closeSidebar(); },
      onkeydown: (event) => { if (event.key === "Enter") { open(info.id); closeSidebar(); } },
    }, info.pinned && h("span", { class: "pin" }, "📌"), h("span", { class: "title", title: label }, label), more);
  }

  async function rename(info) {
    const value = await promptDialog("Rename conversation", "Title", info.title || "", "Rename", { hint: "Leave it empty to let Clara choose a title." });
    if (value === null) return;
    await safely(async () => {
      await api.patch(conversationPath(info.id), { ...who, title: value.trim() });
      if (!value.trim()) await api.post(conversationPath(info.id) + "/title", who).catch(() => {});
      await loadList();
      if (info.id === state.current) setTitle();
    });
  }

  async function pin(info) {
    await safely(async () => { await api.patch(conversationPath(info.id), { ...who, pinned: !info.pinned }); await loadList(); });
  }

  async function remove(info) {
    const label = info.title || "this conversation";
    if (!await confirmDialog("Delete conversation", `Delete “${label}”? What Clara knows about you is kept.`, "Delete", true)) return;
    await safely(async () => {
      await api.delete(conversationPath(info.id), who);
      if (info.id === state.current) newChat();
      await loadList();
    });
  }

  async function safely(action) {
    try {
      await action();
    } catch (error) {
      if (error.status !== 401) toast(error.detail || String(error), true);
    }
  }

  // ---- the conversation ------------------------------------------------------------------------------
  function setTitle() {
    const info = state.list.find((item) => item.id === state.current);
    title.textContent = info?.title || "New chat";
  }

  function newChat() {
    if (state.busy) return toast("Wait for Clara to finish, or stop the answer first.");
    state.current = `${SURFACE}:${user.name}:${randomId()}`;
    state.messages = [];
    state.summary = "";
    state.earlier = false;
    state.context = null;
    state.live = null;
    remember();
    renderAll();
    input.focus();
  }

  function remember() {
    try { localStorage.setItem(lastKey, state.current); } catch { /* private mode */ }
  }

  async function open(id) {
    if (state.busy && id !== state.current) return toast("Wait for Clara to finish, or stop the answer first.");
    state.current = id;
    state.live = null;
    remember();
    try {
      const body = await api.get(conversationPath(id) + "/messages", who);
      state.messages = body.messages.map((m) => ({ role: m.role, content: m.content }));
      state.summary = body.summary || "";
      state.earlier = Boolean(body.earlier);
    } catch (error) {
      if (error.status === 404 || error.status === 403) {
        state.messages = []; state.summary = ""; state.earlier = false;
      } else {
        return toast(error.detail || String(error), true);
      }
    }
    renderAll();
    refreshContext();
  }

  async function refreshContext() {
    const id = state.current;
    try {
      const info = await api.get(conversationPath(id));
      if (id === state.current) { state.context = info; renderMeter(); }
    } catch { /* a conversation that does not exist yet has no context */ }
  }

  function renderMeter() {
    const info = state.context;
    const percent = info ? Math.min(100, info.percent || 0) : 0;
    bar.style.width = percent + "%";
    meter.classList.toggle("hot", percent >= 80);
    meter.title = info ? `Context ${percent.toFixed(0)}% full (${(info.tokens || 0).toLocaleString()} of ${(info.window || 0).toLocaleString()} tokens)` : "Context";
    compactButton.hidden = !info || !(info.messages > 2);
  }

  function renderAll() {
    renderList();
    setTitle();
    renderMessages();
    renderMeter();
  }

  function messageNode(message) {
    if (message.role === "user") {
      const { text, names } = splitMessage(message.content);
      return h("div", { class: "msg user" },
        h("div", { class: "bubble" }, text, names.length > 0 && h("div", { class: "chips" }, names.map((name) => h("span", { class: "chip" }, "📎 " + name)))));
    }
    return h("div", { class: "msg assistant" + (message.failed ? " failed" : "") },
      h("div", { class: "bubble" }, message.content ? renderMarkdown(message.content) : null, message.failed && h("p", { class: "error" }, message.failed)));
  }

  function renderMessages() {
    clear(messagesInner);
    if (state.summary) messagesInner.append(h("div", { class: "summary" }, h("strong", {}, "Earlier in this conversation: "), state.summary));
    else if (state.earlier) messagesInner.append(h("div", { class: "summary" }, "Older messages are not shown."));
    if (!state.messages.length && !state.live) {
      messagesInner.append(h("div", { class: "empty" }, h("h2", {}, `Hello ${user.person?.name || user.name}`), h("p", {}, "How can I help?")));
    }
    for (const message of state.messages) messagesInner.append(messageNode(message));
    scrollDown(true);
  }

  function scrollDown(force) {
    if (force || state.stick) messagesBox.scrollTop = messagesBox.scrollHeight;
  }
  messagesBox.addEventListener("scroll", () => {
    state.stick = messagesBox.scrollHeight - messagesBox.scrollTop - messagesBox.clientHeight < 80;
  });

  // ---- sending ------------------------------------------------------------------------------------------
  function setBusy(busy) {
    state.busy = busy;
    sendButton.textContent = busy ? "Stop" : "Send";
    sendButton.classList.toggle("danger", busy);
    sendButton.classList.toggle("primary", !busy);
    input.disabled = false;
  }

  async function send() {
    if (state.busy) { state.abort?.abort(); return; }
    const text = input.value;
    if (!text.trim() && !state.docs.length) return;
    const message = compose(text, state.docs);
    const conversation = state.current;
    input.value = "";
    autosize();
    state.docs = [];
    renderChips();
    state.messages.push({ role: "user", content: message });
    const reply = { role: "assistant", content: "" };
    state.messages.push(reply);
    state.live = { reply, node: null };
    renderMessages();
    const bubble = messagesInner.lastElementChild.querySelector(".bubble");
    bubble.classList.add("typing");
    state.stick = true;
    state.abort = new AbortController();
    setBusy(true);
    let frame = 0;
    const paint = () => {
      frame = 0;
      clear(bubble).append(...(reply.content ? [renderMarkdown(reply.content)] : []));
      scrollDown();
    };
    let finished = false;
    try {
      const body = {
        ...who, user_name: user.person?.name || user.name, message, conversation, instructions: INSTRUCTIONS,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      };
      for await (const event of streamChat(body, state.abort.signal)) {
        if (event.type === "token") {
          reply.content += event.text;
          if (!frame) frame = requestAnimationFrame(paint);
        } else if (event.type === "compacted") toast("Older messages were summarised to make room.");
        else if (event.type === "warning") toast(event.message);
        else if (event.type === "error") { reply.failed = event.message; finished = true; }
        else if (event.type === "done") {
          if (event.reply) reply.content = event.reply;
          state.context = { ...state.context, ...event.context, messages: (state.context?.messages || 0) + 2 };
          finished = true;
        }
      }
    } catch (error) {
      if (error.name === "AbortError") reply.failed = "Stopped. (What was written is not kept by Clara.)";
      else if (error instanceof ApiError) reply.failed = error.status === 401 ? "" : error.detail;
      else reply.failed = String(error);
    }
    if (!finished && !reply.failed) reply.failed = "The answer was cut off.";
    cancelAnimationFrame(frame);
    state.live = null;
    state.abort = null;
    setBusy(false);
    if (conversation === state.current) {
      renderMessages();
      renderMeter();
    }
    input.focus();
    await loadList();
    if (finished && !reply.failed) titleIfNeeded(conversation);
  }

  async function titleIfNeeded(id) {
    const info = state.list.find((item) => item.id === id);
    if (!info || info.title) return;
    try {
      await api.post(conversationPath(id) + "/title", who);
      await loadList();
      if (id === state.current) setTitle();
    } catch { /* the preview stands in for a title */ }
  }

  async function compact() {
    if (state.busy) return;
    compactButton.disabled = true;
    try {
      const result = await api.post(conversationPath(state.current) + "/compact", {});
      toast(`Summarised: the context went from ${result.before_percent}% to ${result.after_percent}%.`);
      await open(state.current);
    } catch (error) {
      toast(error.detail || String(error), true);
    } finally {
      compactButton.disabled = false;
    }
  }

  // ---- the input box and documents ---------------------------------------------------------------------
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 220) + "px";
  }
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); }
  });
  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (files.length) { event.preventDefault(); addFiles(files); }
  });
  for (const type of ["dragenter", "dragover"]) composer.addEventListener(type, (event) => { event.preventDefault(); composer.classList.add("drop"); });
  for (const type of ["dragleave", "drop"]) composer.addEventListener(type, () => composer.classList.remove("drop"));
  composer.addEventListener("drop", (event) => { event.preventDefault(); addFiles([...event.dataTransfer.files]); });

  async function addFiles(files) {
    for (const file of files) {
      if (state.docs.some((doc) => doc.name === file.name)) { toast(`${file.name} is already attached.`); continue; }
      try {
        const doc = await readDocument(file);
        if (totalChars([...state.docs, doc]) > MAX_TOTAL_CHARS) {
          toast(`${file.name} does not fit: a message holds about ${MAX_TOTAL_CHARS.toLocaleString()} characters of documents.`, true);
          continue;
        }
        state.docs.push(doc);
        renderChips();
      } catch (error) {
        toast(error instanceof DocumentError ? error.message : String(error), true);
      }
    }
  }

  function renderChips() {
    clear(chips);
    for (const doc of state.docs) {
      chips.append(h("span", { class: "chip", title: doc.note }, "📎 " + doc.name,
        h("button", { class: "ghost", "aria-label": `Remove ${doc.name}`, onclick: () => { state.docs = state.docs.filter((d) => d !== doc); renderChips(); } }, "✕")));
    }
    const info = main.querySelector("#docinfo");
    info.textContent = state.docs.length ? `${totalChars(state.docs).toLocaleString()} / ${MAX_TOTAL_CHARS.toLocaleString()} characters of documents` : "";
  }

  // ---- start -------------------------------------------------------------------------------------------
  (async () => {
    await loadList();
    let last = null;
    try { last = localStorage.getItem(lastKey); } catch { /* private mode */ }
    const start = state.list.find((info) => info.id === last) || state.list[0];
    if (start) await open(start.id);
    else newChat();
  })();

  return {
    destroy() { state.abort?.abort(); clearTimeout(searchTimer); root.remove(); },
  };
}
