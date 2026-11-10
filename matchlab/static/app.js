"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  forecast:null, ai:false, teamsLoading:false, teamsLoaded:false, teamsRequest:0,
  forecastRequest:0, selectedQuote:null, marketOffset:0, marketRequest:0,
  marketDetailRequest:0, marketDocument:null, eventId:null, selection:null,
  analysisContext:null, detailTab:"markets", route:"", phase:"upcoming", catalogReturn:"#matches",
  favorites:new Set(), historyRequest:0, statusRequest:0, explainRequest:0,
  aiBusy:false, aiStage:"analysis", predicting:false, pendingSelection:null, toastTimer:null,
  sport:"football", sports:[], refreshRequest:0, streamEvent:false, catalogSource:null, archiveOffset:0, archiveRequest:0,
};
const themeMedia = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
function storedTheme() {
  try { const value=localStorage.getItem('matchlab:theme:v1'); return value==='light'||value==='dark' ? value : null; }
  catch { return null; }
}
let themeOverride=storedTheme();
function setTheme(theme, save = false) {
  document.documentElement.dataset.theme=theme;
  const dark=theme==='dark', button=$('theme-toggle');
  button.setAttribute('aria-label','Тёмная тема');
  button.setAttribute('aria-pressed',String(dark));
  button.title=dark ? 'Включить светлую тему' : 'Включить тёмную тему';
  document.querySelector('meta[name="theme-color"]').content=dark ? '#121618' : '#f5f5f2';
  if (save) try { localStorage.setItem('matchlab:theme:v1',theme); } catch { /* Theme still applies for this visit. */ }
}
let guideOpen=true;
try { guideOpen=localStorage.getItem('matchlab:guide:v1')!=='hidden'; } catch { /* Show the guide when storage is unavailable. */ }
function syncGuide() {
  const open=state.route==='#matches' && guideOpen;
  $('quick-guide').hidden=!open;
  $('guide-toggle').setAttribute('aria-expanded',String(open));
}
function setGuide(open) {
  guideOpen=open;
  try { localStorage.setItem('matchlab:guide:v1',open ? 'shown' : 'hidden'); } catch { /* The choice works for this visit. */ }
  syncGuide();
}
const percent = (value) => `${(value * 100).toFixed(1)}%`;
const localDate = (value) =>
  new Date(`${value}T12:00:00Z`).toLocaleDateString("ru-RU");
const seasonLabel = (value) => `20${value.slice(0, 2)}/${value.slice(2)}`;
const bookmakerName = (source) =>
  ({ betboom: "BetBoom", winline: "Winline" })[source] || source;
const moscowTime = (value) =>
  new Date(value).toLocaleString("ru-RU", {
    timeZone: "Europe/Moscow",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
const today = new Intl.DateTimeFormat("en-CA", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  timeZone: "UTC",
}).format(new Date());

function showError(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}
async function api(url, options = {}) {
  return window.Account.api(url, options);
}

function renderSources(data) {
  $("stat-matches").textContent = data.matches.toLocaleString("ru-RU");
  $("stat-latest").textContent = data.last_match
    ? localDate(data.last_match)
    : "Нет данных";
  $("stat-period").textContent = data.first_match
    ? `${localDate(data.first_match)} — ${localDate(data.last_match)}`
    : "Нужно загрузить историю";
  if (data.stale_days > 14) {
    $("data-warning").textContent =
      `История результатов заканчивается ${localDate(data.last_match)} (${data.stale_days} дн. назад). В статистической модели нет более свежих результатов; это ограничивает её оценку новых матчей.`;
    $("data-warning").hidden = false;
  }
  $("sources-table").replaceChildren(
    ...data.sources.map((source) => {
      const row = document.createElement("tr");
      const values = [
        seasonLabel(source.season),
        source.row_count.toString(),
        "",
        new Date(source.imported_at).toLocaleDateString("ru-RU"),
      ];
      values.forEach((text) => {
        const cell = document.createElement("td");
        cell.textContent = text;
        row.append(cell);
      });
      const label = source.source.includes("raw.githubusercontent.com")
        ? "GitHub · Football-Data"
        : source.source === "local CSV"
          ? "Локальный CSV"
          : "Football-Data";
      if (/^https:\/\//.test(source.source)) {
        const link = document.createElement("a");
        link.href = source.source;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = label;
        row.children[2].append(link);
      } else row.children[2].textContent = label;
      return row;
    }),
  );
}

async function loadTeams() {
  const requestId = ++state.teamsRequest;
  state.teamsLoading = true;
  $("predict-button").disabled = true;
  try {
    const data = await api(
      `/api/teams?as_of=${encodeURIComponent($("as-of").value)}`,
    );
    if (requestId !== state.teamsRequest) return;
    for (const [id, fallback] of [
      ["home-team", "Arsenal"],
      ["away-team", "Chelsea"],
    ]) {
      const previous = $(id).value;
      $(id).replaceChildren(
        ...data.teams.map((team) => {
          const option = document.createElement("option");
          option.value = team.name;
          option.textContent = team.name;
          return option;
        }),
      );
      const names = data.teams.map((team) => team.name);
      $(id).value = names.includes(previous)
        ? previous
        : names.includes(fallback)
          ? fallback
          : names[id === "home-team" ? 0 : 1] || "";
    }
    showError("forecast-error", "");
    $("predict-button").disabled = data.teams.length < 2;
  } catch (error) {
    if (requestId === state.teamsRequest)
      showError("forecast-error", error.message);
  } finally {
    if (requestId === state.teamsRequest) state.teamsLoading = false;
  }
}

function invalidateForecast() {
  state.forecastRequest++;
  state.forecast = null;
  $("result-content").hidden = true;
  $("result-empty").hidden = false;
  $("explanation").hidden = true;
  syncAIButton();
  showError("forecast-error", "");
}

function renderForecast(result) {
  state.forecast = result;
  $("result-empty").hidden = true;
  $("result-content").hidden = false;
  $("result-tag").textContent = result.quote_id
    ? `${bookmakerName(result.market.source)} / СНИМОК`
    : result.historical_scenario
      ? "ИСТОРИЧЕСКИЙ СЦЕНАРИЙ"
      : "РУЧНОЙ СЦЕНАРИЙ";
  $("result-home").textContent = result.home_label || result.home;
  $("result-away").textContent = result.away_label || result.away;
  $("home-monogram").textContent = (result.home_label || result.home)
    .slice(0, 3)
    .toUpperCase();
  $("away-monogram").textContent = (result.away_label || result.away)
    .slice(0, 3)
    .toUpperCase();
  $("result-date").textContent = result.kickoff_at
    ? moscowTime(result.kickoff_at)
    : localDate(result.as_of);
  for (const key of ["home", "draw", "away"]) {
    $("prob-" + key).textContent = percent(result.probabilities[key]);
    $("bar-" + key).style.width = `${result.probabilities[key] * 100}%`;
  }
  $("expected-goals").textContent =
    `${result.expected_goals.home.toFixed(2)} : ${result.expected_goals.away.toFixed(2)}`;
  $("scorelines").replaceChildren(
    ...result.scorelines.map((score) => {
      const item = document.createElement("span");
      item.className = "score-chip";
      item.textContent = `${score.home}:${score.away}`;
      const chance = document.createElement("small");
      chance.textContent = percent(score.probability);
      item.append(chance);
      return item;
    }),
  );
  const training = result.training;
  $("training-note").textContent =
    `Обучение: ${training.matches.toLocaleString("ru-RU")} матчей с ${localDate(training.first_match)} по ${localDate(training.last_match)}. Учтены только результаты до ${localDate(training.before)}. ` +
    (result.quote_id
      ? "Пара и коэффициенты взяты из выбранного снимка букмекера."
      : "Расписание встречи не проверялось.");
  renderMarket(result.market);
  $("explanation").hidden = true;
  $("forecast").hidden = false;
  syncAIButton();
}

function renderMarket(market) {
  const container = $("market-result");
  container.hidden = !market;
  container.replaceChildren();
  if (!market) return;
  const title = document.createElement("div");
  const captured = ["betboom", "winline"].includes(market.source);
  title.textContent = `${captured ? bookmakerName(market.source) + " · " + observationTime(market.observed_at) + " МСК" : "Ручные коэффициенты"} · маржа ${(market.overround * 100).toFixed(1)}%`;
  container.append(title);
  const table = document.createElement("table");
  const header = document.createElement("tr");
  for (const value of ["Исход", "Коэфф.", "Рынок без маржи", "EV по модели"]) {
    const cell = document.createElement("th");
    cell.textContent = value;
    header.append(cell);
  }
  table.append(header);
  for (const [key, label] of [
    ["home", "П1"],
    ["draw", "Ничья"],
    ["away", "П2"],
  ]) {
    const row = document.createElement("tr");
    for (const value of [
      label,
      market.odds[key].toFixed(2),
      percent(market.probabilities[key]),
      percent(market.model_ev[key]),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    table.append(row);
  }
  container.append(table);
  const note = document.createElement("div");
  note.className = "muted small";
  note.textContent =
    (captured
      ? market.is_stale
        ? "Устаревший снимок: цена сейчас может отличаться. "
        : "Коэффициенты наблюдались на публичной странице. "
      : "Коэффициенты не проверены у букмекера. ") +
    "EV — расчёт из вероятностей модели, доходность не подтверждена.";
  container.append(note);
}

function drawCalibration(bins) {
  const svg = $("calibration-chart"),
    ns = "http://www.w3.org/2000/svg";
  svg.replaceChildren();
  const element = (name, attrs, text) => {
    const node = document.createElementNS(ns, name);
    Object.entries(attrs).forEach(([key, value]) =>
      node.setAttribute(key, value),
    );
    if (text !== undefined) node.textContent = text;
    svg.append(node);
    return node;
  };
  for (let i = 0; i <= 4; i++) {
    const y = 150 - i * 32;
    element("line", {
      x1: 32,
      y1: y,
      x2: 345,
      y2: y,
      stroke: "var(--line)",
      "stroke-width": 1,
    });
    element(
      "text",
      {
        x: 23,
        y: y + 3,
        fill: "var(--muted)",
        "font-size": 8,
        "text-anchor": "end",
      },
      `${i * 25}%`,
    );
  }
  element("line", {
    x1: 32,
    y1: 150,
    x2: 345,
    y2: 22,
    stroke: "var(--muted)",
    "stroke-width": 1,
    "stroke-dasharray": "4 5",
  });
  for (const bin of bins) {
    const x = 32 + bin.predicted * 313;
    const y = 150 - bin.observed * 128;
    const dot = element("circle", {
      cx: x,
      cy: y,
      r: Math.min(7, 3 + Math.sqrt(bin.count) / 12),
      fill: "var(--lime)",
    });
    const title = document.createElementNS(ns, "title");
    title.textContent = `Прогноз ${percent(bin.predicted)}, факт ${percent(bin.observed)}, наблюдений ${bin.count}`;
    dot.append(title);
  }
  [0, 0.25, 0.5, 0.75, 1].forEach((p) =>
    element(
      "text",
      {
        x: 32 + p * 313,
        y: 169,
        fill: "var(--muted)",
        "font-size": 8,
        "text-anchor": "middle",
      },
      percent(p).replace(".0", ""),
    ),
  );
}

async function loadBacktest() {
  const data = await api("/api/backtest");
  if (!data.available) {
    $("backtest-empty").hidden = false;
    $("backtest-empty").textContent = data.reason;
    $("stat-tested").textContent = "—";
    $("stat-tested-note").textContent = "Нет актуального отчёта";
    return;
  }
  const report = data.report;
  $("backtest-content").hidden = false;
  $("stat-tested").textContent = report.model.matches.toLocaleString("ru-RU");
  $("stat-tested-note").textContent = report.seasons
    .map(seasonLabel)
    .join(" · ");
  for (const [id, key] of [
    ["log", "log_loss"],
    ["brier", "brier"],
    ["accuracy", "accuracy"],
  ]) {
    $("metric-" + id).textContent =
      key === "accuracy"
        ? percent(report.model[key])
        : report.model[key].toFixed(3);
    $("base-" + id).textContent =
      key === "accuracy"
        ? percent(report.baseline[key])
        : report.baseline[key].toFixed(3);
  }
  $("backtest-period").textContent =
    `${localDate(report.first_test_date)} — ${localDate(report.last_test_date)}. Brier: сумма ошибок по трём исходам.`;
  $("skip-note").textContent =
    `Оценено ${report.model.matches} из ${report.selected_matches} матчей. Пропущено ${report.skipped_matches}: недостаточно прошлых матчей команды. Сравнение с букмекерами пока недоступно.`;
  drawCalibration(report.calibration);
}

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
// Render a small formatting subset using DOM nodes; source and AI text never become HTML.
function analysisInline(parent, text) {
  const pattern = /\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*/g;
  let position = 0, match;
  while ((match = pattern.exec(text))) {
    parent.append(document.createTextNode(text.slice(position, match.index)));
    const element = document.createElement(match[3] ? "em" : "strong");
    element.textContent = match[1] || match[2] || match[3];
    parent.append(element); position = pattern.lastIndex;
  }
  parent.append(document.createTextNode(text.slice(position)));
}
function analysisDisplayText(value) {
  const names = {statistical_model:"статистическая модель",team_statistics:"статистика участников",archive_history:"архив результатов",head_to_head:"личные встречи",web_research:"интернет-поиск"};
  return String(value || "")
    .replace(/&#(?:x([0-9a-f]+)|(\d+));/gi, (all, hex, decimal) => {
      const code = parseInt(hex || decimal, hex ? 16 : 10);
      return Number.isFinite(code) && code >= 32 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff) ? String.fromCodePoint(code) : all;
    })
    .replace(/\\([\\`*_{}\[\]()#+\-.!>])/g, "$1")
    .replace(/`?\b(statistical_model|team_statistics|archive_history|head_to_head|web_research)\b`?/gi, (_, name) => names[name.toLowerCase()]);
}
function reportParts(text) {
  const pattern=/(?:^|\s)(Победитель|Победные рынки(?: \(оценка\))?|Аргументы|Ограничения)[ \t]*:[ \t]*/g;
  const matches=[...text.matchAll(pattern)];
  if(matches.length<3 || matches.length>4 || text.slice(0,matches[0].index).trim()) return null;
  const labels=matches.map(match=>match[1]);
  if(labels[0]!=="Победитель" || !labels[1].startsWith("Победные рынки") || labels[2]!=="Аргументы" || (labels[3] && labels[3]!=="Ограничения")) return null;
  return matches.map((match,index)=>text.slice(match.index+match[0].length,matches[index+1]?.index ?? text.length).trim());
}
function reportItems(value) {
  const items=[]; let start=0, depth=0;
  for(let index=0;index<value.length;index++) {
    if(value[index]==="(" || value[index]==="[") depth++;
    else if(value[index]===")" || value[index]==="]") depth=Math.max(0,depth-1);
    else if(value[index]===";" && depth===0) {items.push(value.slice(start,index).trim());start=index+1;}
  }
  items.push(value.slice(start).trim());
  return items.filter(Boolean);
}
function reportProbability(parent,value) {
  const pattern=/>?\d+(?:[.,]\d+)?%/g; let start=0;
  for(const match of value.matchAll(pattern)) {
    parent.append(document.createTextNode(value.slice(start,match.index)),el("strong","report-probability",match[0]));
    start=match.index+match[0].length;
  }
  parent.append(document.createTextNode(value.slice(start)));
}
function renderReport(target,parts) {
  const nodes=[], lead=el("section","report-section report-lead");
  const heading=el("div","report-heading"), byMarket=parts[0].includes("по линии");
  heading.append(el("h3","","Наиболее вероятный исход"));
  if(/\d+(?:[.,]\d+)?%/.test(parts[0])) heading.append(el("span","report-badge",byMarket ? "По линии" : "Расчёт"));
  lead.append(heading);
  const winner=parts[0].match(/^(.*?)\s+[—–]\s+(>?\d+(?:[.,]\d+)?%)(.*)$/s);
  if(winner) {
    const row=el("div","report-winner"); row.append(el("span","report-team",winner[1]),el("strong","report-chance",winner[2]));lead.append(row);
    const scope=winner[3].replace(/^[\s(]*(?:по линии|оценка)[;,]\s*/,"").replace(/\)[.]?$/,"").trim();
    if(scope) lead.append(el("p","report-scope",scope));
  } else lead.append(el("p","report-no-winner",parts[0]));
  if(byMarket) lead.append(el("p","report-basis","Архивная оценка по коэффициентам букмекера; в новых разборах коэффициенты не используются."));
  else if(/\d+(?:[.,]\d+)?%/.test(parts[0])) lead.append(el("p","report-basis","Независимая оценка по подтверждённым результатам матчей. Коэффициенты букмекера в расчёте не используются."));
  nodes.push(lead);
  for(const [index,title] of [[1,"Рынки"],[2,"Аргументы"]]) {
    const section=el("section","report-section"); section.append(el("h3","",title));
    const list=el("ul",index===1 ? "report-markets" : "report-facts");
    for(const item of reportItems(parts[index])) {
      const li=el("li"); if(index===1) reportProbability(li,item); else li.textContent=item;
      list.append(li);
    }
    section.append(list);nodes.push(section);
  }
  if(parts[3]) {
    const details=el("details","report-limitations"); details.open=true;
    details.append(el("summary","","Ограничения данных"),el("p","",parts[3])); nodes.push(details);
  }
  target.replaceChildren(...nodes);
}
function renderAnalysis(target, text) {
  const displayed=analysisDisplayText(text), parts=reportParts(displayed);
  target.classList.toggle("structured-report",Boolean(parts));
  if(parts) {renderReport(target,parts); return;}
  const nodes = []; let paragraph = [], list = null;
  const flush = () => { if (paragraph.length) { const p=el("p"); analysisInline(p,paragraph.join("\n")); nodes.push(p); paragraph=[]; } };
  for (const line of displayed.split(/\r?\n/)) {
    if (!line.trim()) { flush(); list=null; continue; }
    const heading=line.match(/^\s*#{1,6}\s+(.+?)\s*#*\s*$/);
    const bullet=line.match(/^\s*[-*•]\s+(.+)$/);
    const numbered=line.match(/^\s*\d{1,3}[.)]\s+(.+)$/);
    if (heading) { flush(); list=null; const h=el("h3"); analysisInline(h,heading[1]); nodes.push(h); }
    else if (bullet || numbered) { flush(); const kind=numbered ? "OL" : "UL"; if (!list || list.tagName !== kind) { list=el(kind.toLowerCase()); nodes.push(list); } const item=el("li"); analysisInline(item,(bullet || numbered)[1]); list.append(item); }
    else { list=null; paragraph.push(line); }
  }
  flush(); target.replaceChildren(...nodes);
}
function analysisPreview(text) {
  return analysisDisplayText(text).replace(/^\s*#{1,6}\s+/gm, "").replace(/^\s*[-*•]\s+/gm, "• ").replace(/\*\*([^*\n]+)\*\*|__([^_\n]+)__|\*([^*\n]+)\*/g, (_,a,b,c) => a || b || c);
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3500);
}
function clearToast() { clearTimeout(state.toastTimer); $("toast").hidden = true; }
function readFavorites() {
  try {
    const values = JSON.parse(localStorage.getItem("matchlab:favorites:v1") || "[]");
    return new Set(Array.isArray(values) ? values.filter(value => typeof value === "string" && /^\d{1,20}$/.test(value)).slice(0,100) : []);
  } catch { return new Set(); }
}
function toggleFavorite(id) {
  if (!state.favorites.has(id) && state.favorites.size >= 100) { toast("В избранном уже 100 матчей. Удалите ненужные встречи."); return; }
  const added = !state.favorites.has(id);
  added ? state.favorites.add(id) : state.favorites.delete(id);
  if(added) window.ProductAnalytics?.goal('favorite_add');
  try {
    localStorage.setItem("matchlab:favorites:v1", JSON.stringify([...state.favorites]));
    toast(added ? "Матч добавлен в избранное" : "Матч удалён из избранного");
  } catch { toast("Избранное сохранено только до закрытия страницы."); }
  $("nav-favorites").textContent = state.favorites.size;
  document.querySelectorAll("[data-favorite]").forEach(button => {
    const active = state.favorites.has(button.dataset.favorite);
    button.setAttribute("aria-pressed", String(active));
    button.textContent = active ? "★" : "☆";
    button.setAttribute("aria-label", active ? "Удалить матч из избранного" : "Добавить матч в избранное");
  });
  if (state.eventId) updateEventFavorite();
  if (state.route === "#favorites") loadCatalogue();
}
function updateEventFavorite() {
  const active = state.favorites.has(state.eventId);
  $("event-favorite").setAttribute("aria-pressed", String(active));
  $("event-favorite").textContent = active ? "★ В избранном" : "☆ В избранное";
}
function observationTime(value) { return new Intl.DateTimeFormat("ru-RU",{timeZone:"Europe/Moscow",hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(new Date(value)); }
function ageLabel(event) {
  const age = event.age_seconds ?? Math.max(0, (Date.now() - Date.parse(event.capture_started_at)) / 1000);
  if (event.source_kind === "stream" && age < 45) return "Обновлено " + observationTime(event.capture_started_at) + " МСК";
  if (age < 60) return "Обновлено меньше минуты назад";
  if (age < 3600) return `Обновлено ${Math.floor(age / 60)} мин. назад`;
  if (age < 86400) return `Обновлено ${Math.floor(age / 3600)} ч. назад`;
  return `Данные от ${moscowTime(event.capture_started_at)}`;
}
function phaseLabel(event) {
  if (event.phase === "live") return event.is_stale ? "LIVE · ДАННЫЕ ЗАДЕРЖИВАЮТСЯ" : "LIVE";
  if (event.phase === "finished") return "ИТОГ ПОЛУЧЕН";
  if (event.source_present === false) return "СОБЫТИЕ БОЛЬШЕ НЕ ОБНОВЛЯЕТСЯ";
  if (event.kickoff_at && Date.parse(event.kickoff_at) <= Date.now()) return "НАЧАЛСЯ";
  return event.phase === "prematch" ? "ДО МАТЧА" : "СТАТУС НЕ УКАЗАН";
}
function dayValue(offset) {
  const now = new Date(Date.now() + offset * 86400000);
  return new Intl.DateTimeFormat("en-CA", {timeZone:"Europe/Moscow",year:"numeric",month:"2-digit",day:"2-digit"}).format(now);
}
function showView(id) {
  document.querySelectorAll("[data-view]").forEach(view => { view.hidden = view.id !== id; });
}
function mainMarket(document) {
  if (document.headline?.key) return document.markets.find(market => market.key === document.headline.key);
  return document.markets.find(market => /^Исход\s+1[XХ]2$/i.test(market.name) && !market.parameter && ["оснвремя","основноевремя"].includes((market.period || "").replace(/[.\s]/g, "").toLowerCase()));
}
function eventTitle(event) {
  return event.title || [event.home,event.away].filter(Boolean).join(" — ") || "Спортивное событие";
}
function sportLabel(key) {
  return state.sports.find(sport => sport.key === key)?.label || ({football:"Футбол",hockey:"Хоккей",tennis:"Теннис",basketball:"Баскетбол",volleyball:"Волейбол",table_tennis:"Настольный теннис"})[key] || key || "Весь спорт";
}
function renderSports(sports = state.sports) {
  state.sports = sports;
  const selected = state.sport;
  const select = $("sport-filter");
  select.replaceChildren(el("option", "", "Все виды спорта"), ...sports.map(sport => { const option = el("option", "", `${sport.label} · ${sport.total}`); option.value = sport.key; return option; }));
  select.options[0].value = ""; select.value = selected;
  const popular = ["","football","hockey","tennis","basketball","volleyball","table_tennis"];
  if (selected && !popular.includes(selected)) popular.push(selected);
  $("sport-tabs").replaceChildren(...popular.map(key => {
    const button = el("button", key === selected ? "sport-tab active" : "sport-tab", sportLabel(key)); button.type = "button";
    button.setAttribute("aria-pressed", String(key === selected)); button.addEventListener("click", () => chooseSport(key)); return button;
  }));
  if (state.route === "#matches") {
    $("catalog-title").textContent = sportLabel(selected);
    $("catalog-eyebrow").textContent = `${sportLabel(selected).toLocaleUpperCase("ru-RU")} · СПОРТИВНАЯ АНАЛИТИКА`;
    $("breadcrumb").textContent = `Линия / ${sportLabel(selected)}`;
  }
}
function chooseSport(key) {
  state.sport = key; state.marketOffset = 0;
  $("competition-filter").value = ""; $("match-search").value = "";
  try { localStorage.setItem("matchlab:sport:v1",key); } catch { /* The selection still works for this page. */ }
  renderSports(); loadCatalogue();
}
function oddsStrip(preview, eventId, selectable = false) {
  const strip = el("div", "odds-strip");
  const outcomes = preview.outcomes || [["home","П1"],["draw","X"],["away","П2"]].map(([side,name]) => ({side,name,odds:preview[side],state:"open"}));
  strip.style.setProperty("--odds-count",outcomes.length);
  for (const outcome of outcomes) {
    const tile = el(selectable ? "button" : "div", "odds-tile");
    if (selectable) {
      tile.type = "button";
      tile.disabled = outcome.state !== "open";
      tile.setAttribute("aria-label", `${outcome.name}, коэффициент ${outcome.odds ?? "недоступен"}. Выбрать для разбора ИИ`);
      tile.addEventListener("click", () => openSelection(eventId, preview.key ? {marketKey:preview.key,outcomeKey:outcome.key} : outcome.side));
    }
    tile.append(el("span", "", outcome.name), el("strong", "", outcome.state === "open" ? Number(outcome.odds).toFixed(2) : outcome.state === "suspended" ? "Пауза" : "Недоступен"));
    strip.append(tile);
  }
  return strip;
}
function setOptions(id, values, placeholder) {
  const select = $(id), selected = select.value;
  const signature = JSON.stringify(values);
  if (select.dataset.options === signature) return;
  select.dataset.options = signature;
  select.replaceChildren(el("option", "", placeholder), ...values.map(value => {
    const option = el("option", "", value); option.value = value; return option;
  }));
  select.options[0].value = "";
  select.value = values.includes(selected) ? selected : "";
}
function renderLeagues(competitions) {
  const box = $("league-nav");
  const buttons = ["", ...competitions].map(value => {
    const button = el("button", value === $("competition-filter").value ? "league-link active" : "league-link", value || "Все турниры");
    button.type = "button"; button.title = value || "Все турниры";
    button.addEventListener("click", () => {
      $("competition-filter").value = value; state.marketOffset = 0;
      if (!["#matches","#favorites"].includes(state.route)) location.hash = "#matches";
      else loadCatalogue();
    }); return button;
  });
  box.replaceChildren(el("p", "eyebrow", "ТУРНИРЫ"), ...buttons);
}
async function loadCatalogue() {
  const request = ++state.marketRequest;
  const favorites = state.route === "#favorites";
  $("market-event-rows").setAttribute("aria-busy", "true");
  $("market-refresh").disabled = true;
  try {
    if (favorites && !state.favorites.size) {
      renderCatalogue({events:[],total:0,offset:0,limit:30});
      return true;
    }
    const query = new URLSearchParams({offset:String(state.marketOffset),limit:"30",include_started:"true",phase:state.phase,sport:state.sport,search:$("match-search").value.trim(),competition:$("competition-filter").value,sort:$("sort-filter").value});
    const day = $("day-filter").value;
    if (day) query.set("day", dayValue(day === "tomorrow" ? 1 : 0));
    if (favorites) query.set("ids", [...state.favorites].join(","));
    const result = await api("/api/bookmakers/events?" + query);
    if (request !== state.marketRequest || !["#matches","#favorites"].includes(state.route)) return;
    if (state.marketOffset > 0 && state.marketOffset >= result.total) { state.marketOffset = 0; return loadCatalogue(); }
    renderCatalogue(result);
    return true;
  } catch (error) {
    if (request === state.marketRequest) showError("market-list-error", error.message);
  } finally {
    if (request === state.marketRequest) {
      $("market-event-rows").setAttribute("aria-busy", "false");
      $("market-refresh").disabled = false;
    }
  }
}
function renderCatalogue(result) {
  showError("market-list-error", "");
  state.catalogSource=result.source_kind || null;
  const favoriteView = state.route === "#favorites";
  if (result.summary) {
    renderSports(result.sports || state.sports);
    $("nav-matches").textContent = result.summary.upcoming;
    $("count-upcoming").textContent = result.summary.upcoming;
    $("count-live").textContent = result.summary.live;
    $("count-fresh").textContent = result.summary.fresh;
    $("catalog-updated").textContent = result.summary.last_observed_at ? `Последние данные · ${moscowTime(result.summary.last_observed_at)} МСК` : "Ожидаем первый сбор";
    setOptions("competition-filter", result.competitions, "Все турниры");
    renderLeagues(result.competitions);
  }
  $("market-list-summary").textContent = result.total ? `Найдено событий: ${result.total} · ${result.offset + 1}–${result.offset + result.events.length}` : "Совпадений нет";
  syncClearFilters();
  $("catalog-page").textContent = result.total ? `${Math.floor(result.offset / result.limit) + 1} / ${Math.ceil(result.total / result.limit)}` : "";
  $("market-prev").disabled = result.offset === 0;
  $("market-next").disabled = result.offset + result.events.length >= result.total;
  $("market-list-empty").hidden = result.events.length > 0;
  const isEmptyFavorites = favoriteView && !state.favorites.size;
  $("catalog-empty-title").textContent = isEmptyFavorites ? "Сохраните интересные матчи" : "Матчи не найдены";
  $("catalog-empty-text").textContent = isEmptyFavorites ? "Нажмите звезду рядом с встречей — она появится здесь. Избранное хранится в этом браузере." : "Попробуйте другую дату, турнир или статус. Если сбор только начался, обновите список немного позже.";
  $("reset-filters").hidden = isEmptyFavorites;
  const focused = document.activeElement?.dataset.favorite;
  const rows = [], groups = new Map();
  for (const event of result.events) {
    const group = state.sport ? event.competition : `${event.sport_label || sportLabel(event.sport)} · ${event.competition}`;
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(event);
  }
  for (const [competition, events] of groups) {
    const group = el("section", "tournament-group");
    const heading = el("div", "tournament-heading"); heading.append(el("h2", "", competition), el("span", "", `${events.length} в списке`)); group.append(heading);
    for (const event of events) {
      const card = el("article", "match-card"); card.dataset.eventId = event.source_event_id;
      const favorite = el("button", "favorite-button", state.favorites.has(event.source_event_id) ? "★" : "☆");
      favorite.type = "button"; favorite.dataset.favorite = event.source_event_id;
      favorite.setAttribute("aria-pressed", String(state.favorites.has(event.source_event_id)));
      favorite.setAttribute("aria-label", state.favorites.has(event.source_event_id) ? "Удалить матч из избранного" : "Добавить матч в избранное");
      favorite.addEventListener("click", () => toggleFavorite(event.source_event_id)); card.append(favorite);
      const info = el("div", "match-info");
      const title = el("a", "match-card-title", event.title || `${event.home}\n${event.away}`); title.href = eventRoute(event);
      if (event.live_score) info.append(el("strong","live-score",`${event.live_score}${event.live_clock ? " · " + event.live_clock : ""}`));
      const meta = el("div", "match-card-meta"); meta.append(el("span", event.phase === "live" ? "phase-label live" : "phase-label", phaseLabel(event)), el("span", "", event.kickoff_at ? moscowTime(event.kickoff_at) : "Время не указано"));
      info.append(title, meta); card.append(info);
      const headline = event.headline;
      const quoteBox = el("div", "headline-box");
      if (headline) {
        quoteBox.append(el("p", "headline-caption", [headline.name,headline.period,headline.outcome_count > 3 ? `${headline.outcome_count} исходов` : null].filter(Boolean).join(" · ")), oddsStrip(headline,event.source_event_id,true));
      } else if (event.primary_odds) quoteBox.append(oddsStrip(event.primary_odds,event.source_event_id,true));
      else quoteBox.append(el("div","odds-placeholder","Коэффициенты — в рынках события"));
      card.append(quoteBox);
      const actions = el("div", "match-actions");
      const ai = el("button", "match-ai-button", "Разбор ИИ"); ai.type = "button";
      ai.addEventListener("click", () => {window.ProductAnalytics?.goal('ai_intent');openSelection(event.source_event_id, null, true);});
      const markets = el("a", "match-open", event.source_kind === "stream" ? "Матч и рынки →" : `${event.coverage.captured_markets} рынков →`); markets.href = eventRoute(event);
      const bookmaker=el("a","match-bookmaker-link","На Winline ↗");bookmaker.href=`/go/winline/${event.source_event_id}?source=${event.source_kind === "stream" ? "stream" : "legacy"}`;bookmaker.target="_blank";bookmaker.rel="sponsored noopener noreferrer";
      actions.append(ai, markets, bookmaker); card.append(actions);
      const freshness = el("div", "match-freshness"); freshness.append(el("span", event.is_stale ? "freshness-badge stale" : "freshness-badge", ageLabel(event)));
      if (!event.coverage.complete && event.source_kind !== "stream") freshness.append(el("span", "partial-label", event.source_kind === "stream" ? "Основные рынки" : "Неполный снимок"));
      card.append(freshness); group.append(card);
    }
    rows.push(group);
  }
  $("market-event-rows").replaceChildren(...rows);
  if (focused) [...document.querySelectorAll("[data-favorite]")].find(button => button.dataset.favorite === focused)?.focus({preventScroll:true});
}
function resetFilters() {
  for (const id of ["match-search","competition-filter","day-filter"]) $(id).value = "";
  $("sort-filter").value = "kickoff"; state.phase = "upcoming"; state.marketOffset = 0;
  syncPhase(); loadCatalogue();
}
function syncClearFilters() {
  $('clear-active-filters').hidden=!(state.phase!=='upcoming' || $('match-search').value.trim() || $('competition-filter').value || $('day-filter').value || $('sort-filter').value!=='kickoff');
}
function syncPhase() {
  document.querySelectorAll("[data-phase]").forEach(button => {
    const selected = button.dataset.phase === state.phase;
    button.classList.toggle("active", selected); button.setAttribute("aria-pressed", String(selected));
  });
}
function clearAIOutput() {
  state.explainRequest++;
  state.aiAnalysis = null;
  stopAIWait(); $("ai-wait").hidden = true;
  $("explanation").hidden = true; $("ai-analysis-meta").hidden = true;
  $("ai-analysis-sources").replaceChildren();
  showError("ai-request-error", "");
}
function syncAIButton() {
  $("explain-button").disabled = state.aiBusy || !state.ai || (!state.marketDocument && !state.forecast);
  $("explain-button").textContent = state.aiBusy ? (state.aiTarget === state.eventId && state.aiTargetRequest === state.explainRequest ? "ИИ готовит разбор…" : "Завершаем предыдущий разбор…") : "Получить разбор ИИ";
  $("ai-wait").hidden = !(state.aiBusy && state.aiTarget === state.eventId && state.aiTargetRequest === state.explainRequest);
  $("ai-hint").textContent = !state.ai ? "Сервис ИИ пока не подключён. Рынки доступны для просмотра." : !state.marketDocument && !state.forecast ? "Сначала выберите матч или рассчитайте сценарий." : "ИИ сопоставляет рынки и доступную историю участников. Это разбор фактов, а не гарантия исхода.";
}
let aiWaitTimer = null;
const aiWaitMessages = ["Проверяем доступные сведения о матче.", "Свежесть источников влияет на вывод.", "Если сведений мало, мы отметим ограничения.", "Подготовка ответа может занять некоторое время."];
const jobWaitMessages = ["Модель сопоставляет сведения о матче.", "Разбор выполняется в фоне и сохранится в истории.", "Большой объём данных требует времени.", "Можно вернуться к матчу позже — повторный запрос не нужен."];
const queueWaitMessages = ["Разбор в очереди. Ожидаем освобождения модели.", "Задание сохранено. Результат появится в истории."];
const statisticsWaitMessages = ["Получаем статистику выбранного матча.", "Проверяем форму и встречи участников.", "Данные источника загружаются; затем начнётся разбор."];
const marketWaitMessages = ["Ожидаем обновления рынков выбранного матча.", "Проверяем доступные условия для независимой оценки."];
function startAIWait() {
  clearInterval(aiWaitTimer);
  let index = 0;
  const show=()=>{const messages=state.aiStage === "statistics" ? statisticsWaitMessages : state.aiStage === "markets" ? marketWaitMessages : state.aiStage === "queued" ? queueWaitMessages : state.aiStage === "background" ? jobWaitMessages : aiWaitMessages; $("ai-wait-message").textContent=messages[index % messages.length];};
  show();
  aiWaitTimer = setInterval(() => { index++; show(); }, 8500);
}
function stopAIWait() { clearInterval(aiWaitTimer); aiWaitTimer = null; }
function selectAI(market = null, outcome = null) {
  if (!state.marketDocument) return;
  state.selection = market ? {marketKey:market.key,outcomeKey:outcome?.key || null} : null;
  clearAIOutput(); renderAISelection(); renderMarketGroups();
}
function marketVariantLabel(market) {
  if (!market.variant_id) return null;
  const siblings=(state.marketDocument?.markets || []).filter(item => item.variant_id &&
    item.name===market.name && item.group===market.group && item.period===market.period && item.parameter===market.parameter);
  const others=new Set(siblings.filter(item=>item.key!==market.key).flatMap(item=>item.outcomes.map(outcome=>outcome.name)));
  const distinct=market.outcomes.map(outcome=>outcome.name).filter(name=>!others.has(name));
  const labels=distinct.length ? distinct : market.outcomes.map(outcome=>outcome.name);
  return `Вариант ${Math.max(1,siblings.findIndex(item=>item.key===market.key)+1)} · исходы: ${labels.slice(0,4).join(", ")}${labels.length>4 ? ", …" : ""}`;
}
function renderAISelection() {
  const document = state.marketDocument;
  if (!document) { syncAIButton(); return; }
  $("ai-empty").hidden = true; $("ai-selection").hidden = false;
  $("ai-match-label").textContent = eventTitle(document);
  const market = document.markets.find(item => item.key === state.selection?.marketKey);
  const outcome = market?.outcomes.find(item => item.key === state.selection?.outcomeKey);
  $("ai-market-label").textContent = market ? [market.name,market.period,market.parameter,marketVariantLabel(market)].filter(Boolean).join(" · ") : "Матч целиком";
  $("ai-outcome-label").hidden = !outcome;
  $("ai-outcome-label").textContent = outcome ? `${outcome.name} · ${outcome.odds ?? "недоступен"}` : "";
  $("ai-clear-selection").hidden = !market;
  $("ai-snapshot-label").textContent = ageLabel(document);
  renderAIAnalysisMeta();
  syncAIButton();
}
function renderAIAnalysisMeta() {
  const analysis = state.aiAnalysis;
  if (!analysis?.context) return;
  const context = analysis.context;
  const market = context.markets.find(item => item.key === context.selection.market_key);
  const outcome = market?.outcomes.find(item => item.key === context.selection.outcome_key);
  const changed = state.marketDocument && ((context.snapshot.content_version ? context.snapshot.content_version !== state.marketDocument.content_version : state.marketDocument.observed_at !== analysis.observed_at) || context.archive_history?.revision !== state.archiveHistory?.revision);
  const prep=context.statistics_preparation?.state;
  const focused=context.market_selection;
  const focusNote=focused ? ` Для разбора выбрано ${focused.selected_markets} из ${focused.available_markets} рынков.${focused.context_only_keys?.length ? " Свежей линии нет; сохранённая показана только для контекста." : ""}` : "";
  const prepNote=prep && prep !== "ok" ? " Статистику сейчас обновить не удалось; использованы доступные данные." : analysis.preparation_notice || "";
  $("ai-analysis-meta").textContent = `${analysis.analysis_type === "limited_summary" ? "Автоматическая сводка" : analysis.analysis_type === "statistical_report" ? "Расчётный разбор" : analysis.analysis_type === "partial_model" ? "Частичный разбор ИИ" : "Разбор"} от ${moscowTime(analysis.generated_at)} МСК.${changed ? ` Данные обновились; ответ относится к снимку от ${moscowTime(context.snapshot.observed_at)} МСК.` : ""}${context.archive_history?.state === "ready" ? " История участников найдена." : ""}${context.team_statistics ? " Статистика участников доступна." : ""}${focusNote}${prepNote}`;
  $("ai-analysis-meta").hidden = false;
  renderResearchSources($("ai-analysis-sources"), context.web_research);
}
function renderResearchSources(target, research) {
  target.replaceChildren();
  if (!research || ["disabled", "not_applicable"].includes(research.state)) return;
  if (research.state !== "ready") {
    target.append(el("p", "small muted", research.state === "empty" ? "Подходящих интернет-источников не найдено." : "Интернет-поиск временно недоступен; разбор основан на остальных данных."));
    return;
  }
  const details = el("details", "research-sources"), summary = el("summary", "", `Интернет-источники · ${research.sources.length}`), list = el("ol");
  details.append(summary, el("p", "small muted", `Поиск: ${moscowTime(research.searched_at)} МСК.${research.partial ? " Часть запросов не выполнена." : ""} Сведения источников требуют проверки.`));
  for (const source of research.sources) {
    let url;
    try { url = new URL(source.url); } catch { continue; }
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) continue;
    const item = el("li"), link = el("a", "", source.title);
    link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
    item.append(link);
    if (source.published_at) item.append(el("span", "small muted", ` · дата источника: ${source.published_at}${source.date_inferred ? " (предположительно)" : ""}`));
    list.append(item);
  }
  details.append(list); target.append(details);
}
function eventRoute(event) { return `#${event.source_kind === "stream" ? "stream" : "match"}/${event.source_event_id}`; }
function eventAPI(id) { return `${state.streamEvent ? "/api/archive/events/" : "/api/bookmakers/events/"}${id}`; }
function isEventRoute() { return /^#(?:match|stream)\//.test(state.route); }
function openSelection(id, side = null, auto = false) {
  if (auto && !Account.user) {
    Account.ensure(()=>openSelection(id,side,auto)).then(allowed=>{if(allowed)openSelection(id,side,auto)}); return;
  }
  state.pendingSelection = {id, side, auto};
  const stream=state.marketDocument ? state.marketDocument.source_kind === "stream" : state.catalogSource === "stream";
  const target=`#${stream ? "stream" : "match"}/${id}`;
  if (state.route === target) applyPendingSelection(); else location.hash=target;
}
function applyPendingSelection() {
  const pending = state.pendingSelection;
  if (!pending || pending.id !== state.eventId || !state.marketDocument) return;
  state.pendingSelection = null;
  const market = typeof pending.side === "object" && pending.side ? state.marketDocument.markets.find(item => item.key === pending.side.marketKey) : mainMarket(state.marketDocument);
  const keys = {home:["П1","1"],draw:["X","Х"],away:["П2","2"]};
  const outcome = pending.side && market ? market.outcomes.find(item => typeof pending.side === "object" ? item.key === pending.side.outcomeKey : keys[pending.side].includes(item.name.toUpperCase())) : null;
  selectAI(outcome ? market : null, outcome);
  if (pending.auto) requestAI();
  if (pending.side) $("ai-desk").scrollIntoView({behavior:"smooth",block:"nearest"});
}
async function loadEvent(refresh = false) {
  const id = state.eventId, request = ++state.marketDetailRequest;
  if (!refresh) { state.refreshRequest++; $("event-refresh").disabled=false; $("event-refresh-status").textContent=""; }
  const prefix=eventAPI(id),stream=state.streamEvent;
  if(stream) api(`/api/archive/events/${id}/watch`,{method:"POST",body:"{}"}).catch(()=>{});
  const [detail, context, teamStatistics, archived] = await Promise.allSettled([api(prefix+"/markets"),api(prefix+"/analysis"),api(prefix+"/statistics"),api(`/api/archive/events/${id}/history?source=${stream ? "stream" : "legacy"}`)]);
  if (request !== state.marketDetailRequest || state.eventId !== id || !isEventRoute()) return;
  if (detail.status === "rejected") { showError("detail-error", detail.reason.message); return; }
  showError("detail-error", "");
  state.marketDocument = detail.value;
  const document = detail.value;
  state.analysisContext = context.status === "fulfilled" ? context.value : null;
  renderTeamStatistics(teamStatistics.status === "fulfilled" ? teamStatistics.value : null);
  state.archiveHistory = archived.status === "fulfilled" ? archived.value : null;
  renderArchiveHistory(state.archiveHistory);
  $("event-header").hidden = false;
  $("market-detail-title").textContent = eventTitle(document);
  $("event-competition").textContent = `${document.sport_label || sportLabel(document.sport)} · ${document.competition}`;
  $("breadcrumb").textContent = `Линия / ${document.sport_label || sportLabel(document.sport)}`;
  $("tab-analysis").hidden = !state.analysisContext?.prediction_available;
  $("event-kickoff").textContent = document.kickoff_at ? `${moscowTime(document.kickoff_at)} МСК` : "Начало встречи в снимке не указано";
  $("event-phase").textContent = [phaseLabel(document),document.final_result?.score_text || document.live_score,document.source_present === false ? null : document.live_clock].filter(Boolean).join(" · ");
  $("event-phase").classList.toggle("live", document.phase === "live");
  const cards=document.red_card_presence || {};
  const cardTeams=document.phase === "live" ? [["home","Хозяева"],["away","Гости"]].filter(([side])=>cards[side] === true).map(([side,fallback])=>document[side] || fallback) : [];
  $("event-red-card").hidden=!cardTeams.length;
  $("event-red-card").textContent=cardTeams.length ? `Удаление: ${cardTeams.join(", ")} · ${moscowTime(document.observed_at)} МСК${document.is_stale ? " · данные могли устареть" : ""}` : "";
  $("event-source").href = `/go/winline/${id}?source=${document.source_kind === "stream" ? "stream" : "legacy"}`;
  $("event-freshness").textContent = ageLabel(document);
  $("event-freshness").classList.toggle("stale", document.is_stale);
  $("market-detail-meta").textContent = `Источник: Winline. ${document.coverage.captured_markets} рынков, ${document.coverage.captured_outcomes} исходов. Наблюдение: ${moscowTime(document.capture_started_at)}–${moscowTime(document.observed_at)} МСК. ${document.coverage.complete ? "Полный снимок страницы." : "Снимок неполный: часть рынков могла не загрузиться."}`;
  if(document.source_kind === "stream") $("market-detail-meta").textContent=`Получено ${document.coverage.captured_markets} рынков${document.coverage.expected_markets ? " из " + document.coverage.expected_markets + " объявленных" : ""}. Событие наблюдалось ${moscowTime(document.capture_started_at)} МСК. У цен своё время наблюдения; дополнительные рынки запрашиваются при открытии матча.`;
  $("market-count").textContent = document.coverage.captured_markets;
  $("event-primary").hidden = !document.headline;
  $("event-primary").replaceChildren(...(document.headline ? [el("p","headline-caption",[document.headline.name,document.headline.period,document.headline.outcome_count > 3 ? `${document.headline.outcome_count} исходов` : null].filter(Boolean).join(" · ")), oddsStrip(document.headline,id,true), el("p","odds-explainer","Выберите коэффициент, чтобы разобрать исход.")] : []));
  if (state.selection && !document.markets.some(item => item.key === state.selection.marketKey && (!state.selection.outcomeKey || item.outcomes.some(outcome => outcome.key === state.selection.outcomeKey)))) {
    state.selection = null; clearAIOutput(); toast("Рынки изменились. Выберите исход заново.");
  }
  setOptions("market-group-filter", [...new Set(document.markets.map(market => market.group).filter(Boolean))].sort((a,b) => a.localeCompare(b,"ru")), "Все группы");
  updateEventFavorite(); renderMarketGroups(); renderAISelection();
  $("analysis-availability").textContent = state.analysisContext?.prediction_available ? "Расчёт по истории голов АПЛ и сохранённому основному исходу. ИИ-разбор доступен справа для любого рынка." : state.analysisContext?.unavailable_reason || "Статистическая модель пока недоступна. ИИ-разбор рынков доступен в панели аналитика.";
  $("event-predict").disabled = state.predicting || !state.analysisContext?.prediction_available;
  if (!refresh) applyPendingSelection();
  return true;
}
function renderTeamStatistics(data) {
  const snapshot=data?.statistics, stale=snapshot?.observed_at && Date.now()-Date.parse(snapshot.observed_at)>3600000;
  const available=snapshot?.state === "ok";
  const blocked=data?.link_status === "found" && Date.parse(data?.source_blocked_until || "") > Date.now();
  $("team-statistics-status").textContent=available ? stale ? "· последнее обновление давно" : snapshot.capture_kind === "fast" ? "· предварительная сводка" : "· краткая сводка" : blocked ? "· временно недоступна" : "· пока нет данных";
  $("team-statistics-note").textContent=available ? `Обновлено ${moscowTime(snapshot.observed_at)} МСК.${snapshot.match_phase === "finished" ? " Включает результат этой встречи." : ""}${blocked ? " Источник временно ограничил обновление; показаны сохранённые данные." : ""}` : blocked ? "Источник временно ограничил доступ к расширенной статистике. Повторный запрос будет доступен после паузы." : "Статистику этого матча запросим при запуске ИИ-разбора. Ниже показана доступная история.";
  if(available && snapshot.capture_kind === "fast") $("team-statistics-note").textContent+=" Доступна предварительная информация; подробные показатели ещё не получены.";
  if(available && Array.isArray(snapshot.observed_views) && snapshot.observed_views.length<3) $("team-statistics-note").textContent+=" Часть вкладок источника пока недоступна.";
  if(available && snapshot.sections_truncated) $("team-statistics-note").textContent+=" Часть данных не вошла в снимок.";
  if(available && snapshot.season_filters_complete === false) $("team-statistics-note").textContent+=" Часть сезонных показателей пока не получена.";
  $("team-statistics-source").hidden=true;
  const brief=(text,limit=240)=>{const lines=[...new Set((text || "").split(/\n+/).map(s=>s.trim()).filter(Boolean))];const value=lines.join(" · ");return value.length>limit ? value.slice(0,limit).replace(/\s+\S*$/,"")+"…" : value;};
  const cards=[];
  if(available && snapshot.header){const card=el("article","statistics-brief-card");card.append(el("h3","","Обзор участников"),el("p","",brief(snapshot.header)));cards.push(card);}
  const sectionPriority=section=>/статистика матча|статистика игры|статистика встречи|match statistics|match stats|статистика сезона|таблица|лучшие игроки|top players/i.test(section.title) ? 0 : 1;
  for(const section of available ? snapshot.sections.filter(section=>!section.filters).sort((a,b)=>sectionPriority(a)-sectionPriority(b)).slice(0,4) : []){const card=el("article","statistics-brief-card");card.append(el("h3","",section.title),el("p","",brief(section.text)));cards.push(card);}
  $("team-statistics-sections").classList.add("statistics-brief-grid");
  $("team-statistics-sections").replaceChildren(...cards);
}

function renderMarketGroups() {
  if (!state.marketDocument) return;
  const focusedSelection = document.activeElement?.dataset.selection;
  const opened = new Set([...$("market-groups").querySelectorAll("details[open]")].map(item => item.dataset.key));
  const query = $("market-search").value.trim().toLocaleLowerCase("ru-RU"), group = $("market-group-filter").value;
  const markets = state.marketDocument.markets.filter(market => (!group || market.group === group) && [market.name,market.group,market.period,market.parameter,...market.outcomes.map(outcome => outcome.name)].filter(Boolean).join(" ").toLocaleLowerCase("ru-RU").includes(query));
  $("market-search-count").textContent = `${markets.length} из ${state.marketDocument.markets.length} рынков`;
  $("market-search-empty").hidden = markets.length > 0;
  $("market-groups").replaceChildren(...markets.map(market => {
    const details = el("details", "market-group"); details.dataset.key = market.key;
    const summary = el("summary", "", market.name);
    const caption = [market.group,market.period,market.parameter,marketVariantLabel(market)].filter(Boolean).join(" · ");
    summary.append(el("small", "", `${caption ? caption + " · " : ""}${market.outcomes.length} исходов${market.observed_at ? " · " + observationTime(market.observed_at) + " МСК" + (market.is_stale ? " · прошлое наблюдение" : "") : ""}`)); details.append(summary);
    if (market.variant_id) details.append(el("p","small muted","У источника есть несколько блоков с этим названием. Условия каждого варианта уточняйте на странице события."));
    let rendered = false;
    const render = () => {
      if (rendered || !details.open) return;
      rendered = true; const outcomes = el("div", "market-outcomes");
      market.outcomes.forEach(outcome => {
        const button = el("button", "market-outcome"); button.type = "button";
        button.dataset.selection = `${market.key}/${outcome.key}`;
        button.disabled = outcome.state !== "open";
        button.setAttribute("aria-pressed", String(state.selection?.marketKey === market.key && state.selection?.outcomeKey === outcome.key));
        const price = outcome.state === "open" ? String(outcome.odds) : outcome.state === "suspended" ? "Пауза" : "Недоступен";
        button.append(el("span", "", [outcome.name,outcome.parameter].filter(Boolean).join(" · ")), el("strong", "", price));
        button.title = "Выбрать для разбора ИИ";
        button.addEventListener("click", () => { selectAI(market,outcome); if (window.innerWidth <= 1050) $("ai-desk").scrollIntoView({behavior:"smooth",block:"nearest"}); });
        outcomes.append(button);
      }); details.append(outcomes);
    };
    details.addEventListener("toggle", render);
    details.open = opened.has(market.key); render(); return details;
  }));
  if (focusedSelection) [...$("market-groups").querySelectorAll("[data-selection]")].find(button => button.dataset.selection === focusedSelection)?.focus({preventScroll:true});
}
function setDetailTab(tab) {
  state.detailTab = tab;
  const markets = tab === "markets";
  $("market-detail").hidden = !markets;
  $("analysis-panel").hidden = markets;
  for (const value of ["markets","analysis"]) {
    const selected = value === tab;
    $("tab-" + value).setAttribute("aria-selected", String(selected)); $("tab-" + value).tabIndex = selected ? 0 : -1;
  }
}
async function waitForAnalysisJob(job, current) {
  let failures=0;
  while(current()) {
    if(job.state === "completed") {
      if(!job.result) throw new Error("Разбор завершён. Откройте результат в истории.");
      return job.result;
    }
    if(job.state === "failed") throw new Error(job.error || "Не удалось завершить разбор. Попытка не списана.");
    const stage=job.state === "queued" ? "queued" : "background";
    if(state.aiStage !== stage) {
      state.aiStage=stage;
      $("ai-wait-message").textContent=(stage === "queued" ? queueWaitMessages : jobWaitMessages)[0];
    }
    await new Promise(resolve=>setTimeout(resolve,2000));
    if(!current()) return null;
    try { job=await api("/api/analysis-jobs/"+job.id,{cache:"no-store"}); failures=0; }
    catch(error) {
      if([401,403,404].includes(error.status)) throw error;
      if(++failures>=5) throw new Error("Связь с сайтом прервалась. Разбор сохранён; вернитесь к матчу или проверьте историю позже.");
    }
  }
  return null;
}
async function requestAI() {
  if (!await Account.ensure(()=>requestAI())) return;
  const accountId=Account.user.id;
  Account.track("ai_intent",state.eventId || "forecast");
  if (state.aiBusy) { toast("Дождитесь завершения предыдущего разбора."); return; }
  const document = state.marketDocument, forecast = state.forecast;
  if (!document && !forecast) return;
  window.ProductAnalytics?.goal('ai_request_start');
  const request = ++state.explainRequest, selection = state.selection ? {...state.selection} : null;
  const eventId = state.eventId, forecastId = forecast?.id;
  state.aiBusy = true; state.aiTarget = eventId; state.aiTargetRequest = request; state.aiStage = document ? "statistics" : "analysis"; startAIWait(); syncAIButton(); showError("ai-request-error", "");
  $("explanation").hidden = true; $("ai-analysis-meta").hidden = true; $("ai-analysis-sources").replaceChildren();
  $("explanation").setAttribute("aria-busy", "true");
  try {
    const current=()=>Account.user?.id === accountId && request === state.explainRequest && eventId === state.eventId;
    let statisticsJobId=null, preparationNotice="";
    const pending=document ? (await api('/api/analysis-jobs/current',{cache:'no-store'})).job : null;
    if(pending && (pending.event_id !== eventId || pending.source !== (state.streamEvent ? "archive" : "bookmakers")))
      throw new Error("Для вашего аккаунта уже готовится другой разбор. Результат появится в истории.");
    if(document && !pending) {
      const prefix=eventAPI(eventId);
      try {
        // The server also starts watching this event's markets before queueing statistics.
        const queued=await api(prefix+"/statistics/request",{method:"POST",body:"{}",cache:"no-store"});
        let finished=null;
        for(let attempt=0;attempt<60 && current();attempt++) {
          const status=await api(prefix+"/statistics/request/"+queued.job_id,{cache:"no-store"});
          if(status.state === "finished") {finished=status; break;}
          if(status.state === "timeout") break;
          await new Promise(resolve=>setTimeout(resolve,2000));
        }
        if(!current()) return;
        if(finished) statisticsJobId=finished.job_id;
        else preparationNotice=" Статистику не удалось дождаться; использованы доступные данные.";
      } catch(error) {
        if(error.status===401 || error.status===403) throw error;
        preparationNotice=" Статистику сейчас обновить не удалось; использованы доступные данные.";
      }
      if(!current()) return;
      if(state.streamEvent) {
        state.aiStage="markets"; $("ai-wait-message").textContent=marketWaitMessages[0];
        try {
          for(let attempt=0;attempt<5 && current();attempt++) {
            const fresh=await api(prefix+"/markets",{cache:"no-store"});
            if(fresh.source_present === false || fresh.markets.filter(m=>!m.is_stale).length>1) break;
            await new Promise(resolve=>setTimeout(resolve,2000));
          }
        } catch(error) {
          if(error.status===401 || error.status===403) throw error;
          preparationNotice+=" Обновление условий рынков пока не подтверждено.";
        }
        if(!current()) return;
      }
      await loadEvent(true);
      if(!current()) return;
    }
    state.aiStage="analysis"; $("ai-wait-message").textContent=aiWaitMessages[0];
    const latest=document ? state.marketDocument : null;
    if(document && !latest) throw new Error("Не удалось обновить матч. Повторите разбор.");
    const endpoint = document ? eventAPI(eventId)+"/analysis-jobs" : "/api/explain";
    const body = document ? {content_version:latest.content_version || null, observed_at:latest.observed_at, market_key:selection?.marketKey || null, outcome_key:selection?.outcomeKey || null, notes:$("notes").value, statistics_job_id:statisticsJobId} : {forecast_id:forecastId,notes:$("notes").value};
    const submitted=pending || await api(endpoint, {method:"POST",body:JSON.stringify(body)});
    const result=document ? await waitForAnalysisJob(submitted,current) : submitted;
    if(!result) return;
    if (Account.user?.id !== accountId || request !== state.explainRequest || eventId !== state.eventId || (!document && forecastId !== state.forecast?.id)) return;
    renderAnalysis($("explanation"),result.text); $("explanation").hidden = false;
    window.ProductAnalytics?.goal(['limited_summary','statistical_report','partial_model'].includes(result.analysis_type) ? 'ai_summary_result' : 'ai_model_result');
    if (result.context) {
      result.preparation_notice=preparationNotice;
      state.aiAnalysis = result; renderAIAnalysisMeta();
    }
  } catch (error) {
    if(request === state.explainRequest) window.ProductAnalytics?.goal('ai_request_error');
    if(error.status===401 && eventId===state.eventId) Account.show("login",()=>requestAI());
    else if (request === state.explainRequest) showError("ai-request-error", error.message);
  } finally {
    state.aiBusy = false; stopAIWait(); $("explanation").setAttribute("aria-busy", "false"); syncAIButton();
  }
}
async function calculateEvent() {
  const quote = state.analysisContext?.quote;
  if (!quote || state.predicting) return;
  invalidateForecast(); const request = state.forecastRequest, eventId = state.eventId;
  state.predicting = true; $("event-predict").disabled = true; $("event-predict").textContent = "Рассчитываем…";
  try {
    const result = await api("/api/forecast", {method:"POST",body:JSON.stringify({home:quote.model_teams[0],away:quote.model_teams[1],quote_id:quote.id})});
    if (request === state.forecastRequest && eventId === state.eventId) renderForecast(result);
  } catch (error) { if (request === state.forecastRequest) showError("detail-error", error.message); }
  finally {
    state.predicting = false; $("event-predict").textContent = "Рассчитать вероятности";
    $("event-predict").disabled = !state.analysisContext?.prediction_available;
  }
}
async function loadHistory() {
  if (!await Account.ensure(()=>loadHistory())) return;
  const request = ++state.historyRequest;
  try {
    const result = await api("/api/analyses");
    if (request !== state.historyRequest || state.route !== "#history") return;
    showError("history-error", ""); $("history-empty").hidden = result.analyses.length > 0;
    $("history-list").replaceChildren(...result.analyses.map(analysis => {
      const card = el("article", "panel history-card"), info = el("div");
      info.append(el("h2", "", eventTitle(analysis.event)), el("p", "small muted", `${analysis.event.sport_label || "Футбол"} · ${analysis.market_name} · ${moscowTime(analysis.generated_at)} МСК`));
      const preview=analysisPreview(analysis.text);
      const excerpt = el("p", "history-excerpt", preview.length > 200 ? preview.slice(0,200) + "…" : preview); info.append(excerpt);
      const open = el("a", "secondary-button", "Открыть разбор"); open.href = `#analysis/${analysis.id}`; card.append(info,open); return card;
    }));
    return true;
  } catch (error) { if (request === state.historyRequest) showError("history-error", error.message); }
}
async function loadSavedAnalysis(id) {
  if (!await Account.ensure(()=>loadSavedAnalysis(id))) return;
  const request = ++state.marketDetailRequest;
  try {
    const analysis = await api(`/api/analyses/${encodeURIComponent(id)}`);
    if (request !== state.marketDetailRequest || state.route !== `#analysis/${id}`) return;
    const context = analysis.context;
    $("manual-heading").hidden = false;
    $("manual-heading").querySelector("h1").textContent = eventTitle(analysis.event);
    $("manual-heading").querySelector(".subtitle").textContent = `${analysis.analysis_type === "limited_summary" ? "Автоматическая сводка" : analysis.analysis_type === "statistical_report" ? "Расчётный разбор" : analysis.analysis_type === "partial_model" ? "Частичный разбор ИИ" : "Разбор"} от ${moscowTime(analysis.generated_at)} МСК · снимок ${moscowTime(analysis.observed_at)} МСК`;
    $("saved-analysis").hidden = false;
    $("saved-analysis-title").textContent = analysis.market_name;
    renderAnalysis($("saved-analysis-text"),analysis.text);
    $("saved-analysis-coverage").textContent = `Учтено ${context.coverage.analyzed_markets} из ${context.coverage.stored_markets} сохранённых рынков. Данные и текст относятся ко времени этого разбора.`;
    renderResearchSources($("saved-analysis-sources"), context.web_research);
    $("saved-analysis-open").href = eventRoute(analysis.event);
  } catch (error) { if (request === state.marketDetailRequest) showError("detail-error", error.message); }
}
async function loadStatus() {
  const request = ++state.statusRequest;
  try {
    const status = await api("/api/status");
    if (request !== state.statusRequest) return;
    state.ai = Boolean(status.ai?.configured); renderSources(status.data);
    $("ai-state").textContent = state.ai ? "ИИ настроен" : "ИИ не подключён";
    renderArchiveStatus(status.archive || {});
    const worker = status.bookmakers.worker;
    const labels = {starting:"Сбор запускается",ok:"Последний цикл завершён",partial:"Сбор идёт, данные частичные",stopped:"Сбор остановлен",error:"Ошибка сбора",blocked:"Источник ограничил доступ",empty:"Нет распознанных матчей",disconnected:"Нет соединения",unavailable:"Источник недоступен"};
    const current = worker?.heartbeat_recent;
    const label = worker ? (current ? labels[worker.state] || "Проверяем сбор" : "Давно нет ответа сборщика") : "Сборщик ещё не отчитался";
    $("feed-state").textContent = label; $("feed-dot").classList.toggle("green", Boolean(current && ["ok","partial"].includes(worker.state)));
    if(status.archive?.available){
      $("feed-state").textContent=status.archive.connected && !status.archive.source_stale ? "Поток Winline обновляется" : "Сохранённый поток Winline";
      $("feed-dot").classList.toggle("green",Boolean(status.archive.connected && !status.archive.source_stale));
    }
    $("worker-title").textContent = label;
    $("worker-description").textContent = worker ? `Последний отчёт: ${moscowTime(worker.last_attempt_at)} МСК. Целевой интервал — ${worker.interval_seconds} с. Свежесть каждого матча указана отдельно.` : "Ожидаем первый отчёт серверного сборщика.";
    $("worker-progress").textContent = worker?.progress ? `В текущем цикле: ${worker.progress.discovered} найдено, ${worker.progress.saved} доставлено, ${worker.progress.failed} ошибок.` : "";
    showError("global-error", ""); syncAIButton();
    return true;
  } catch (error) { if (request === state.statusRequest) { showError("global-error", error.message); $("feed-state").textContent = "Нет ответа сервиса"; $("feed-dot").classList.remove("green"); } }
}
function moveAIDesk() {
  const mobile = window.innerWidth <= 1050;
  const host = mobile ? $("ai-mobile-host") : $("ai-desktop-host");
  if ($("ai-desk").parentElement !== host) host.append($("ai-desk"));
}
async function route() {
  const previous = state.route;
  let hash = location.hash || "#matches";
  const aliases = {"#markets":"#matches","#bookmakers":"#matches","#forecast":"#calculator","#validation":"#status","#data":"#status"};
  if (aliases[hash]) { hash = aliases[hash]; history.replaceState(null,"",hash); }
  const match = hash.match(/^#(?:match|stream)\/(\d{1,20})$/), saved = hash.match(/^#analysis\/([a-f0-9]{32})$/);
  if (!["#matches","#favorites","#history","#archive","#status","#calculator","#account","#admin"].includes(hash) && !match && !saved) { hash = "#matches"; history.replaceState(null,"",hash); }
  state.route = hash; document.getElementById('view-matches').classList.toggle('ym-show-content',hash === '#matches'); Account.track("page_view",hash); window.ProductAnalytics?.hit(hash); state.marketRequest++; state.marketDetailRequest++; state.historyRequest++;
  $("league-nav").hidden=!["#matches","#favorites"].includes(hash) && !match;
  document.body.classList.toggle("without-ai-desk", ["#matches","#favorites","#history","#archive","#status","#account","#admin"].includes(hash) || Boolean(saved));
  syncGuide();
  state.streamEvent=hash.startsWith("#stream/");
  if (previous !== hash) { invalidateForecast(); state.marketDocument = null; state.analysisContext = null; state.eventId = null; state.selection = null; clearAIOutput(); renderTeamStatistics(null); renderArchiveHistory(null); $("notes").value = ""; $("ai-empty").hidden = false; $("ai-selection").hidden = true; }
  $("saved-analysis").hidden = true;
  $("team-statistics").hidden = !match;
  document.querySelectorAll(".nav-link").forEach(link => { const active = link.getAttribute("href") === hash; link.classList.toggle("active",active); active ? link.setAttribute("aria-current","page") : link.removeAttribute("aria-current"); });
  $("nav-favorites").textContent = state.favorites.size;
  $("forecast").hidden = true; $("event-header").hidden = true; $("manual-heading").hidden = true; $("manual-selection").hidden = true;
  showError("detail-error", "");
  if (hash === "#matches" || hash === "#favorites") {
    showView("view-matches");
    $("catalog-title").textContent = hash === "#favorites" ? "Избранное" : sportLabel(state.sport);
    $("catalog-subtitle").textContent = hash === "#favorites" ? "Ваши матчи в этом браузере. Сохраните звёздочкой нужные встречи." : "Выберите матч, чтобы изучить рынки и получить разбор ИИ.";
    $("catalog-stats").hidden = hash === "#favorites";
    $("breadcrumb").textContent = hash === "#favorites" ? "Линия / Избранное" : `Линия / ${sportLabel(state.sport)}`;
    state.catalogReturn = hash; state.marketOffset = 0; loadCatalogue();
  } else if (hash === "#account") {
    showView("view-account"); $("breadcrumb").textContent="Личный кабинет"; Account.renderPage();
  } else if (hash === "#admin") {
    showView("view-admin"); $("breadcrumb").textContent="Управление / Обзор"; window.Admin?.open();
  } else if (hash === "#archive") {
    showView("view-archive"); $("breadcrumb").textContent="Данные / Архив матчей"; state.archiveOffset=0; loadArchive();
  } else if (hash === "#history") {
    showView("view-history"); $("breadcrumb").textContent = "История / Разборы ИИ"; loadHistory();
  } else if (hash === "#status") {
    showView("view-status"); $("breadcrumb").textContent = "Состояние сервиса";
    loadStatus(); loadBacktest().catch(error => showError("backtest-empty",error.message));
  } else {
    showView("view-analysis"); $("event-tabs").hidden = !match;
    $("analysis-panel").hidden = Boolean(saved); $("market-detail").hidden = !match;
    $("event-analysis-controls").hidden = !match;
    $("forecast").classList.toggle("manual-mode",hash === "#calculator");
    if (match) {
      state.eventId = match[1]; Account.track("match_view",match[1]); $("breadcrumb").textContent = "Линия / Матч";
      $("match-back").href = state.catalogReturn; $("match-back").textContent = "← К линии";
      $("market-search").value = ""; $("market-group-filter").value = "";
      $("market-groups").replaceChildren(el("p","small muted","Загружаем рынки…"));
      setDetailTab("markets"); loadEvent();
    } else if (hash === "#calculator") {
      $("breadcrumb").textContent = "Инструменты / Ручной расчёт";
      $("match-back").href = "#status"; $("match-back").textContent = "← К состоянию сервиса";
      $("manual-heading").hidden = false;
      $("manual-heading").querySelector("h1").textContent = "Ручной расчёт";
      $("manual-heading").querySelector(".subtitle").textContent = "Исследовательский сценарий по истории АПЛ. Выбранная пара не подтверждает наличие матча в расписании.";
      $("manual-selection").hidden = false; $("forecast").hidden = false;
      $("as-of").value ||= today; if (!state.teamsLoaded) loadTeams().then(() => { state.teamsLoaded = true; });
    } else if (saved) {
      $("breadcrumb").textContent = "История / Сохранённый разбор";
      $("match-back").href = "#history"; $("match-back").textContent = "← К истории разборов"; loadSavedAnalysis(saved[1]);
    }
  }
  moveAIDesk(); syncAIButton();
  if (previous !== hash) { window.scrollTo(0,0); $("main-content").focus({preventScroll:true}); }
}
function clearQuote() {
  state.selectedQuote = null; $("quote-selection").hidden = true;
  for (const key of ["home","draw","away"]) $("odds-" + key).disabled = $("as-of").value !== today;
}
$("forecast-form").addEventListener("submit", async event => {
  event.preventDefault(); if (state.teamsLoading) return;
  invalidateForecast(); const request = state.forecastRequest;
  $("predict-button").disabled = true; $("predict-button").textContent = "Рассчитываем…";
  document.querySelector(".result-panel").setAttribute("aria-busy","true");
  try {
    const payload = {home:$("home-team").value,away:$("away-team").value,as_of:$("as-of").value};
    const values = ["home","draw","away"].map(key => $("odds-" + key).value);
    if (values.some(Boolean)) { if (!values.every(Boolean)) throw new Error("Заполните все три коэффициента или оставьте поля пустыми."); payload.odds = Object.fromEntries(["home","draw","away"].map((key,i) => [key,Number(values[i])])); }
    const result = await api("/api/forecast",{method:"POST",body:JSON.stringify(payload)});
    if (request === state.forecastRequest) {
      renderForecast(result); $("ai-empty").hidden = true; $("ai-selection").hidden = false;
      $("ai-match-label").textContent = `${result.home} — ${result.away}`;
      $("ai-market-label").textContent = "Ручной сценарий · статистическая оценка";
      $("ai-outcome-label").hidden = true; $("ai-clear-selection").hidden = true;
      $("ai-snapshot-label").textContent = `Расчёт на ${localDate(result.as_of)}`; syncAIButton();
    }
  } catch (error) { if (request === state.forecastRequest) showError("forecast-error",error.message); }
  finally { $("predict-button").disabled = state.teamsLoading; $("predict-button").textContent = "Рассчитать вероятности"; document.querySelector(".result-panel").setAttribute("aria-busy","false"); }
});
$("swap-teams").addEventListener("click", () => { clearQuote(); [$("home-team").value,$("away-team").value] = [$("away-team").value,$("home-team").value]; invalidateForecast(); });
for (const id of ["home-team","away-team","odds-home","odds-draw","odds-away"]) $(id).addEventListener("change", () => { clearQuote(); invalidateForecast(); });
$("as-of").addEventListener("change", () => { clearQuote(); invalidateForecast(); for (const key of ["home","draw","away"]) { $("odds-" + key).disabled = $("as-of").value !== today; $("odds-" + key).value = ""; } loadTeams(); });
$("quote-clear").addEventListener("click", () => { clearQuote(); invalidateForecast(); });
$("explain-button").addEventListener("click",()=>{window.ProductAnalytics?.goal('ai_intent');requestAI();});
$("ai-clear-selection").addEventListener("click", () => selectAI());
$("event-predict").addEventListener("click",calculateEvent);
$("event-favorite").addEventListener("click", () => toggleFavorite(state.eventId));
$("event-refresh").addEventListener("click", async () => {
  clearToast();
  const id=state.eventId, request=++state.refreshRequest, status=$("event-refresh-status");
  if(state.marketDocument?.source_present === false){status.textContent="Встреча ушла из текущего потока. Показаны последние наблюдения и доступный итог источника.";return;}
  const current=()=>state.refreshRequest===request && state.eventId===id && isEventRoute();
  $("event-refresh").disabled=true;
  status.textContent="Запрашиваем свежие данные этого матча…";
  try {
    if(state.streamEvent){
      await api(`/api/archive/events/${id}/watch`,{method:"POST",body:"{}"});
      status.textContent="Обновление запрошено. Новые данные появятся автоматически; смотрите время события и цен.";
      await loadEvent(true); return;
    }
    const queued=await api(`/api/bookmakers/events/${id}/refresh`,{method:"POST",body:"{}",cache:"no-store"});
    for (let attempt=0;attempt<150 && current();attempt++) {
      const result=await api(`/api/bookmakers/events/${id}/refresh/${queued.job_id}`,{cache:"no-store"});
      if (!current()) return;
      if (result.state==="finished") {
        await loadEvent(true);
        if (!current()) return;
        const markets={ok:"Рынки обновлены.",partial:"Рынки получены частично; показан доступный сохранённый снимок.",blocked:"Источник ограничил доступ к рынкам.",unavailable:"Не удалось обновить рынки."};
        const stats={ok:"Статистика обновлена.",blocked:"Источник ограничил доступ к статистике; повтор будет выполнен после паузы.",empty:"Источник не предоставил статистику.",unavailable:"Не удалось обновить статистику.",missing_link:"Ссылка на статистику этой встречи не найдена."};
        status.textContent=`${markets[result.market_state] || "Результат обновления рынков неизвестен."} ${stats[result.statistics_state] || "Результат обновления статистики неизвестен."}`;
        return;
      }
      if (result.state==="timeout") { status.textContent="Сборщик не завершил запрос вовремя. Показаны сохранённые данные; попробуйте позже."; return; }
      status.textContent=result.state==="running" ? "Собираем рынки и статистику этого матча…" : "Запрос в очереди сборщика…";
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
    if (current()) status.textContent="Ожидание обновления завершилось. Проверьте состояние сборщика и повторите позже.";
  } catch (error) { if (current()) status.textContent=error.message; }
  finally { if (current()) $("event-refresh").disabled=false; }
});
$("market-refresh").addEventListener("click", async () => { clearToast(); loadStatus(); if (await loadCatalogue()) toast("Список обновлён из сохранённых данных. Сбор идёт автоматически."); });
$("event-show-markets").addEventListener("click",() => { setDetailTab("markets"); const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches; $("event-tabs").scrollIntoView({behavior:reduced ? "auto" : "smooth",block:"start"}); $("tab-markets").focus({preventScroll:true}); });
$("history-refresh").addEventListener("click", async () => { clearToast(); $("history-refresh").disabled = true; try { if (await loadHistory()) toast("История обновлена."); } finally { $("history-refresh").disabled = false; } });
$("status-refresh").addEventListener("click", async () => { clearToast(); $("status-refresh").disabled = true; try { const [ok] = await Promise.all([loadStatus(),loadBacktest().catch(error => showError("backtest-empty",error.message))]); if (ok) toast("Состояние сервиса обновлено."); } finally { $("status-refresh").disabled = false; } });
$("market-prev").addEventListener("click", () => { state.marketOffset = Math.max(0,state.marketOffset - 30); loadCatalogue(); });
$("market-next").addEventListener("click", () => { state.marketOffset += 30; loadCatalogue(); });
$("reset-filters").addEventListener("click",resetFilters);
$("clear-active-filters").addEventListener("click",resetFilters);
$('theme-toggle').addEventListener('click',() => { themeOverride=document.documentElement.dataset.theme==='dark' ? 'light' : 'dark'; setTheme(themeOverride,true); });
$('guide-close').addEventListener('click',() => { setGuide(false); $('guide-toggle').focus(); });
$('guide-toggle').addEventListener('click',() => {
  if (state.route!=='#matches') { setGuide(true); location.hash='#matches'; }
  else setGuide(!guideOpen);
});
if (themeMedia?.addEventListener) themeMedia.addEventListener('change',event => { if (!themeOverride) setTheme(event.matches ? 'light' : 'dark'); });
let searchTimer;
$("match-search").addEventListener("input", () => { syncClearFilters(); clearTimeout(searchTimer); state.marketRequest++; searchTimer = setTimeout(() => { state.marketOffset = 0; loadCatalogue(); },250); });
for (const id of ["competition-filter","day-filter","sort-filter"]) $(id).addEventListener("change", () => { syncClearFilters(); state.marketOffset = 0; loadCatalogue(); });
document.querySelectorAll("[data-phase]").forEach(button => button.addEventListener("click", () => { state.phase = button.dataset.phase; state.marketOffset = 0; syncPhase(); syncClearFilters(); loadCatalogue(); }));
$("market-search").addEventListener("input",renderMarketGroups);
$("market-group-filter").addEventListener("change",renderMarketGroups);
$("sport-filter").addEventListener("change", () => chooseSport($("sport-filter").value));
for (const tab of ["markets","analysis"]) $("tab-" + tab).addEventListener("click", () => setDetailTab(tab));
$("event-tabs").addEventListener("keydown", event => { if ($("tab-analysis").hidden || !["ArrowLeft","ArrowRight","Home","End"].includes(event.key)) return; event.preventDefault(); const tab = event.key === "Home" ? "markets" : event.key === "End" ? "analysis" : state.detailTab === "markets" ? "analysis" : "markets"; setDetailTab(tab); $("tab-" + tab).focus(); });
window.addEventListener("hashchange",route);
window.addEventListener("resize",moveAIDesk);
window.addEventListener("storage",event => { if (event.key === "matchlab:favorites:v1") { state.favorites = readFavorites(); $("nav-favorites").textContent = state.favorites.size; if (["#matches","#favorites"].includes(state.route)) loadCatalogue(); if (state.eventId) updateEventFavorite(); } });
window.addEventListener('storage',event => {
  if (event.key==='matchlab:theme:v1') { themeOverride=storedTheme(); setTheme(themeOverride || (themeMedia?.matches ? 'light' : 'dark')); }
  if (event.key==='matchlab:guide:v1') { guideOpen=event.newValue!=='hidden'; syncGuide(); }
});
setInterval(() => { if (document.visibilityState !== "visible") return; loadStatus(); if (["#matches","#favorites"].includes(state.route)) loadCatalogue(); else if (state.eventId) loadEvent(true); else if(state.route === "#archive") loadArchive(); },10000);
state.favorites = readFavorites(); $("as-of").value = today;
try { const key = localStorage.getItem("matchlab:sport:v1"); if (key !== null && (!key || /^[a-z][a-z0-9_-]{0,49}$/.test(key))) state.sport = key; } catch { /* Default to football when storage is unavailable. */ }
setTheme(document.documentElement.dataset.theme); loadStatus(); route();

function renderArchiveStatus(data) {
  const active=data.connected && !data.source_stale;
  const label=active ? "Поток обновляется" : data.available ? "Сохранённые данные" : "Ожидаем первую передачу";
  const text=data.last_snapshot_at ? `Последнее наблюдение: ${moscowTime(data.last_snapshot_at)} МСК. Исторических результатов: ${(data.stored_results || 0).toLocaleString("ru-RU")}.${data.sent_cursor < data.source_cursor ? " Начальная передача истории ещё идёт." : ""}${!data.connected ? " Нет свежего отчёта о доставке." : data.source_stale ? " Источник давно не обновлялся; время у каждого матча указано отдельно." : ""}` : "База на сервере заполняется. После первой передачи здесь появятся данные.";
  $("archive-service-title").textContent=label; $("archive-service-note").textContent=text;
  if(data.unidentified_results) $("archive-service-note").textContent+=` Записей источника без даты или участников: ${data.unidentified_results}; они сохранены отдельно от списка результатов.`;
  $("archive-total").textContent=(data.stored_results || 0).toLocaleString("ru-RU");
  $("archive-events-total").textContent=(data.events || 0).toLocaleString("ru-RU");
  $("archive-sync-note").textContent=text;
}
function resultRow(row) {
  const item=el("div","archive-result-row");
  item.append(el("time","small muted",moscowTime(row.start_at)),el("span","",row.members ? row.members.join(" — ") : `${row.member1} — ${row.member2}`),el("strong","archive-result-score",row.score_text || row.score || "Счёт не указан"));
  return item;
}
function renderArchiveHistory(data) {
  const box=$("archive-team-history");
  box.hidden=!data;
  if(!data)return;
  $("archive-binding").textContent=data.state === "ready" ? "Последние игры" : "Данных пока недостаточно";
  $("archive-team-note").textContent=data.state === "ready" ? "Последние результаты участников до начала выбранной встречи." : data.reason || "Сопоставленная история ещё не поступила. Отсутствие записей не означает отсутствие игр.";
  $("archive-team-cards").replaceChildren(...(data.teams || []).map(team=>{
    const card=el("article","archive-team-card");card.append(el("h3","",team.name));
    card.append(el("p","small muted",team.available_result_records ? "Недавние результаты" : "Совпадающие результаты ещё не накоплены"));
    card.append(...team.recent_results.slice(0,3).map(resultRow));return card;
  }));
  $("archive-h2h-list").replaceChildren(...(data.head_to_head?.length ? data.head_to_head.slice(0,2).map(resultRow) : [el("p","small muted","Совпадающих личных встреч пока нет в базе.")]));
}
async function loadArchive() {
  const request=++state.archiveRequest;
  $("archive-results").setAttribute("aria-busy","true");
  try {
    const query=new URLSearchParams({limit:"30",offset:String(state.archiveOffset),search:$("archive-search").value.trim()});
    if($("archive-sport").value)query.set("sport",$("archive-sport").value);
    const [data,status]=await Promise.all([api("/api/archive/results?"+query),api("/api/archive/status")]);
    if(request!==state.archiveRequest || state.route!=="#archive")return;
    renderArchiveStatus(status);
    const select=$("archive-sport"),selected=select.value,signature=JSON.stringify((status.sports || []).map(s=>[s.sport_id,s.sport_label]));
    if(select.dataset.options!==signature){
      select.dataset.options=signature;
      select.replaceChildren(el("option","","Все виды спорта"),...(status.sports || []).map(s=>{const o=el("option","",s.sport_label);o.value=s.sport_id;return o}));
      select.options[0].value="";select.value=selected;
    }
    showError("archive-error","");
    $("archive-summary").textContent=data.total ? `Результатов: ${data.total} · ${data.offset+1}–${data.offset+data.results.length}` : "Совпадающих результатов пока нет";
    $("archive-results").replaceChildren(...(data.results.length ? data.results.map(r=>{
      const card=el("article","archive-result-card");card.append(el("p","small muted",`${r.sport_label} · ${moscowTime(r.observed_at)} МСК — время получения`),resultRow(r));return card;
    }) : [el("p","empty-state panel",status.connected ? "История ещё наполняется. Попробуйте другой вид спорта или участника." : "Ожидаем передачу с сервера сбора. Текущие матчи доступны в разделе «Матчи».")]));
    $("archive-prev").disabled=state.archiveOffset===0;$("archive-next").disabled=data.offset+data.results.length>=data.total;
  } catch(error){if(request===state.archiveRequest)showError("archive-error",error.message)}
  finally{if(request===state.archiveRequest)$("archive-results").setAttribute("aria-busy","false")}
}
let archiveSearchTimer;
$("archive-search").addEventListener("input",()=>{clearTimeout(archiveSearchTimer);archiveSearchTimer=setTimeout(()=>{state.archiveOffset=0;loadArchive()},300)});
$("archive-sport").addEventListener("change",()=>{state.archiveOffset=0;loadArchive()});
$("archive-refresh").addEventListener("click",loadArchive);
$("archive-prev").addEventListener("click",()=>{state.archiveOffset=Math.max(0,state.archiveOffset-30);loadArchive()});
$("archive-next").addEventListener("click",()=>{state.archiveOffset+=30;loadArchive()});

document.addEventListener("accountchange",()=>{
  state.explainRequest++;clearAIOutput();syncAIButton();
  $("explanation").replaceChildren();$("history-list").replaceChildren();$("saved-analysis-text").replaceChildren();
  if(state.route==="#account") Account.renderPage();
  if(state.route==="#history"){if(Account.user)loadHistory();else $("history-empty").hidden=false;}
});
