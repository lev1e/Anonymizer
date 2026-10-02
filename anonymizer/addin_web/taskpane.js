"use strict";
/* Панель Anonymizer внутри Word, Excel и PowerPoint.
   Порядок для каждой операции: прочитать открытый файл, отправить его программе на этом компьютере (127.0.0.1),
   получить результат, заменить им содержимое документа, заново прочитать документ и сверить с ожидаемым.
   Копия исходного файла перед заменой хранится в программе (зашифрованной), её можно вернуть кнопкой «Откатить». */

const $ = (s, r = document) => r.querySelector(s);
const TOKEN = $('meta[name="addin-token"]').content;
const EXT = {Word: "docx", Excel: "xlsx", PowerPoint: "pptx"};
const SLICE = 4 * 1024 * 1024;
const MAX_BYTES = 100 * 1024 * 1024;
let HOST = null;
let busy = false;
let last = null;                       // последняя операция: нужна для повторного прохода и отката

const dbg = msg => { try { fetch("/api/addin/debug", {method: "POST", body: String(msg), headers: {"X-Requested-With": "anonymizer", "X-Addin-Token": TOKEN}}); } catch (e) { /* отладка не должна ломать работу */ } };
const esc = v => { const d = document.createElement("div"); d.textContent = v == null ? "" : String(v); return d.innerHTML; };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const store = {
  get(k, d) { try { const v = localStorage.getItem("anon." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("anon." + k, JSON.stringify(v)); } catch (e) { /* хранилище недоступно: не страшно */ } },
};

/* ---------- связь с программой ---------- */
async function api(path, opts = {}) {
  const headers = {"X-Requested-With": "anonymizer", "X-Addin-Token": TOKEN, ...(opts.headers || {})};
  let r;
  try { r = await fetch(path, {...opts, headers, cache: "no-store"}); }
  catch (e) { throw new Error("Нет связи с программой Anonymizer. Запустите её и повторите."); }
  const type = r.headers.get("Content-Type") || "";
  if (type.includes("json")) {
    const data = await r.json();
    if (!r.ok) { const err = new Error(data.error || "Ошибка " + r.status + "."); err.code = data.code; err.count = data.count; throw err; }
    return data;
  }
  if (!r.ok) throw new Error("Ошибка " + r.status + ".");
  return r;
}
const postJson = (path, body) => api(path, {method: "POST", body: JSON.stringify(body), headers: {"Content-Type": "application/json"}});

/* ---------- чтение и запись документа ---------- */
function documentName() {
  let name = "";
  try { name = decodeURIComponent((Office.context.document.url || "").split(/[\\/]/).pop().split("?")[0]); } catch (e) { /* без имени */ }
  const ext = "." + EXT[HOST];
  if (!name) return "document" + ext;
  return name.toLowerCase().endsWith(ext) ? name : name.replace(/\.[^.]*$/, "") + ext;
}
/* Office иногда не отвечает на чтение файла (документ занят, идёт сохранение): ждать вечно нельзя. */
function withTimeout(promise, ms, message) {
  let timer;
  const limit = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); });
  return Promise.race([promise, limit]).finally(() => clearTimeout(timer));
}
const getFile = () => withTimeout(readFile(), 90000, "Office не отдал документ за 90 секунд. Подождите и повторите.");
function readFile() {
  return new Promise((resolve, reject) => {
    Office.context.document.getFileAsync(Office.FileType.Compressed, {sliceSize: SLICE}, res => {
      if (res.status !== Office.AsyncResultStatus.Succeeded) return reject(new Error("Не удалось прочитать документ: " + res.error.message));
      const file = res.value, parts = new Array(file.sliceCount);
      const fail = msg => { file.closeAsync(); reject(new Error("Не удалось прочитать документ: " + msg)); };
      const next = i => {
        if (i >= file.sliceCount) {
          file.closeAsync();
          let total = 0; parts.forEach(p => total += p.length);
          const out = new Uint8Array(total); let at = 0;
          parts.forEach(p => { out.set(p, at); at += p.length; });
          return resolve(out);
        }
        file.getSliceAsync(i, s => {
          if (s.status !== Office.AsyncResultStatus.Succeeded) return fail(s.error.message);
          parts[i] = Uint8Array.from(s.value.data);
          next(i + 1);
        });
      };
      next(0);
    });
  });
}
function toBase64(bytes) {
  let out = "";
  for (let i = 0; i < bytes.length; i += 0x8000) out += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(out);
}
const supported = (set, v) => { try { return Office.context.requirements.isSetSupported(set, v); } catch (e) { return false; } };

/* Исправления ищет программа по самому файлу (надёжнее, чем API Word, которого нет в части версий). Принять их кнопкой
   можно, если API есть; иначе нужно сделать это самому на вкладке «Рецензирование». */
async function wordAcceptAll() {
  await Word.run(async ctx => { ctx.document.body.getTrackedChanges().acceptAll(); await ctx.sync(); });
}
/* Свойства файла (автор, название, компания) Office при вставке не переносит: их нужно записать отдельно. */
async function applyProperties(host, structure) {
  const wanted = (structure && structure.props) || {}, custom = (structure && structure.custom) || [];
  const keys = ["author", "title", "subject", "keywords", "comments", "category", "manager", "company"];
  const failures = [];
  const api = host === "Word" ? Word : host === "Excel" ? Excel : PowerPoint;
  const root = ctx => host === "Word" ? ctx.document.properties : host === "Excel" ? ctx.workbook.properties : ctx.presentation.properties;
  try {
    await api.run(async ctx => {
      const target = root(ctx);
      for (const key of keys) {
        try { target[key] = wanted[key] || ""; await ctx.sync(); } catch (e) { /* поле недоступно: проверка ниже скажет об этом */ }
      }
      target.load(keys.join(","));
      await ctx.sync();
      for (const key of keys) {
        let have = ""; try { have = target[key] || ""; } catch (e) { continue; }
        if (have !== (wanted[key] || "")) failures.push("свойство «" + key + "»");
      }
    });
  } catch (e) { failures.push("свойства файла"); }
  try {
    await api.run(async ctx => {
      const props = root(ctx);
      const bag = host === "Excel" ? props.custom : props.customProperties;
      bag.deleteAll();
      for (const item of custom) bag.add(item.name, item.value);
      await ctx.sync();
    });
  } catch (e) { /* пользовательских свойств нет или API недоступен */ }
  return failures;
}
/* Колонтитулы по разделам: вставка файла заменяет тело документа, но не колонтитулы. */
async function applyHeadersFooters(parts) {
  const failed = [];
  await Word.run(async ctx => {
    const sections = ctx.document.sections; sections.load("items"); await ctx.sync();
    for (const item of parts) {
      const section = sections.items[item.section];
      if (!section) { failed.push("колонтитул раздела " + (item.section + 1)); continue; }
      const body = item.kind === "header" ? section.getHeader(item.type) : section.getFooter(item.type);
      try {
        if (!item.docx) throw new Error("text");
        body.insertFileFromBase64(item.docx, "Replace"); await ctx.sync();
      } catch (e) {
        try { body.clear(); body.insertText(item.text || "", "Start"); await ctx.sync(); }
        catch (e2) { failed.push("колонтитул раздела " + (item.section + 1)); }
      }
    }
  });
  return failed;
}
async function applyWord(b64) {
  await Word.run(async ctx => {
    const doc = ctx.document;
    let previous = "Off";
    if (supported("WordApi", "1.4")) {
      doc.load("changeTrackingMode"); await ctx.sync();
      previous = doc.changeTrackingMode;
      // Замена при включённых исправлениях оставила бы прежний текст внутри самих исправлений.
      if (previous !== "Off") { doc.changeTrackingMode = "Off"; await ctx.sync(); }
    }
    const options = {importTheme: true, importStyles: true, importParagraphSpacing: true, importPageColor: true,
      importChangeTrackingMode: true, importCustomProperties: true, importCustomXmlParts: true, importDifferentOddEvenPages: true};
    let done = false;
    // Документ целиком (колонтитулы, разделы, водяные знаки) умеют заменять новые версии Word; тело — только содержимое.
    if (supported("WordApiDesktop", "1.1")) {
      try { doc.insertFileFromBase64(b64, "Replace", options); await ctx.sync(); done = true; } catch (e) { done = false; }
    }
    if (!done) {
      try { doc.body.insertFileFromBase64(b64, "Replace", options); await ctx.sync(); }
      catch (e) { doc.body.insertFileFromBase64(b64, "Replace"); await ctx.sync(); }
    }
    if (previous !== "Off") { doc.changeTrackingMode = previous; await ctx.sync(); }
  });
}
async function applyPowerPoint(b64) {
  await PowerPoint.run(async ctx => {
    const slides = ctx.presentation.slides; slides.load("items/id"); await ctx.sync();
    const oldIds = slides.items.map(s => s.id);
    ctx.presentation.insertSlidesFromBase64(b64, {formatting: "KeepSourceFormatting"});
    await ctx.sync();
    for (const id of oldIds) ctx.presentation.slides.getItem(id).delete();
    await ctx.sync();
  });
}
async function applyExcel(b64, structure) {
  if (!supported("ExcelApi", "1.13")) throw new Error("Эта версия Excel не умеет заменять листы целиком (нужен Excel 2021 или Microsoft 365).");
  const failedNames = [];
  await Excel.run(async ctx => {
    const wb = ctx.workbook, sheets = wb.worksheets;
    sheets.load("items/id,items/name,items/visibility"); wb.names.load("items/name"); await ctx.sync();
    const oldSheets = sheets.items.map(s => ({id: s.id, name: s.name, visibility: s.visibility}));
    // Имена книги ссылаются на старые листы и сами могут содержать данные: Excel переносит их вместе с новыми листами.
    wb.names.items.forEach(n => n.delete());
    await ctx.sync();
    wb.insertWorksheetsFromBase64(b64, {positionType: "End"});
    await ctx.sync();
    sheets.load("items/id,items/name"); await ctx.sync();
    const oldIds = oldSheets.map(s => s.id);
    const fresh = sheets.items.filter(s => !oldIds.includes(s.id));
    if (!fresh.length) throw new Error("Excel не вставил листы из результата. Документ не изменён.");
    // Скрытые и «очень скрытые» листы Excel удалять не даёт: сначала они делаются видимыми.
    for (const old of oldSheets) {
      if (old.visibility !== "Visible") { try { sheets.getItem(old.id).visibility = "Visible"; } catch (e) { /* проверим при удалении */ } }
    }
    await ctx.sync();
    const stuck = [];
    for (const old of oldSheets) {
      try { sheets.getItem(old.id).delete(); await ctx.sync(); }
      catch (e) { stuck.push(old.name + (e && e.message ? " (" + e.message + ")" : "")); }
    }
    if (stuck.length) throw new Error("Не удалось убрать прежние листы: " + stuck.join(", ") + ". В книге остались и прежние листы, и новые: нажмите «Откатить к исходному».");
    const want = (structure && structure.sheets) || [];
    if (want.length === fresh.length) {
      fresh.forEach((sheet, i) => { if (sheet.name !== want[i].name) sheet.name = want[i].name; });
      await ctx.sync();
      // Видимость листов как в результате (скрытые остаются скрытыми). Последний видимый лист скрыть нельзя.
      for (let i = want.length - 1; i >= 0; i--) {
        const state = want[i].state;
        if (state && state !== "visible") {
          try { fresh[i].visibility = state === "veryHidden" ? "VeryHidden" : "Hidden"; await ctx.sync(); } catch (e) { failedNames.push("видимость листа " + want[i].name); }
        }
      }
    }
    for (const item of (structure && structure.names) || []) {
      // Excel переносит имена книги вместе с листами; недостающие (например, на формулы) создаются здесь.
      const present = wb.names.getItemOrNullObject(item.name); await ctx.sync();
      if (!present.isNullObject) continue;
      // Имя на диапазон создаётся по объекту диапазона, остальное (формулы, константы) — по тексту формулы.
      const m = /^(?:'((?:[^']|'')+)'|([^!']+))!(\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?)$/.exec(item.ref || "");
      let added = false, why = "";
      for (const attempt of m ? ["range", "formula"] : ["formula"]) {
        try {
          const target = attempt === "range" ? wb.worksheets.getItem((m[1] || m[2]).replace(/''/g, "'")).getRange(m[3]) : "=" + item.ref;
          wb.names.add(item.name, target); await ctx.sync(); added = true; break;
        } catch (e) { why = e && e.message ? e.message : String(e); }
      }
      if (!added) failedNames.push(item.name + " (" + why + ")");
    }
  });
  return failedNames;
}
async function applyToDocument(b64, structure) {
  let extra = [];
  if (HOST === "Word") { await applyWord(b64); extra = await applyHeadersFooters((structure && structure.parts) || []); }
  else if (HOST === "PowerPoint") await applyPowerPoint(b64);
  else extra = await applyExcel(b64, structure);
  const failed = await applyProperties(HOST, structure);
  return [...(extra || []), ...failed];
}

/* ---------- экран ---------- */
const STEPS = {anonymize: ["Чтение документа", "Обезличивание", "Замена в документе", "Проверка"],
               restore: ["Чтение документа", "Восстановление", "Замена в документе", "Проверка"]};
function show(...ids) { for (const id of ["offline", "ready", "busy", "result"]) $("#" + id).hidden = !ids.includes(id); $("#more").hidden = !ids.includes("ready") && !ids.includes("result"); }
function drawSteps(kind, now, failed) {
  $("#steps").innerHTML = STEPS[kind].map((t, i) => {
    const cls = failed && i === now ? "fail" : i < now ? "done" : i === now ? "now" : "";
    const mark = failed && i === now ? "!" : i < now ? "+" : i === now ? "&gt;" : "";
    return `<li class="${cls}"><span class="mark">${mark}</span><span>${esc(t)}</span></li>`;
  }).join("");
  $("#bar").style.width = Math.round(((now + (failed ? 0 : .4)) / STEPS[kind].length) * 100) + "%";
}
function setBusy(on) {
  busy = on;
  for (const id of ["go-anon", "go-rest", "retry"]) $("#" + id).disabled = on;
}
function options() {
  return {numbers: $("#opt-numbers").checked, strict: $("#opt-strict").checked, countries: $("#opt-countries").checked};
}
const noteList = items => `<ul class="notes">${items.map(i => `<li class="${i.level}">${esc(i.text)}</li>`).join("")}</ul>`;
const headline = (text, error) => `<p class="status${error ? " error" : ""}">${esc(text)}</p>`;

function summaryText(job) {
  const parts = (job.result.summary || []).map(s => `${s.label}: ${s.count}`);
  return parts.join(", ");
}
function messagesOf(job) {
  return (job.files[0] ? job.files[0].messages : []).map(m => ({level: m.level === "error" ? "error" : m.level === "warn" ? "warn" : "info", text: m.text}));
}

function renderUnchanged(title, job, extra) {
  show("result", "ready");
  $("#result").innerHTML = headline(title, true) + `<p class="sub">Документ не изменён.</p>` + noteList([...messagesOf(job), ...(extra || [])]);
}

function renderReview(job) {
  const suggestions = job.result.suggestions || [];
  const rows = suggestions.map((s, i) => `<label><input type="checkbox" data-sug="${i}"><span><b>${esc(s.text)}</b>${s.count > 1 ? " ×" + s.count : ""}<div class="ctx">${esc(s.reason)}. …${esc((s.context || "").slice(0, 100))}…</div></span></label>`).join("");
  show("result", "ready");
  $("#result").innerHTML = headline("Нужно проверить перед заменой.") +
    `<p class="sub">Найдено значений для замены: ${job.result.total || 0}. Документ ещё не изменён.</p>` +
    noteList(messagesOf(job)) +
    (suggestions.length ? `<div><h2 class="sec">Возможно, нужно скрыть ещё</h2><div class="sug">${rows}</div></div>` : "") +
    `<div class="actions">` +
    (suggestions.length ? `<button class="btn" id="btn-rerun">Скрыть отмеченное и подготовить заново</button>` : "") +
    `<button class="btn main" id="btn-apply">Заменить в документе</button></div>`;
  $("#btn-apply").onclick = () => applyAndVerify(job);
  const rerun = $("#btn-rerun");
  if (rerun) rerun.onclick = () => {
    const overrides = {};
    document.querySelectorAll("[data-sug]").forEach(box => { if (box.checked) overrides[suggestions[+box.dataset.sug].text] = "hide"; });
    run("anonymize", overrides);
  };
}

function renderFinal(kind, job, verify, failedNames) {
  const problems = (verify && verify.problems || []).map(p => ({level: "error", text: p.text}));
  const notes = (verify && verify.notes || []).map(t => ({level: "info", text: t}));
  const warns = (verify && verify.warnings || []).map(p => ({level: "warn", text: p.text}));
  const extra = [];
  // Свойства файла Office иногда менять не даёт: это ограничение версии, а не расхождение в документе.
  const propertyLimit = (failedNames || []).filter(n => n.indexOf("свойств") >= 0);
  const brokenParts = (failedNames || []).filter(n => n.indexOf("свойств") < 0);
  if (brokenParts.length) extra.push({level: "warn", text: "Не удалось воссоздать: " + brokenParts.join(", ") + "."});
  if (propertyLimit.length) extra.push({level: "warn", text: "Эта версия Office не позволяет менять свойства файла (автор, название, комментарии): очистите их вручную (Файл, Сведения) или обработайте файл в программе Anonymizer."});
  const clean = !problems.length && !brokenParts.length;
  const attention = messagesOf(job).filter(m => m.level !== "info");
  const verb = kind === "anonymize" ? "Документ обезличен." : "Данные возвращены.";
  const title = clean ? (attention.length ? verb + " Есть замечания." : verb) : "Документ заменён, но есть расхождения.";
  const counts = kind === "anonymize" ? `Заменено значений: ${job.result.total || 0}. ${summaryText(job)}` : `Восстановлено значений: ${job.result.restored || 0}.`;
  let html = headline(title, !clean) + `<p class="sub">${esc(counts)}</p>`;
  html += noteList([...problems, ...extra, ...warns, ...attention, ...(clean ? [{level: "ok", text: "Документ заново прочитан и сверен с результатом."}] : []), ...notes]);
  const unknown = Object.keys(job.result.unknown || {});
  if (kind === "restore" && unknown.length) html += `<div><h2 class="sec">Метки, которых нет в хранилище</h2><div class="chips">${unknown.slice(0, 12).map(u => `<span class="chip">${esc(u)}</span>`).join("")}</div></div>`;
  html += `<div class="actions"><button class="btn" id="btn-rollback">Откатить к исходному</button></div>`;
  html += `<p class="hint">Отмена в Office (Ctrl+Z) после такой замены может не сработать: используйте «Откатить».</p>`;
  show("result", "ready");
  $("#result").innerHTML = html;
  $("#btn-rollback").onclick = () => rollback(last && last.backup);
}

function renderError(error, canRollback) {
  show("result", "ready");
  let html = headline("Не получилось.", true) + `<p class="sub">${esc(error.message || error)}</p>`;
  if (canRollback) html += `<div class="actions"><button class="btn" id="btn-rollback">Откатить к исходному</button></div>`;
  $("#result").innerHTML = html;
  const b = $("#btn-rollback"); if (b) b.onclick = () => rollback(last && last.backup);
}

/* ---------- операции ---------- */
async function waitJob(id) {
  const until = Date.now() + 20 * 60 * 1000;
  for (;;) {
    if (Date.now() > until) throw new Error("Обработка идёт слишком долго. Документ не изменён: повторите или обработайте файл в программе.");
    await sleep(450);
    const job = await api("/api/addin/jobs/" + id);
    if (job.state === "done" || job.state === "failed") return job;
    $("#bar").style.width = Math.round((.25 + job.percent * .25) * 100) + "%";
  }
}

async function run(kind, overrides) {
  dbg("run " + kind + " busy=" + busy);
  if (busy) return;
  setBusy(true);
  let step = 0;
  show("busy");
  try {
    drawSteps(kind, 0);
    let started;
    if (overrides) {
      started = await postJson("/api/addin/rerun", {job: last.job, options: options(), overrides});
    } else {
      dbg("reading file");
      const bytes = await getFile();
      dbg("file read " + bytes.length);
      if (bytes.length > MAX_BYTES) throw new Error("Документ больше 100 МБ: Office не позволяет заменить его целиком. Обработайте файл в программе Anonymizer.");
      started = await api("/api/addin/" + kind, {method: "POST", body: bytes, headers: {
        "X-Filename": encodeURIComponent(documentName()), "Content-Type": "application/octet-stream",
        "X-Options": encodeURIComponent(JSON.stringify(options()))}});
    }
    step = 1; drawSteps(kind, 1);
    dbg("job started " + started.job);
    const job = await waitJob(started.job);
    dbg("job state " + job.state);
    if (job.state === "failed") throw new Error(job.error || "Не удалось обработать документ.");
    last = {job: job.id, backup: started.backup, kind, structure: job.structure, data: job};
    const file = job.files[0];
    if (!file || !file.downloadable) return renderUnchanged(kind === "anonymize" ? "Документ не обезличен." : "Данные не возвращены.", job);
    if (!file.total && !(job.result.numbers && job.result.numbers.replaced)) {
      const text = kind === "anonymize" ? "Данных для замены не найдено." : "В документе не найдено меток для восстановления.";
      return renderUnchanged(text, job);
    }
    if (kind === "anonymize" && (!$("#opt-auto").checked || (job.result.suggestions || []).length || file.status === "attention")) {
      setBusy(false);
      return renderReview(job);
    }
    setBusy(false);
    await applyAndVerify(job);
  } catch (e) {
    setBusy(false);
    if (e.code === "tracked_changes") return renderTracked(e.count, kind);
    show("busy"); drawSteps(kind, step, true);
    renderError(e, false);
  } finally { setBusy(false); }
}

function renderTracked(count, kind) {
  show("result", "ready");
  $("#result").innerHTML = headline("В документе есть исправления.") +
    `<p class="sub">Исправлений: ${count}. Они хранят прежний текст, поэтому его не удалось бы убрать из документа. Примите их или отклоните, затем повторите.</p>` +
    `<div class="actions"><button class="btn main" id="btn-accept">Принять все исправления и продолжить</button></div><p class="hint" id="accept-note"></p>`;
  $("#btn-accept").onclick = async () => {
    try { await wordAcceptAll(); } catch (e) {
      $("#accept-note").textContent = "В этой версии Word кнопка недоступна: примите исправления на вкладке «Рецензирование» (Принять, Все исправления) и нажмите «Обезличить документ» снова.";
      return;
    }
    run(kind);
  };
}

function renderChanged(kind, check) {
  show("result", "ready");
  $("#result").innerHTML = headline("Документ изменился во время обработки.") +
    `<p class="sub">Замена затёрла бы ваши правки (изменено слов: ${check.changed}). Документ не тронут.</p>` +
    `<div class="actions"><button class="btn main" id="btn-again">Повторить с текущей версией</button>` +
    `<button class="btn" id="btn-force">Всё равно заменить</button></div>`;
  $("#btn-again").onclick = () => run(kind);
  $("#btn-force").onclick = () => applyAndVerify(last.data, true);
}

async function applyAndVerify(job, force) {
  if (busy) return;
  setBusy(true);
  const kind = last.kind;
  let step = 2, touched = false;
  show("busy");
  try {
    drawSteps(kind, 2);
    if (!force) {
      // Пока шла обработка, документ могли править: замена затёрла бы эти правки.
      const now = await getFile();
      const check = await api("/api/addin/unchanged/" + job.id, {method: "POST", body: now, headers: {"Content-Type": "application/octet-stream"}});
      if (!check.same) { setBusy(false); return renderChanged(kind, check); }
    }
    const result = await api("/api/addin/result/" + job.id);
    const bytes = new Uint8Array(await result.arrayBuffer());
    touched = true;
    const failedNames = await applyToDocument(toBase64(bytes), job.structure || last.structure) || [];
    step = 3; drawSteps(kind, 3);
    const reread = await getFile();
    const verify = await api("/api/addin/verify/" + job.id, {method: "POST", body: reread, headers: {"Content-Type": "application/octet-stream"}});
    renderFinal(kind, job, verify, failedNames);
    loadBackups();
  } catch (e) {
    show("busy"); drawSteps(kind, step, true);
    renderError(e, touched);
  } finally { setBusy(false); }
}

async function rollback(backupId) {
  if (!backupId || busy) return;
  setBusy(true); show("busy");
  const kind = (last && last.kind) || "anonymize";
  try {
    drawSteps(kind, 2);
    const {structure} = await api("/api/addin/backup-info/" + backupId);
    const r = await api("/api/addin/backup/" + backupId);
    const bytes = new Uint8Array(await r.arrayBuffer());
    const failedNames = await applyToDocument(toBase64(bytes), structure) || [];
    show("result", "ready");
    $("#result").innerHTML = headline("Документ возвращён к состоянию до замены.") +
      (failedNames.length ? noteList([{level: "warn", text: "Не удалось воссоздать: " + failedNames.join(", ") + "."}]) : "");
  } catch (e) { renderError(e, false); }
  finally { setBusy(false); }
}

/* ---------- копии, настройки, связь ---------- */
async function loadBackups() {
  const box = $("#backups");
  try {
    const {items} = await api("/api/addin/backups");
    const own = items.filter(i => i.name.toLowerCase().endsWith("." + EXT[HOST]));
    box.innerHTML = own.length ? own.map(i => `<li><span class="name">${esc(i.name)}<div class="meta">${esc(i.kind === "anonymize" ? "до обезличивания" : "до возврата")}, ${new Date(i.time * 1000).toLocaleString("ru-RU")}</div></span><button class="btn small" data-back="${esc(i.id)}">Вернуть</button></li>`).join("")
      : `<li><span class="meta">Пока нет копий.</span></li>`;
    box.querySelectorAll("[data-back]").forEach(b => b.onclick = () => {
      if (b.dataset.armed) return rollback(b.dataset.back);
      b.dataset.armed = "1"; b.textContent = "Подтвердить";
      setTimeout(() => { delete b.dataset.armed; b.textContent = "Вернуть"; }, 4000);
    });
  } catch (e) { box.innerHTML = ""; }
}

async function connect() {
  const conn = $("#conn");
  try {
    const info = await api("/api/addin/info");
    conn.textContent = "Подключено"; conn.className = "conn ok";
    if (!$("#opt-numbers").dataset.set) {
      $("#opt-numbers").checked = !!info.prefs.numbers; $("#opt-strict").checked = !!info.prefs.strict;
      $("#opt-countries").checked = !!info.prefs.countries; $("#opt-numbers").dataset.set = "1";
    }
    const note = $("#notice");
    note.hidden = !info.notice; note.textContent = info.notice || "";
    if (!last) show("ready");
    $("#more").hidden = false;
    loadBackups();
    return true;
  } catch (e) {
    conn.textContent = "Нет связи"; conn.className = "conn bad";
    show("offline");
    return false;
  }
}

function init(info) {
  HOST = info && info.host ? String(info.host) : null;
  if (!HOST || !EXT[HOST]) {
    $("#conn").textContent = "Не в Office"; $("#conn").className = "conn bad";
    show("offline");
    $("#offline").innerHTML = `<p class="lead">Панель работает внутри Word, Excel и PowerPoint.</p>`;
    return;
  }
  $("#opt-auto").checked = store.get("auto", true);
  $("#opt-auto").onchange = e => store.set("auto", e.target.checked);
  $("#go-anon").onclick = () => run("anonymize");
  $("#go-rest").onclick = () => run("restore");
  $("#retry").onclick = connect;
  $("#open-folder").onclick = () => postJson("/api/addin/open-app", {tab: "anon"}).catch(() => {});
  window.addEventListener("focus", () => { if (!busy) connect(); });
  connect();
}

/* Кнопки на ленте работают с одного нажатия: общий runtime Office выполняет их в этой же странице. */
let initialized = false, readyWaiters = [];
function whenReady() { return initialized ? Promise.resolve() : new Promise(r => readyWaiters.push(r)); }
async function command(kind, event) {
  dbg("command " + kind + " start");
  try { await Office.addin.showAsTaskpane(); dbg("showAsTaskpane ok"); } catch (e) { dbg("showAsTaskpane error " + (e && e.message)); }
  try { await whenReady(); dbg("ready host=" + HOST); if (HOST && EXT[HOST]) await run(kind); dbg("run finished"); }
  catch (e) { dbg("command error " + (e && e.message)); }
  finally { try { event.completed(); } catch (e) { /* ничего */ } dbg("completed"); }
}
if (window.Office && Office.actions && Office.actions.associate) {
  Office.actions.associate("anonymizeCommand", e => command("anonymize", e));
  Office.actions.associate("restoreCommand", e => command("restore", e));
}

if (window.Office && Office.onReady) Office.onReady(info => { init(info); initialized = true; readyWaiters.forEach(r => r()); });
else init(null);
