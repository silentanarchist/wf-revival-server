// WF-Revival developer code browser (/#code). Sent by the server (Revival/CodeUi) only to accounts with the Developer
// switch, and run inside the site's page; uses the site's call() for every request. Not a public file: it lives in
// content/code/, outside content/files. Plan: claude/code-browser-plan.md.
(() => {
  if (window.codeBrowser) return;
  const LINE_H = 19, OVERSCAN = 80;
  const css = `
  #codeView { position: fixed; inset: 0; z-index: 50; display: flex; flex-direction: column; background: var(--bg); color: var(--text); }
  #codeView.hidden { display: none; }
  #codeView .cbBar { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; padding: 10px 14px; background: #0e1013; border-bottom: 2px solid var(--accent); }
  #codeView .cbBar h2 { margin: 0 8px 0 0; font: 600 16px/1 Georgia, serif; letter-spacing: .05em; text-transform: uppercase; color: var(--accent); }
  #codeView .cbBar input[type=search] { width: 260px; }
  #codeView .cbBar label { display: flex; gap: 4px; align-items: center; }
  #codeView .cbBar .grow { flex: 1; }
  #codeView .cbMain { flex: 1; display: grid; grid-template-columns: 300px 1fr 340px; min-height: 0; }
  #codeView .cbLeft, #codeView .cbRight { overflow: auto; background: var(--panel); border-right: 1px solid var(--line); min-height: 0; }
  #codeView .cbRight { border-right: 0; border-left: 1px solid var(--line); padding: 10px 12px; }
  #codeView .cbTabs { display: flex; gap: 2px; position: sticky; top: 0; background: var(--panel); border-bottom: 1px solid var(--line); z-index: 1; }
  #codeView .cbTabs button { border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: none; padding: 8px 10px; color: var(--dim); }
  #codeView .cbTabs button.on { color: var(--text); border-bottom-color: var(--accent); }
  #codeView .cbList { padding: 6px 0; font-size: 13px; }
  #codeView .cbList .dir { cursor: pointer; padding: 2px 8px; color: var(--dim); user-select: none; white-space: nowrap; }
  #codeView .cbList .dir:hover { color: var(--text); }
  #codeView .cbList .file, #codeView .cbList .hit { display: block; padding: 2px 8px; color: var(--text); text-decoration: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
  #codeView .cbList .file:hover, #codeView .cbList .hit:hover { background: var(--panel2); }
  #codeView .cbList .file.on { background: #2f3540; color: var(--accent); }
  #codeView .cbList .dim, #codeView .cbRight .dim { color: var(--dim); }
  #codeView .cbList h4, #codeView .cbRight h4 { margin: 10px 8px 4px; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--dim); }
  #codeView .cbRight h4 { margin: 14px 0 4px; }
  #codeView .cbList .kind { display: inline-block; min-width: 54px; font-size: 11px; color: var(--accent2); }
  #codeView .cbList .hit code { color: var(--dim); }
  #codeView .cbList mark, #codeView .cbCode mark { background: #6b5a1f; color: inherit; border-radius: 2px; }
  #codeView .cbCenter { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  #codeView .cbPath { padding: 6px 12px; font-size: 13px; color: var(--dim); border-bottom: 1px solid var(--line); background: var(--panel); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #codeView .cbScroll { flex: 1; overflow: auto; position: relative; min-height: 0; }
  #codeView .cbCode { position: relative; font: 13px/${LINE_H}px "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; }
  #codeView .cbCode .l { position: absolute; left: 0; right: 0; height: ${LINE_H}px; white-space: pre; padding-left: 64px; cursor: text; }
  #codeView .cbCode .l .n { position: absolute; left: 0; width: 54px; text-align: right; color: #5c6370; user-select: none; }
  #codeView .cbCode .l.cur { background: #262b33; }
  #codeView .cbCode .l.head { background: #1f242c; }
  #codeView .cbCode .l.flash { background: #3a3220; }
  #codeView .k { color: #c678dd; } #codeView .s { color: #98c379; } #codeView .c { color: #6a737d; font-style: italic; }
  #codeView .num { color: #d19a66; } #codeView .f { color: #61afef; } #codeView .t { color: #e5c07b; }
  #codeView .cbEmpty { padding: 30px; color: var(--dim); max-width: 640px; line-height: 1.6; }
  #codeView .cbRight a, #codeView .cbList a.in { color: var(--accent2); text-decoration: none; cursor: pointer; }
  #codeView .cbRight a:hover { text-decoration: underline; }
  #codeView .cbRight .row { font-size: 13px; padding: 2px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #codeView .cbRight .title { font-weight: 600; word-break: break-all; }
  #codeView .cbRight .note { font-size: 13px; color: var(--warn); margin: 6px 0; }
  #codeView .cbBar button.toggle { padding: 4px 10px; }
  #codeView .cbBar button.toggle.on { border-color: var(--accent); color: var(--accent); }
  #codeView .cbMain.noLeft .cbLeft, #codeView .cbMain.noRight .cbRight { display: none; }
  #codeView .cbMain.noLeft { grid-template-columns: 1fr 340px; }
  #codeView .cbMain.noRight { grid-template-columns: 300px 1fr; }
  #codeView .cbMain.noLeft.noRight { grid-template-columns: 1fr; }
  @media (max-width: 1000px) { #codeView .cbMain { grid-template-columns: 1fr; grid-template-rows: 30% 1fr 30%; } #codeView .cbLeft, #codeView .cbRight { border: 0; border-bottom: 1px solid var(--line); }
    #codeView .cbMain.noLeft, #codeView .cbMain.noRight, #codeView .cbMain.noLeft.noRight { grid-template-columns: 1fr; }
    #codeView .cbMain.noLeft { grid-template-rows: 1fr 30%; } #codeView .cbMain.noRight { grid-template-rows: 30% 1fr; }
    #codeView .cbMain.noLeft.noRight { grid-template-rows: 1fr; } }`;
  document.head.append(Object.assign(document.createElement("style"), { textContent: css }));

  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "text") e.textContent = v; else if (k === "class") e.className = v;
      else if (k.startsWith("on")) e[k] = v; else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(c);
    return e;
  };
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  // ---- syntax colours (per line; good enough for the decompiler's C#-like output)
  const KW = new Set(("abstract as base bool break byte case catch char class const continue default delegate do double else enum event " +
    "explicit extern false finally fixed float for foreach goto if implicit in int interface internal is lock long namespace new null " +
    "object operator out override params private protected public readonly ref return sbyte sealed short sizeof static string struct " +
    "switch this throw true try typeof uint ulong unchecked unsafe ushort using virtual void volatile while var ptr").split(" "));
  const TOK = /(\/\/.*$|\/\*.*?\*\/)|("(?:[^"\\]|\\.)*")|\b(0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b|([A-Za-z_][\w]*)(?=\s*\()|([A-Za-z_]\w*)/g;
  function colour(line, mark) {
    let out = "", last = 0, m;
    TOK.lastIndex = 0;
    while ((m = TOK.exec(line))) {
      out += esc(line.slice(last, m.index));
      const w = m[0];
      if (m[1]) out += `<span class="c">${esc(w)}</span>`;
      else if (m[2]) out += `<span class="s">${esc(w)}</span>`;
      else if (m[3]) out += `<span class="num">${esc(w)}</span>`;
      else if (m[4]) out += KW.has(w) ? `<span class="k">${w}</span>` : `<span class="f">${esc(w)}</span>`;
      else out += KW.has(w) ? `<span class="k">${w}</span>` : /^[A-Z]/.test(w) ? `<span class="t">${esc(w)}</span>` : esc(w);
      last = m.index + w.length;
    }
    out += esc(line.slice(last));
    if (mark) out = markIn(out, mark);
    return out;
  }
  function markIn(htmlText, re) {            // highlight matches in already-coloured text (outside tags only)
    return htmlText.replace(/(<[^>]*>)|([^<]+)/g, (all, tag, txt) => tag ? tag : txt.replace(re, s => `<mark>${s}</mark>`));
  }

  // ---- state
  const S = { files: [], folders: [], open: new Set(), path: "", lines: [], methods: [], fields: [], cur: 0, mark: null, cache: new Map(), leftTab: "files", results: null, outline: [] };

  // ---- layout
  const q = h("input", { type: "search", placeholder: "Search names and code…", maxlength: 200 });
  const kind = h("select", {},
    ...[["all", "All"], ["types", "Types"], ["methods", "Methods"], ["fields", "Fields"], ["code", "Code text"], ["strings", "Strings"]].map(([v, t]) => h("option", { value: v, text: t })));
  const folder = h("select", {}, h("option", { value: "", text: "All folders" }));
  const caseBox = h("input", { type: "checkbox" }), wordBox = h("input", { type: "checkbox" });
  const back = h("button", { class: "small", text: "Back to the site", onclick: () => close() });
  // panel switches: hide the file list or the cross-references to give the code more room (tablets, small
  // screens); remembered in this browser only
  const leftBtn = h("button", { class: "small toggle", text: "Files", title: "Show or hide the file list" });
  const rightBtn = h("button", { class: "small toggle", text: "Calls / called by", title: "Show or hide the cross-references" });
  const bar = h("div", { class: "cbBar" }, h("h2", { text: "Code reference" }), q, kind, folder,
    h("label", {}, caseBox, "Match case"), h("label", {}, wordBox, "Whole word"), h("span", { class: "grow" }), leftBtn, rightBtn, back);
  const tabs = h("div", { class: "cbTabs" });
  const leftList = h("div", { class: "cbList" });
  const left = h("div", { class: "cbLeft" }, tabs, leftList);
  const pathBar = h("div", { class: "cbPath", text: "" });
  const code = h("div", { class: "cbCode" });
  const scroll = h("div", { class: "cbScroll" }, code);
  const center = h("div", { class: "cbCenter" }, pathBar, scroll);
  const right = h("div", { class: "cbRight" });
  const main = h("div", { class: "cbMain" }, left, center, right);
  const view = h("div", { id: "codeView", class: "hidden" }, bar, main);
  document.body.append(view);
  const PANELS_KEY = "codeBrowser.panels";
  let panels = { left: true, right: true };
  try { Object.assign(panels, JSON.parse(localStorage.getItem(PANELS_KEY) || "{}")); } catch (e) { }
  function drawPanels() {
    main.classList.toggle("noLeft", !panels.left);
    main.classList.toggle("noRight", !panels.right);
    leftBtn.classList.toggle("on", panels.left);
    rightBtn.classList.toggle("on", panels.right);
    leftBtn.setAttribute("aria-pressed", panels.left ? "true" : "false");
    rightBtn.setAttribute("aria-pressed", panels.right ? "true" : "false");
  }
  function setPanel(which, on) {
    panels[which] = on;
    try { localStorage.setItem(PANELS_KEY, JSON.stringify(panels)); } catch (e) { }
    drawPanels();
    if (which === "right" && on && S.path && S.cur) xrefAt(S.cur);      // catch up on the line chosen while hidden
    if (S.path) paint(true);                                            // the code column changed width
  }
  leftBtn.onclick = () => setPanel("left", !panels.left);
  rightBtn.onclick = () => setPanel("right", !panels.right);
  drawPanels();

  // ---- routing: #code, #code/<path>, #code/<path>:<line>
  function parseHash() {
    const hs = decodeURIComponent(location.hash || "");
    if (!hs.startsWith("#code")) return null;
    const rest = hs.slice(5).replace(/^\//, "");
    const m = rest.match(/^(.*?)(?::(\d+))?$/);
    return { path: m[1] || "", line: m[2] ? +m[2] : 0 };
  }
  const hashFor = (path, line) => "#code" + (path ? "/" + path + (line ? ":" + line : "") : "");
  function go(path, line) {                 // a link inside the browser: a new history entry
    const want = hashFor(path, line);
    if (location.hash !== want) location.hash = want; else route();
  }
  let shown = false;
  async function route() {
    const r = parseHash();
    if (!r) { view.classList.add("hidden"); document.body.style.overflow = ""; shown = false; return; }
    if (!shown) { view.classList.remove("hidden"); document.body.style.overflow = "hidden"; shown = true; if (!S.files.length) await loadTree(); }
    if (r.path && r.path !== S.path) await openFile(r.path);
    if (!r.path && !S.path) showWelcome();
    if (r.line) goLine(r.line, true);
  }
  function close() {
    history.pushState(null, "", location.pathname + location.search);
    route();
  }
  window.addEventListener("hashchange", route);

  // ---- left panel: Files / Outline / Results
  function drawTabs() {
    tabs.textContent = "";
    for (const [k, t] of [["files", "Files"], ["outline", "Outline"], ["results", "Results"]]) {
      if (k === "results" && !S.results) continue;
      if (k === "outline" && !S.path) continue;
      tabs.append(h("button", { class: S.leftTab === k ? "on" : "", text: t, onclick: () => { S.leftTab = k; drawLeft(); } }));
    }
  }
  function drawLeft() {
    drawTabs();
    leftList.textContent = "";
    if (S.leftTab === "results" && S.results) return drawResults();
    if (S.leftTab === "outline" && S.path) return drawOutline();
    drawTree();
  }
  function treeOf(files) {
    const root = { dirs: new Map(), files: [] };
    for (const [p, n] of files) {
      const parts = p.split("/"); let node = root;
      for (const d of parts.slice(0, -1)) { if (!node.dirs.has(d)) node.dirs.set(d, { dirs: new Map(), files: [] }); node = node.dirs.get(d); }
      node.files.push([p, parts[parts.length - 1], n]);
    }
    return root;
  }
  function drawTree() {
    const add = (node, prefix, depth) => {
      for (const [name, sub] of [...node.dirs].sort((a, b) => a[0].localeCompare(b[0]))) {
        const key = prefix + name;
        const isOpen = S.open.has(key);
        leftList.append(h("div", { class: "dir", style: `padding-left:${8 + depth * 14}px`, text: (isOpen ? "▾ " : "▸ ") + name,
          onclick: () => { isOpen ? S.open.delete(key) : S.open.add(key); drawLeft(); } }));
        if (isOpen) add(sub, key + "/", depth + 1);
      }
      for (const [p, name, n] of node.files.sort((a, b) => a[1].localeCompare(b[1])))
        leftList.append(h("a", { class: "file" + (p === S.path ? " on" : ""), href: hashFor(p), style: `padding-left:${8 + depth * 14}px`,
          title: `${p} (${n} lines)`, text: name.replace(/\.cs$/, "") }));
    };
    add(treeOf(S.files), "", 0);
  }
  function drawOutline() {
    if (!S.outline.length) return leftList.append(h("p", { class: "dim", style: "padding:0 10px", text: "No methods in this file." }));
    for (const [line, , id, disp] of S.outline)
      leftList.append(h("a", { class: "hit", href: hashFor(S.path, line), title: disp, text: disp.split(".").slice(-1)[0] }));
  }
  function drawResults() {
    const r = S.results;
    const re = searchRe();
    if (r.symbols.length) {
      leftList.append(h("h4", { text: `Names (${r.symbols.length}${r.moreSymbols ? "+" : ""})` }));
      for (const [name, k, owner, p, line] of r.symbols) {
        const a = h("a", { class: "hit", href: hashFor(p, line), title: `${owner ? owner + "." : ""}${name} — ${p}:${line}` },
          h("span", { class: "kind", text: k }));
        a.insertAdjacentHTML("beforeend", (owner ? `<code>${esc(owner)}.</code>` : "") + markIn(esc(name), re));
        leftList.append(a);
      }
    }
    if (r.lines.length) {
      leftList.append(h("h4", { text: `Lines (${r.lines.length}${r.more ? "+" : ""})` }));
      let lastFile = "";
      for (const [p, line, text] of r.lines) {
        if (p !== lastFile) { leftList.append(h("div", { class: "dir", text: p })); lastFile = p; }
        const a = h("a", { class: "hit", href: hashFor(p, line), title: `${p}:${line}` });
        a.innerHTML = `<code>${line}</code> ` + markIn(esc(text), re);
        leftList.append(a);
      }
    }
    if (!r.symbols.length && !r.lines.length) leftList.append(h("p", { class: "dim", style: "padding:0 10px", text: "Nothing found." }));
    if (r.more || r.moreSymbols) leftList.append(h("p", { class: "dim", style: "padding:0 10px", text: "Only the first results are shown: narrow the search with the folder or kind." }));
  }

  // ---- data
  async function loadTree() {
    let d;
    try { d = await call("CodeTree"); } catch (e) { return fail("Could not load the file list: " + e.message); }
    S.files = d.files || []; S.folders = d.folders || [];
    const subs = new Set();
    for (const [p] of S.files) { const parts = p.split("/"); subs.add(parts[0]); if (parts.length > 2) subs.add(parts.slice(0, 2).join("/")); }
    for (const f of [...subs].sort()) folder.append(h("option", { value: f, text: f }));
    for (const f of S.folders) S.open.add(f);
    drawLeft();
  }
  function fail(msg) { code.style.height = "auto"; code.innerHTML = ""; code.append(h("div", { class: "cbEmpty", text: msg })); }
  function showWelcome() {
    pathBar.textContent = "";
    code.style.height = "auto"; code.textContent = "";
    code.append(h("div", { class: "cbEmpty" },
      h("p", { text: "The game's code, decompiled and made readable (the parts named so far: " + (S.folders.join(", ") || "none") + ")." }),
      h("p", { text: "Open a file on the left, or search names and code above. Click a line inside a method to see what it calls and what calls it; Ctrl+click (or double-click) a name to jump to where it is defined." }),
      h("p", { class: "dim", text: "Pseudo-code for reading and finding hook points: it does not compile. Names of types, methods, fields and parameters are the game's own; local variable names and the one-line summaries were chosen by reading each function." })));
    right.textContent = "";
  }
  async function openFile(path) {
    let d = S.cache.get(path);
    if (!d) {
      try { d = await call("CodeFile", { path }); } catch (e) { return fail("Could not load the file: " + e.message); }
      if (!d || d.missing) return fail("This file is not in the code reference.");
      if (S.cache.size > 8) S.cache.delete(S.cache.keys().next().value);
      S.cache.set(path, d);
    }
    S.path = path; S.lines = d.text.split("\n"); S.methods = d.methods; S.fields = d.fields; S.outline = d.methods; S.cur = 0;
    S.heads = new Set(d.methods.map(m => m[0]));
    const parts = path.split("/");
    for (let i = 1; i < parts.length; i++) S.open.add(parts.slice(0, i).join("/"));
    pathBar.textContent = `${path} · ${S.lines.length} lines · ${d.methods.length} methods`;
    code.style.height = S.lines.length * LINE_H + "px";
    scroll.scrollTop = 0;
    if (S.leftTab !== "results") S.leftTab = "files";
    drawLeft(); paint(true);
    right.textContent = ""; right.append(h("p", { class: "dim", text: "Click a line inside a method (or a field) to see its cross-references." }));
  }

  // ---- the code view: only the visible lines are drawn (files run to 60,000 lines)
  let painted = [-1, -1];
  function paint(force) {
    if (!S.path) return;
    const top = scroll.scrollTop, hgt = scroll.clientHeight || 600;
    const a = Math.max(0, Math.floor(top / LINE_H) - OVERSCAN), b = Math.min(S.lines.length, Math.ceil((top + hgt) / LINE_H) + OVERSCAN);
    if (!force && a >= painted[0] && b <= painted[1]) return;
    painted = [Math.max(0, a - OVERSCAN), Math.min(S.lines.length, b + OVERSCAN)];
    let html = "";
    for (let i = painted[0]; i < painted[1]; i++) {
      const n = i + 1;
      const cls = "l" + (n === S.cur ? " cur" : "") + (S.heads.has(n) ? " head" : "");
      html += `<div class="${cls}" style="top:${i * LINE_H}px" data-n="${n}"><span class="n">${n}</span>${colour(S.lines[i], S.mark)}</div>`;
    }
    code.innerHTML = html;
  }
  scroll.addEventListener("scroll", () => paint(false), { passive: true });
  window.addEventListener("resize", () => paint(true));
  function goLine(n, flash) {
    if (!S.path || n < 1) return;
    S.cur = n;
    scroll.scrollTop = Math.max(0, (n - 6) * LINE_H);
    paint(true);
    if (flash) { const e = code.querySelector(`.l[data-n="${n}"]`); if (e) { e.classList.add("flash"); setTimeout(() => e.classList.remove("flash"), 900); } }
    xrefAt(n);
  }
  code.addEventListener("click", e => {
    const l = e.target.closest(".l"); if (!l) return;
    const n = +l.dataset.n;
    if (e.ctrlKey || e.metaKey) { const w = wordAt(e); if (w) return define(w); }
    if (n !== S.cur) { S.cur = n; history.replaceState(null, "", hashFor(S.path, n)); paint(true); xrefAt(n); }
  });
  code.addEventListener("dblclick", e => { const w = wordAt(e); if (w) define(w); });
  function wordAt(e) {
    let node, off;
    if (document.caretPositionFromPoint) { const p = document.caretPositionFromPoint(e.clientX, e.clientY); if (p) { node = p.offsetNode; off = p.offset; } }
    else if (document.caretRangeFromPoint) { const r = document.caretRangeFromPoint(e.clientX, e.clientY); if (r) { node = r.startContainer; off = r.startOffset; } }
    if (!node || node.nodeType !== 3) return "";
    const t = node.textContent;
    let a = off, b = off;
    while (a > 0 && /\w/.test(t[a - 1])) a--;
    while (b < t.length && /\w/.test(t[b])) b++;
    const w = t.slice(a, b);
    return /^[A-Za-z_]\w*$/.test(w) && !KW.has(w) ? w : "";
  }
  async function define(word) {           // jump to where a name is defined (or list the candidates)
    let d;
    try { d = await call("CodeSearch", { q: word, kind: "all", case: true, word: true, folder: "" }); } catch (e) { return; }
    const exact = (d.symbols || []).filter(s => s[0] === word && s[1] !== "property");
    if (exact.length === 1) return go(exact[0][3], exact[0][4]);
    S.results = { symbols: exact.length ? exact : d.symbols || [], lines: [], more: false, moreSymbols: false, q: word };
    S.mark = null; S.leftTab = "results"; drawLeft();
  }

  // ---- cross-references
  let xrefAsk = 0;
  async function xrefAt(n) {
    if (!panels.right) return;                // hidden: fetched when the panel is shown again
    const f = S.fields.find(x => x[0] === n);
    let id = f ? f[1] : "";
    if (!id) {
      let best = null;
      for (const m of S.methods) if (m[0] <= n && n <= m[1] && (!best || m[0] > best[0])) best = m;
      if (best) id = best[2];
    }
    if (!id) { right.textContent = ""; right.append(h("p", { class: "dim", text: "This line is not inside a method." })); return; }
    const ask = ++xrefAsk;
    let d;
    try { d = await call("CodeXref", { id }); } catch (e) { return; }
    if (ask !== xrefAsk || !d || d.missing) return;
    drawXref(d);
  }
  function itemRow(it) {
    const [, disp, p, line] = it;
    return h("div", { class: "row", title: p ? `${disp} — ${p}:${line}` : `${disp} (not in the code reference yet)` },
      p ? h("a", { href: hashFor(p, line), text: disp }) : h("span", { class: "dim", text: disp }));
  }
  function section(title, list) {
    const box = h("div", {}, h("h4", { text: `${title} (${list.length})` }));
    if (!list.length) box.append(h("div", { class: "row dim", text: "none" }));
    for (const it of list.slice(0, 400)) box.append(itemRow(it));
    if (list.length > 400) box.append(h("div", { class: "row dim", text: `…and ${list.length - 400} more` }));
    return box;
  }
  const NOTES = { unity: "Nothing in the code calls this: Unity calls it (a message such as Start or Update, or a button or event set up in the game's scenes).",
    rpc: "Nothing in the code calls this: Photon calls it by name when the other player's game sends this message.",
    coroutine: "Nothing calls this directly: it is a step of a coroutine, run by Unity after StartCoroutine.",
    lambda: "A lambda: it runs wherever the delegate made from it is called." };
  function drawXref(d) {
    right.textContent = "";
    const [, disp, p, line] = d.item;
    right.append(h("div", { class: "title" }, p ? h("a", { href: hashFor(p, line), text: disp }) : disp));
    if (d.kind === "field") {
      right.append(section("Read by", d.readBy), section("Written by", d.writtenBy));
      return;
    }
    if (d.note && NOTES[d.note]) right.append(h("p", { class: "note", text: NOTES[d.note] }));
    right.append(section("Called by", d.calledBy), section("Calls", d.calls), section("Reads fields", d.reads), section("Writes fields", d.writes));
  }

  // ---- search
  function searchRe() {
    const v = (S.results && S.results.q) || q.value.trim();
    if (!v) return null;
    const src = v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    try { return new RegExp(wordBox.checked ? `\\b${src}\\b` : src, caseBox.checked ? "g" : "gi"); } catch (e) { return null; }
  }
  async function search() {
    const v = q.value.trim();
    if (v.length < 2) return;
    let d;
    try { d = await call("CodeSearch", { q: v, kind: kind.value, folder: folder.value, case: caseBox.checked, word: wordBox.checked }); }
    catch (e) { S.results = { symbols: [], lines: [], q: v }; return drawLeft(); }
    S.results = Object.assign({ symbols: [], lines: [] }, d, { q: v });
    S.mark = searchRe(); S.leftTab = "results"; drawLeft(); if (S.path) paint(true);
  }
  q.addEventListener("keydown", e => { if (e.key === "Enter") search(); });
  for (const c of [kind, folder, caseBox, wordBox]) c.addEventListener("change", () => { if (q.value.trim().length >= 2) search(); });
  q.addEventListener("search", () => { if (!q.value) { S.mark = null; if (S.path) paint(true); } });

  // ---- the site's tab bar gets a link
  const nav = document.getElementById("mainTabs");
  if (nav && !document.getElementById("tabCode")) nav.append(h("a", { id: "tabCode", href: "#code", text: "Code" }));
  window.codeBrowser = { route };
  route();
})();
