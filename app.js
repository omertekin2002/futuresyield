const DATA_URL = "data/market.json";
// The snapshot schema this dashboard renders; see scripts/snapshot_schema.py.
const SNAPSHOT_SCHEMA_VERSION = 4;
const HISTORY_URL = "data/history";
const HISTORY_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISTANBUL_TIMEZONE = "Europe/Istanbul";

const elements = {
  metaDescription: document.querySelector('meta[name="description"]'),
  wordmarkPair: document.querySelector("#wordmark-pair"),
  marketButtons: document.querySelectorAll(".market-options [data-market]"),
  marketCounts: document.querySelectorAll("[data-market-count]"),
  heroMarketLabel: document.querySelector("#hero-market-label"),
  dataState: document.querySelector("#data-state"),
  refreshButton: document.querySelector("#refresh-button"),
  historyControl: document.querySelector("#history-control"),
  historySelect: document.querySelector("#history-select"),
  historyBanner: document.querySelector("#history-banner"),
  historyBannerDate: document.querySelector("#history-banner-date"),
  historyNotice: document.querySelector("#history-notice"),
  daysNote: document.querySelector("#days-note"),
  liveButton: document.querySelector("#live-button"),
  compareControl: document.querySelector("#compare-control"),
  compareSelect: document.querySelector("#compare-select"),
  curveLegend: document.querySelector("#curve-legend"),
  yieldLegend: document.querySelector("#yield-legend"),
  curveKey: document.querySelector("#curve-key"),
  yieldKey: document.querySelector("#yield-key"),
  curveEmpty: document.querySelector("#curve-empty"),
  yieldEmpty: document.querySelector("#yield-empty"),
  spotTag: document.querySelector("#spot-tag"),
  spotValue: document.querySelector("#spot-value"),
  spotChange: document.querySelector("#spot-change"),
  spotLow: document.querySelector("#spot-low"),
  spotHigh: document.querySelector("#spot-high"),
  spotUnit: document.querySelector("#spot-unit"),
  marketDate: document.querySelector("#market-date"),
  curveUnit: document.querySelector("#curve-unit"),
  curveSummary: document.querySelector("#curve-summary"),
  contractCount: document.querySelector("#contract-count"),
  frontContract: document.querySelector("#front-contract"),
  frontPremium: document.querySelector("#front-premium"),
  curveHorizon: document.querySelector("#curve-horizon"),
  yieldSummary: document.querySelector("#yield-summary"),
  yieldCanvas: document.querySelector("#yield-chart"),
  yieldTooltip: document.querySelector("#yield-tooltip"),
  yieldMarker: document.querySelector("#yield-marker"),
  yieldAnnouncer: document.querySelector("#yield-announcer"),
  yieldPeriodLabel: document.querySelector("#yield-period-label"),
  yieldFrontValue: document.querySelector("#yield-front-value"),
  yieldAverageValue: document.querySelector("#yield-average-value"),
  yieldPeriodButtons: document.querySelectorAll("[data-yield-period]"),
  updatedAt: document.querySelector("#updated-at"),
  rows: document.querySelector("#contract-rows"),
  carrySection: document.querySelector("#carry"),
  carrySummary: document.querySelector("#carry-summary"),
  carryRows: document.querySelector("#carry-rows"),
  carryFootnote: document.querySelector("#carry-footnote"),
  carryContractSize: document.querySelector("#carry-contract-size"),
  trendWindow: document.querySelector("#trend-window"),
  trendDaily: document.querySelector("#trend-daily"),
  trendAnnualized: document.querySelector("#trend-annualized"),
  trendStartLabel: document.querySelector("#trend-start-label"),
  trendStartSpot: document.querySelector("#trend-start-spot"),
  trendEndLabel: document.querySelector("#trend-end-label"),
  trendEndSpot: document.querySelector("#trend-end-spot"),
  trendLookback: document.querySelector("#trend-lookback"),
  contractsCaption: document.querySelector("#contracts-caption"),
  canvas: document.querySelector("#curve-chart"),
  tooltip: document.querySelector("#chart-tooltip"),
  marker: document.querySelector("#curve-marker"),
  announcer: document.querySelector("#curve-announcer"),
  footerMarketCopy: document.querySelector("#footer-market-copy"),
  curveTitle: document.querySelector("#curve-title"),
  selectionLine: document.querySelector("#selection-line"),
  selectionLabel: document.querySelector("#selection-label"),
  selectionClear: document.querySelector("#selection-clear"),
  selectionAnnouncer: document.querySelector("#selection-announcer"),
  inspector: document.querySelector("#inspector"),
  inspectorName: document.querySelector("#inspector-name"),
  inspectorDay: document.querySelector("#inspector-day"),
  inspectorNote: document.querySelector("#inspector-note"),
  inspectorContract: document.querySelector("#inspector-contract"),
  inspectorQuote: document.querySelector("#inspector-quote"),
  inspectorSpot: document.querySelector("#inspector-spot"),
  inspectorYield: document.querySelector("#inspector-yield"),
  inspectorCarryGroup: document.querySelector("#inspector-carry-group"),
  inspectorCarry: document.querySelector("#inspector-carry"),
};

const state = {
  snapshot: null,
  data: null,
  marketKey: null,
  chartPoints: [],
  yieldChartPoints: [],
  yieldPeriod: "daily",
  refreshTimer: null,
  resizeTimer: null,
  // Archived end-of-day dates, newest first.
  historyDays: [],
  // The requested view and the one on screen: an archived date, or null for live.
  viewDate: null,
  renderedDate: null,
  // Only the latest load may render, so a slow live poll can't replace a past close.
  requestId: 0,
  // The archived close overlaid on both curves, or null, and its loaded snapshot.
  compareDate: null,
  compareSnapshot: null,
  compareRequestId: 0,
  // The view date the compare options were last built for.
  compareReference: null,
  // The Istanbul day the live snapshot's days left were moved to, or null.
  adjustedDay: null,
  // The maturity selected across the charts and tables, by maturityKey(), or null.
  selectedKey: null,
};

// Quick comparison picks: the archived close nearest each target, if one is
// within the tolerance, in calendar days.
const COMPARE_PICKS = [
  { label: "1 week ago", days: 7, tolerance: 3 },
  { label: "1 month ago", months: 1, tolerance: 7 },
  { label: "3 months ago", months: 3, tolerance: 14 },
];
const COMPARE_COLOR = "#5a1a8c";

const YIELD_PERIODS = {
  daily: {
    label: "Daily",
    shortLabel: "1D",
    days: 1,
    field: "daily_yield_percent",
    digits: 4,
  },
  monthly: {
    label: "Monthly",
    shortLabel: "30D",
    days: 30,
    field: "monthly_yield_percent",
    digits: 2,
  },
  annualized: {
    label: "Annualized",
    shortLabel: "365D",
    days: 365,
    field: "annualized_yield_percent",
    digits: 2,
  },
};

const percentFormat = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "always",
});

const profitFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 0,
  signDisplay: "always",
});

// Gross daily factors, such as 1.00077824.
const factorFormat = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 8,
  maximumFractionDigits: 8,
});

const compactFormat = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const dateFormat = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: ISTANBUL_TIMEZONE,
});

const dateTimeFormat = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: ISTANBUL_TIMEZONE,
});

// A contract's last trade: "27 Oct" and "18:07", in Istanbul time.
const tradeDayFormat = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  timeZone: ISTANBUL_TIMEZONE,
});

const tradeTimeFormat = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: ISTANBUL_TIMEZONE,
});

function hasNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

// Snapshot strings come from upstream market data, so any of them that goes
// into an HTML template is escaped first.
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function signedClass(value) {
  if (!hasNumber(value) || value === 0) return "";
  return value > 0 ? "positive" : "negative";
}

function priceFormatter(signed = false) {
  const digits = state.data?.price_digits ?? 4;
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    ...(signed ? { signDisplay: "always" } : {}),
  });
}

function formatPrice(value) {
  return hasNumber(value) ? priceFormatter().format(value) : "—";
}

function formatSignedPrice(value) {
  return hasNumber(value) ? priceFormatter(true).format(value) : "—";
}

function formatPercent(value) {
  return hasNumber(value) ? `${percentFormat.format(value)}%` : "—";
}

// The gross daily factor a contract's yields compound: the snapshot's own, or
// the updater's rule applied to its price, spot and days left.
function yieldFactor(contract, spot = state.data?.spot?.last) {
  return hasNumber(contract.daily_yield_factor)
    ? contract.daily_yield_factor
    : dailyYieldFactor(contract.last, spot, contract.days_to_maturity);
}

function yieldValue(contract, period = state.yieldPeriod, spot = state.data?.spot?.last) {
  const settings = YIELD_PERIODS[period];
  if (hasNumber(contract[settings.field])) return contract[settings.field];
  return compoundedYieldPercent(yieldFactor(contract, spot), settings.days);
}

function formatYield(value, period = state.yieldPeriod) {
  if (!hasNumber(value)) return "—";
  const digits = YIELD_PERIODS[period].digits;
  return `${new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    signDisplay: "always",
  }).format(value)}%`;
}

// A change in yield, in percentage points.
function formatYieldDelta(value, period = state.yieldPeriod) {
  if (!hasNumber(value)) return "—";
  const digits = YIELD_PERIODS[period].digits;
  return `${new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    signDisplay: "always",
  }).format(value)} pp`;
}

function formatProfit(value) {
  return hasNumber(value) ? profitFormat.format(value) : "—";
}

function formatFactor(value) {
  return hasNumber(value) ? factorFormat.format(value) : "—";
}

// A contract's bid and ask as the board shows them.
function formatBidAsk(contract) {
  return hasNumber(contract.bid) || hasNumber(contract.ask)
    ? `${formatPrice(contract.bid)} / ${formatPrice(contract.ask)}`
    : "—";
}

function parseMarketDate(value) {
  return new Date(`${value}T12:00:00+03:00`);
}

function formatDay(value) {
  return dateFormat.format(parseMarketDate(value));
}

function dayOffset(value, { days = 0, months = 0 }) {
  const [year, month, day] = value.split("-").map(Number);
  // Clamp to the target month's last day, so 31 Mar minus a month is 28/29 Feb.
  const lastDay = new Date(Date.UTC(year, month - 1 - months + 1, 0)).getUTCDate();
  const date = new Date(Date.UTC(year, month - 1 - months, Math.min(day, lastDay) - days));
  return date.toISOString().slice(0, 10);
}

function daysBetween(first, second) {
  return Math.abs(Date.parse(`${first}T00:00:00Z`) - Date.parse(`${second}T00:00:00Z`)) / 86_400_000;
}

// Calendar days from one ISO date to a later one; negative if it is earlier.
function daysUntil(from, to) {
  return Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000);
}

const istanbulDayFormat = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  timeZone: ISTANBUL_TIMEZONE,
});

// The Istanbul date of a moment, today by default, as YYYY-MM-DD, whatever
// the browser's own zone.
function istanbulToday(now = new Date()) {
  const parts = Object.fromEntries(
    istanbulDayFormat.formatToParts(now).map(({ type, value }) => [type, value]),
  );
  return `${parts.year}-${parts.month}-${parts.day}`;
}

const istanbulClockFormat = new Intl.DateTimeFormat("en-US", {
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "numeric",
  minute: "numeric",
  second: "numeric",
  hourCycle: "h23",
  timeZone: ISTANBUL_TIMEZONE,
});

// The Istanbul wall-clock time of date, as milliseconds on a UTC timeline, so
// Istanbul times compare and step by days whatever the browser's own zone.
function istanbulWallClock(date) {
  const parts = Object.fromEntries(
    istanbulClockFormat.formatToParts(date).map(({ type, value }) => [type, Number(value)]),
  );
  return Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
}

// Each contract's last trade, mapped from the contract, against the newest one
// in its market and snapshot: the time alone on that Istanbul day, the date
// and time on an earlier day, which makes it stale, and "—" without a time,
// which is never stale. Older snapshots filled a missing time in with their
// own generated_at, so that exact time counts as none.
function lastTrades(contracts, generatedAt) {
  const standIn = Date.parse(generatedAt);
  const times = contracts.map((contract) => {
    const time = typeof contract.updated_at === "string" ? Date.parse(contract.updated_at) : NaN;
    return Number.isNaN(time) || time === standIn ? null : new Date(time);
  });
  const days = times.map((time) => time && istanbulToday(time));
  const newestDay = days.reduce(
    (newest, day) => day && (!newest || day > newest) ? day : newest,
    null,
  );
  return new Map(contracts.map((contract, index) => {
    const time = times[index];
    if (!time) return [contract, { text: "—", stale: false }];
    const clock = tradeTimeFormat.format(time);
    return [contract, days[index] === newestDay
      ? { text: clock, stale: false }
      : { text: `${tradeDayFormat.format(time)} ${clock}`, stale: true }];
  }));
}

const STALE_DESCRIPTION = ", last traded before the market’s latest quote day";

// The tooltip line for a contract of the day on screen.
function lastTradeLine(contract) {
  const trade = lastTrades(state.data.contracts, state.snapshot.generated_at).get(contract);
  return { text: `Last trade ${trade.text}${trade.stale ? " · stale" : ""}` };
}

// Snapshots refresh hourly at :20 on weekdays, 09:20 to 23:20 Istanbul time.
const FIRST_REFRESH_MINUTE = 9 * 60 + 20;
const LAST_REFRESH_MINUTE = 23 * 60 + 20;
const FRESH_MINUTES = 90;
const MINUTE_MS = 60_000;
const DAY_MS = 24 * 60 * MINUTE_MS;

function isWeekday(wallClock) {
  const day = new Date(wallClock).getUTCDay();
  return day >= 1 && day <= 5;
}

// The refresh state of a live snapshot generated at generatedAt, at now:
// "fresh", "delayed", or "closed" when refreshes have stopped for the night or
// weekend and the snapshot is no older than the last scheduled refresh allows.
function refreshState(generatedAt, now = new Date()) {
  if ((now - generatedAt) / MINUTE_MS <= FRESH_MINUTES) return "fresh";
  const nowClock = istanbulWallClock(now);
  const midnight = nowClock - (nowClock % DAY_MS);
  const minuteOfDay = (nowClock - midnight) / MINUTE_MS;
  const inWindow = isWeekday(nowClock)
    && minuteOfDay >= FIRST_REFRESH_MINUTE
    && minuteOfDay <= LAST_REFRESH_MINUTE;
  if (inWindow) return "delayed";

  // Outside the window, measure from 23:20 on the most recent weekday.
  let lastDay = minuteOfDay > LAST_REFRESH_MINUTE ? midnight : midnight - DAY_MS;
  while (!isWeekday(lastDay)) lastDay -= DAY_MS;
  const lastRefresh = lastDay + LAST_REFRESH_MINUTE * MINUTE_MS;
  return istanbulWallClock(generatedAt) >= lastRefresh - FRESH_MINUTES * MINUTE_MS
    ? "closed"
    : "delayed";
}

// The updater's yield and carry rules, from scripts/update_data.py.
function dailyYieldFactor(last, spot, days) {
  if (!hasNumber(last) || !hasNumber(spot) || last <= 0 || spot <= 0 || days <= 0) return null;
  return Math.pow(last / spot, 1 / days);
}

function compoundedYieldPercent(factor, periodDays) {
  return hasNumber(factor) && factor > 0 ? (Math.pow(factor, periodDays) - 1) * 100 : null;
}

function projectedSpot(spot, dailyFactor, days) {
  return hasNumber(spot) && hasNumber(dailyFactor) && days >= 0
    ? spot * Math.pow(dailyFactor, days)
    : null;
}

// The live snapshot's days left, and everything built on them, are fixed for
// its market date. Once Istanbul has moved on to a later day, return a copy
// with those terms recomputed for today and expired contracts dropped, as the
// updater would. Prices, premiums and the spot trend stay as published.
function rollSnapshotForward(snapshot, today) {
  if (!(snapshot.market_date < today)) return snapshot;
  const rolled = structuredClone(snapshot);
  Object.values(rolled.markets).forEach((market) => {
    const spot = market.spot?.last;
    const trendFactor = market.spot_trend?.daily_factor;
    market.contracts = market.contracts
      .map((contract) => {
        const days = daysUntil(today, contract.maturity_date);
        const factor = dailyYieldFactor(contract.last, spot, days);
        const expectedSpot = hasNumber(market.contract_size)
          ? projectedSpot(spot, trendFactor, days)
          : null;
        return {
          ...contract,
          days_to_maturity: days,
          annualized_premium_percent:
            hasNumber(contract.premium_percent) && days > 0
              ? contract.premium_percent * 365 / days
              : null,
          daily_yield_factor: factor,
          daily_yield_percent: compoundedYieldPercent(factor, 1),
          monthly_yield_percent: compoundedYieldPercent(factor, 30),
          annualized_yield_percent: compoundedYieldPercent(factor, 365),
          expected_spot_at_maturity: expectedSpot,
          expected_short_profit_try:
            hasNumber(contract.bid) && hasNumber(expectedSpot)
              ? (contract.bid - expectedSpot) * market.contract_size
              : null,
        };
      })
      .filter((contract) => contract.days_to_maturity >= 0);
  });
  return rolled;
}

function setStatus(className, text) {
  elements.dataState.classList.remove("is-fresh", "is-delayed", "is-closed", "is-error", "is-historical");
  elements.dataState.classList.add(className);
  elements.dataState.lastChild.textContent = ` ${text}`;
}

function updateStatus(generatedAt) {
  if (state.renderedDate) {
    setStatus("is-historical", `Historical · close of ${formatDay(state.renderedDate)}`);
    return;
  }
  const generated = new Date(generatedAt);
  const status = refreshState(generated);
  if (status === "fresh") {
    setStatus("is-fresh", "Latest snapshot");
  } else if (status === "closed") {
    setStatus("is-closed", `Market closed · snapshot ${dateTimeFormat.format(generated)} TRT`);
  } else {
    const minutesOld = Math.floor((Date.now() - generated.getTime()) / MINUTE_MS);
    setStatus("is-delayed", `Update delayed · ${minutesOld}m old`);
  }
}

function renderMarketRail(snapshot) {
  elements.marketCounts.forEach((count) => {
    const market = snapshot.markets[count.dataset.marketCount];
    const total = market?.contracts?.length ?? 0;
    count.textContent = `${total} ${total === 1 ? "maturity" : "maturities"}`;
  });

  elements.marketButtons.forEach((button) => {
    button.disabled = !snapshot.markets[button.dataset.market];
  });
}

function renderMarketIdentity(market) {
  const spacedPair = market.pair.replace("/", " · ");
  const closeOf = state.renderedDate ? ` · close of ${formatDay(state.renderedDate)}` : "";

  document.body.dataset.market = market.key;
  document.title = `Vade — ${market.pair} Futures Curve${closeOf}`;
  elements.metaDescription.content =
    `The latest ${market.pair} spot reference and VİOP futures curve, ` +
    "refreshed hourly during VİOP trading hours on weekdays.";
  elements.wordmarkPair.textContent = spacedPair;
  elements.heroMarketLabel.textContent = `${market.pair},`;
  elements.spotUnit.textContent = market.spot_unit;
  elements.curveUnit.textContent = market.curve_unit;
  elements.contractsCaption.textContent =
    `${state.renderedDate ? "Closing" : "Latest"} VİOP ${market.pair} futures prices${closeOf}, ` +
    "maturity dates, days remaining, and compounded implied yields";
  elements.footerMarketCopy.textContent =
    `Built as a clear window into the ${market.pair} term structure.`;

  elements.marketButtons.forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.market === market.key));
  });
}

function renderSpot(spot) {
  elements.spotValue.textContent = formatPrice(spot.last);
  elements.spotValue.classList.remove("loading-value");
  elements.spotLow.textContent = formatPrice(spot.low);
  elements.spotHigh.textContent = formatPrice(spot.high);

  elements.spotChange.className = `change-value ${signedClass(spot.change_percent)}`;
  const absolute = formatSignedPrice(spot.change);
  elements.spotChange.textContent = `${absolute} · ${formatPercent(spot.change_percent)}`;
}

function renderStats(data) {
  const available = data.contracts.filter((contract) => hasNumber(contract.last));
  const front = available[0];
  const final = available.at(-1);

  elements.contractCount.textContent = String(data.contracts.length).padStart(2, "0");
  elements.frontContract.textContent = front ? front.label : "—";
  elements.frontPremium.textContent = front ? formatPercent(front.premium_percent) : "—";
  elements.frontPremium.className = signedClass(front?.premium_percent);
  elements.curveHorizon.textContent = final ? `${final.days_to_maturity} days` : "—";

  if (front && final) {
    const direction = final.last >= front.last ? "rises" : "falls";
    elements.curveSummary.textContent =
      `${data.contracts.length} listed maturities. The curve ${direction} from ` +
      `${formatPrice(front.last)} to ${formatPrice(final.last)} ${data.spot_unit}.`;
  } else {
    elements.curveSummary.textContent =
      `${data.contracts.length} listed maturities. None has a price to plot.`;
  }
}

function renderRows(contracts) {
  if (!contracts.length) {
    elements.rows.innerHTML = '<tr class="empty-row"><td colspan="11">No active contracts found.</td></tr>';
    return;
  }

  // Redrawing replaces every row, so note a focused Select button to refocus.
  const focusedKey = elements.rows.contains(document.activeElement)
    ? document.activeElement.closest("tr[data-key]")?.dataset.key
    : undefined;
  const trades = lastTrades(contracts, state.snapshot.generated_at);
  elements.rows.innerHTML = contracts.map((contract, index) => {
    const changeClass = signedClass(contract.change_percent);
    const premiumClass = signedClass(contract.premium_percent);
    const dailyYield = yieldValue(contract, "daily");
    const monthlyYield = yieldValue(contract, "monthly");
    const annualizedYield = yieldValue(contract, "annualized");
    const maturity = dateFormat.format(parseMarketDate(contract.maturity_date));
    const volume = hasNumber(contract.volume) ? compactFormat.format(contract.volume) : "—";
    const spread = formatBidAsk(contract);
    const nearClass = contract.days_to_maturity <= 31 ? "is-near" : "";
    const unavailable = contract.status !== "available" ? " (unavailable)" : "";
    const trade = trades.get(contract);
    const staleTag = trade.stale
      ? ` <span class="stale-tag">stale<span class="sr-only">${STALE_DESCRIPTION}</span></span>`
      : "";

    const key = maturityKey(contract);
    const selected = key === state.selectedKey;

    return `
      <tr data-key="${escapeHtml(key)}"${selected ? ' class="is-selected"' : ""}>
        <td data-label="Contract">
          <span class="contract-cell">
            <span class="contract-name">
              <strong>${escapeHtml(contract.label)}</strong>
              <small>${escapeHtml(contract.code)}${index === 0 ? " · FRONT" : ""}${unavailable}</small>
            </span>
            <button
              type="button"
              class="select-button"
              aria-pressed="${selected}"
              aria-label="Select ${escapeHtml(contract.label)} · ${escapeHtml(contract.code)}"
            >Select</button>
          </span>
        </td>
        <td class="numeric" data-label="Last">
          <span class="last-cell">
            <span class="last-price">${formatPrice(contract.last)}</span>
            <small class="last-trade"><span class="sr-only">Last trade </span>${trade.text}${staleTag}</small>
          </span>
        </td>
        <td class="numeric ${changeClass}" data-label="Change">
          <span>${formatPercent(contract.change_percent)}</span>
        </td>
        <td class="numeric" data-label="Bid / Ask"><span class="cell-subtle">${spread}</span></td>
        <td data-label="Maturity">${maturity}</td>
        <td class="numeric" data-label="Days left">
          <span class="days-badge ${nearClass}">${escapeHtml(contract.days_to_maturity)}</span>
        </td>
        <td class="numeric ${premiumClass}" data-label="vs. spot">${formatPercent(contract.premium_percent)}</td>
        <td class="numeric yield-table-value ${signedClass(dailyYield)}" data-label="Daily yield · 1D">
          ${formatYield(dailyYield, "daily")}
        </td>
        <td class="numeric yield-table-value ${signedClass(monthlyYield)}" data-label="Monthly yield · 30D">
          ${formatYield(monthlyYield, "monthly")}
        </td>
        <td class="numeric yield-table-value ${signedClass(annualizedYield)}" data-label="Annualized · 365D">
          ${formatYield(annualizedYield, "annualized")}
        </td>
        <td class="numeric" data-label="Volume">${volume}</td>
      </tr>`;
  }).join("");

  if (focusedKey === undefined) return;
  [...elements.rows.querySelectorAll("tr[data-key]")]
    .find((row) => row.dataset.key === focusedKey)
    ?.querySelector(".select-button")
    ?.focus({ preventScroll: true });
}

function renderCarry(data) {
  elements.carrySection.hidden = !hasNumber(data.contract_size);
  if (elements.carrySection.hidden) return;

  const trend = data.spot_trend;
  const lookback = trend?.lookback_days ?? 180;
  const contractSize = data.contract_size.toLocaleString("en-US");
  elements.trendLookback.textContent = lookback;
  elements.carryContractSize.textContent = contractSize;
  elements.carryFootnote.textContent =
    "A projection, not a forecast. It assumes spot keeps rising at its average " +
    `daily pace, a short filled at the current bid, and ${contractSize} ` +
    `${data.base_asset} per contract. Fees, taxes, and margin are ignored. ` +
    `The window starts at the last daily close on or before ${lookback} days ago.`;

  if (trend) {
    const start = dateFormat.format(parseMarketDate(trend.start_date));
    const end = dateFormat.format(parseMarketDate(trend.end_date));
    elements.trendWindow.textContent = `${start} → ${end} · ${trend.days} days`;
    elements.trendStartLabel.textContent = `Spot · ${start}`;
    elements.trendEndLabel.textContent = `Spot · ${end}`;
  } else {
    elements.trendWindow.textContent = "Unavailable";
    elements.trendStartLabel.textContent = "Spot at start";
    elements.trendEndLabel.textContent = "Spot now";
  }
  elements.trendDaily.textContent = formatYield(trend?.daily_percent, "daily");
  elements.trendDaily.className = signedClass(trend?.daily_percent);
  elements.trendAnnualized.textContent = formatYield(
    trend?.annualized_percent,
    "annualized",
  );
  elements.trendAnnualized.className = signedClass(trend?.annualized_percent);
  elements.trendStartSpot.textContent = formatPrice(trend?.start_spot);
  elements.trendEndSpot.textContent = formatPrice(trend?.end_spot);

  const priced = data.contracts.filter((contract) =>
    hasNumber(contract.expected_short_profit_try)
  );
  const profitable = priced.filter(
    (contract) => contract.expected_short_profit_try > 0,
  ).length;
  const noBid = data.contracts.filter((contract) => !hasNumber(contract.bid)).length;
  const noBidNote = noBid
    ? ` ${noBid} ${noBid === 1 ? "contract has" : "contracts have"} no bid in this snapshot.`
    : "";
  elements.carrySummary.textContent = trend
    ? `At the ${trend.days}-day average pace of ` +
      `${formatYield(trend.daily_percent, "daily")} a day, ${profitable} of ` +
      `${priced.length} contracts with a bid show an expected profit when shorted.` +
      noBidNote
    : `The ${lookback}-day spot history was unavailable for this snapshot, ` +
      "so no projection is shown.";

  elements.carryRows.innerHTML = data.contracts.map((contract) => {
    const profit = contract.expected_short_profit_try;
    const nearClass = contract.days_to_maturity <= 31 ? "is-near" : "";
    const bidNote = hasNumber(contract.bid) ? "" : " · NO BID";
    const key = maturityKey(contract);

    return `
      <tr data-key="${escapeHtml(key)}"${key === state.selectedKey ? ' class="is-selected"' : ""}>
        <td data-label="Contract">
          <span class="contract-name">
            <strong>${escapeHtml(contract.label)}</strong>
            <small>${escapeHtml(contract.code)}${bidNote}</small>
          </span>
        </td>
        <td class="numeric" data-label="Bid"><span class="last-price">${formatPrice(contract.bid)}</span></td>
        <td class="numeric" data-label="Days left">
          <span class="days-badge ${nearClass}">${escapeHtml(contract.days_to_maturity)}</span>
        </td>
        <td class="numeric" data-label="Exp. spot at maturity">
          ${formatPrice(contract.expected_spot_at_maturity)}
        </td>
        <td class="numeric ${signedClass(profit)}" data-label="Exp. profit · TRY">
          <span class="carry-profit">${formatProfit(profit)}</span>
        </td>
      </tr>`;
  }).join("");
}

// The comparison close's data for the selected market, once it has loaded.
function comparisonMarket() {
  return state.compareSnapshot?.markets?.[state.marketKey] ?? null;
}

// Contracts are matched by maturity month, so each keeps one slot on the x
// axis whichever of the two days it is listed on.
function maturityKey(contract) {
  return contract.maturity_date?.slice(0, 7) ?? contract.code;
}

// Merge the current and comparison series into one slot per maturity month.
// A contract listed on only one of the two days fills only that side.
function alignSeries(current, compare) {
  const slots = new Map();
  [["current", current], ["compare", compare]].forEach(([side, points]) => {
    points.forEach((point) => {
      const key = maturityKey(point.contract);
      if (!slots.has(key)) slots.set(key, { key, current: null, compare: null });
      slots.get(key)[side] = point;
    });
  });
  const aligned = [...slots.values()].map((slot) => ({
    ...slot,
    label: (slot.current ?? slot.compare).contract.label,
  }));
  // Without a comparison the snapshot's own order stands.
  return compare.length
    ? aligned.sort((first, second) => first.key.localeCompare(second.key))
    : aligned;
}

// The listed contract of the selected maturity, or null.
function selectedContract() {
  if (!state.selectedKey || !state.data) return null;
  return state.data.contracts.find((contract) => maturityKey(contract) === state.selectedKey) ?? null;
}

// Change the selection and announce it once in the live region. Doesn't
// redraw; the caller renders next.
function updateSelection(key) {
  if (key === state.selectedKey) return;
  state.selectedKey = key;
  const contract = selectedContract();
  elements.selectionAnnouncer.textContent = contract
    ? `Selected ${contract.label}, ${contract.code}.`
    : "Selection cleared.";
}

// Mark the selection outside the charts: the line naming it, and its rows in
// the contract board and the carry table. Snapshot strings go in as text.
function renderSelection() {
  const contract = selectedContract();
  elements.selectionLine.hidden = !contract;
  elements.selectionLabel.textContent = contract ? `${contract.label} · ${contract.code}` : "";
  [elements.rows, elements.carryRows].forEach((body) => {
    body.querySelectorAll("tr[data-key]").forEach((row) => {
      const selected = row.dataset.key === state.selectedKey;
      row.classList.toggle("is-selected", selected);
      row.querySelector(".select-button")?.setAttribute("aria-pressed", String(selected));
    });
  });
  renderInspector(contract);
}

// One inspector row: a term, its value, and an optional line under the value
// with the formula filled in or the reason the value is missing. Snapshot
// strings go in as text.
function inspectorRow({ key, term, value, detail, className = "", tag = "", stale = false }) {
  const row = document.createElement("div");
  row.dataset.item = key;
  const termNode = document.createElement("dt");
  termNode.textContent = term;
  if (tag) {
    const tagNode = document.createElement("span");
    tagNode.className = "inspector-tag";
    tagNode.textContent = tag;
    termNode.append(" ", tagNode);
  }
  const valueNode = document.createElement("span");
  valueNode.className = `inspector-value ${className}`.trim();
  valueNode.textContent = value;
  if (stale) {
    const staleTag = document.createElement("span");
    staleTag.className = "stale-tag";
    staleTag.textContent = "stale";
    const note = document.createElement("span");
    note.className = "sr-only";
    note.textContent = STALE_DESCRIPTION;
    staleTag.append(note);
    valueNode.append(" ", staleTag);
  }
  const description = document.createElement("dd");
  description.append(valueNode);
  if (detail) {
    const detailNode = document.createElement("small");
    detailNode.className = "inspector-detail";
    detailNode.textContent = detail;
    description.append(detailNode);
  }
  row.append(termNode, description);
  return row;
}

function fillInspectorList(list, rows) {
  list.replaceChildren(...rows.map(inspectorRow));
}

// Why a contract has no daily factor, and so no yield.
function noYieldReason(contract, spot) {
  if (!hasNumber(contract.last)) return "No yield: the contract has no last price.";
  if (!hasNumber(spot) || spot <= 0) return "No yield: the spot reference is missing.";
  if (contract.days_to_maturity <= 0) {
    return "No yield: the contract matures today, with 0 days left.";
  }
  return "No yield: the price and spot give no daily factor.";
}

// The selected maturity's inputs and the README's formulas with them filled
// in, for the day on screen. Every value is the one the board, the carry
// table and the chart tooltips show; nothing missing is filled in.
function renderInspector(contract) {
  elements.inspector.hidden = !contract;
  if (!contract) return;
  const data = state.data;
  const spot = data.spot.last;
  const days = contract.days_to_maturity;
  const price = formatPrice(contract.last);
  const trade = lastTrades(data.contracts, state.snapshot.generated_at).get(contract);
  const countedFrom = state.adjustedDay ?? state.snapshot.market_date;

  elements.inspectorName.textContent = contract.label;
  elements.inspectorDay.textContent =
    `${state.renderedDate ? "Close" : "Live"} · ${formatDay(referenceDate())}`;
  elements.inspectorNote.hidden = !state.adjustedDay;
  elements.inspectorNote.textContent = daysNoteText(state.snapshot);

  fillInspectorList(elements.inspectorContract, [
    { key: "label", term: "Label", value: contract.label },
    { key: "symbol", term: "VİOP symbol", value: contract.symbol },
    { key: "code", term: "Table code", value: contract.code },
    { key: "maturity", term: "Maturity", value: formatDay(contract.maturity_date) },
    {
      key: "days",
      term: "Days left",
      value: String(days),
      detail: `Calendar days from ${formatDay(countedFrom)}${state.adjustedDay ? ", adjusted" : ""}`,
    },
  ]);

  fillInspectorList(elements.inspectorQuote, [
    {
      key: "last",
      term: "Last price",
      value: price,
      detail: hasNumber(contract.last) ? data.spot_unit : "No last price in this snapshot.",
    },
    { key: "bid-ask", term: "Bid / Ask", value: formatBidAsk(contract) },
    { key: "last-trade", term: "Last trade", value: trade.text, stale: trade.stale },
  ]);

  const spotUpdated = typeof data.spot.updated_at === "string" ? Date.parse(data.spot.updated_at) : NaN;
  const spotTime = Number.isNaN(spotUpdated) ? "—" : `${dateTimeFormat.format(spotUpdated)} TRT`;
  const synthetic = /^synthetic/i.test(data.spot.source ?? "");
  fillInspectorList(elements.inspectorSpot, [
    { key: "spot", term: "Spot", value: formatPrice(spot), detail: data.spot_unit },
    { key: "spot-time", term: "Spot time", value: spotTime },
    {
      key: "spot-source",
      term: "Reference",
      value: synthetic
        ? `Synthetic ${data.base_asset}/USD × USD/${data.quote_asset} gram reference`
        : data.spot.source ?? "—",
    },
  ]);

  const factor = yieldFactor(contract, spot);
  const canDivide = hasNumber(contract.last) && hasNumber(spot);
  fillInspectorList(elements.inspectorYield, [
    {
      key: "premium",
      term: "Premium",
      value: formatPercent(contract.premium_percent),
      className: signedClass(contract.premium_percent),
      detail: canDivide && hasNumber(contract.premium_percent)
        ? `(${price} / ${formatPrice(spot)} − 1) × 100`
        : hasNumber(contract.last)
          ? "No premium in this snapshot."
          : "No premium: the contract has no last price.",
    },
    {
      key: "factor",
      term: "Daily factor",
      value: formatFactor(factor),
      detail: hasNumber(factor)
        ? `(${price} / ${formatPrice(spot)})^(1 / ${days})`
        : noYieldReason(contract, spot),
    },
    ...Object.entries(YIELD_PERIODS).map(([period, settings]) => {
      const value = yieldValue(contract, period, spot);
      return {
        key: `yield-${period}`,
        term: `${settings.label} yield · ${settings.shortLabel}`,
        tag: period === state.yieldPeriod ? "In chart" : "",
        value: formatYield(value, period),
        className: signedClass(value),
        detail: hasNumber(factor)
          ? `(${formatFactor(factor)}^${settings.days} − 1) × 100`
          : hasNumber(value) ? "" : "No yield without a daily factor.",
      };
    }),
  ]);

  elements.inspectorCarryGroup.hidden = !hasNumber(data.contract_size);
  if (elements.inspectorCarryGroup.hidden) {
    elements.inspectorCarry.replaceChildren();
    return;
  }
  const trend = data.spot_trend;
  const trendFactor = trend?.daily_factor;
  const expectedSpot = contract.expected_spot_at_maturity;
  const profit = contract.expected_short_profit_try;
  const contractSize = data.contract_size.toLocaleString("en-US");
  const noProjection =
    `No projection: the ${trend?.lookback_days ?? 180}-day spot history was unavailable for this snapshot.`;
  fillInspectorList(elements.inspectorCarry, [
    {
      key: "trend-window",
      term: "Trend window",
      value: trend
        ? `${formatDay(trend.start_date)} → ${formatDay(trend.end_date)}`
        : "Unavailable",
      detail: trend ? `${trend.days} days` : noProjection,
    },
    {
      key: "trend-factor",
      term: "Daily appreciation",
      value: formatFactor(trendFactor),
      detail: trend && hasNumber(trend.start_spot) && hasNumber(trend.end_spot)
        ? `(${formatPrice(trend.end_spot)} / ${formatPrice(trend.start_spot)})^(1 / ${trend.days})`
        : "",
    },
    {
      key: "expected-spot",
      term: "Expected spot at maturity",
      value: formatPrice(expectedSpot),
      detail: hasNumber(expectedSpot) && hasNumber(trendFactor)
        ? `${formatPrice(spot)} × ${formatFactor(trendFactor)}^${days}`
        : noProjection,
    },
    {
      key: "profit",
      term: "Expected profit",
      value: hasNumber(profit) ? `${formatProfit(profit)} TRY` : "—",
      className: signedClass(profit),
      detail: hasNumber(profit)
        ? `(${formatPrice(contract.bid)} − ${formatPrice(expectedSpot)}) × ${contractSize}`
        : !hasNumber(contract.bid)
          ? "No profit: the contract has no bid in this snapshot."
          : "No profit: there is no expected spot to compare the bid with.",
    },
  ]);
}

function setSelection(key) {
  if (key === state.selectedKey) return;
  updateSelection(key);
  renderSelection();
  renderCharts();
}

// Selecting the selected maturity again clears it. Only a maturity listed on
// the day on screen can be selected.
function toggleSelection(key) {
  if (key === state.selectedKey) {
    setSelection(null);
  } else if (state.data?.contracts.some((contract) => maturityKey(contract) === key)) {
    setSelection(key);
  }
}

// Blank a chart that has nothing to plot, so the previous view's curve doesn't
// linger, and say why inside its frame. The canvas label carries the same
// words for screen readers, so the visible message is hidden from them.
function clearChart(canvas, label, { empty, key, message }) {
  const context = canvas.getContext("2d");
  // The last render left the context scaled by the pixel ratio.
  context.setTransform(1, 0, 0, 1, 0, 0);
  context.clearRect(0, 0, canvas.width, canvas.height);
  canvas.setAttribute("aria-label", label);
  empty.textContent = message;
  empty.hidden = false;
  key.hidden = true;
}

// The plotted contracts of the day on screen that are stale, by the board's
// lastTrades() rule. Comparison points are never in it.
function staleContracts(contracts) {
  const trades = lastTrades(state.data.contracts, state.snapshot.generated_at);
  return new Set(contracts.filter((contract) => trades.get(contract)?.stale));
}

function staleCountText(staleCount, total) {
  return `${staleCount} of ${total} ${total === 1 ? "maturity" : "maturities"} ` +
    "last traded before the latest quote day.";
}

// The key under a chart, shown only when the day on screen plots a stale point.
function renderChartKey(key, staleCount, total) {
  key.hidden = !staleCount;
  if (!staleCount) return;
  const swatch = document.createElement("span");
  swatch.className = "stale-swatch";
  swatch.setAttribute("aria-hidden", "true");
  const name = document.createElement("strong");
  name.textContent = "Stale:";
  key.replaceChildren(swatch, name, ` ${staleCountText(staleCount, total)}`);
}

// A round point marker. A stale one is hollow with a dotted outline and at
// least 5.5px across, so it differs in more than colour; the line still
// passes through it. Points without a trade time are drawn normally.
function drawPointMarker(context, { x, y }, { radius, filled, lineWidth = 2, stale = false }) {
  context.save();
  context.beginPath();
  context.arc(x, y, stale ? Math.max(radius, 5.5) : radius, 0, Math.PI * 2);
  context.fillStyle = filled && !stale ? "#000000" : "#ffffff";
  context.fill();
  context.strokeStyle = "#000000";
  context.lineWidth = lineWidth;
  if (stale) context.setLineDash([2.5, 2.5]);
  context.stroke();
  context.restore();
}

function tracePath(context, points) {
  context.beginPath();
  points.forEach(({ x, y }, index) => {
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
}

// The comparison is a dashed line with hollow square markers, so it differs
// from the current curve's solid line and round markers in more than colour.
function drawComparison(context, points) {
  if (!points.length) return;
  context.save();
  context.strokeStyle = COMPARE_COLOR;
  context.lineWidth = 2;
  context.setLineDash([7, 4]);
  tracePath(context, points);
  context.stroke();
  context.setLineDash([]);
  points.forEach(({ x, y }) => {
    context.fillStyle = "#ffffff";
    context.fillRect(x - 4, y - 4, 8, 8);
    context.strokeRect(x - 4, y - 4, 8, 8);
  });
  context.restore();
}

// The selected maturity: a solid guide behind the curve and an outlined
// diamond over its point, so it differs from the keyboard ring, the round
// markers and the yield peak's dashed guide and circle in more than colour.
function drawSelectionGuide(context, point, top, bottom) {
  if (!point) return;
  context.save();
  context.strokeStyle = "#000000";
  context.lineWidth = 2;
  context.beginPath();
  context.moveTo(point.x, top);
  context.lineTo(point.x, bottom);
  context.stroke();
  context.restore();
}

function drawSelectionMarker(context, point) {
  if (!point) return;
  const size = 13;
  context.save();
  context.beginPath();
  context.moveTo(point.x, point.y - size);
  context.lineTo(point.x + size, point.y);
  context.lineTo(point.x, point.y + size);
  context.lineTo(point.x - size, point.y);
  context.closePath();
  context.strokeStyle = "#ffffff";
  context.lineWidth = 6;
  context.stroke();
  context.strokeStyle = "#000000";
  context.lineWidth = 2.5;
  context.stroke();
  context.restore();
}

function drawAxisLabels(context, slots, xAt, width, height) {
  const labelEvery = width < 620 ? Math.ceil(slots.length / 4) : Math.ceil(slots.length / 8);
  const lastX = xAt(slots.length - 1);
  context.font = '11px Helvetica, Arial, sans-serif';
  slots.forEach((slot, index) => {
    const x = xAt(index);
    if (index === slots.length - 1 || (index % labelEvery === 0 && lastX - x > 58)) {
      context.fillStyle = "#000000";
      context.textAlign = index === slots.length - 1 ? "right" : index === 0 ? "left" : "center";
      context.fillText(slot.label.replace(" 20", " ’"), x, height - 20);
    }
  });
}

function comparisonNote() {
  return comparisonMarket()
    ? ` Dashed line with square markers: close of ${formatDay(state.compareDate)}.`
    : "";
}

function renderChart() {
  if (!state.data) return;
  const pricePoints = (contracts) => contracts
    .filter((contract) => hasNumber(contract.last))
    .map((contract) => ({ contract, value: contract.last }));
  const current = pricePoints(state.data.contracts);
  const compareMarket = comparisonMarket();
  const slots = alignSeries(current, compareMarket ? pricePoints(compareMarket.contracts) : []);
  const canvas = elements.canvas;
  if (current.length < 1) {
    state.chartPoints = [];
    clearChart(canvas, `${state.data.pair} futures price curve: no priced maturities.`, {
      empty: elements.curveEmpty,
      key: elements.curveKey,
      message: state.data.contracts.length
        ? "No priced maturities to plot."
        : "No listed maturities to plot.",
    });
    return;
  }
  elements.curveEmpty.hidden = true;
  const stale = staleContracts(current.map((point) => point.contract));
  renderChartKey(elements.curveKey, stale.size, current.length);
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width || !bounds.height) return;

  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(bounds.width * ratio);
  canvas.height = Math.round(bounds.height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);

  const width = bounds.width;
  const height = bounds.height;
  const margin = { top: 22, right: 20, bottom: 48, left: 48 };
  const chartWidth = width - margin.left - margin.right;
  const chartHeight = height - margin.top - margin.bottom;
  const values = slots.flatMap((slot) => [slot.current?.value, slot.compare?.value]).filter(hasNumber);
  const rawMin = Math.min(...values, state.data.spot.last);
  const rawMax = Math.max(...values, state.data.spot.last);
  const padding = Math.max((rawMax - rawMin) * 0.12, 0.1);
  const min = rawMin - padding;
  const max = rawMax + padding;

  const xAt = (index) => margin.left + (slots.length === 1 ? chartWidth / 2 : index * chartWidth / (slots.length - 1));
  const yAt = (value) => margin.top + (max - value) / (max - min) * chartHeight;
  const seriesPoints = (side) => slots.flatMap((slot, index) =>
    slot[side] ? [{ ...slot[side], key: slot.key, x: xAt(index), y: yAt(slot[side].value) }] : []
  );
  const currentPoints = seriesPoints("current");
  const selectedPoint = currentPoints.find((point) => point.key === state.selectedKey);

  context.font = '11px Helvetica, Arial, sans-serif';
  context.textBaseline = "middle";
  context.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const value = max - ((max - min) * i / 4);
    const y = margin.top + chartHeight * i / 4;
    context.strokeStyle = "#a5b8c0";
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(width - margin.right, y);
    context.stroke();
    context.fillStyle = "#000000";
    context.textAlign = "right";
    context.fillText(
      value.toFixed(Math.min(state.data.price_digits, 2)),
      margin.left - 10,
      y,
    );
  }

  tracePath(context, currentPoints);
  context.lineTo(currentPoints.at(-1).x, height - margin.bottom);
  context.lineTo(currentPoints[0].x, height - margin.bottom);
  context.closePath();
  context.fillStyle = "#c0d4a7";
  context.fill();

  drawSelectionGuide(context, selectedPoint, margin.top, height - margin.bottom);

  const spotY = yAt(state.data.spot.last);
  context.save();
  context.setLineDash([6, 5]);
  context.strokeStyle = "#000000";
  context.beginPath();
  context.moveTo(margin.left, spotY);
  context.lineTo(width - margin.right, spotY);
  context.stroke();
  context.restore();
  context.fillStyle = "#000000";
  context.textAlign = "left";
  context.fillText("SPOT", margin.left + 7, spotY - 10);

  drawComparison(context, seriesPoints("compare"));

  tracePath(context, currentPoints);
  context.strokeStyle = "#000000";
  context.lineWidth = 2;
  context.stroke();

  currentPoints.forEach((point, index) => {
    drawPointMarker(context, point, {
      radius: index === 0 ? 5 : 3.5,
      filled: index === 0,
      stale: stale.has(point.contract),
    });
  });
  drawSelectionMarker(context, selectedPoint);

  drawAxisLabels(context, slots, xAt, width, height);
  state.chartPoints = slots.map((slot, index) => ({
    ...slot,
    x: xAt(index),
    y: yAt((slot.current ?? slot.compare).value),
    isSelected: slot.key === selectedPoint?.key,
    stale: Boolean(slot.current && stale.has(slot.current.contract)),
  }));
  const staleNote = stale.size ? ` ${staleCountText(stale.size, current.length)}` : "";
  canvas.setAttribute(
    "aria-label",
    `${state.data.pair} futures price curve across ${current.length} maturities, ` +
    `from ${formatPrice(current[0].value)} to ` +
    `${formatPrice(current.at(-1).value)} ${state.data.spot_unit}.${staleNote}${comparisonNote()}`,
  );
}

function renderYieldChart() {
  if (!state.data) return;
  const settings = YIELD_PERIODS[state.yieldPeriod];
  const yieldPoints = (market) => market.contracts
    .map((contract) => ({ contract, value: yieldValue(contract, state.yieldPeriod, market.spot?.last) }))
    .filter((point) => hasNumber(point.value));
  const contracts = yieldPoints(state.data);
  const compareMarket = comparisonMarket();
  const slots = alignSeries(contracts, compareMarket ? yieldPoints(compareMarket) : []);
  const canvas = elements.yieldCanvas;
  if (contracts.length < 1) {
    state.yieldChartPoints = [];
    const spot = state.data.spot?.last;
    clearChart(canvas, `${state.data.pair} futures net yield: no maturities with a yield.`, {
      empty: elements.yieldEmpty,
      key: elements.yieldKey,
      message: !state.data.contracts.length
        ? "No listed maturities to plot."
        : state.data.contracts.some((contract) => hasNumber(contract.last)) &&
          (!hasNumber(spot) || spot <= 0)
          ? "No maturities with a yield to plot: the spot reference is missing."
          : "No maturities with a yield to plot: every listed contract matures today " +
            "or has no price.",
    });
    elements.yieldPeriodLabel.textContent = `${settings.label} · ${settings.shortLabel}`;
    [elements.yieldFrontValue, elements.yieldAverageValue].forEach((element) => {
      element.textContent = "—";
      element.className = "";
    });
    elements.yieldSummary.textContent = "No maturity has a yield to plot.";
    return;
  }
  elements.yieldEmpty.hidden = true;
  const stale = staleContracts(contracts.map((point) => point.contract));
  renderChartKey(elements.yieldKey, stale.size, contracts.length);
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width || !bounds.height) {
    state.yieldChartPoints = [];
    return;
  }

  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(bounds.width * ratio);
  canvas.height = Math.round(bounds.height * ratio);
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);

  const width = bounds.width;
  const height = bounds.height;
  const margin = { top: 24, right: 20, bottom: 48, left: 62 };
  const chartWidth = width - margin.left - margin.right;
  const chartHeight = height - margin.top - margin.bottom;
  const values = contracts.map((point) => point.value);
  const plotted = slots.flatMap((slot) => [slot.current?.value, slot.compare?.value]).filter(hasNumber);
  const rawMin = Math.min(0, ...plotted);
  const rawMax = Math.max(0, ...plotted);
  const range = rawMax - rawMin;
  const padding = Math.max(range * 0.12, state.yieldPeriod === "daily" ? 0.001 : 0.05);
  const min = rawMin - padding;
  const max = rawMax + padding;

  const xAt = (index) => margin.left + (
    slots.length === 1
      ? chartWidth / 2
      : index * chartWidth / (slots.length - 1)
  );
  const yAt = (value) => margin.top + (max - value) / (max - min) * chartHeight;
  const seriesPoints = (side) => slots.flatMap((slot, index) =>
    slot[side] ? [{ ...slot[side], key: slot.key, x: xAt(index), y: yAt(slot[side].value) }] : []
  );
  const currentPoints = seriesPoints("current");
  const selectedPoint = currentPoints.find((point) => point.key === state.selectedKey);

  context.font = '11px Helvetica, Arial, sans-serif';
  context.textBaseline = "middle";
  context.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const value = max - ((max - min) * i / 4);
    const y = margin.top + chartHeight * i / 4;
    context.strokeStyle = "#a5b8c0";
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(width - margin.right, y);
    context.stroke();
    context.fillStyle = "#000000";
    context.textAlign = "right";
    context.fillText(
      `${value.toFixed(state.yieldPeriod === "daily" ? 3 : 1)}%`,
      margin.left - 10,
      y,
    );
  }

  const zeroY = yAt(0);
  context.strokeStyle = "#000000";
  context.lineWidth = 1.5;
  context.beginPath();
  context.moveTo(margin.left, zeroY);
  context.lineTo(width - margin.right, zeroY);
  context.stroke();

  tracePath(context, currentPoints);
  context.lineTo(currentPoints.at(-1).x, zeroY);
  context.lineTo(currentPoints[0].x, zeroY);
  context.closePath();
  context.fillStyle = "#a5b8c0";
  context.fill();

  drawSelectionGuide(context, selectedPoint, margin.top, height - margin.bottom);
  drawComparison(context, seriesPoints("compare"));

  tracePath(context, currentPoints);
  context.strokeStyle = "#000000";
  context.lineWidth = 2.5;
  context.stroke();

  const peak = currentPoints.reduce(
    (highest, point) => point.value > highest.value ? point : highest,
  );

  context.save();
  context.setLineDash([4, 5]);
  context.strokeStyle = "#000000";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(peak.x, margin.top);
  context.lineTo(peak.x, height - margin.bottom);
  context.stroke();
  context.restore();

  currentPoints.forEach((point, index) => {
    const isPeak = point === peak;
    drawPointMarker(context, point, {
      radius: isPeak ? 7 : index === 0 ? 5 : 3.5,
      filled: isPeak || index === 0,
      lineWidth: isPeak ? 3 : 2,
      stale: stale.has(point.contract),
    });

    if (isPeak) {
      context.beginPath();
      context.arc(point.x, point.y, 11, 0, Math.PI * 2);
      context.strokeStyle = "#000000";
      context.lineWidth = 2;
      context.stroke();
    }
  });
  drawSelectionMarker(context, selectedPoint);

  drawAxisLabels(context, slots, xAt, width, height);
  state.yieldChartPoints = slots.map((slot, index) => ({
    ...slot,
    x: xAt(index),
    y: yAt((slot.current ?? slot.compare).value),
    isPeak: slot.key === peak.key,
    isSelected: slot.key === selectedPoint?.key,
    stale: Boolean(slot.current && stale.has(slot.current.contract)),
  }));

  const peakStale = stale.has(peak.contract);
  const peakTag = `HIGHEST ${settings.label.toUpperCase()} · ${formatYield(peak.value)}` +
    (peakStale ? " · STALE" : "");
  context.font = '700 11px Helvetica, Arial, sans-serif';
  const peakTagWidth = context.measureText(peakTag).width + 18;
  const peakTagX = Math.min(
    Math.max(peak.x - peakTagWidth / 2, margin.left),
    width - margin.right - peakTagWidth,
  );
  const peakTagY = peak.y < margin.top + 42 ? peak.y + 17 : peak.y - 34;
  context.fillStyle = "#000000";
  context.fillRect(peakTagX, peakTagY, peakTagWidth, 22);
  context.fillStyle = "#ffffff";
  context.textAlign = "center";
  context.fillText(peakTag, peakTagX + peakTagWidth / 2, peakTagY + 11);

  const front = contracts[0];
  const average = values.reduce((sum, value) => sum + value, 0) / values.length;
  elements.yieldPeriodLabel.textContent = `${settings.label} · ${settings.shortLabel}`;
  elements.yieldFrontValue.textContent = formatYield(front.value);
  elements.yieldFrontValue.className = signedClass(front.value);
  elements.yieldAverageValue.textContent = formatYield(average);
  elements.yieldAverageValue.className = signedClass(average);
  const peakText =
    `Highest ${settings.label.toLowerCase()} yield is ${formatYield(peak.value)} at ` +
    `${peak.contract.label}` +
    (peakStale ? ", which is stale: it last traded before the latest quote day." : ".");
  elements.yieldSummary.textContent =
    `${peakText} ${settings.label} net yield ranges from ` +
    `${formatYield(Math.min(...values))} to ${formatYield(Math.max(...values))}.`;
  const staleNote = stale.size ? ` ${staleCountText(stale.size, contracts.length)}` : "";
  canvas.setAttribute(
    "aria-label",
    `${settings.label} compounded ${state.data.pair} futures net yield by maturity. ` +
    `${peakText}${staleNote}${comparisonNote()}`,
  );
}

function selectMarket(marketKey, updateUrl = false) {
  const market = state.snapshot?.markets?.[marketKey];
  if (!market) return;

  // A selection is kept across redraws of the same market while its maturity
  // is still listed.
  const switched = marketKey !== state.marketKey;
  state.marketKey = marketKey;
  state.data = market;
  if (switched || !selectedContract()) updateSelection(null);
  state.chartPoints = [];
  state.yieldChartPoints = [];
  elements.tooltip.hidden = true;
  elements.yieldTooltip.hidden = true;

  renderMarketIdentity(market);
  renderSpot(market.spot);
  renderStats(market);
  renderRows(market.contracts);
  renderCarry(market);
  renderSelection();
  renderComparisonLegends();
  renderCharts();

  if (updateUrl) {
    const url = new URL(window.location.href);
    url.searchParams.set("market", marketKey);
    window.history.replaceState({}, "", url);
  }
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  renderMarketRail(snapshot);
  renderCompareControl();

  const queryMarket = new URLSearchParams(window.location.search)
    .get("market")
    ?.toUpperCase();
  const selectedMarket = snapshot.markets[state.marketKey]
    ? state.marketKey
    : snapshot.markets[queryMarket]
      ? queryMarket
      : snapshot.default_market;
  selectMarket(selectedMarket);

  elements.marketDate.textContent = dateFormat.format(
    parseMarketDate(snapshot.market_date),
  );
  elements.updatedAt.textContent =
    `${dateTimeFormat.format(new Date(snapshot.generated_at))} TRT`;
  updateStatus(snapshot.generated_at);
  renderDaysNote(snapshot);
}

// Days left that no longer match the snapshot's own date are never silent.
function daysNoteText(snapshot) {
  return state.adjustedDay
    ? `Days left and yields adjusted to ${formatDay(state.adjustedDay)}; ` +
      `prices are from the ${formatDay(snapshot.market_date)} snapshot.`
    : "";
}

function renderDaysNote(snapshot) {
  elements.daysNote.hidden = !state.adjustedDay;
  elements.daysNote.textContent = daysNoteText(snapshot);
}

async function fetchSnapshot(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`Market snapshot returned ${response.status}`);
  const data = await response.json();
  // A light mirror of the updater's schema check, so a bad live or archived
  // file shows the load error instead of a broken render.
  if (data?.schema_version !== SNAPSHOT_SCHEMA_VERSION) {
    throw new Error(`Unsupported snapshot schema version ${data?.schema_version}`);
  }
  const marketsAreValid =
    data.markets &&
    Array.isArray(data.market_order) &&
    data.market_order.every((key) =>
      data.markets[key]?.spot &&
      Array.isArray(data.markets[key]?.contracts) &&
      data.markets[key].contracts.every((contract) =>
        typeof contract?.maturity_date === "string" &&
        Number.isInteger(contract.days_to_maturity)
      )
    );
  if (!marketsAreValid) throw new Error("Invalid multi-market snapshot");
  return data;
}

function showNotice(text) {
  elements.historyNotice.textContent = text;
  elements.historyNotice.hidden = false;
}

function hideNotice() {
  elements.historyNotice.hidden = true;
  elements.historyNotice.textContent = "";
}

// The day the comparison is measured from: the close on screen, or the live market date.
function referenceDate() {
  return state.renderedDate ?? state.snapshot?.market_date ?? null;
}

// Archived closes that can be compared with the view: only earlier ones, newest first.
function compareDays(reference = referenceDate()) {
  return reference ? state.historyDays.filter((day) => day < reference) : [];
}

function quickPicks(reference, days) {
  const picks = [];
  COMPARE_PICKS.forEach((pick) => {
    const target = dayOffset(reference, pick);
    const nearest = days.reduce((best, day) =>
      best === null || daysBetween(day, target) < daysBetween(best, target) ? day : best,
    null);
    if (
      nearest &&
      daysBetween(nearest, target) <= pick.tolerance &&
      !picks.some((existing) => existing.day === nearest)
    ) {
      picks.push({ label: pick.label, day: nearest });
    }
  });
  return picks;
}

function updateCompareUrl() {
  const url = new URL(window.location.href);
  if (state.compareDate) url.searchParams.set("compare", state.compareDate);
  else url.searchParams.delete("compare");
  if (url.href !== window.location.href) window.history.replaceState({}, "", url);
}

function clearCompare() {
  state.compareDate = null;
  state.compareSnapshot = null;
  state.compareRequestId += 1;
  elements.compareSelect.value = "";
  updateCompareUrl();
}

// Rebuild the compare options for the view's date and drop a comparison that
// is no longer earlier than it. Doesn't redraw; the caller renders next.
function renderCompareControl() {
  const reference = referenceDate();
  const days = compareDays(reference);
  elements.compareControl.hidden = !days.length;

  if (reference !== state.compareReference) {
    state.compareReference = reference;
    const options = [new Option("None", "")];
    const picks = reference ? quickPicks(reference, days) : [];
    if (picks.length) {
      const group = document.createElement("optgroup");
      group.label = "Quick picks";
      group.append(...picks.map(({ label, day }) => new Option(`${label} · ${formatDay(day)}`, day)));
      options.push(group);
    }
    if (days.length) {
      const group = document.createElement("optgroup");
      group.label = "Archived closes";
      group.append(...days.map((day) => new Option(`Close · ${formatDay(day)}`, day)));
      options.push(group);
    }
    elements.compareSelect.replaceChildren(...options);
  }

  if (state.compareDate && reference && !days.includes(state.compareDate)) {
    showNotice(
      `The close of ${formatDay(state.compareDate)} is not earlier than ` +
      `${formatDay(reference)}, so the comparison is off.`,
    );
    clearCompare();
  }
  elements.compareSelect.value = state.compareDate ?? "";
}

function renderComparisonLegends() {
  const shown = Boolean(state.compareSnapshot) && Boolean(state.data);
  [elements.curveLegend, elements.yieldLegend].forEach((legend) => {
    legend.hidden = !shown;
  });
  if (!shown) return;

  const current = `${state.renderedDate ? "Close" : "Live"} · ${formatDay(referenceDate())}`;
  const missing = comparisonMarket() ? "" : ` · no ${escapeHtml(state.data.pair)} data`;
  const html =
    `<li><span class="legend-swatch is-current" aria-hidden="true"></span>` +
    `${current} <small>solid</small></li>` +
    `<li><span class="legend-swatch is-compare" aria-hidden="true"></span>` +
    `Close · ${formatDay(state.compareDate)}${missing} <small>dashed</small></li>`;
  elements.curveLegend.innerHTML = html;
  elements.yieldLegend.innerHTML = html;
}

function renderComparison() {
  elements.tooltip.hidden = true;
  elements.yieldTooltip.hidden = true;
  renderComparisonLegends();
  renderCharts();
}

// Overlay an archived close on both curves, or remove the overlay with null.
// A comparison that fails to load is dropped with a notice.
async function setCompare(date) {
  clearCompare();
  state.compareDate = date;
  elements.compareSelect.value = date ?? "";
  updateCompareUrl();
  renderComparison();
  if (!date) return;

  const request = state.compareRequestId;
  try {
    const data = await fetchSnapshot(`${HISTORY_URL}/${date}.json`);
    if (request !== state.compareRequestId) return;
    state.compareSnapshot = data;
  } catch (error) {
    if (request !== state.compareRequestId) return;
    console.error(error);
    showNotice(`The close of ${formatDay(date)} could not be loaded for comparison.`);
    clearCompare();
  }
  renderComparison();
}

function renderHistoryControl() {
  elements.historyControl.hidden = !state.historyDays.length;
  elements.historySelect.replaceChildren(
    new Option("Live", ""),
    ...state.historyDays.map((day) => new Option(`Close · ${formatDay(day)}`, day)),
  );
  elements.historySelect.value = state.viewDate ?? "";
}

function renderViewState() {
  const historical = Boolean(state.renderedDate);
  elements.historySelect.value = state.viewDate ?? "";
  elements.historyBanner.hidden = !historical;
  elements.historyBannerDate.textContent = historical ? formatDay(state.renderedDate) : "—";
  elements.spotTag.textContent = historical ? "Past close" : "≈ 15 min delay";
  document.body.dataset.view = historical ? "historical" : "live";

  const url = new URL(window.location.href);
  if (historical) url.searchParams.set("date", state.renderedDate);
  else url.searchParams.delete("date");
  if (url.href !== window.location.href) window.history.replaceState({}, "", url);
}

async function loadHistoryIndex() {
  try {
    const response = await fetch(`${HISTORY_URL}/index.json?v=${Date.now()}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`History index returned ${response.status}`);
    const manifest = await response.json();
    const days = (Array.isArray(manifest?.days) ? manifest.days : [])
      .map((entry) => entry?.date)
      .filter((day) => typeof day === "string" && HISTORY_DATE.test(day));
    state.historyDays = [...new Set(days)].sort().reverse();
  } catch (error) {
    // History is optional; the live dashboard works without it.
    console.warn("Archived snapshots are unavailable.", error);
    state.historyDays = [];
  }
  renderHistoryControl();
}

// Load and render one view: an archived date, or null for the live snapshot.
// A view that fails to load leaves the current one on screen.
async function loadView(date) {
  state.viewDate = date;
  const request = ++state.requestId;
  elements.refreshButton.classList.add("is-loading");
  elements.refreshButton.disabled = true;
  try {
    const data = date
      ? await fetchSnapshot(`${HISTORY_URL}/${date}.json`)
      : await fetchSnapshot(`${DATA_URL}?v=${Date.now()}`, { cache: "no-store" });
    if (request !== state.requestId) return;
    // Only the live view moves on with the calendar; a past close is its own day.
    const today = istanbulToday();
    const shown = date ? data : rollSnapshotForward(data, today);
    state.renderedDate = date;
    state.adjustedDay = shown === data ? null : today;
    renderSnapshot(shown);
  } catch (error) {
    if (request !== state.requestId) return;
    console.error(error);
    state.viewDate = state.renderedDate;
    if (date) {
      showNotice(`The close of ${formatDay(date)} could not be loaded.`);
      if (!state.snapshot) {
        loadView(null);
        return;
      }
    } else if (state.renderedDate) {
      showNotice(
        "Live data is unavailable. Still showing the close of " +
        `${formatDay(state.renderedDate)}.`,
      );
    } else {
      setStatus("is-error", "Data unavailable");
      if (!state.snapshot) {
        elements.rows.innerHTML = '<tr class="empty-row"><td colspan="11">The market snapshot could not be loaded. Try refreshing shortly.</td></tr>';
      }
    }
  } finally {
    if (request === state.requestId) {
      elements.refreshButton.classList.remove("is-loading");
      elements.refreshButton.disabled = false;
      renderViewState();
    }
  }
}

function goLive() {
  hideNotice();
  loadView(null);
}

// Refresh always shows live data, so it also leaves a past close.
elements.refreshButton.addEventListener("click", goLive);
elements.liveButton.addEventListener("click", goLive);

elements.historySelect.addEventListener("change", () => {
  const date = elements.historySelect.value;
  hideNotice();
  loadView(state.historyDays.includes(date) ? date : null);
});

elements.compareSelect.addEventListener("change", () => {
  const date = elements.compareSelect.value;
  hideNotice();
  setCompare(compareDays().includes(date) ? date : null);
});

elements.marketButtons.forEach((button) => {
  button.addEventListener("click", () => {
    selectMarket(button.dataset.market, true);
  });
});

elements.rows.addEventListener("click", (event) => {
  const button = event.target.closest(".select-button");
  if (button) toggleSelection(button.closest("tr").dataset.key);
});

// The line hides with the selection, so focus moves on to the charts' heading.
elements.selectionClear.addEventListener("click", () => {
  setSelection(null);
  elements.curveTitle.focus();
});

window.addEventListener("resize", () => {
  window.clearTimeout(state.resizeTimer);
  state.resizeTimer = window.setTimeout(renderCharts, 120);
});

elements.yieldPeriodButtons.forEach((button) => {
  button.addEventListener("click", () => {
    state.yieldPeriod = button.dataset.yieldPeriod;
    elements.yieldPeriodButtons.forEach((candidate) => {
      candidate.setAttribute(
        "aria-pressed",
        String(candidate === button),
      );
    });
    elements.yieldTooltip.hidden = true;
    renderYieldChart();
    syncKeyboardPoint(charts.yield);
    renderInspector(selectedContract());
  });
});

// Both days' values for one maturity month, and the change from the
// comparison close to the view. A side without the contract reads "not listed".
function comparisonLines(point, format, formatDelta) {
  const value = (side) => point[side] ? format(point[side].value) : "not listed";
  const delta = point.current && point.compare
    ? `Δ ${formatDelta(point.current.value - point.compare.value)}`
    : "Δ —";
  // Days left and the last trade describe only the day on screen.
  return [
    { text: `${formatDay(referenceDate())}: ${value("current")}` },
    { text: `${formatDay(state.compareDate)}: ${value("compare")}` },
    { text: delta, delta: true },
    ...(point.current
      ? [
        { text: `${point.current.contract.days_to_maturity} days left` },
        lastTradeLine(point.current.contract),
      ]
      : [{ text: "Not listed on the viewed day" }]),
  ];
}

// One point's tooltip as plain text: its maturity label and the lines below
// it. Pointer and keyboard both show it, and the live region reads it out.
function priceTooltip(point) {
  const unit = state.data.spot_unit;
  return {
    label: point.label,
    lines: comparisonMarket()
      ? comparisonLines(point, formatPrice, (delta) => `${formatSignedPrice(delta)} ${unit}`)
      : [
        { text: `${formatPrice(point.current.value)} · ${unit}` },
        { text: `${point.current.contract.days_to_maturity} days left` },
        lastTradeLine(point.current.contract),
      ],
  };
}

function yieldTooltip(point) {
  const settings = YIELD_PERIODS[state.yieldPeriod];
  const lines = comparisonMarket()
    ? comparisonLines(
      point,
      (value) => `${formatYield(value)} ${settings.shortLabel}`,
      (delta) => formatYieldDelta(delta),
    )
    : [
      { text: `${formatYield(point.current.value)} ${settings.shortLabel}` },
      { text: `${point.current.contract.days_to_maturity} days left` },
      lastTradeLine(point.current.contract),
    ];
  if (point.isPeak) lines.push({ text: `Highest ${settings.label.toLowerCase()} yield` });
  return { label: point.label, lines };
}

// Snapshot strings go in as text nodes, never as HTML.
function fillTooltip(tooltip, { label, lines }) {
  const title = document.createElement("strong");
  title.textContent = label;
  const nodes = [title];
  lines.forEach(({ text, delta }, index) => {
    if (index) nodes.push(document.createElement("br"));
    if (delta) {
      const span = document.createElement("span");
      span.className = "tooltip-delta";
      span.textContent = text;
      nodes.push(span);
    } else {
      nodes.push(document.createTextNode(text));
    }
  });
  tooltip.replaceChildren(...nodes);
}

function placeTooltip(tooltip, point, bounds, minX) {
  tooltip.classList.toggle("is-compare", Boolean(comparisonMarket()));
  tooltip.hidden = false;
  const tooltipX = Math.min(Math.max(point.x + 12, minX), bounds.width - tooltip.offsetWidth - 1);
  const tooltipY = Math.max(point.y - tooltip.offsetHeight - 12, 8);
  tooltip.style.left = `${tooltipX}px`;
  tooltip.style.top = `${tooltipY}px`;
}

// Each chart's tooltip and keyboard state. The active point is the one the
// arrow keys move from, kept by maturity across redraws; keyboard is true
// while a keyboard-opened tooltip and its marker show.
const charts = {
  price: {
    canvas: elements.canvas,
    tooltip: elements.tooltip,
    marker: elements.marker,
    announcer: elements.announcer,
    minX: 45,
    points: () => state.chartPoints,
    content: priceTooltip,
    activeIndex: null,
    activeKey: null,
    keyboard: false,
  },
  yield: {
    canvas: elements.yieldCanvas,
    tooltip: elements.yieldTooltip,
    marker: elements.yieldMarker,
    announcer: elements.yieldAnnouncer,
    minX: 60,
    points: () => state.yieldChartPoints,
    content: yieldTooltip,
    activeIndex: null,
    activeKey: null,
    keyboard: false,
  },
};

function setActivePoint(chart, index) {
  chart.activeIndex = index;
  chart.activeKey = chart.points()[index].key;
}

function endKeyboardPoint(chart) {
  chart.keyboard = false;
  chart.marker.hidden = true;
  chart.announcer.textContent = "";
}

function hideKeyboardPoint(chart) {
  endKeyboardPoint(chart);
  chart.tooltip.hidden = true;
}

// Show the point's tooltip and marker, and put its text in the live region.
// The region only changes when the text does, so a redraw with the same
// values isn't read out again.
function showKeyboardPoint(chart, index) {
  setActivePoint(chart, index);
  chart.keyboard = true;
  const point = chart.points()[index];
  const content = chart.content(point);
  fillTooltip(chart.tooltip, content);
  placeTooltip(chart.tooltip, point, chart.canvas.getBoundingClientRect(), chart.minX);
  chart.marker.style.left = `${chart.canvas.offsetLeft + point.x}px`;
  chart.marker.style.top = `${chart.canvas.offsetTop + point.y}px`;
  chart.marker.hidden = false;
  const text = `${[content.label, ...content.lines.map((line) => line.text)].join(". ")}.`;
  if (chart.announcer.textContent !== text) chart.announcer.textContent = text;
}

// After a redraw, keep the active maturity if it's still plotted, otherwise
// clamp to the new points; a keyboard tooltip follows with fresh values.
function syncKeyboardPoint(chart) {
  const points = chart.points();
  if (!points.length) {
    chart.activeIndex = null;
    chart.activeKey = null;
    if (chart.keyboard) hideKeyboardPoint(chart);
    return;
  }
  if (chart.activeIndex !== null) {
    const same = points.findIndex((point) => point.key === chart.activeKey);
    setActivePoint(chart, same >= 0 ? same : Math.min(chart.activeIndex, points.length - 1));
  }
  if (chart.keyboard) showKeyboardPoint(chart, chart.activeIndex);
}

function renderCharts() {
  renderChart();
  renderYieldChart();
  syncKeyboardPoint(charts.price);
  syncKeyboardPoint(charts.yield);
}

const KEY_STEPS = { ArrowLeft: -1, ArrowRight: 1 };

function handleChartKey(chart, event) {
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  const points = chart.points();
  if (!points.length) return;
  const last = points.length - 1;
  let index;
  if (event.key === "Escape") {
    if (!chart.keyboard) return;
    event.preventDefault();
    hideKeyboardPoint(chart);
    return;
  } else if (event.key === "Home") {
    index = 0;
  } else if (event.key === "End") {
    index = last;
  } else if (event.key in KEY_STEPS) {
    index = chart.activeIndex === null
      ? 0
      : Math.min(Math.max(chart.activeIndex + KEY_STEPS[event.key], 0), last);
  } else if (event.key === "Enter" || event.key === " ") {
    // Select the active point, after opening it if no point is active yet.
    // A held key repeats; only its first press toggles the selection.
    event.preventDefault();
    if (event.repeat) return;
    showKeyboardPoint(chart, chart.activeIndex ?? 0);
    selectPoint(points[chart.activeIndex]);
    return;
  } else {
    return;
  }
  event.preventDefault();
  showKeyboardPoint(chart, index);
}

// A point plotted from the day on screen selects its maturity; one listed
// only on the comparison day can't be selected.
function selectPoint(point) {
  if (point?.current) toggleSelection(point.key);
}

// The index of the point nearest the pointer across, or null when none is
// within 35px of it.
function nearestPointIndex(chart, event) {
  const points = chart.points();
  if (!points.length) return null;
  const x = event.clientX - chart.canvas.getBoundingClientRect().left;
  const index = points.reduce((best, point, candidate) =>
    Math.abs(point.x - x) < Math.abs(points[best].x - x) ? candidate : best, 0);
  return Math.abs(points[index].x - x) > 35 ? null : index;
}

Object.values(charts).forEach((chart) => {
  chart.canvas.addEventListener("pointermove", (event) => {
    const points = chart.points();
    if (!points.length) return;
    const index = nearestPointIndex(chart, event);
    // The pointer takes over from the keyboard; arrow keys go on from its point.
    if (chart.keyboard) endKeyboardPoint(chart);

    if (index === null) {
      chart.tooltip.hidden = true;
      return;
    }

    const nearest = points[index];
    setActivePoint(chart, index);
    fillTooltip(chart.tooltip, chart.content(nearest));
    placeTooltip(chart.tooltip, nearest, chart.canvas.getBoundingClientRect(), chart.minX);
  });

  // A click or tap on a point selects its maturity, or clears it if selected.
  chart.canvas.addEventListener("click", (event) => {
    const index = nearestPointIndex(chart, event);
    if (index !== null) selectPoint(chart.points()[index]);
  });

  chart.canvas.addEventListener("pointerleave", () => {
    hideKeyboardPoint(chart);
  });

  chart.canvas.addEventListener("keydown", (event) => handleChartKey(chart, event));

  // A click focuses the canvas too, but only keyboard focus opens a tooltip.
  chart.canvas.addEventListener("focus", () => {
    if (!chart.canvas.matches(":focus-visible") || !chart.points().length) return;
    showKeyboardPoint(chart, chart.activeIndex ?? 0);
  });

  chart.canvas.addEventListener("blur", () => {
    hideKeyboardPoint(chart);
  });
});

async function init() {
  await loadHistoryIndex();
  const params = new URLSearchParams(window.location.search);
  const requestedCompare = params.get("compare");
  if (requestedCompare && state.historyDays.includes(requestedCompare)) {
    // Checked against the view's date once that has rendered.
    setCompare(requestedCompare);
  } else if (requestedCompare) {
    showNotice(
      HISTORY_DATE.test(requestedCompare)
        ? `No archived close for ${requestedCompare} to compare with.`
        : "That comparison date is not in the archive.",
    );
    updateCompareUrl();
  }
  const requested = params.get("date");
  if (requested && state.historyDays.includes(requested)) {
    loadView(requested);
  } else {
    if (requested) {
      showNotice(
        HISTORY_DATE.test(requested)
          ? `No archived close for ${requested}. Showing live data.`
          : "That date is not in the archive. Showing live data.",
      );
    }
    loadView(null);
  }
  // The poll only refreshes the live view; a past close stays until the user leaves it.
  state.refreshTimer = window.setInterval(() => {
    if (!state.viewDate) loadView(null);
  }, 60_000);
}

init();
