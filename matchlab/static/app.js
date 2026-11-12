"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  forecast:null, ai:false, analysisMode:"independent", verdictSports:{}, teamsLoading:false, teamsLoaded:false, teamsRequest:0,
  forecastRequest:0, selectedQuote:null, marketOffset:0, marketRequest:0,
  marketDetailRequest:0, marketDocument:null, eventId:null, selection:null,
  analysisContext:null, detailTab:"markets", route:"", phase:"upcoming", catalogReturn:"#matches",
  favorites:new Set(), historyRequest:0, statusRequest:0, explainRequest:0,
  aiBusy:false, aiStage:"analysis", predicting:false, pendingSelection:null, toastTimer:null,
  sport:"football", sports:[], refreshRequest:0, streamEvent:false, catalogSource:null, archiveOffset:0, archiveRequest:0,
};
const isAdmin = () => window.Account?.user?.role === "admin";
const supportedVerdict = event => Boolean(state.verdictSports[event?.sport]) && (!event.event_kind || event.event_kind==='match');
function syncRoleUI() {
  document.documentElement.dataset.role = isAdmin() ? "admin" : "user";
  if (!isAdmin() && $("sort-filter").value === "freshness") $("sort-filter").value = "kickoff";
}
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
let guideOpen=false;
try { guideOpen=localStorage.getItem('matchlab:guide:v1')==='shown'; } catch { /* Show the guide when storage is unavailable. */ }
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
function renderReport(target,parts,assessment=null,generation=null) {
  const nodes=[], lead=el("section","report-section report-lead");
  const heading=el("div","report-heading"), byMarket=parts[0].includes("по линии");
  heading.append(el("h3","","Наиболее вероятный исход"));
  if(/\d+(?:[.,]\d+)?%/.test(parts[0])) heading.append(el("span","report-badge",byMarket ? "По линии" : "Предварительная оценка"));
  lead.append(heading);
  const winner=parts[0].match(/^(.*?)\s+[—–]\s+(>?\d+(?:[.,]\d+)?%)(.*)$/s);
  if(winner) {
    const row=el("div","report-winner"); row.append(el("span","report-team",winner[1]),el("strong","report-chance",winner[2]));lead.append(row);
    const scope=winner[3].replace(/^[\s(]*(?:по линии|оценка)[;,]\s*/,"").replace(/\)[.]?$/,"").trim();
    if(scope) lead.append(el("p","report-scope",scope));
  } else lead.append(el("p","report-no-winner",parts[0]));
  if(winner && !byMarket) {
    lead.append(el("p","report-quality","Вероятности модели предварительные: точность процентов ещё не подтверждена проверкой на независимой выборке."));
    const probabilities=assessment?.outcome_probabilities;
    if(probabilities && ["home","draw","away"].every(key=>Number.isFinite(probabilities[key]) && probabilities[key]>=0 && probabilities[key]<=1) && Math.abs(probabilities.home+probabilities.draw+probabilities.away-1)<0.00001) {
      const distribution=el("dl","report-distribution"); distribution.setAttribute("aria-label","Вероятности исходов в основное время");
      for(const [key,label] of [["home",assessment.participants?.home || "П1"],["draw","Ничья"],["away",assessment.participants?.away || "П2"]]) {
        const cell=el("div");cell.append(el("dt","",label),el("dd","",`${(probabilities[key]*100).toLocaleString("ru-RU",{minimumFractionDigits:1,maximumFractionDigits:1})}%`));distribution.append(cell);
      }
      lead.append(distribution);
      const best=Math.max(...Object.values(probabilities));
      if(best<.5) lead.append(el("p","small muted",`Наиболее вероятный из трёх исходов. На остальные вместе приходится ${((1-best)*100).toLocaleString("ru-RU",{maximumFractionDigits:1})}%.`));
    }
    for(const person of assessment?.data_quality?.participants || []) {
      let description=`${person.name}: подтверждённых прошлых матчей — ${person.confirmed_matches}.`;
      if(person.confirmed_matches && person.history_from && person.history_to) description+=` Период: ${new Date(person.history_from).toLocaleDateString("ru-RU")}–${new Date(person.history_to).toLocaleDateString("ru-RU")}.`;
      if(person.uses_season) description+=person.season_matches ? ` Сезонная статистика: ${person.season_matches_inferred ? "около " : ""}${person.season_matches} матчей${person.season_matches_inferred ? " (число восстановлено по округлённым средним)" : ""}.` : " Число матчей сезонной статистики не установлено.";
      lead.append(el("p","report-sample",description));
    }
  }
  if(byMarket) lead.append(el("p","report-basis","Архивная оценка по коэффициентам букмекера; в новых разборах коэффициенты не используются."));
  else if(/\d+(?:[.,]\d+)?%/.test(parts[0])) lead.append(el("p","report-basis","Независимая оценка по истории и статистике участников. Коэффициенты букмекера в расчёте не используются."));
  nodes.push(lead);
  for(const [index,title] of [[1,"Рынки"],[2,"Аргументы"]]) {
    const section=el("section","report-section"); section.append(el("h3","",title));
    const list=el("ul",index===1 ? "report-markets" : "report-facts");
    for(const item of reportItems(parts[index])) {
      const li=el("li"); if(index===1) reportProbability(li,item); else li.textContent=item;
      if(index===1 && !byMarket) {
        const selected=(assessment?.recommended_markets || [])[list.children.length];
        const explanations=selected && generation?.market_explanations?.[selected.market_key];
        if(explanations?.length) li.append(el("p","report-market-explanation",explanations.join(" ")));
        else if(selected) li.append(el("p","small muted","Отдельное объяснение ИИ для этого рынка не получено."));
        if(selected && /тайм/i.test(selected.period || "")) li.append(el("p","small muted","Оценка тайма приближённая: отдельной подтверждённой истории таймов нет."));
      }
      list.append(li);
    }
    section.append(list);nodes.push(section);
    if(index===1 && !byMarket && winner) section.append(el("p","small muted","Высокая вероятность не доказывает выгодность ставки. Рынки одного матча связаны и не являются независимыми подтверждениями прогноза."));
  }
  if(parts[3]) {
    const details=el("details","report-limitations"); details.open=true;
    details.append(el("summary","","Ограничения данных"),el("p","",parts[3])); nodes.push(details);
  }
  target.replaceChildren(...nodes);
}
function renderNeuralVerdict(target,verdict,generation) {
  target.classList.add('structured-report','verdict-report');
  const lead=el('section','report-section report-lead'),heading=el('div','report-heading');
  const isModel=verdict.engine==='qwen',bet=verdict.decision==='bet' && Number.isFinite(verdict.stake_percent) && verdict.stake_percent>0 && verdict.stake_percent<=.5;
  heading.append(el('h3','','Вердикт'),el('span','report-badge',isModel?(verdict.data_forced_skip?'Пропуск: недостаточно данных':'Экспериментальное мнение ИИ'):'Решение ограничено данными'));
  lead.append(heading);
  if(window.Credits?.enabled)lead.append(el('p','credit-rule',verdict.decision==='skip'?'Полный ответ «Пропустить» — бесплатно. Разборы не списаны.':'Разбор списывается ТОЛЬКО за открытие решения «Ставить». Повторный просмотр бесплатен.'));
  if(Object.prototype.hasOwnProperty.call(verdict,'preference')) {
    lead.append(el('p','small muted','Претендент'),el('p','verdict-preference',verdict.preference_label || 'Никто'));
    lead.append(el('p','small muted','Предпочтение по победителю отделено от решения о ставке.'),el('h3','','Решение'));
  }
  lead.append(el('p','verdict-choice',bet?`Рассмотреть победу «${verdict.selection_label}»`:'Пропустить матч'));
  if(bet) {
    lead.append(el('p','report-scope',verdict.market_label || 'Победа в основное время'));
    lead.append(el('p','verdict-stake',`Лимит риска: до ${verdict.stake_percent.toLocaleString('ru-RU',{maximumFractionDigits:4})}% игрового банка на эту ставку`));
    if(Number.isFinite(verdict.odds)) lead.append(el('p','small muted',`Коэффициент в снимке: ${verdict.odds.toLocaleString('ru-RU')}`));
  }
  if(!isModel) lead.append(el('p','small muted',verdict.reason?.text || (verdict.engine==='model_error'?'Не удалось подтвердить вывод ИИ. Оснований для рекомендации нет — пропускаем матч.':verdict.engine==='timing_guard'?(generation?.reason==='quote_expired'?'Наблюдение коэффициентов устарело во время ожидания.':'Матч начался во время ожидания ответа.'):'Подтверждённого входа для выбора команды недостаточно.')));
  if(verdict.data_forced_skip && verdict.limitations?.length) lead.append(el('p','small muted',verdict.limitations.join(' ')));
  const sections=[];
  if(verdict.comparison) {
    const section=el('section','report-section');section.append(el('h3','','Сравнение участников'));
    const list=el('ul','report-facts');for(const side of ['home','away']) if(verdict.comparison[side]) list.append(el('li','',verdict.comparison[side].text));section.append(list);sections.push(section);
    for(const [key,label] of [['counterargument','Встречный довод'],['market_context','Линия букмекера'],['reason','Причина решения']]) {
      if(!verdict[key]?.text) continue;
      const heading=key==='counterargument' && Object.prototype.hasOwnProperty.call(verdict,'preference') ? (verdict.preference_label?`Что может помешать победе «${verdict.preference_label}»`:'Что мешает выбрать сторону') : label;
      const part=el('section',`report-section verdict-${key}`);part.append(el('h3','',heading));
      if(key==='market_context' && verdict.line_odds && ['home','away'].every(s=>Number.isFinite(verdict.line_odds[s])))
        part.append(el('p','small muted',`${verdict.market_label || 'Коэффициенты в снимке'}: `+[['home','П1'],['draw','Х'],['away','П2']].filter(([s])=>Number.isFinite(verdict.line_odds[s])).map(([s,label])=>`${label} — ${verdict.line_odds[s].toLocaleString('ru-RU')}`).join(' · ')));
      part.append(el('p','',verdict[key].text));sections.push(part);
    }
  } else {
    const section=el('section','report-section');section.append(el('h3','','Аргументы'));
    const list=el('ul','report-facts');for(const item of verdict.arguments || []) list.append(el('li','',item.text));section.append(list);sections.push(section);
  }
  if(!verdict.data_forced_skip && verdict.limitations?.length) {
    const details=el('details','report-limitations');details.append(el('summary','','Ограничения данных'),el('p','',verdict.limitations.join(' ')));sections.push(details);
  }
  target.replaceChildren(lead,...sections,el('p','verdict-risk-note',verdict.risk_note || 'Проигрыш возможен при любом вердикте.'));
}
function renderAnalysis(target, text, assessment=null,generation=null,verdict=null) {
  target.classList.remove('verdict-report');
  if(verdict && ['skip','bet'].includes(verdict.decision)) {renderNeuralVerdict(target,verdict,generation);return;}
  const displayed=analysisDisplayText(text), parts=reportParts(displayed);
  target.classList.toggle("structured-report",Boolean(parts));
  if(parts) {renderReport(target,parts,assessment,generation); return;}
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
function renderPrivateAnswer(result){
  if(result.locked){
    Credits.renderLocked($('explanation'),result,async()=>{
      const owner=Account.user?.id,opened=await api('/api/analyses/'+result.id);
      if(Account.user?.id!==owner||state.aiAnalysis?.id!==result.id)return;
      state.aiAnalysis=opened;renderPrivateAnswer(opened);renderAIAnalysisMeta();
    });
  }else renderAnalysis($('explanation'),result.text,result.context?.probability_assessment,result.context?.ai_generation,result.context?.neural_verdict);
  $('explanation').hidden=false;$('ai-result-toolbar').hidden=!!result.locked;
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
  const optionsKey=JSON.stringify(sports);
  if(document.activeElement!==select && select.dataset.optionsKey!==optionsKey) {
    select.replaceChildren(el("option", "", "Все виды спорта"), ...sports.map(sport => { const option = el("option", "", `${sport.label} · ${sport.total}`); option.value = sport.key; return option; }));
    select.options[0].value = "";select.dataset.optionsKey=optionsKey;
  }
  if(document.activeElement!==select) select.value = selected;
  const popular = ["","football","hockey","tennis","basketball","volleyball"];
  if (selected && !popular.includes(selected)) popular.push(selected);
  ProductUX.replaceContent("sport-tabs",popular.map(key => {
    const button = el("button", key === selected ? "sport-tab active" : "sport-tab", sportLabel(key)); button.type = "button";
    button.dataset.focusKey="sport:"+key;
    button.setAttribute("aria-pressed", String(key === selected)); button.addEventListener("click", () => chooseSport(key)); return button;
  }));
  if (state.route === "#matches") {
    $("catalog-title").textContent = "Матчи";
    $("catalog-eyebrow").textContent = "СПОРТИВНАЯ АНАЛИТИКА";
    $("breadcrumb").textContent = `Линия / ${sportLabel(selected)}`;
  }
}
function chooseSport(key) {
  state.sport = key; state.marketOffset = 0;
  $("competition-filter").value = "";
  try { localStorage.setItem("matchlab:sport:v1",key); } catch { /* The selection still works for this page. */ }
  renderSports(); loadCatalogue();
}
function oddsStrip(preview, eventId, selectable = false) {
  selectable = selectable && state.analysisMode !== "neural-verdict";
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
    button.type = "button"; button.title = value || "Все турниры"; button.dataset.focusKey="league:"+value;
    button.addEventListener("click", () => {
      $("competition-filter").value = value; state.marketOffset = 0;
      if (!["#matches","#favorites"].includes(state.route)) location.hash = "#matches";
      else loadCatalogue();
    }); return button;
  });
  ProductUX.replaceContent("league-nav",[el("p", "eyebrow", "ТУРНИРЫ"), ...buttons]);
}
let catalogueController;
async function loadCatalogue() {
  catalogueController?.abort(); catalogueController=new AbortController();
  const signal=catalogueController.signal;
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
    const result = await api("/api/bookmakers/events?" + query,{signal});
    if (request !== state.marketRequest || !["#matches","#favorites"].includes(state.route)) return;
    if (state.marketOffset > 0 && state.marketOffset >= result.total) { state.marketOffset = 0; return loadCatalogue(); }
    renderCatalogue(result);
    return true;
  } catch (error) {
    if (request === state.marketRequest && error.name !== "AbortError") { showError("market-list-error", "Не удалось загрузить матчи. Проверьте соединение и попробуйте снова."); $("catalog-retry").hidden=false; }
  } finally {
    if (request === state.marketRequest) {
      $("market-event-rows").setAttribute("aria-busy", "false");
      $("market-refresh").disabled = false;
    }
  }
}
function renderCatalogue(result) {
  showError("market-list-error", ""); $("catalog-retry").hidden=true;
  state.catalogSource=result.source_kind || null;
  const favoriteView = state.route === "#favorites";
  $("catalog-list-title").textContent = favoriteView ? "Ваши матчи" : state.sport ? sportLabel(state.sport) : "Все матчи";
  FeaturedEvents.update({visible:!favoriteView&&state.marketOffset===0&&!$("match-search").value.trim()&&!$("competition-filter").value&&!$("day-filter").value,
    sport:state.sport,phase:state.phase,neural:state.analysisMode==='neural-verdict',supported:supportedVerdict,route:eventRoute,
    onAI:event=>{window.ProductAnalytics?.goal('ai_intent');openSelection(event.source_event_id,null,true,event.source_kind==='stream'?'stream':'legacy');}});
  if (result.summary) {
    renderSports(result.sports || state.sports);
    $("nav-matches").textContent = result.summary.upcoming;
    $("count-upcoming").textContent = result.summary.upcoming;
    $("phase-upcoming-count").textContent = result.summary.upcoming;
    $("phase-live-count").textContent = result.summary.live;
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
  $("view-matches").dataset.emptyFavorites=String(isEmptyFavorites);
  $("catalog-empty-title").textContent = isEmptyFavorites ? "Сохраните интересные матчи" : "Матчи не найдены";
  $("catalog-empty-text").textContent = isEmptyFavorites ? "Нажмите звезду рядом с встречей — она появится здесь. Избранное хранится в этом браузере." : "Попробуйте другую дату, турнир или статус. Если сбор только начался, обновите список немного позже.";
  $("reset-filters").hidden = isEmptyFavorites;

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
      favorite.type = "button"; favorite.dataset.favorite = event.source_event_id; favorite.dataset.focusKey=event.source_event_id+":favorite";
      favorite.setAttribute("aria-pressed", String(state.favorites.has(event.source_event_id)));
      favorite.setAttribute("aria-label", state.favorites.has(event.source_event_id) ? "Удалить матч из избранного" : "Добавить матч в избранное");
      favorite.addEventListener("click", () => toggleFavorite(event.source_event_id)); card.append(favorite);
      const info = el("div", "match-info");
      const title = el("a", "match-card-title", event.title || `${event.home}\n${event.away}`); publicMatchLink(title,event); title.dataset.focusKey=event.source_event_id+":title";
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
      const canAnalyze=state.analysisMode!=='neural-verdict'||(supportedVerdict(event)&&event.phase==='prematch');
      const ai = el("button", "match-ai-button", canAnalyze?"Разбор ИИ":"Открыть матч"); ai.type = "button"; ai.dataset.focusKey=event.source_event_id+":ai"; ai.prepend(ProductUX.icon(canAnalyze?"spark":"arrow"));
      ai.addEventListener("click", () => {if(canAnalyze)window.ProductAnalytics?.goal('ai_intent');openSelection(event.source_event_id,null,canAnalyze,event.source_kind==='stream'?'stream':'legacy');});
      const markets = el("a", "match-open", "О матче →"); publicMatchLink(markets,event); markets.dataset.focusKey=event.source_event_id+":open";
      const bookmaker=el("a","match-bookmaker-link","На Winline");bookmaker.append(ProductUX.icon('arrow'));bookmaker.href=`/go/winline/${event.source_event_id}?source=${event.source_kind === "stream" ? "stream" : "legacy"}`;bookmaker.dataset.focusKey=event.source_event_id+":source";bookmaker.target="_blank";bookmaker.rel="sponsored noopener noreferrer";
      actions.append(ai, markets, bookmaker); card.append(actions);
      const freshness = el("div", "match-freshness"); freshness.append(el("span", event.is_stale ? "freshness-badge stale" : "freshness-badge", ageLabel(event)));
      if (isAdmin() && !event.coverage.complete && event.source_kind !== "stream") freshness.append(el("span", "partial-label", event.source_kind === "stream" ? "Основные рынки" : "Неполный снимок"));
      card.append(freshness); group.append(card);
    }
    rows.push(group);
  }
  ProductUX.replaceList(rows);
}
function resetFilters() {
  for (const id of ["match-search","competition-filter","day-filter"]) $(id).value = "";
  $("sort-filter").value = "kickoff"; state.phase = state.route === "#favorites" ? "" : "upcoming"; state.sport=""; state.marketOffset = 0; renderSports();
  syncPhase(); loadCatalogue();
}
function syncClearFilters() {
  $('clear-active-filters').hidden=!(state.phase!=='upcoming' || $('match-search').value.trim() || $('competition-filter').value || $('day-filter').value || $('sort-filter').value!=='kickoff');
  $('match-search-clear').hidden=!$('match-search').value;
  const chips=[];
  if($('match-search').value.trim() && state.sport) chips.push({key:'sport',label:sportLabel(state.sport),clear:()=>{state.sport='';state.marketOffset=0;renderSports();syncClearFilters();loadCatalogue();}});
  if($('match-search').value.trim() && state.phase) chips.push({key:'phase',label:state.phase==='live'?'Live':'До матча',clear:()=>{state.phase='';state.marketOffset=0;syncPhase();syncClearFilters();loadCatalogue();}});

  for(const [id,prefix] of [['match-search','Поиск: '],['competition-filter','Турнир: '],['day-filter','Дата: ']]) {
    const value=$(id).value;if(!value)continue;
    const label=id==='day-filter'?(value==='today'?'Сегодня':'Завтра'):value;
    chips.push({key:id,label:prefix+label,clear:()=>{$(id).value='';state.marketOffset=0;syncClearFilters();loadCatalogue();}});
  }
  ProductUX.filters(chips);

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
  $("explanation").hidden = true; $("ai-analysis-meta").hidden = true; $("ai-result-toolbar").hidden=true;
  $("ai-analysis-sources").replaceChildren();
  showError("ai-request-error", "");
  $('ai-context-retry').hidden=true;
}
async function openAI() {
  if(!await Account.ensure(()=>openAI()))return;
  state.aiDialogEvent=state.eventId;
  AnalysisDialog.open();syncAIButton();
  if(!state.aiBusy&&!state.aiAnalysis)requestAI();
}
function syncAIButton() {
  const d=state.marketDocument,note=$('event-ai-support');
  const unsupported=state.analysisMode==='neural-verdict' && d && (d.phase!=='prematch'||!supportedVerdict(d)||(d.kickoff_at&&new Date(d.kickoff_at)<=new Date()));
  note.hidden=!unsupported;
  if(unsupported)note.textContent=d.phase==='live'?'Матч уже идёт. Счёт и рынки доступны, но ИИ-разбор работает до начала матча. Претендент — никто; решение — пропустить.':d.phase==='finished'?'Матч завершён. Решение о ставке больше не применяется.':!supportedVerdict(d)?'Для этого вида спорта или формата события ИИ-разбор пока недоступен. Решение — пропустить.':'Предматчевый статус не подтверждён. Решение — пропустить.';
  $("explain-button").disabled = state.aiBusy || !state.ai || (!state.marketDocument && !state.forecast);
  $("explain-button").textContent = state.aiBusy ? (state.aiTarget === state.eventId && state.aiTargetRequest === state.explainRequest ? "ИИ готовит разбор…" : "Завершаем предыдущий разбор…") : unsupported?"Посмотреть решение о пропуске":state.aiAnalysis?"Повторить запрос":state.analysisMode==='neural-verdict'?"Получить вердикт ИИ":"Получить разбор ИИ";
  const action=$('event-ai-button');
  action.disabled=!d||!state.ai;
  action.textContent=state.aiBusy&&state.aiTarget===state.eventId?'Разбор готовится…':state.aiAnalysis?'Посмотреть разбор':'Разбор ИИ';
  action.title=!state.ai?'Сервис ИИ пока недоступен':'Открыть разбор матча';
  $('manual-ai-button').disabled=!state.forecast||!state.ai;
  AnalysisDialog.state(state.aiBusy&&state.aiTarget===state.eventId?'loading':state.aiAnalysis?'ready':'idle');
  $("ai-result-toolbar").hidden=$("explanation").hidden;
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
  clearInterval(aiWaitTimer); ProductUX.startWait();
  let index = 0, ticks=0;
  const show=()=>{const messages=state.aiStage === "statistics" ? statisticsWaitMessages : state.aiStage === "markets" ? marketWaitMessages : state.aiStage === "queued" ? queueWaitMessages : state.aiStage === "background" ? jobWaitMessages : aiWaitMessages; $("ai-wait-message").textContent=messages[index % messages.length];};
  show();
  aiWaitTimer = setInterval(() => { ticks++; if(ticks%8===0){index++;show();} ProductUX.wait(state.aiStage); },1000);
}
function stopAIWait() { clearInterval(aiWaitTimer); aiWaitTimer = null; }
function selectAI(market = null, outcome = null) {
  if (!state.marketDocument) return;
  if(state.analysisMode==='neural-verdict') {market=null;outcome=null;}
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
  $("ai-market-label").textContent = state.analysisMode==='neural-verdict' ? (state.verdictSports[document.sport]?.market_label || 'Победитель матча') : market ? [market.name,market.period,market.parameter,marketVariantLabel(market)].filter(Boolean).join(" · ") : "Матч целиком";
  $("ai-outcome-label").hidden = !outcome;
  $("ai-outcome-label").textContent = outcome ? `${outcome.name} · ${outcome.odds ?? "недоступен"}` : "";
  $("ai-clear-selection").hidden = !market;
  $("ai-snapshot-label").textContent = ageLabel(document);
  renderAIAnalysisMeta();
  syncAIButton();
}
function renderAIAnalysisMeta() {
  const analysis = state.aiAnalysis;
  if(analysis?.locked){$('ai-analysis-meta').textContent=`Разбор от ${moscowTime(analysis.generated_at)} МСК. Полный ответ доступен после разблокировки.`;$('ai-analysis-meta').hidden=false;return;}
  if (!analysis?.context) return;
  const context = analysis.context;
  const market = context.markets.find(item => item.key === context.selection.market_key);
  const outcome = market?.outcomes.find(item => item.key === context.selection.outcome_key);
  const changed = state.marketDocument && ((context.snapshot.content_version ? context.snapshot.content_version !== state.marketDocument.content_version : state.marketDocument.observed_at !== analysis.observed_at) || (isAdmin() && state.archiveHistory && context.archive_history?.revision !== state.archiveHistory.revision));
  const prep=context.statistics_preparation?.state;
  const focused=context.market_selection;
  const focusNote=focused ? (focused.policy === "source-popular-v1" ? ` В разборе ${focused.selected_markets} популярных рынков со страницы Winline.` : ` Для разбора выбрано ${focused.selected_markets} из ${focused.available_markets} рынков.${focused.context_only_keys?.length ? " Свежей линии нет; сохранённая показана только для контекста." : ""}`) : "";
  const prepNote=prep && prep !== "ok" ? " Статистику сейчас обновить не удалось; использованы доступные данные." : analysis.preparation_notice || "";
  $("ai-analysis-meta").textContent = `${analysis.analysis_type === "neural_verdict" ? "Вердикт ИИ" : analysis.analysis_type === "limited_summary" ? "Автоматическая сводка" : analysis.analysis_type === "statistical_report" ? "Расчётный разбор" : analysis.analysis_type === "partial_model" ? "Частичный разбор ИИ" : "Разбор"} от ${moscowTime(analysis.generated_at)} МСК.${changed ? ` Данные обновились; ответ относится к снимку от ${moscowTime(context.snapshot.observed_at)} МСК.` : ""}${context.archive_history?.state === "ready" ? " История участников найдена." : ""}${context.team_statistics ? " Статистика участников доступна." : ""}${focusNote}${prepNote}`;
  if (!isAdmin()) $("ai-analysis-meta").textContent = `Разбор от ${moscowTime(analysis.generated_at)} МСК.${changed ? " Сведения о матче изменились после разбора." : ""}`;
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
function publicMatchLink(link,event) {
  link.href=event.public_url||eventRoute(event);
  if(event.public_url)link.addEventListener('click',action=>{
    if(action.defaultPrevented||action.button!==0||action.ctrlKey||action.metaKey||action.shiftKey||action.altKey)return;
    action.preventDefault();location.hash=eventRoute(event);
  });
}
function eventAPI(id) { return `${state.streamEvent ? "/api/archive/events/" : "/api/bookmakers/events/"}${id}`; }
function isEventRoute() { return /^#(?:match|stream)\//.test(state.route); }
function openSelection(id, side = null, auto = false, source = null) {
  if (auto && !Account.user) {
    Account.ensure(()=>openSelection(id,side,auto,source)).then(allowed=>{if(allowed)openSelection(id,side,auto,source)}); return;
  }
  if(auto||side) {
    state.aiDialogEvent=id;
    AnalysisDialog.open({loading:state.eventId!==id||!state.marketDocument});
    if(id===state.eventId&&!side&&(state.aiBusy||state.aiAnalysis))return;
  }
  state.pendingSelection = {id, side, auto};
  const stream=source ? source==='stream' : state.marketDocument ? state.marketDocument.source_kind === "stream" : state.catalogSource === "stream";
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
  AnalysisDialog.ready();
  if (pending.auto) requestAI(false);
}
async function loadEvent(refresh = false) {
  const id = state.eventId, request = ++state.marketDetailRequest;
  if (!refresh) { state.refreshRequest++; $("event-refresh").disabled=false; $("event-refresh-status").textContent=""; }
  const prefix=eventAPI(id),stream=state.streamEvent;
  if(stream) api(`/api/archive/events/${id}/watch`,{method:"POST",body:"{}"}).catch(()=>{});
  const [detail, context, teamStatistics, archived] = await Promise.allSettled([
    api(prefix+"/markets"),
    isAdmin() ? api(prefix+"/analysis") : Promise.resolve(null),
    isAdmin() ? api(prefix+"/statistics") : Promise.resolve(null),
    isAdmin() ? api(`/api/archive/events/${id}/history?source=${stream ? "stream" : "legacy"}`) : Promise.resolve(null)
  ]);
  if (request !== state.marketDetailRequest || state.eventId !== id || !isEventRoute()) return;
  if (detail.status === "rejected") {
    showError("detail-error", detail.reason.message);
    if(state.aiDialogEvent===id) {AnalysisDialog.ready();showError('ai-request-error',detail.reason.message);$('ai-context-retry').hidden=false;}
    return;
  }
  $('ai-context-retry').hidden=true;
  showError("detail-error", "");
  state.marketDocument = detail.value;
  const document = detail.value;
  state.analysisContext = context.status === "fulfilled" ? context.value : null;
  renderTeamStatistics(teamStatistics.status === "fulfilled" ? teamStatistics.value : null);
  state.archiveHistory = archived.status === "fulfilled" ? archived.value : null;
  renderArchiveHistory(state.archiveHistory);
  $("event-header").hidden = false;
  $("market-detail-title").textContent = eventTitle(document);
  window.document.title=eventTitle(document)+" · prognozich ai";
  $("event-competition").textContent = `${document.sport_label || sportLabel(document.sport)} · ${document.competition}`;
  $("breadcrumb").textContent = `Линия / ${document.sport_label || sportLabel(document.sport)}`;
  $("tab-analysis").hidden = !isAdmin() || !state.analysisContext?.prediction_available;
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
  $("event-primary").replaceChildren(...(document.headline ? [el("p","headline-caption",[document.headline.name,document.headline.period,document.headline.outcome_count > 3 ? `${document.headline.outcome_count} исходов` : null].filter(Boolean).join(" · ")), oddsStrip(document.headline,id,true), ...(state.analysisMode === "neural-verdict" ? [] : [el("p","odds-explainer","Выберите коэффициент, чтобы разобрать исход.")])] : []));
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
    const caption = [isAdmin() ? market.group : null,market.period,market.parameter,marketVariantLabel(market)].filter(Boolean).join(" · ");
    summary.append(el("small", "", `${caption ? caption + " · " : ""}${market.outcomes.length} исходов${market.observed_at ? " · " + observationTime(market.observed_at) + " МСК" + (market.is_stale ? " · прошлое наблюдение" : "") : ""}`)); details.append(summary);
    if (market.variant_id) details.append(el("p","small muted","У источника есть несколько блоков с этим названием. Условия каждого варианта уточняйте на странице события."));
    let rendered = false;
    const render = () => {
      if (rendered || !details.open) return;
      rendered = true; const outcomes = el("div", "market-outcomes");
      market.outcomes.forEach(outcome => {
        const button = el(state.analysisMode === "neural-verdict" ? "div" : "button", "market-outcome"); button.type = "button";
        button.dataset.selection = `${market.key}/${outcome.key}`;
        button.disabled = outcome.state !== "open";
        button.setAttribute("aria-pressed", String(state.selection?.marketKey === market.key && state.selection?.outcomeKey === outcome.key));
        const price = outcome.state === "open" ? String(outcome.odds) : outcome.state === "suspended" ? "Пауза" : "Недоступен";
        button.append(el("span", "", [outcome.name,outcome.parameter].filter(Boolean).join(" · ")), el("strong", "", price));
        button.title = state.analysisMode === "neural-verdict" ? "Коэффициент на момент обновления" : "Выбрать для разбора ИИ";
        if (state.analysisMode !== "neural-verdict") button.addEventListener("click", () => { selectAI(market,outcome);state.aiDialogEvent=state.eventId;AnalysisDialog.open(); });
        outcomes.append(button);
      }); details.append(outcomes);
    };
    details.addEventListener("toggle", render);
    details.open = opened.has(market.key); render(); return details;
  }));
  if (focusedSelection) [...$("market-groups").querySelectorAll("[data-selection]")].find(button => button.dataset.selection === focusedSelection)?.focus({preventScroll:true});
}
function setDetailTab(tab) {
  if (tab === "analysis" && !isAdmin()) tab = "markets";
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
      ProductUX.wait(stage);
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
async function requestAI(present=true) {
  if (!await Account.ensure(()=>requestAI())) return;
  const accountId=Account.user.id;
  Account.track("ai_intent",state.eventId || "forecast");
  if (state.aiBusy) { toast("Дождитесь завершения предыдущего разбора."); return; }
  const document = state.marketDocument, forecast = state.forecast;
  if (!document && !forecast) return;
  if(present){state.aiDialogEvent=state.eventId;AnalysisDialog.open();}
  window.ProductAnalytics?.goal('ai_request_start');
  const request = ++state.explainRequest, selection = state.selection ? {...state.selection} : null;
  const eventId = state.eventId, forecastId = forecast?.id;
  state.aiBusy = true; state.aiTarget = eventId; state.aiTargetRequest = request; state.aiStage = document ? "statistics" : "analysis"; startAIWait(); syncAIButton(); showError("ai-request-error", "");
  $("explanation").hidden = true; $("ai-analysis-meta").hidden = true; $("ai-result-toolbar").hidden=true; $("ai-analysis-sources").replaceChildren();
  $("explanation").setAttribute("aria-busy", "true");
  try {
    const current=()=>Account.user?.id === accountId && request === state.explainRequest && eventId === state.eventId;
    let statisticsJobId=null, preparationNotice="";
    const pending=document ? (await api('/api/analysis-jobs/current',{cache:'no-store'})).job : null;
    if(pending && (pending.event_id !== eventId || pending.source !== (state.streamEvent ? "archive" : "bookmakers")))
      throw new Error("Для вашего аккаунта уже готовится другой разбор. Результат появится в истории.");
    const unsupported=state.analysisMode==='neural-verdict' && document && (document.phase!=='prematch' || !supportedVerdict(document) || (document.kickoff_at && new Date(document.kickoff_at)<=new Date()));
    if(document && !pending && !unsupported) {
      const prefix=eventAPI(eventId);
      try {
        // The server also starts watching this event's markets before queueing statistics.
        const queued=await api(prefix+"/statistics/request",{method:"POST",body:"{}",cache:"no-store"});
        let finished=null;
        for(let attempt=0;attempt<125 && current();attempt++) {
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
    const body = document ? {content_version:latest.content_version || null, observed_at:latest.observed_at, market_key:state.analysisMode==='neural-verdict'?null:selection?.marketKey || null, outcome_key:state.analysisMode==='neural-verdict'?null:selection?.outcomeKey || null, notes:$("notes").value, statistics_job_id:statisticsJobId} : {forecast_id:forecastId,notes:$("notes").value};
    const submitted=pending || await api(endpoint, {method:"POST",body:JSON.stringify(body)});
    const result=document ? await waitForAnalysisJob(submitted,current) : submitted;
    if(!result) return;
    if (Account.user?.id !== accountId || request !== state.explainRequest || eventId !== state.eventId || (!document && forecastId !== state.forecast?.id)) return;
    renderPrivateAnswer(result);
    window.ProductAnalytics?.goal(['limited_summary','statistical_report','partial_model'].includes(result.analysis_type) ? 'ai_summary_result' : 'ai_model_result');
    if (result.context || result.locked) {
      result.preparation_notice=preparationNotice;
      state.aiAnalysis = result; renderAIAnalysisMeta();
      if(!AnalysisDialog.isOpen)toast('Разбор готов. Откройте его кнопкой «Посмотреть разбор».');
    }
  } catch (error) {
    if(request === state.explainRequest) window.ProductAnalytics?.goal('ai_request_error');
    if(error.status===401 && eventId===state.eventId) Account.show("login",()=>requestAI());
    else if (request === state.explainRequest) showError("ai-request-error", error.message);
  } finally {
    state.aiBusy = false; stopAIWait(); $("explanation").setAttribute("aria-busy", "false"); syncAIButton();
    if(request===state.explainRequest && AnalysisDialog.isOpen && !$("explanation").hidden)
      $('ai-dialog').scrollTo({top:0,behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'});
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
  await Account.ready;
  $("history-guest").hidden=!!Account.user;
  $("history-tools").hidden=!Account.user;
  if (!Account.user) { $("history-empty").hidden=true; return; }
  const request = ++state.historyRequest;
  $("history-list").setAttribute("aria-busy","true");
  try {
    const result = await api("/api/analyses");
    if (request !== state.historyRequest || state.route !== "#history") return;
    showError("history-error", ""); $("history-empty").hidden = result.analyses.length > 0;
    $("history-list").replaceChildren(...result.analyses.map(analysis => {
      const card = el("article", "panel history-card"), info = el("div");
      info.append(el("h2", "", analysis.locked?'Закрытый разбор ИИ':eventTitle(analysis.event)), el("p", "small muted", analysis.locked?`Ответ от ${moscowTime(analysis.generated_at)} МСК`:`${analysis.event.sport_label || "Футбол"} · ${analysis.market_name} · ${moscowTime(analysis.generated_at)} МСК`));
      const preview=analysisPreview(analysis.text);
      const excerpt = el("p", "history-excerpt", analysis.locked?(analysis.text||'Открытие решения «Ставить» — 1 разбор. «Пропустить» — бесплатно.'):(preview.length > 200 ? preview.slice(0,200) + "…" : preview)); info.append(excerpt);
      const open = el("a", "secondary-button", "Открыть разбор"); open.append(ProductUX.icon('arrow')); open.href = `#analysis/${analysis.id}`; card.append(info,open); return card;
    }));
    ProductUX.filterHistory();
    return true;
  } catch (error) { if (request === state.historyRequest) showError("history-error", error.message); }
  finally {if(request===state.historyRequest)$("history-list").setAttribute("aria-busy","false");}
}
async function loadSavedAnalysis(id) {
  if (!await Account.ensure(()=>loadSavedAnalysis(id))) return;
  const request = ++state.marketDetailRequest;
  try {
    const analysis = await api(`/api/analyses/${encodeURIComponent(id)}`);
    if (request !== state.marketDetailRequest || state.route !== `#analysis/${id}`) return;
    if(analysis.locked){
      $('manual-heading').hidden=false;$('manual-heading').querySelector('h1').textContent='Сохранённый разбор';
      $('manual-heading').querySelector('.subtitle').textContent=`Разбор от ${moscowTime(analysis.generated_at)} МСК`;
      $('saved-analysis').hidden=false;$('saved-analysis-title').textContent='Полный ответ ИИ';
      $('saved-analysis-coverage').hidden=true;$('saved-analysis-open').hidden=true;$('saved-analysis-sources').replaceChildren();
      Credits.renderLocked($('saved-analysis-text'),analysis,()=>loadSavedAnalysis(id));return;
    }
    $('saved-analysis-coverage').hidden=false;$('saved-analysis-open').hidden=false;
    const context = analysis.context;
    $("manual-heading").hidden = false;
    $("manual-heading").querySelector(".eyebrow").textContent = "СОХРАНЁННЫЙ РАЗБОР";
    $("manual-heading").querySelector("h1").textContent = eventTitle(analysis.event);
    document.title=eventTitle(analysis.event)+" · Разбор · prognozich ai";
    $("manual-heading").querySelector(".subtitle").textContent = `${analysis.analysis_type === "neural_verdict" ? "Вердикт ИИ" : analysis.analysis_type === "limited_summary" ? "Автоматическая сводка" : analysis.analysis_type === "statistical_report" ? "Расчётный разбор" : analysis.analysis_type === "partial_model" ? "Частичный разбор ИИ" : "Разбор"} от ${moscowTime(analysis.generated_at)} МСК · снимок ${moscowTime(analysis.observed_at)} МСК`;
    if (!isAdmin()) $("manual-heading").querySelector(".subtitle").textContent = `Разбор от ${moscowTime(analysis.generated_at)} МСК`;
    $("saved-analysis").hidden = false;
    $("saved-analysis-title").textContent = analysis.market_name;
    renderAnalysis($("saved-analysis-text"),analysis.text,analysis.context?.probability_assessment,analysis.context?.ai_generation,analysis.context?.neural_verdict);
    $("saved-analysis-coverage").textContent = context.neural_verdict ? `${context.neural_verdict.market_label || 'Победа в основное время'}. Вердикт относится к сохранённым данным этого разбора.` : (context.market_selection?.policy === "source-popular-v1" ? `Популярные рынки источника: ${context.coverage.analyzed_markets}.` : `Учтено ${context.coverage.analyzed_markets} из ${context.coverage.stored_markets} сохранённых рынков.`)+" Данные и текст относятся ко времени этого разбора.";
    renderResearchSources($("saved-analysis-sources"), context.web_research);
    $("saved-analysis-open").href = eventRoute(analysis.event);
  } catch (error) { if (request === state.marketDetailRequest) showError("detail-error", error.message); }
}
async function loadStatus() {
  const request = ++state.statusRequest;
  try {
    const status = await api("/api/status");
    if (request !== state.statusRequest) return;
    state.ai = Boolean(status.ai?.configured);state.analysisMode=status.ai?.analysis_mode || 'independent';state.verdictSports=status.ai?.verdict_sports || {};
    if(state.analysisMode==='neural-verdict') state.selection=null;
    renderSources(status.data);
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
function syncAnalysisLayout() {
  document.body.classList.add('inline-ai-desk','without-ai-desk');
}
async function route() {
  const previous = state.route;
  let restoredPosition=null;
  let hash = location.hash || "#matches";
  const aliases = {"#markets":"#matches","#bookmakers":"#matches","#forecast":"#calculator","#validation":"#status","#data":"#status"};
  if (aliases[hash]) { hash = aliases[hash]; history.replaceState(null,"",hash); }
  if (["#archive", "#status", "#calculator"].includes(hash)) {
    await Account.ready;
    if ((location.hash || "#matches") !== hash) return;
    if (!isAdmin()) { hash = "#matches"; history.replaceState(null,"",hash); }
  }
  syncRoleUI();
  const match = hash.match(/^#(?:match|stream)\/(\d{1,20})$/), saved = hash.match(/^#analysis\/([a-f0-9]{32})$/),publicSaved=hash.match(/^#result\/([a-f0-9]{32})$/);
  if (!["#matches","#results","#favorites","#history","#archive","#status","#calculator","#account","#admin"].includes(hash) && !match && !saved && !publicSaved) { hash = "#matches"; history.replaceState(null,"",hash); }
  if(previous!==hash)ProductUX.capture(previous,state);
  document.title=hash==='#matches'?'Прогнозы на спорт от ИИ — prognozich ai':({"#favorites":"Избранное","#history":"Мои разборы","#account":"Личный кабинет","#admin":"Управление"}[hash]||"Матч")+" · prognozich ai";
  state.route = hash; document.getElementById('view-matches').classList.toggle('ym-show-content',hash === '#matches'); Account.track("page_view",hash); window.ProductAnalytics?.hit(hash); state.marketRequest++; state.marketDetailRequest++; state.historyRequest++;
  PublicResults.activate(hash);
  $("league-nav").hidden=!["#matches","#favorites"].includes(hash) && !match;
  if(previous!==hash && AnalysisDialog.isOpen && !(match&&match[1]===state.aiDialogEvent))AnalysisDialog.close();
  document.body.classList.add('without-ai-desk');
  syncGuide();
  state.streamEvent=hash.startsWith("#stream/");
  if (previous !== hash) { invalidateForecast(); state.marketDocument = null; state.analysisContext = null; state.eventId = null; state.selection = null; clearAIOutput(); renderTeamStatistics(null); renderArchiveHistory(null); $("notes").value = ""; $("ai-empty").hidden = false; $("ai-selection").hidden = true; }
  $("saved-analysis").hidden = true;
  $("team-statistics").hidden = !match;
  document.querySelectorAll(".nav-link").forEach(link => { const active = link.getAttribute("href") === hash; link.classList.toggle("active",active); active ? link.setAttribute("aria-current","page") : link.removeAttribute("aria-current"); });
  $("nav-favorites").textContent = state.favorites.size;
  $("forecast").hidden = true; $("event-header").hidden = true; $("manual-heading").hidden = true; $("manual-selection").hidden = true;
  $('manual-ai-button').hidden=hash!=='#calculator';
  showError("detail-error", "");
  if (hash === "#matches" || hash === "#favorites") {
    showView("view-matches");
    $("catalog-title").textContent = hash === "#favorites" ? "Избранное" : "Матчи";
    $("catalog-subtitle").textContent = hash === "#favorites" ? "Ваши матчи в этом браузере. Сохраните звёздочкой нужные встречи." : "Найдите встречу. ИИ сравнит доступные факты и объяснит своё решение.";
    $("catalog-stats").hidden = hash === "#favorites";
    $("breadcrumb").textContent = hash === "#favorites" ? "Линия / Избранное" : `Линия / ${sportLabel(state.sport)}`;
    state.catalogReturn = hash;
    restoredPosition=ProductUX.restore(hash);
    if(restoredPosition){state.sport=restoredPosition.sport;state.phase=restoredPosition.phase;state.marketOffset=restoredPosition.offset;}
    else {state.marketOffset=0; if(hash==="#favorites"){state.sport="";state.phase="";}}
    syncPhase(); renderSports(); await loadCatalogue();

  } else if (hash === "#account") {
    showView("view-account"); $("breadcrumb").textContent="Личный кабинет"; Account.renderPage();
  } else if (hash === "#admin") {
    showView("view-admin"); $("breadcrumb").textContent="Управление / Обзор"; window.Admin?.open();
  } else if (hash === "#archive") {
    showView("view-archive"); $("breadcrumb").textContent="Данные / Архив матчей"; state.archiveOffset=0; loadArchive();
  } else if (hash === "#history") {
    showView("view-history"); $("breadcrumb").textContent = "История / Разборы ИИ"; loadHistory();
  } else if(hash==='#results'||publicSaved) {
    showView(publicSaved?'view-result':'view-results');$('breadcrumb').textContent=publicSaved?'Результаты / Ответ ИИ':'Результаты за 30 дней';document.title='Результаты ИИ · prognozich ai';
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
      $("match-back").href = state.catalogReturn; $("match-back").textContent = "← К матчам";
      $("market-search").value = ""; $("market-group-filter").value = "";
      $("market-groups").replaceChildren(el("p","small muted","Загружаем рынки…"));
      setDetailTab("markets"); loadEvent();
    } else if (hash === "#calculator") {
      $("breadcrumb").textContent = "Инструменты / Ручной расчёт";
      $("match-back").href = "#status"; $("match-back").textContent = "← К состоянию сервиса";
      $("manual-heading").hidden = false;
      $("manual-heading").querySelector(".eyebrow").textContent = "СТАТИСТИЧЕСКАЯ МОДЕЛЬ · АПЛ";
      $("manual-heading").querySelector("h1").textContent = "Ручной расчёт";
      $("manual-heading").querySelector(".subtitle").textContent = "Исследовательский сценарий по истории АПЛ. Выбранная пара не подтверждает наличие матча в расписании.";
      $("manual-selection").hidden = false; $("forecast").hidden = false;
      $("as-of").value ||= today; if (!state.teamsLoaded) loadTeams().then(() => { state.teamsLoaded = true; });
    } else if (saved) {
      $("breadcrumb").textContent = "История / Сохранённый разбор";
      $("match-back").href = "#history"; $("match-back").textContent = "← К истории разборов"; loadSavedAnalysis(saved[1]);
    }
  }
  if(state.route!==hash)return;
  syncAnalysisLayout(); syncAIButton();
  if (previous !== hash) { window.scrollTo(0,restoredPosition?.scroll || 0); $("main-content").focus({preventScroll:true}); }
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
$('event-ai-button').addEventListener('click',()=>{window.ProductAnalytics?.goal('ai_intent');openAI();});
$('manual-ai-button').addEventListener('click',openAI);
$('ai-context-retry').addEventListener('click',()=>{showError('ai-request-error','');$('ai-context-retry').hidden=true;AnalysisDialog.open({loading:true});loadEvent();});
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
        status.textContent=isAdmin() ? `${markets[result.market_state] || "Результат обновления рынков неизвестен."} ${stats[result.statistics_state] || "Результат обновления статистики неизвестен."}` : result.market_state === "ok" ? "Данные матча обновлены." : "Свежие сведения пока недоступны. Показаны последние полученные данные.";
        return;
      }
      if (result.state==="timeout") { status.textContent="Сборщик не завершил запрос вовремя. Показаны сохранённые данные; попробуйте позже."; return; }
      status.textContent=result.state==="running" ? "Обновляем данные матча…" : "Обновление ожидает своей очереди…";
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
    if (current()) status.textContent="Ожидание обновления завершилось. Проверьте состояние сборщика и повторите позже.";
  } catch (error) { if (current()) status.textContent=error.message; }
  finally { if (current()) $("event-refresh").disabled=false; }
});
$("market-refresh").addEventListener("click", async () => { clearToast(); loadStatus(); if (await loadCatalogue()) toast("Список матчей обновлён."); });
$("event-show-markets").addEventListener("click",() => { setDetailTab("markets"); const reduced=window.matchMedia?.('(prefers-reduced-motion: reduce)').matches; $("event-tabs").scrollIntoView({behavior:reduced ? "auto" : "smooth",block:"start"}); $("tab-markets").focus({preventScroll:true}); });
$("history-refresh").addEventListener("click", async () => { clearToast(); $("history-refresh").disabled = true; try { if (await loadHistory()) toast("История обновлена."); } finally { $("history-refresh").disabled = false; } });
$("status-refresh").addEventListener("click", async () => { clearToast(); $("status-refresh").disabled = true; try { const [ok] = await Promise.all([loadStatus(),loadBacktest().catch(error => showError("backtest-empty",error.message))]); if (ok) toast("Состояние сервиса обновлено."); } finally { $("status-refresh").disabled = false; } });
$("market-prev").addEventListener("click", async () => { state.marketOffset = Math.max(0,state.marketOffset - 30); if(await loadCatalogue()) $("catalog-section").scrollIntoView({block:"start"}); });
$("market-next").addEventListener("click", async () => { state.marketOffset += 30; if(await loadCatalogue()) $("catalog-section").scrollIntoView({block:"start"}); });
$("catalog-retry").addEventListener("click",loadCatalogue);
$("history-login").addEventListener("click",()=>Account.ensure(()=>loadHistory()));
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
window.addEventListener("storage",event => { if (event.key === "matchlab:favorites:v1") { state.favorites = readFavorites(); $("nav-favorites").textContent = state.favorites.size; if (["#matches","#favorites"].includes(state.route)) loadCatalogue(); if (state.eventId) updateEventFavorite(); } });
window.addEventListener('storage',event => {
  if (event.key==='matchlab:theme:v1') { themeOverride=storedTheme(); setTheme(themeOverride || (themeMedia?.matches ? 'light' : 'dark')); }
  if (event.key==='matchlab:guide:v1') { guideOpen=event.newValue!=='hidden'; syncGuide(); }
});
setInterval(() => { if (document.visibilityState !== "visible") return; loadStatus(); if (["#matches","#favorites"].includes(state.route)) loadCatalogue(); else if (state.eventId) loadEvent(true); else if(state.route === "#archive") loadArchive(); },10000);
state.favorites = readFavorites(); $("as-of").value = today;
try { const key = localStorage.getItem("matchlab:sport:v1"); if (key !== null && key!=='table_tennis' && (!key || /^[a-z][a-z0-9_-]{0,49}$/.test(key))) state.sport = key; } catch { /* Default to football when storage is unavailable. */ }
setTheme(document.documentElement.dataset.theme); syncRoleUI(); loadStatus(); route();

function renderArchiveStatus(data) {
  const active=data.connected && !data.source_stale;
  const label=active ? "Поток обновляется" : data.available ? "Сохранённые данные" : "Ожидаем первую передачу";
  const text=data.last_snapshot_at ? `Последнее наблюдение: ${moscowTime(data.last_snapshot_at)} МСК. Исторических результатов: ${(data.stored_results || 0).toLocaleString("ru-RU")}.${data.sent_cursor < data.source_cursor ? " Начальная передача истории ещё идёт." : ""}${!data.connected ? " Нет свежего отчёта о доставке." : data.source_stale ? " Источник давно не обновлялся; время у каждого матча указано отдельно." : ""}` : "База на сервере заполняется. После первой передачи здесь появятся данные.";
  $("archive-service-title").textContent=label; $("archive-service-note").textContent=text;
  if(data.unidentified_results) $("archive-service-note").textContent+=` Записей источника без даты или участников: ${data.unidentified_results}; они сохранены отдельно от списка результатов.`;
  if(data.quarantined_results) $("archive-service-note").textContent+=` Отдельно сохранено записей с датой позже наблюдения: ${data.quarantined_results}; до корректного подтверждения они не используются в результатах и аналитике.`;
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
  AnalysisDialog.close();
  syncRoleUI();
  if (!isAdmin() && ["#archive", "#status", "#calculator"].includes(state.route)) { history.replaceState(null,"","#matches"); route(); }
  syncAnalysisLayout();
  state.explainRequest++;clearAIOutput();syncAIButton();
  if (state.eventId) { setDetailTab("markets"); loadEvent(true); }
  $("history-search").value=""; $("history-search-status").textContent=""; $("history-search-empty").hidden=true;
  $("explanation").replaceChildren();$("history-list").replaceChildren();$("saved-analysis-text").replaceChildren();
  if(!Account.user && /^#analysis\//.test(state.route)) location.hash="#history";
  if(state.route==="#account") Account.renderPage();
  if(state.route==="#history")loadHistory();
});
