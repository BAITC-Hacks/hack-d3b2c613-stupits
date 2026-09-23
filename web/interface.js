"use strict";

// Все суммы и показатели берём из API. Клиент хранит только выбор и состояние показа.
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const fmt = (value, digits = 2) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("ru-RU", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })
    : "—";
const signed = (value) =>
  typeof value === "number" ? (value > 0 ? "+" : "") + fmt(value) : "—";
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.city}</svg>`;
const directionIcon = {
  transport: "bus",
  ecology: "leaf",
  social: "school",
  safety: "shield",
  services: "service",
};
const motion = () =>
  matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 1;
const clone = (value) => structuredClone(value);
let catalog = null,
  baseline = null,
  geojson = null,
  events = [];
let map = null,
  mapReady = false,
  popup = null,
  hovered = null,
  markers = [],
  toastTimer;
const requests = new Map();
const state = {
  live: false,
  tab: "overview",
  district: null,
  filter: "all",
  targets: {},
  plan: [],
  status: null,
  result: null,
  resultKey: null,
  after: false,
  errors: [],
  event: null,
  version: 0,
  mutation: 0,
  busy: false,
  mutating: false,
  bootstrapBusy: false,
  drawer: null,
  advisor: null,
  advisorError: null,
  advisorPending: false,
  messages: [],
  chatBusy: false,
  optResults: [],
  optEvent: null,
  optBusy: false,
  tour: -1,
  threeD: false,
  savedPlans: [],
  checkedPlans: [],
  libraryBusy: false,
  libraryRevision: 0,
  restoring: false,
};
const SESSION_KEY = "akim-session-v1";
const SESSION_SCHEMA = 1;
const MAX_SAVED_PLANS = 10;
const measure = (id) => catalog?.measures.find((item) => item.id === id);
const district = (id) => baseline?.districts.find((item) => item.id === id);
const viewDistrict = (id) =>
  state.after && state.result
    ? state.result.districts.find((item) => item.id === id)
    : district(id);
const districtScore = (row) => row?.D_after ?? row?.D;
const indicatorValue = (row) => row?.after ?? row?.value;
const weakestId = () =>
  (state.after && state.result ? state.result : baseline)?.min_district?.id;
const locked = () =>
  !state.live || state.mutating || state.busy || state.bootstrapBusy;
const planKey = (plan) =>
  JSON.stringify(
    plan
      .map((item) => [item.measure, item.district ?? null])
      .sort((a, b) => a[0].localeCompare(b[0])),
  );
const currentKey = () => `${state.event || ""}:${planKey(state.plan)}`;
const weakIndicators = (row) =>
  Object.entries(row?.indicators || {})
    .sort((a, b) => indicatorValue(a[1]) - indicatorValue(b[1]))
    .slice(0, 3);

// В хранилище нет результата для показа: после F5 Score заново считает сервер.
// Отпечаток нужен только для проверки применимости сохранённого разбора.
function fingerprint(value) {
  const text = JSON.stringify(value);
  let hash = 2166136261;
  for (let i = 0; i < text.length; i++)
    hash = Math.imul(hash ^ text.charCodeAt(i), 16777619);
  return `${text.length}:${hash >>> 0}`;
}
function modelFingerprint() {
  return fingerprint({ catalog, baseline, events });
}
function cleanDecisions(value) {
  if (!Array.isArray(value) || value.length > 30)
    throw new Error("Нужен список решений допустимого размера.");
  return value.map((item) => {
    if (!item || typeof item.measure !== "string" || item.measure.length > 40 ||
        (item.district !== null && typeof item.district !== "string") ||
        (typeof item.district === "string" && item.district.length > 80))
      throw new Error("Решение должно содержать measure и district (код района или null).");
    return { measure: item.measure, district: item.district };
  });
}
function cleanSavedPlan(value) {
  if (!value || typeof value.name !== "string" || !value.name.trim() || value.name.length > 120)
    throw new Error("У каждого плана должно быть название до 120 символов.");
  if (value.event_id != null && (typeof value.event_id !== "string" || value.event_id.length > 40))
    throw new Error("Некорректный код события в сохранённом плане.");
  return { name: value.name.trim(), decisions: cleanDecisions(value.decisions), event_id: value.event_id || null };
}
function readSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (value.schema !== SESSION_SCHEMA) return null;
    value.plan = cleanDecisions(value.plan);
    value.savedPlans = (Array.isArray(value.savedPlans) ? value.savedPlans : [])
      .slice(0, MAX_SAVED_PLANS).map(cleanSavedPlan);
    if (value.event != null && (typeof value.event !== "string" || value.event.length > 40)) return null;
    value.messages = (Array.isArray(value.messages) ? value.messages : []).filter(
      (item) => ["user", "assistant"].includes(item?.role) && typeof item.content === "string",
    ).slice(-60);
    return value;
  } catch {
    // Повреждённое или запрещённое браузером хранилище не мешает запуску.
    return null;
  }
}
function persistSession() {
  if (state.restoring || state.bootstrapBusy || !catalog || !state.live) return;
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      schema: SESSION_SCHEMA, plan: state.plan, event: state.event,
      district: state.district, targets: state.targets, tab: state.tab,
      after: state.after, mapMetric: state.mapMetric || "D",
      savedPlans: state.savedPlans, messages: state.messages.slice(-60),
      advisor: state.advisor, advisorError: state.advisorError,
      checked_plans: conversationContext().checked_plans,
      resultKey: state.resultKey,
      resultFingerprint: state.result ? fingerprint(state.result) : null,
      modelFingerprint: modelFingerprint(),
    }));
  } catch {
    // Например, приватный режим или переполненная квота. Расчёт остаётся доступен.
    if (!state.storageNoticeShown) {
      state.storageNoticeShown = true;
      toast("Браузер не сохранил сессию. Экспортируйте планы в JSON перед обновлением страницы.");
    }
  }
}
function restoreAnalysis(saved, result) {
  if (!saved || saved.resultKey !== currentKey() ||
      saved.modelFingerprint !== modelFingerprint() ||
      saved.resultFingerprint !== fingerprint(result)) return false;
  state.advisor = saved.advisor && typeof saved.advisor.answer === "string" ? saved.advisor : null;
  state.advisorError = typeof saved.advisorError === "string" ? saved.advisorError : null;
  state.messages = saved.messages || [];
  state.checkedPlans = Array.isArray(saved.checked_plans) ? saved.checked_plans.slice(-6) : [];
  state.tab = ["overview", "projects", "results"].includes(saved.tab) ? saved.tab : "results";
  state.after = saved.after !== false;
  return !!state.advisor || !!state.advisorError;
}

function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
  });
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.add("hidden"), 5000);
}
function errorText(error) {
  if (error instanceof TypeError)
    return "Нет соединения с сервером. Проверьте запуск приложения и повторите запрос.";
  return error.name === "AbortError"
    ? "Ответ сервера не получен вовремя. Повторите запрос."
    : error.message || "Не удалось связаться с сервером.";
}
async function request(path, body, channel = path, timeout = 30000) {
  requests.get(channel)?.abort();
  const controller = new AbortController();
  requests.set(channel, controller);
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error(
        "Сервер вернул нечитаемый ответ. Проверьте запуск приложения.",
      );
    }
    if (!response.ok)
      throw new Error(
        data.error || data.errors?.join(" ") || "Сервер не выполнил запрос.",
      );
    return data;
  } finally {
    clearTimeout(timer);
    if (requests.get(channel) === controller) requests.delete(channel);
  }
}
function invalidateResult() {
  state.version++;
  for (const name of [
    "calculate",
    "validate",
    "advisor",
    "chat",
    "compare",
    "robustness",
  ])
    requests.get(name)?.abort();
  state.result = null;
  state.resultKey = null;
  state.after = false;
  state.advisor = null;
  state.advisorError = null;
  state.advisorPending = false;
  state.messages = [];
  state.checkedPlans = [];
  state.chatBusy = false;
  state.errors = [];
  state.busy = false;
  $("advisor-card").classList.add("hidden");
  if (["advisor", "compare", "robustness"].includes(state.drawer))
    closeDrawer();
}
function quickErrors(project, target) {
  if (state.plan.some((item) => item.measure === project.id))
    return "Проект уже в плане";
  if (state.plan.length >= catalog.num_decisions)
    return `Выбрано ${catalog.num_decisions} проектов — сначала уберите один`;
  if (!state.status) return "Ожидаем проверку бюджета";
  if (project.cost > state.status.budget_left) return "Не хватает бюджета";
  if (
    state.plan.filter(
      (item) => measure(item.measure)?.direction === project.direction,
    ).length >= catalog.max_per_direction
  )
    return `Не более ${catalog.max_per_direction} проектов одного направления`;
  for (const rule of catalog.incompatibilities || []) {
    const other = state.plan.find((item) => rule.pair.includes(item.measure));
    if (
      rule.pair.includes(project.id) &&
      other &&
      (rule.scope === "any" || other.district === target)
    )
      return rule.reason;
  }
  return "";
}

// Изменение плана — транзакция: быстрые клики блокируются до ответа plan-status.
async function commitPlan(nextPlan) {
  if (locked()) return false;
  if (planKey(nextPlan) === planKey(state.plan)) {
    if (nextPlan.length === catalog.num_decisions) await calculate();
    return true;
  }
  const mutation = ++state.mutation,
    version = state.version,
    event = state.event;
  state.mutating = true;
  state.errors = [];
  render();
  try {
    const status = await request(
      "/api/plan-status",
      { decisions: nextPlan, event_id: event },
      "plan-status",
    );
    if (mutation !== state.mutation || version !== state.version) return false;
    // Переход к полному набору разрешает только серверный валидатор.
    if (
      status.cost > status.budget ||
      (nextPlan.length === catalog.num_decisions &&
        status.validation?.valid === false)
    ) {
      state.errors = status.validation?.errors ||
        status.errors || ["План отклонён движком."];
      return false;
    }
    invalidateResult();
    state.plan = clone(nextPlan);
    state.status = status;
    return true;
  } catch (error) {
    if (mutation === state.mutation) state.errors = [errorText(error)];
    return false;
  } finally {
    if (mutation === state.mutation) {
      state.mutating = false;
      render();
    }
  }
}
async function addProject(id) {
  const project = measure(id);
  if (!project || locked()) return;
  const target =
    project.scope === "city"
      ? null
      : state.targets[id] || catalog.districts[0].id;
  const reason = quickErrors(project, target);
  if (reason) return toast(reason);
  if (
    (await commitPlan([...state.plan, { measure: id, district: target }])) &&
    state.plan.length === catalog.num_decisions
  )
    await calculate();
}
async function removeProject(id) {
  await commitPlan(state.plan.filter((item) => item.measure !== id));
}
async function clearPlan() {
  await commitPlan([]);
}
function setTab(tab) {
  state.tab = tab;
  render();
  $("panel-scroll").scrollTop = 0;
}
function setPhase(after) {
  if (!after || state.result) {
    state.after = after;
    render();
  }
}
function statusOf(value) {
  return value < catalog.crit_threshold
    ? ["Критично", "negative", "#d66052"]
    : value < 50
      ? ["Требует внимания", "warning", "#d9a441"]
      : ["В норме", "positive", "#579775"];
}
function errorBox() {
  return state.errors.length
    ? `<div class="error-box" role="alert"><b>Нужна проверка</b><ul>${state.errors.map((text) => `<li>${esc(text)}</li>`).join("")}</ul></div>`
    : "";
}
function completionWarning() {
  return state.plan.length === catalog.num_decisions - 1 &&
    state.status?.can_complete === false
    ? '<div class="advisor-notice" role="status">Этот план нельзя завершить: для оставшегося проекта нет допустимого варианта в бюджете. Уберите или замените выбранный проект.</div>'
    : "";
}
function render() {
  if (!catalog || !baseline) return;
  renderHeader();
  renderTabs();
  renderPanel();
  renderPlan();
  updateMap();
  if (!mapReady && !$("map-fallback").classList.contains("hidden"))
    renderMapFallback();
  persistSession();
}
function renderTabs() {
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === state.tab);
    button.setAttribute(
      "aria-current",
      button.dataset.tab === state.tab ? "page" : "false",
    );
  });
}
function renderHeader() {
  const shown = state.result && state.after ? state.result : baseline;
  // Никакого tween Score: промежуточные значения движок не рассчитывал.
  $("city-score").textContent = fmt(shown.score);
  $("score-mode").textContent =
    state.result && state.after
      ? "РЕЗУЛЬТАТ"
      : state.event
        ? "ПОСЛЕ СОБЫТИЯ"
        : "ИСХОДНОЕ";
  $("score-delta").classList.toggle("hidden", !state.result || !state.after);
  if (state.result)
    $("score-delta").textContent = signed(state.result.delta.score);
  $("score-caption").textContent =
    `База: ${fmt(baseline.score)} · горизонт ${catalog.horizon_quarters} кварталов`;
  const status = state.status;
  $("budget-left").innerHTML = status
    ? `${fmt(status.budget_left, 0)} <span>из ${fmt(status.budget, 0)} у.е.</span>`
    : "—";
  $("budget-left").classList.toggle(
    "negative",
    !!status && status.budget_left < 0,
  );
  $("budget-bar").style.width =
    status && status.budget > 0
      ? `${Math.max(0, Math.min(100, (status.budget_left / status.budget) * 100))}%`
      : "0%";
  $("budget-bar").style.background =
    status?.budget_left < 0 ? "var(--red)" : "var(--green)";
  $("calculate-button").disabled =
    locked() || state.plan.length !== catalog.num_decisions;
  $("calculate-button").innerHTML =
    state.busy || state.mutating
      ? '<span class="spinner"></span>Проверяем план…'
      : state.resultKey === currentKey()
        ? `Открыть результат ${icon("arrow")}`
        : state.plan.length === catalog.num_decisions
          ? `Рассчитать результат ${icon("arrow")}`
          : `Выберите ${catalog.num_decisions} проектов ${icon("arrow")}`;
  $("budget-note").textContent =
    `${state.plan.length} из ${catalog.num_decisions} проектов · ${state.result ? "результат готов" : "расчёт выполняет движок"}`;
  $("show-after").disabled = !state.result;
  for (const [id, active] of [
    ["show-before", !state.after],
    ["show-after", state.after],
  ]) {
    $(id).classList.toggle("active", active);
    $(id).setAttribute("aria-pressed", String(active));
  }
  $("map-phase").textContent = state.after ? "Стало" : "Было";
  $("clear-plan").disabled = locked() || !state.plan.length;
  $("events-button").disabled = state.bootstrapBusy;
  $("tour-button").disabled = locked();
  if ($("saved-plans-button")) $("saved-plans-button").disabled = !state.live || state.bootstrapBusy;
  if ($("print-report-button")) $("print-report-button").disabled = !state.result || locked();
  $("city-summary").textContent =
    `${catalog.districts.length} районов · ${catalog.measures.length} проектов · одно будущее`;
  document.querySelector(".tab-count").textContent = catalog.measures.length;
}
function renderPanel() {
  if (state.tab === "projects") renderProjects();
  else if (state.tab === "results") renderResults();
  else if (state.district) renderDistrict();
  else renderOverview();
}
function districtCards() {
  const modeled = baseline.districts
    .map(
      (row) => `<button class="district-row" data-district="${esc(row.id)}">
    <i class="district-dot" style="background:${ratingColor(districtScore(viewDistrict(row.id)))}"></i>
    <span class="district-title">${esc(row.name)}${row.id === weakestId() ? '<span class="weak-flag">Самый уязвимый район</span>' : ""}</span>
    <span class="district-value">${fmt(districtScore(viewDistrict(row.id)))}</span>${icon("arrow")}</button>`,
    )
    .join("");
  const extra = (geojson?.features || []).filter((feature) => !district(feature.properties?.id));
  return modeled + extra.map((feature) => `<button class="district-row" data-district="${esc(feature.properties.id)}"><i class="district-dot" style="background:#9aa6aa"></i><span class="district-title">${esc(feature.properties.name || feature.properties.id)}<br><small class="muted">Нет показателей в модели</small></span><span class="district-value">—</span>${icon("arrow")}</button>`).join("");
}
function bindDistricts(root) {
  root.querySelectorAll("[data-district]").forEach((button) => {
    button.onclick = () => selectDistrict(button.dataset.district);
  });
}
function renderOverview() {
  const weakest = baseline.min_district;
  $("panel").innerHTML =
    `${errorBox()}${completionWarning()}<div class="section-heading"><h3>Ваш город. Ваши решения.</h3></div>
    <p class="intro">Выберите ровно ${catalog.num_decisions} проектов в пределах бюджета. Район на карте нужен для просмотра; место проекта выбирается отдельно.</p>
    <div class="section-heading"><span class="eyebrow">Районы Астаны</span><span class="muted">Оценка D</span></div>${districtCards()}
    <div class="mission-card"><div class="mission-title">${icon("spark")} С чего начать?</div>
    <p>Самый слабый район — ${esc(weakest?.name)}. Изучите его показатели и проверьте план из задания.</p>
    <button class="btn lime wide" id="example-plan" ${locked() ? "disabled" : ""}>Попробовать план из ТЗ ${icon("arrow")}</button></div>
    <button id="optimize-button" class="text-button" ${locked() ? "disabled" : ""}>${icon("spark")} Найти лучший план</button>
    <button id="overview-saved-plans" class="text-button">Планы команд · импорт и экспорт</button>`;
  bindDistricts($("panel"));
  $("example-plan").onclick = applyExample;
  $("optimize-button").onclick = () => optimize();
  $("overview-saved-plans").onclick = openSavedPlans;
}
function indicatorHtml([key, row]) {
  const value = indicatorValue(row),
    severity = statusOf(value);
  return `<div class="indicator"><div class="indicator-top"><span>${esc(row.name || catalog.indicators[key].name)}</span><b>${fmt(value)}</b></div>
    <div class="track"><i style="width:${Math.max(0, Math.min(100, value))}%;background:${severity[2]}"></i></div>
    <div class="indicator-status"><span class="${severity[1]}">${severity[0]}</span>${state.after && row.delta !== undefined ? `<span class="${row.delta < 0 ? "negative" : "positive"}">${signed(row.delta)} к исходному</span>` : ""}</div></div>`;
}
function renderDistrict() {
  const row = district(state.district);
  if (!row) {
    const feature = geojson?.features?.find((item) => item.properties?.id === state.district);
    $("panel").innerHTML = `<button class="back" id="back-city">${icon("back")} Все районы</button><span class="eyebrow">Географический район</span><h2>${esc(feature?.properties?.name || state.district)}</h2><div class="advisor-notice">В учебном датасете этого района нет. Его границы показаны для географического контекста; оценка D, показатели и размещение проектов здесь недоступны.</div><p>Городской Score рассчитывается по районам, для которых движок содержит данные.</p>`;
    $("back-city").onclick = () => { state.district = null; render(); flyOverview(); };
    return;
  }
  const shown = viewDistrict(row.id);
  $("panel").innerHTML =
    `${errorBox()}<button class="back" id="back-city">${icon("back")} Все районы</button>
    <div class="district-head"><div><span class="eyebrow">Район для просмотра</span><h2>${esc(row.name)}</h2></div>
    <div class="district-big-score"><strong>${fmt(districtScore(shown))}</strong><span>оценка района</span></div></div>
    <p class="district-profile">${esc(row.profile)}</p><p class="small muted">Доля населения: ${new Intl.NumberFormat("ru-RU", { style: "percent" }).format(row.population_share)}.</p>
    <p class="result-note">Просмотр района не меняет размещение проектов.</p>
    <div class="section-heading"><h3>Показатели района</h3><span class="muted">${Object.keys(shown.indicators).length} показателей</span></div>
    ${Object.entries(shown.indicators).map(indicatorHtml).join("")}
    <div class="district-actions"><button id="district-projects" class="btn primary">Выбрать проекты ${icon("arrow")}</button><button id="district-3d" class="btn">Посмотреть район ближе</button></div>`;
  $("back-city").onclick = () => {
    state.district = null;
    render();
    flyOverview();
  };
  $("district-projects").onclick = () => setTab("projects");
  $("district-3d").onclick = () => flyDistrict(row.id, true);
}
function renderProjects() {
  const projects = catalog.measures.filter(
    (item) => state.filter === "all" || item.direction === state.filter,
  );
  $("panel").innerHTML =
    `${errorBox()}${completionWarning()}<div class="section-heading"><h3>Инвестиции в город</h3><span class="muted">${state.plan.length} из ${catalog.num_decisions}</span></div>
    <p class="small muted">Район реализации выбирается в каждой карточке. Просмотр карты на него не влияет.</p>
    <div class="filters">${[["all", "Все"], ...Object.entries(catalog.directions)].map(([key, name]) => `<button class="filter ${state.filter === key ? "active" : ""}" data-filter="${esc(key)}">${esc(name)}</button>`).join("")}</div>
    ${projects
      .map((project) => {
        const chosen = state.plan.find((item) => item.measure === project.id);
        const target =
          chosen?.district ||
          state.targets[project.id] ||
          catalog.districts[0].id;
        const reason = quickErrors(project, target);
        return `<article class="project-card ${chosen ? "chosen" : ""}"><div class="project-meta"><div class="row"><span class="category-icon">${icon(directionIcon[project.direction])}</span>${esc(catalog.directions[project.direction])}</div>
        <div class="project-cost">${fmt(project.cost, 0)} <span>у.е.</span></div></div><h4>${esc(project.name)}</h4>
        <div class="project-effects">${Object.entries(project.effects)
          .map(
            ([key, value]) =>
              `${esc(catalog.indicators[key]?.name || key)}: <span class="${value < 0 ? "negative" : ""}">${signed(value)}</span>`,
          )
          .join(
            " · ",
          )}<br>Лаг: ${project.lag} кв. · эффекты до учёта задержки</div>
        <div class="project-bottom">${project.scope === "city" ? '<span class="city-scope">Во всех районах</span>' : `<select aria-label="Район реализации ${esc(project.name)}" data-target="${esc(project.id)}" ${chosen || locked() ? "disabled" : ""}>${catalog.districts.map((row) => `<option value="${esc(row.id)}" ${row.id === target ? "selected" : ""}>${esc(row.name)}</option>`).join("")}</select>`}
        <button class="add-project" data-${chosen ? "remove" : "add"}="${esc(project.id)}" ${locked() || (!chosen && reason) ? "disabled" : ""} title="${esc(chosen ? "Убрать из плана" : reason || "Добавить проект")}">${chosen ? "✓ В плане" : "＋ Добавить"}</button></div>
        ${chosen ? '<div class="reason">Для смены района уберите проект и добавьте заново.</div>' : reason ? `<div class="reason">${esc(reason)}</div>` : ""}</article>`;
      })
      .join(
        "",
      )}<details class="rules"><summary>Правила и совместимость</summary><p>Доступный бюджет в текущих условиях: ${fmt(state.status?.budget, 0)} у.е. Остаток не даёт бонуса.</p><ul>${catalog.rules
      .filter((rule) => !/^Бюджет(?:\s|:|$)/iu.test(rule))
      .map((rule) => `<li>${esc(rule)}</li>`)
      .join("")}</ul></details>`;
  $("panel")
    .querySelectorAll("[data-filter]")
    .forEach((button) => {
      button.onclick = () => {
        state.filter = button.dataset.filter;
        renderProjects();
      };
    });
  $("panel")
    .querySelectorAll("[data-target]")
    .forEach((select) => {
      select.onchange = () => {
        state.targets[select.dataset.target] = select.value;
        renderProjects();
      };
    });
  bindProjectButtons($("panel"));
}
function bindProjectButtons(root) {
  root.querySelectorAll("[data-add]").forEach((button) => {
    button.onclick = () => addProject(button.dataset.add);
  });
  root.querySelectorAll("[data-remove]").forEach((button) => {
    button.onclick = () => removeProject(button.dataset.remove);
  });
}
function renderPlan() {
  $("plan-count").textContent =
    `${state.plan.length} / ${catalog.num_decisions}`;
  $("plan-cost").textContent = state.status
    ? `Стоимость ${fmt(state.status.cost, 0)} у.е.`
    : "Проверяем стоимость";
  $("plan-slots").style.gridTemplateColumns =
    `repeat(${catalog.num_decisions}, minmax(0,1fr))`;
  $("plan-slots").innerHTML = Array.from(
    { length: catalog.num_decisions },
    (_, index) => {
      const choice = state.plan[index],
        project = choice && measure(choice.measure);
      return choice
        ? `<div class="plan-slot filled"><span class="slot-number">${index + 1}</span><div class="slot-copy" title="${esc(project.name)}"><b>${esc(project.name)}</b>
      <span>${esc(choice.district ? district(choice.district)?.name : "Весь город")} · ${fmt(project.cost, 0)} у.е.</span></div><button class="slot-remove" data-remove="${esc(choice.measure)}" ${locked() ? "disabled" : ""} aria-label="Убрать ${esc(project.name)}">×</button></div>`
        : `<button class="plan-slot empty" data-empty><span class="slot-number">${index + 1}</span>Выбрать проект</button>`;
    },
  ).join("");
  bindProjectButtons($("plan-slots"));
  $("plan-slots")
    .querySelectorAll("[data-empty]")
    .forEach((button) => {
      button.onclick = () => setTab("projects");
    });
}

function dataTable(headings, rows) {
  return `<div class="table-scroll"><table class="data-table"><thead><tr>${headings.map((text) => `<th>${esc(text)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((value) => `<td>${esc(value)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}
function renderResults() {
  const result = state.result;
  if (!result) {
    $("panel").innerHTML =
      `${errorBox()}<div class="empty-state">${icon("chart")}<h3>${state.busy ? "Движок рассчитывает план" : "Будущее ещё не решено"}</h3>
      <p>Выберите ${catalog.num_decisions} проектов. Здесь появятся результат и вклад решений.</p><button class="btn lime" id="go-projects">К проектам ${icon("arrow")}</button></div>`;
    $("go-projects").onclick = () => setTab("projects");
    return;
  }
  $("panel").innerHTML =
    `${errorBox()}<span class="eyebrow">Ваш план · ${catalog.horizon_quarters} кварталов</span><h2>Как меняется город</h2>
    <div class="result-hero"><div class="small muted">Astana Quality of Life Score</div><div class="result-numbers"><span>${fmt(result.baseline.score)}</span><span class="result-arrow">→</span><strong>${fmt(result.score)}</strong></div><span class="pill">${signed(result.delta.score)} к базе</span></div>
    <div class="result-summary"><div><strong>${fmt(result.N_crit, 0)}</strong><span>критических показателей<br>было ${fmt(result.baseline.N_crit, 0)}</span></div><div><strong>${fmt(result.budget_left, 0)} <small>у.е.</small></strong><span>осталось из ${fmt(result.budget, 0)}</span></div></div>
    <hr><h3>Районы: было и стало</h3><p class="small muted">Серый — было · зелёный — стало</p>
    ${result.districts.map((row) => `<div class="comparison-row"><div class="row"><span>${esc(row.name)}</span><strong>${fmt(row.D_before)} → ${fmt(row.D_after)}</strong><span class="${row.D_delta < 0 ? "negative" : "positive"}">${signed(row.D_delta)}</span></div><div class="compare-track"><i style="width:${Math.max(0, Math.min(100, row.D_before))}%"></i></div><div class="compare-track after"><i style="width:${Math.max(0, Math.min(100, row.D_after))}%"></i></div></div>`).join("")}
    ${dataTable(
      ["Район", "Было", "Стало", "Δ"],
      result.districts.map((row) => [
        row.name,
        fmt(row.D_before),
        fmt(row.D_after),
        signed(row.D_delta),
      ]),
    )}
    <button id="open-advisor-result" class="btn lime wide">${icon("spark")} Разбор советника</button>
    <button id="compare-result" class="text-button">${icon("chart")} Сравнить с лучшим планом</button>
    <button id="robustness-result" class="text-button">${icon("shield")} Проверить устойчивость</button>
    <button id="optimize-result" class="text-button">${icon("spark")} Найти лучшие планы</button>
    <button id="result-saved-plans" class="text-button">Планы команд · сравнение / JSON</button>
    <button id="result-print-report" class="text-button">Краткий отчёт · печать / PDF</button>
    <details class="result-details"><summary>Вышли из критической зоны</summary>${
      result.critical_resolved.length
        ? dataTable(
            ["Район / показатель", "Было", "Стало"],
            result.critical_resolved.map((row) => [
              `${row.district_name} · ${row.indicator_name}`,
              fmt(row.before),
              fmt(row.after),
            ]),
          )
        : "<p>Ни один показатель не вышел из критической зоны.</p>"
    }</details>
    <details class="result-details"><summary>Остались критическими</summary>${
      result.critical_indicators.length
        ? dataTable(
            ["Район / показатель", "Значение"],
            result.critical_indicators.map((row) => [
              `${row.district_name} · ${row.indicator_name}`,
              fmt(row.value),
            ]),
          )
        : "<p>Критических показателей нет.</p>"
    }</details>
    <details class="result-details"><summary>Синергии</summary>${
      result.synergies.length
        ? `<ul>${result.synergies
            .map(
              (item) =>
                `<li>${esc(item.pair.map((id) => measure(id)?.name || id).join(" + "))} · ${esc(item.district_name)}: ${Object.entries(
                  item.bonus,
                )
                  .map(
                    ([key, value]) =>
                      `${esc(catalog.indicators[key]?.name || key)} ${signed(value)}`,
                  )
                  .join(", ")}</li>`,
            )
            .join("")}</ul>`
        : "<p>Синергии не сработали.</p>"
    }</details>
    <details class="result-details"><summary>Вклад и последствия каждого проекта</summary>${result.measures
      .map(
        (item) =>
          `<p><b>${esc(item.name)}</b> · ${esc(item.district_name)}<br>Вклад в Score: ${signed(item.score_contribution)}<br>${Object.entries(
            item.effects_realized,
          )
            .map(
              ([key, value]) =>
                `${esc(catalog.indicators[key]?.name || key)}: <span class="${value < 0 ? "negative" : ""}">${signed(value)}</span>`,
            )
            .join("<br>")}</p>`,
      )
      .join(
        "",
      )}<p>Вклад учитывает потерю синергий. Сумма вкладов может отличаться от общего прироста.</p></details>
    <details class="result-details"><summary>Все показатели: было / стало</summary>${result.districts
      .map(
        (row) =>
          `<h4>${esc(row.name)}</h4>${dataTable(
            ["Показатель", "Было", "Стало", "Δ"],
            Object.values(row.indicators).map((item) => [
              item.name,
              fmt(item.before),
              fmt(item.after),
              signed(item.delta),
            ]),
          )}`,
      )
      .join("")}</details>
    <p class="result-note">Изменения переданы движком до округления. Ниже ${catalog.crit_threshold} — критично; до 50 — требует внимания. Это учебная модель.</p>`;
  $("open-advisor-result").onclick = openAdvisor;
  $("optimize-result").onclick = () => optimize();
  $("compare-result").onclick = compareWithBest;
  $("robustness-result").onclick = openRobustness;
  $("result-saved-plans").onclick = openSavedPlans;
  $("result-print-report").onclick = printReport;
}

async function calculate(options = {}) {
  if (
    !state.live ||
    state.busy ||
    state.mutating ||
    state.bootstrapBusy ||
    state.plan.length !== catalog.num_decisions
  )
    return;
  const key = currentKey();
  if (state.result && state.resultKey === key) {
    state.after = true;
    setTab("results");
    return;
  }
  const version = state.version,
    decisions = clone(state.plan),
    event = state.event;
  state.busy = true;
  state.errors = [];
  render();
  try {
    const validated = await request(
      "/api/validate",
      { decisions, event_id: event },
      "validate",
    );
    if (version !== state.version) return;
    if (!validated.valid) {
      state.errors = validated.errors || ["План не прошёл проверку"];
      return;
    }
    const result = await request(
      "/api/simulate",
      { decisions, event_id: event },
      "calculate",
    );
    if (version !== state.version || key !== currentKey()) return;
    if (!result.valid) {
      state.errors = result.errors || ["План не прошёл проверку"];
      return;
    }
    state.result = result;
    state.resultKey = key;
    state.after = true;
    state.tab = "results";
    const reusedAnalysis = restoreAnalysis(options.restoration, result);
    render();
    if (reusedAnalysis) renderAdvisorCard();
    flyOverview();
    $("panel-scroll").scrollTop = 0;
    // Ответ советника приходит отдельно; карта и результаты доступны сразу.
    if (!reusedAnalysis) void loadAdvisor(version, key);
  } catch (error) {
    if (version === state.version) state.errors = [errorText(error)];
  } finally {
    if (version === state.version) {
      state.busy = false;
      render();
    }
  }
}
async function applyExample() {
  const example = catalog.reference_checks?.example_valid_set?.decisions;
  if (!Array.isArray(example))
    return toast("В данных нет примера плана. Выберите проекты из каталога.");
  if (await commitPlan(example)) await calculate();
}
function showDrawer(type, title, subtitle, body) {
  state.drawer = type;
  $("drawer").classList.remove("hidden");
  $("drawer").innerHTML =
    `<div class="drawer-header"><div><h3>${esc(title)}</h3><p>${esc(subtitle)}</p></div><button class="icon-btn" id="close-drawer" aria-label="Закрыть панель">${icon("close")}</button></div><div class="drawer-body" id="drawer-body">${body}</div>`;
  $("close-drawer").onclick = closeDrawer;
}
function closeDrawer() {
  state.drawer = null;
  $("drawer").classList.add("hidden");
}
function markdown(text) {
  // HTML от сервера/модели не исполняется; поддерживаем безопасное простое оформление.
  return esc(text)
    .split(/\n\s*\n/)
    .map(
      (part) =>
        `<p>${part.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>")}</p>`,
    )
    .join("");
}
function traceHtml(response) {
  const calls = response?.tool_calls || [];
  return `<details class="result-details"><summary>Как советник пришёл к выводу</summary>${calls.length ? calls.map((call) => `<div class="tool-call"><b>${esc(call.name)}</b><p class="small muted">${call.source === "model" ? "Выбор ИИ" : call.source === "fallback" ? "Проверка запасного режима" : "Проверка приложения"} · ${esc(call.status || "")}</p>${call.note ? `<p>${esc(call.note)}</p>` : ""}<p>${esc(call.summary)}</p><b>Аргументы</b><pre>${esc(JSON.stringify(call.arguments, null, 2))}</pre><b>Результат</b><pre>${esc(JSON.stringify(call.result, null, 2))}</pre></div>`).join("") : "<p>Новых вызовов инструментов нет.</p>"}</details>`;
}
function responseHtml(response) {
  return `${response.offline ? `<div class="advisor-notice">${esc(response.notice || "Советник работает в офлайн-режиме.")} ${esc(response.reason)}</div>` : ""}<div class="chat-block">${markdown(response.answer)}</div>${traceHtml(response)}`;
}
async function loadAdvisor(version, key) {
  if (state.advisor || state.advisorPending || key !== state.resultKey) return;
  state.advisorPending = true;
  state.advisorError = null;
  renderAdvisorCard();
  try {
    const answer = await request(
      "/api/advisor",
      {
        decisions: clone(state.plan),
        event_id: state.event,
        history: [],
        checked_plans: [],
      },
      "advisor",
      30000,
    );
    if (version !== state.version || key !== state.resultKey) return;
    state.advisor = answer;
  } catch (error) {
    if (version === state.version) state.advisorError = errorText(error);
  } finally {
    if (version === state.version) {
      state.advisorPending = false;
      persistSession();
      renderAdvisorCard();
      if (state.drawer === "advisor") renderAdvisorDrawer();
    }
  }
}
function renderAdvisorCard() {
  if (!state.result) return;
  $("advisor-card").classList.remove("hidden");
  $("advisor-card").innerHTML =
    `<div class="row"><div class="advisor-orb">${icon("spark")}</div><div><h3>Советник акима</h3><span class="small muted">${state.advisorPending ? "Советник думает…" : state.advisor?.offline ? "Офлайн-разбор" : state.advisorError ? "Ответ недоступен" : "Разбор решения"}</span></div><button class="icon-btn close-advisor" id="hide-advisor" aria-label="Скрыть краткий разбор">${icon("close")}</button></div>
    ${state.advisorPending ? '<p><span class="spinner"></span>Проверяем последствия и альтернативы</p>' : state.advisorError ? `<p>${esc(state.advisorError)}</p>` : state.advisor ? markdown(state.advisor.answer.split(/\n\s*\n/)[0]) : ""}<button class="text-button" id="advisor-expand">Разбор и вопросы ${icon("arrow")}</button>`;
  $("hide-advisor").onclick = () => $("advisor-card").classList.add("hidden");
  $("advisor-expand").onclick = openAdvisor;
}
function openAdvisor() {
  if (state.result) renderAdvisorDrawer();
  else toast("Сначала рассчитайте полный план.");
}
function renderAdvisorDrawer() {
  showDrawer(
    "advisor",
    "Советник акима",
    "Сильные стороны, риски и последствия",
    (state.advisor
      ? responseHtml(state.advisor)
      : state.advisorError
        ? `<div class="error-box">${esc(state.advisorError)}</div><button id="retry-advisor" class="btn">Повторить запрос</button>`
        : '<p><span class="spinner"></span>Советник думает…</p>') +
      state.messages
        .map((message) =>
          message.response
            ? responseHtml(message.response)
            : `<div class="chat-block ${message.role === "user" ? "user" : ""}">${markdown(message.content)}</div>`,
        )
        .join("") +
      (state.chatBusy
        ? '<p><span class="spinner"></span>Советник проверяет данные…</p>'
        : ""),
  );
  $("retry-advisor")?.addEventListener("click", () => {
    void loadAdvisor(state.version, state.resultKey);
    renderAdvisorDrawer();
  });
  $("drawer").insertAdjacentHTML(
    "beforeend",
    `<form class="chat-input" id="chat-form"><input id="chat-question" aria-label="Вопрос советнику" placeholder="Какие риски остались?" maxlength="1000" ${state.chatBusy || state.advisorPending ? "disabled" : ""}><button class="icon-btn" aria-label="Отправить вопрос" ${state.chatBusy || state.advisorPending ? "disabled" : ""}>${icon("arrow")}</button></form>`,
  );
  $("chat-form").onsubmit = (event) => {
    event.preventDefault();
    void askQuestion($("chat-question").value);
  };
  if (state.messages.length)
    $("drawer-body").scrollTop = $("drawer-body").scrollHeight;
}
function conversationContext() {
  const history = state.messages
    .slice(-6)
    .map(({ role, content }) => ({ role, content }));
  const plans = clone(state.checkedPlans || []);
  for (const response of [
    state.advisor,
    ...state.messages.map((item) => item.response),
  ]) {
    for (const plan of response?.checked_plans || []) {
      const index = plans.findIndex(
        (item) => JSON.stringify(item) === JSON.stringify(plan),
      );
      if (index >= 0) plans.splice(index, 1);
      plans.push(clone(plan));
    }
  }
  return { history, checked_plans: plans.slice(-6) };
}
async function askQuestion(question) {
  question = question.trim();
  if (!question || state.chatBusy || state.advisorPending || !state.result)
    return;
  const version = state.version,
    context = conversationContext();
  state.messages.push({ role: "user", content: question });
  persistSession();
  state.chatBusy = true;
  renderAdvisorDrawer();
  try {
    const response = await request(
      "/api/advisor",
      {
        question,
        decisions: clone(state.plan),
        event_id: state.event,
        ...context,
      },
      "chat",
      30000,
    );
    if (version !== state.version) return;
    state.messages.push({
      role: "assistant",
      content: response.answer,
      response,
    });
  } catch (error) {
    if (version === state.version)
      state.messages.push({
        role: "assistant",
        content: `Не удалось получить ответ: ${errorText(error)}`,
      });
  } finally {
    if (version === state.version) {
      state.chatBusy = false;
      persistSession();
      if (state.drawer === "advisor") renderAdvisorDrawer();
    }
  }
}

async function optimize(robust = false) {
  if (!state.live || state.bootstrapBusy || state.optBusy) return;
  const event = state.event;
  state.optBusy = true;
  showDrawer(
    "optimize",
    "Лучшие решения для города",
    robust
      ? "Устойчивость проверяется до событий"
      : "Бюджет и совместимость проверяет движок",
    '<div class="empty-state"><span class="spinner"></span>Ищем допустимые планы…</div>',
  );
  try {
    const response = await request(
      "/api/optimize",
      { top_n: 5, event_id: robust ? null : event, constraints: {}, robust },
      "optimize",
      120000,
    );
    if (event !== state.event) return;
    if (response.errors?.length) throw new Error(response.errors.join(" "));
    state.optResults = response.results || [];
    state.optEvent = event;
    state.optRobust = robust;
    if (state.drawer === "optimize") renderOptimization();
  } catch (error) {
    if (event === state.event && state.drawer === "optimize")
      $("drawer-body").innerHTML =
        `<div class="error-box">${esc(errorText(error))}</div>`;
  } finally {
    if (event === state.event) state.optBusy = false;
  }
}
function renderOptimization() {
  showDrawer(
    "optimize",
    "Лучшие решения для города",
    state.optRobust
      ? "Порядок по устойчивости в худшем случае"
      : "Применение заменит текущий план",
    (state.optResults.length
      ? state.optResults
          .map(
            (plan, index) =>
              `<article class="opt-card"><div class="row between"><span class="pill">План ${index + 1}</span><span>${fmt(plan.cost, 0)} у.е.</span></div><div class="row"><strong class="opt-score">${fmt(plan.score)}</strong><span class="positive small">${signed(plan.score_delta)} к базе</span></div>${plan.worst_case ? `<p class="small">Худший случай: ${esc(plan.worst_case.name)} · Score ${fmt(plan.worst_case.score)}</p>` : ""}<ul>${plan.decisions.map((item) => `<li>${esc(measure(item.measure)?.name || item.measure)} · ${esc(item.district ? district(item.district)?.name : "Весь город")}</li>`).join("")}</ul><button class="btn ${index === 0 ? "primary" : ""} wide" data-apply-opt="${index}" ${locked() ? "disabled" : ""}>Применить ${icon("arrow")}</button></article>`,
          )
          .join("")
      : "<p>Допустимые планы не найдены.</p>") +
      `<button class="btn wide" id="search-robust">${state.optRobust ? "Обычный поиск" : "Искать устойчивые планы"}</button>`,
  );
  $("search-robust").onclick = () => optimize(!state.optRobust);
  $("drawer")
    .querySelectorAll("[data-apply-opt]")
    .forEach((button) => {
      button.onclick = async () => {
        if (state.optEvent !== state.event)
          return toast("Событие изменилось. Повторите поиск.");
        const plan = state.optResults[Number(button.dataset.applyOpt)];
        if (await commitPlan(plan.decisions)) {
          closeDrawer();
          await calculate();
        }
      };
    });
}
async function openRobustness() {
  if (!state.result) return toast("Сначала рассчитайте план.");
  const version = state.version;
  showDrawer(
    "robustness",
    "Устойчивость плана",
    "Один состав до всех событий; текущий выбор события не применяется",
    '<span class="spinner"></span>Проверяем события…',
  );
  try {
    const response = await request(
      "/api/robustness",
      { decisions: clone(state.plan) },
      "robustness",
      120000,
    );
    if (version !== state.version || state.drawer !== "robustness") return;
    if (response.errors?.length) throw new Error(response.errors.join(" "));
    $("drawer-body").innerHTML =
      `<p class="small">${esc(response.summary)}</p><h4>Худший случай</h4><p>${esc(response.worst_case?.name)} · ${response.worst_score === null ? "План не проходит" : `Score ${fmt(response.worst_score)}`}</p>` +
      dataTable(
        ["Событие", "Score", "Падение", "Проверка"],
        (response.events || []).map((row) => [
          row.name,
          fmt(row.score),
          fmt(row.drop),
          row.valid ? "Допустимо" : (row.errors || []).join(" "),
        ]),
      );
  } catch (error) {
    if (version === state.version && state.drawer === "robustness")
      $("drawer-body").innerHTML =
        `<div class="error-box">${esc(errorText(error))}</div>`;
  }
}
async function compareWithBest() {
  if (!state.result) return toast("Сначала рассчитайте план.");
  const version = state.version,
    event = state.event,
    decisions = clone(state.plan);
  showDrawer(
    "compare",
    "Ваш план и лучший",
    "Одинаковые условия для обоих наборов",
    '<span class="spinner"></span>Рассчитываем сравнение…',
  );
  try {
    const found = await request(
      "/api/optimize",
      { top_n: 5, event_id: event, constraints: {}, robust: false },
      "compare-search",
      120000,
    );
    if (version !== state.version) return;
    if (found.errors?.length || !found.results?.length)
      throw new Error(
        found.errors?.join(" ") || "Движок не нашёл допустимый план.",
      );
    const response = await request(
      "/api/compare",
      {
        plans: {
          "Ваш план": decisions,
          "Лучший план": found.results[0].decisions,
        },
        event_id: event,
      },
      "compare",
    );
    if (version !== state.version || state.drawer !== "compare") return;
    if (response.errors?.length) throw new Error(response.errors.join(" "));
    $("drawer-body").innerHTML =
      `<p class="small">База ${fmt(response.baseline_score)} · лидер: ${esc(response.leader)}</p>` +
      dataTable(
        ["План", "Score", "Δ к базе", "Стоимость", "Отставание"],
        response.ranking.map((row) => [
          row.name,
          fmt(row.score),
          signed(row.delta_vs_baseline),
          fmt(row.cost, 0),
          fmt(row.gap_to_leader),
        ]),
      ) +
      response.ranking
        .map(
          (row) =>
            `<article class="opt-card"><h4>${esc(row.name)}</h4><p>${row.valid ? "Допустимый план" : esc((row.errors || []).join(" "))}</p><ul>${(row.measures || []).map((text) => `<li>${esc(text)}</li>`).join("")}</ul></article>`,
        )
        .join("");
  } catch (error) {
    if (version === state.version && state.drawer === "compare")
      $("drawer-body").innerHTML =
        `<div class="error-box">${esc(errorText(error))}</div>`;
  }
}

function openEvents() {
  if (!catalog) return;
  showDrawer(
    "events",
    "Город не стоит на месте",
    "Событие меняет исходные условия и доступный бюджет",
    (state.event
      ? '<button class="btn wide" id="cancel-event">Вернуться к обычным условиям</button>'
      : "") +
      events
        .map(
          (event) =>
            `<button class="event-card" data-event="${esc(event.id)}" ${state.bootstrapBusy ? "disabled" : ""}><span class="pill warning">${esc(event.district ? district(event.district)?.name : "Весь город")}</span><h4>${esc(event.name)}</h4><p>${esc(event.description)}</p><span class="event-apply">Смоделировать событие →</span></button>`,
        )
        .join(""),
  );
  $("cancel-event")?.addEventListener("click", () => bootstrap(null));
  $("drawer")
    .querySelectorAll("[data-event]")
    .forEach((button) => {
      button.onclick = () => bootstrap(button.dataset.event);
    });
}
function renderEvent() {
  const event = events.find((item) => item.id === state.event);
  document.body.classList.toggle("event-active", !!event);
  $("event-banner").classList.toggle("hidden", !event);
  $("events-button").classList.toggle("lime", !!event);
  if (event) {
    $("event-banner").innerHTML =
      `<div class="row between"><strong>${icon("bolt")} ${esc(event.name)}</strong><button id="remove-event-banner" class="icon-btn" aria-label="Убрать событие">${icon("close")}</button></div><p>${esc(event.description)}</p>`;
    $("remove-event-banner").onclick = () => bootstrap(null);
  }
}
function renderMapFallback(
  message = "Карта недоступна. Выберите район на карточке.",
) {
  if (!catalog || !baseline) return;
  $("map-fallback").classList.remove("hidden");
  $("map-fallback").innerHTML =
    `<h3>Районы Астаны</h3><p class="small muted">${esc(message)}</p>${districtCards()}`;
  bindDistricts($("map-fallback"));
}
function connection(online, text) {
  $("connection").classList.toggle("offline", !online);
  $("connection").innerHTML = `<i class="dot"></i><span>${esc(text)}</span>`;
}
async function bootstrap(event = null) {
  if (state.bootstrapBusy && event === state.requestedEvent) return;
  state.requestedEvent = event;
  state.bootstrapBusy = true;
  state.live = false;
  state.mutating = false;
  state.mutation++;
  for (const controller of requests.values()) controller.abort();
  invalidateResult();
  closeDrawer();
  state.status = null;
  state.optBusy = false;
  state.optResults = [];
  const version = state.version;
  connection(false, "Подключение…");
  if (catalog) render();
  else
    $("panel").innerHTML =
      '<div class="empty-state"><span class="spinner"></span>Загружаем данные города…</div>';
  try {
    const data = await request(
      "/api/bootstrap" +
        (event ? "?event_id=" + encodeURIComponent(event) : ""),
      undefined,
      "bootstrap",
    );
    if (version !== state.version) return;
    if (
      data.mode !== "live" ||
      !Array.isArray(data.baseline?.districts) ||
      !Array.isArray(data.catalog?.measures)
    )
      throw new Error("Ответ сервера не содержит актуальных данных движка.");
    const status = await request(
      "/api/plan-status",
      { decisions: clone(state.plan), event_id: event },
      "plan-status",
    );
    if (version !== state.version) return;
    catalog = data.catalog;
    baseline = data.baseline;
    geojson = data.geojson;
    events = Array.isArray(data.events)
      ? data.events
      : data.events?.events || [];
    state.event = event;
    state.status = status;
    state.live = true;
    state.errors = [];
    if (state.district && !district(state.district)) state.district = null;
    for (const [mid, target] of Object.entries(state.targets))
      if (!district(target)) delete state.targets[mid];
    renderEvent();
    connection(true, "Движок подключён");
    const approximate = geojson?.features?.some(
      (feature) => feature.properties?.source === "fallback_approx",
    );
    document.querySelector(".map-footer").textContent = approximate
      ? "Границы районов схематичны · Учебная модель"
      : "Учебная модель · данные движка";
    if (!geojson?.features?.length) {
      // Геометрия не подменяется сохранённым примером даже при переключении события.
      map?.remove();
      map = null;
      mapReady = false;
      markers = [];
      renderMapFallback("Границы районов не переданы. Все расчёты доступны.");
    } else if (!map) {
      if (window.maplibregl) loadMap();
      else loadMapLibrary();
    }
  } catch (error) {
    if (version !== state.version) return;
    state.errors = [errorText(error)];
    state.live = false;
    connection(false, "Движок недоступен");
    if (!catalog)
      $("panel").innerHTML =
        `<div class="error-box"><b>Не удалось загрузить город</b><p>${esc(errorText(error))}</p><p>Запустите Python-сервер приложения. Без API расчёты недоступны.</p></div><button class="btn lime" id="retry-bootstrap">Повторить подключение</button>`;
    $("retry-bootstrap")?.addEventListener("click", () => bootstrap(event));
  } finally {
    if (version === state.version) {
      state.bootstrapBusy = false;
      render();
      if (catalog && !state.live) {
        $("panel").insertAdjacentHTML(
          "afterbegin",
          '<button class="btn wide" id="retry-bootstrap">Повторить подключение</button>',
        );
        $("retry-bootstrap").onclick = () => bootstrap(event);
      }
      if (state.live && state.plan.length === catalog.num_decisions)
        void calculate();
    }
  }
}
let mapScriptPending = false;
function loadMapLibrary() {
  if (mapScriptPending) return;
  mapScriptPending = true;
  const script = document.createElement("script");
  script.src = "/vendor/maplibre-gl.js";
  script.onload = () => {
    mapScriptPending = false;
    if (geojson?.features?.length) loadMap();
  };
  script.onerror = () => {
    mapScriptPending = false;
    showMapError(
      "Не удалось загрузить карту. Панель районов и расчёты доступны.",
    );
  };
  document.head.appendChild(script);
}
function tourSteps() {
  return [
    {
      title: "Вы — аким. Каждый район важен.",
      text: "Изучите район с самой низкой оценкой и его показатели.",
      button: "Открыть район",
      action: () => selectDistrict(baseline.min_district.id),
    },
    {
      title: "Бюджет ограничен. Решения — за вами.",
      text: "Проверим пример из задания настоящим движком. Все суммы и эффекты придут от сервера.",
      button: "Рассчитать пример",
      action: applyExample,
    },
    {
      title: "План готов к разбору.",
      text: "Посмотрите последствия, ограничения и журнал инструментов советника.",
      button: "Открыть разбор",
      action: openAdvisor,
    },
    {
      title: "Сравните альтернативы.",
      text: "Найдите лучшие планы и проверьте устойчивость к городским событиям.",
      button: "Найти лучший план",
      action: () => optimize(),
    },
  ];
}
function renderTour() {
  const steps = tourSteps();
  if (state.tour < 0 || state.tour >= steps.length) {
    $("tour").classList.add("hidden");
    state.tour = -1;
    return;
  }
  const step = steps[state.tour];
  $("tour").classList.remove("hidden");
  $("tour").innerHTML =
    `<button class="btn plain close-tour" id="end-tour" aria-label="Завершить демо">${icon("close")}</button><div class="eyebrow">Демо · ${state.tour + 1} / ${steps.length}</div><h3>${esc(step.title)}</h3><p>${esc(step.text)}</p><button class="btn lime" id="next-tour">${esc(step.button)} ${icon("arrow")}</button>`;
  $("end-tour").onclick = () => {
    state.tour = -1;
    renderTour();
  };
  $("next-tour").onclick = async () => {
    const current = state.tour;
    $("next-tour").disabled = true;
    await step.action();
    if (state.tour === current) {
      state.tour++;
      renderTour();
    }
  };
}
document.addEventListener("DOMContentLoaded", () => {
  hydrateIcons();
  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.onclick = () => setTab(button.dataset.tab);
  });
  $("calculate-button").onclick = calculate;
  $("clear-plan").onclick = clearPlan;
  $("show-before").onclick = () => setPhase(false);
  $("show-after").onclick = () => setPhase(true);
  $("events-button").onclick = openEvents;
  $("tour-button").onclick = () => {
    if (!state.live) return;
    if (state.event) return toast("Для демо сначала выключите событие.");
    state.tour = 0;
    closeDrawer();
    renderTour();
  };
  $("overview-map").onclick = () => {
    state.district = null;
    render();
    flyOverview();
  };
  $("zoom-in").onclick = () => map?.zoomIn({ duration: 450 * motion() });
  $("zoom-out").onclick = () => map?.zoomOut({ duration: 450 * motion() });
  $("rotate-map").onclick = () =>
    map?.easeTo({ bearing: map.getBearing() + 35, duration: 1000 * motion() });
  $("toggle-3d").onclick = () => {
    state.threeD = !state.threeD;
    $("toggle-3d").classList.toggle("active", state.threeD);
    $("toggle-3d").setAttribute("aria-pressed", String(state.threeD));
    map?.easeTo({
      pitch: state.threeD ? 52 : 0,
      bearing: state.threeD ? -16 : 0,
      duration: 1100 * motion(),
    });
  };
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeDrawer();
      state.tour = -1;
      if (catalog) renderTour();
    }
  });
  void bootstrap();
});
