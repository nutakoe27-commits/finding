"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  forecast: null,
  qwen: false,
  teamsLoading: false,
  teamsRequest: 0,
  forecastRequest: 0,
  selectedQuote: null,
  bookmakerRequest: 0,
};
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
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => item.msg).join("; ")
      : body.detail;
    throw new Error(detail || `Ошибка сервиса (${response.status})`);
  }
  return body;
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
      `Последний загруженный результат — ${localDate(data.last_match)} (${data.stale_days} дн. назад). Это исторический прототип: актуальные составы и расписание не подключены.`;
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
  $("explain-button").disabled = true;
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
  $("explain-button").disabled = !state.qwen;
}

function renderMarket(market) {
  const container = $("market-result");
  container.hidden = !market;
  container.replaceChildren();
  if (!market) return;
  const title = document.createElement("div");
  const captured = ["betboom", "winline"].includes(market.source);
  title.textContent = `${captured ? bookmakerName(market.source) + " · " + moscowTime(market.observed_at) + " МСК" : "Ручные коэффициенты"} · маржа ${(market.overround * 100).toFixed(1)}%`;
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
      stroke: "#343d2d",
      "stroke-width": 1,
    });
    element(
      "text",
      {
        x: 23,
        y: y + 3,
        fill: "#8c9a80",
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
    stroke: "#9aa98d",
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
      fill: "#d3f887",
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
        fill: "#8c9a80",
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

$("forecast-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.teamsLoading) return;
  showError("forecast-error", "");
  const button = $("predict-button");
  button.disabled = true;
  button.textContent = "Рассчитываем…";
  document.querySelector(".result-panel").setAttribute("aria-busy", "true");
  // Remove previous output before requesting a different scenario.
  invalidateForecast();
  const requestId = state.forecastRequest;
  const payload = {
    home: $("home-team").value,
    away: $("away-team").value,
    as_of: $("as-of").value,
  };
  if (state.selectedQuote) payload.quote_id = state.selectedQuote.id;
  const values = ["home", "draw", "away"].map((key) => $("odds-" + key).value);
  try {
    if (!state.selectedQuote && values.some(Boolean)) {
      if (!values.every(Boolean))
        throw new Error(
          "Заполните все три коэффициента или оставьте все поля пустыми.",
        );
      payload.odds = Object.fromEntries(
        ["home", "draw", "away"].map((key, i) => [key, Number(values[i])]),
      );
    }
    const result = await api("/api/forecast", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (requestId === state.forecastRequest) renderForecast(result);
  } catch (error) {
    if (requestId === state.forecastRequest)
      showError("forecast-error", error.message);
  } finally {
    button.disabled = state.teamsLoading;
    button.replaceChildren(document.createTextNode("Рассчитать вероятности "));
    const arrow = document.createElement("span");
    arrow.innerHTML =
      '<svg class="arrow-icon" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 13 13 3M3 3h10v10"/></svg>';
    button.append(arrow);
    document.querySelector(".result-panel").setAttribute("aria-busy", "false");
  }
});

$("swap-teams").addEventListener("click", () => {
  clearQuote();
  const home = $("home-team").value;
  $("home-team").value = $("away-team").value;
  $("away-team").value = home;
  invalidateForecast();
});
for (const id of [
  "home-team",
  "away-team",
  "odds-home",
  "odds-draw",
  "odds-away",
])
  $(id).addEventListener("change", () => {
    clearQuote();
    invalidateForecast();
  });
$("as-of").addEventListener("change", () => {
  clearQuote();
  invalidateForecast();
  const historical = $("as-of").value !== today;
  for (const key of ["home", "draw", "away"]) {
    $("odds-" + key).disabled = historical;
    $("odds-" + key).value = "";
  }
  loadTeams();
});
$("explain-button").addEventListener("click", async () => {
  if (!state.forecast) return;
  const forecastId = state.forecast.id;
  $("explain-button").disabled = true;
  $("explanation").hidden = false;
  $("explanation").textContent = "Qwen готовит объяснение…";
  try {
    const result = await api("/api/explain", {
      method: "POST",
      body: JSON.stringify({
        forecast_id: forecastId,
        notes: $("notes").value,
      }),
    });
    if (state.forecast?.id === forecastId)
      $("explanation").textContent = result.text;
  } catch (error) {
    if (state.forecast?.id === forecastId)
      $("explanation").textContent = error.message;
  } finally {
    $("explain-button").disabled = !state.forecast || !state.qwen;
  }
});
document.querySelectorAll(".nav-link").forEach((link) =>
  link.addEventListener("click", () => {
    document
      .querySelectorAll(".nav-link")
      .forEach((item) => item.classList.remove("active"));
    link.classList.add("active");
  }),
);

async function init() {
  $("as-of").value = today;
  try {
    const status = await api("/api/status");
    renderSources(status.data);
    state.qwen = status.qwen.configured;
    $("qwen-state").textContent = state.qwen
      ? "Адрес настроен · проверка при запросе"
      : "Не подключён";
    $("qwen-dot").classList.toggle("green", state.qwen);
    $("ai-badge").textContent = state.qwen
      ? "АДРЕС НАСТРОЕН"
      : "ОЖИДАЕТ ПОДКЛЮЧЕНИЯ";
    await Promise.all([loadTeams(), loadBacktest(), loadBookmakers()]);
  } catch (error) {
    showError("global-error", `Не удалось загрузить данные: ${error.message}`);
  }
}

function clearQuote() {
  state.selectedQuote = null;
  $("quote-selection").hidden = true;
  for (const key of ["home", "draw", "away"])
    $("odds-" + key).disabled = $("as-of").value !== today;
}

async function chooseQuote(quote) {
  invalidateForecast();
  state.selectedQuote = quote;
  $("as-of").value = quote.observed_at.slice(0, 10);
  await loadTeams();
  if (state.selectedQuote !== quote) return;
  $("home-team").value = quote.model_teams[0];
  $("away-team").value = quote.model_teams[1];
  if (!$("home-team").value || !$("away-team").value) {
    clearQuote();
    showError(
      "forecast-error",
      "Нет подходящей истории команд на дату снимка.",
    );
    return;
  }
  for (const key of ["home", "draw", "away"]) {
    $("odds-" + key).value = "";
    $("odds-" + key).disabled = true;
  }
  $("quote-selection-text").textContent =
    `${bookmakerName(quote.source)} · ${moscowTime(quote.observed_at)} МСК`;
  $("quote-selection").hidden = false;
  $("forecast-form").requestSubmit();
  $("forecast").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadBookmakers() {
  const requestId = ++state.bookmakerRequest;
  const query = new URLSearchParams({
    include_started: String($("bookmaker-archive").checked),
  });
  if ($("bookmaker-source").value)
    query.set("source", $("bookmaker-source").value);
  try {
    const data = await api("/api/bookmakers?" + query);
    if (requestId !== state.bookmakerRequest) return;
    showError("bookmaker-error", "");
    $("bookmaker-empty").hidden = data.quotes.length > 0;
    const all = data.status.sources;
    const observations = all.reduce(
      (total, source) => total + source.snapshots,
      0,
    );
    $("bookmaker-summary").textContent =
      `${data.quotes.length} котировок в списке · ${observations} наблюдений в истории. ` +
      all
        .map(
          (source) =>
            `${bookmakerName(source.source)}: ${source.events} событий`,
        )
        .join(" · ");
    if (all.length) {
      $("stat-odds-source").textContent = all
        .map((source) => bookmakerName(source.source))
        .join(" · ");
      $("stat-odds-note").textContent = "Снимки публичной линии";
    }
    $("bookmaker-rows").replaceChildren(
      ...data.quotes.map((quote) => {
        const row = document.createElement("tr");
        row.dataset.source = quote.source;
        const cell = (text, className) => {
          const td = document.createElement("td");
          td.textContent = text;
          if (className) td.className = className;
          row.append(td);
          return td;
        };
        const match = cell(`${quote.home} — ${quote.away}`, "bookmaker-match");
        const league = document.createElement("small");
        league.textContent = quote.competition;
        match.append(league);
        cell(moscowTime(quote.kickoff_at));
        cell(bookmakerName(quote.source));
        for (const key of ["home", "draw", "away"])
          cell(quote.odds[key].toFixed(2), "odds-value");
        const timestamp = cell(
          moscowTime(quote.observed_at),
          quote.is_stale ? "stale-quote" : "lime",
        );
        const freshness = document.createElement("small");
        freshness.textContent = quote.started
          ? "Матч начался"
          : quote.is_stale
            ? "Устаревший снимок"
            : "Получен недавно";
        timestamp.append(freshness);
        const action = cell("");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "quote-button";
        button.textContent = "Рассчитать";
        button.disabled = !quote.prediction_available;
        button.title = quote.unavailable_reason;
        button.addEventListener("click", () => chooseQuote(quote));
        action.append(button);
        if (!quote.prediction_available) {
          const why = document.createElement("small");
          why.textContent = quote.unavailable_reason;
          action.append(why);
        }
        return row;
      }),
    );
  } catch (error) {
    if (requestId === state.bookmakerRequest)
      showError("bookmaker-error", error.message);
  }
}

$("quote-clear").addEventListener("click", () => {
  clearQuote();
  invalidateForecast();
});
$("bookmaker-refresh").addEventListener("click", loadBookmakers);
$("bookmaker-source").addEventListener("change", loadBookmakers);
$("bookmaker-archive").addEventListener("change", loadBookmakers);
setInterval(() => {
  if (document.visibilityState === "visible") loadBookmakers();
}, 30000);
init();
