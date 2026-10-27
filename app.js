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
  liveButton: document.querySelector("#live-button"),
  compareControl: document.querySelector("#compare-control"),
  compareSelect: document.querySelector("#compare-select"),
  curveLegend: document.querySelector("#curve-legend"),
  yieldLegend: document.querySelector("#yield-legend"),
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
  footerMarketCopy: document.querySelector("#footer-market-copy"),
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

function yieldValue(contract, period = state.yieldPeriod, spot = state.data?.spot?.last) {
  const settings = YIELD_PERIODS[period];
  if (hasNumber(contract[settings.field])) return contract[settings.field];

  let factor = contract.daily_yield_factor;
  if (
    !hasNumber(factor) &&
    hasNumber(contract.last) &&
    hasNumber(spot) &&
    contract.last > 0 &&
    spot > 0 &&
    contract.days_to_maturity > 0
  ) {
    factor = Math.pow(contract.last / spot, 1 / contract.days_to_maturity);
  }

  return hasNumber(factor)
    ? (Math.pow(factor, settings.days) - 1) * 100
    : null;
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

function setStatus(className, text) {
  elements.dataState.classList.remove("is-fresh", "is-delayed", "is-error", "is-historical");
  elements.dataState.classList.add(className);
  elements.dataState.lastChild.textContent = ` ${text}`;
}

function updateStatus(generatedAt) {
  if (state.renderedDate) {
    setStatus("is-historical", `Historical · close of ${formatDay(state.renderedDate)}`);
    return;
  }
  const minutesOld = Math.max(0, (Date.now() - new Date(generatedAt).getTime()) / 60000);
  if (minutesOld <= 45) {
    setStatus("is-fresh", "Latest snapshot");
  } else {
    setStatus("is-delayed", `Update delayed · ${Math.floor(minutesOld)}m old`);
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
    "refreshed every 15 minutes.";
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
  }
}

function renderRows(contracts) {
  if (!contracts.length) {
    elements.rows.innerHTML = '<tr class="empty-row"><td colspan="11">No active contracts found.</td></tr>';
    return;
  }

  elements.rows.innerHTML = contracts.map((contract, index) => {
    const changeClass = signedClass(contract.change_percent);
    const premiumClass = signedClass(contract.premium_percent);
    const dailyYield = yieldValue(contract, "daily");
    const monthlyYield = yieldValue(contract, "monthly");
    const annualizedYield = yieldValue(contract, "annualized");
    const maturity = dateFormat.format(parseMarketDate(contract.maturity_date));
    const volume = hasNumber(contract.volume) ? compactFormat.format(contract.volume) : "—";
    const spread = hasNumber(contract.bid) || hasNumber(contract.ask)
      ? `${formatPrice(contract.bid)} / ${formatPrice(contract.ask)}`
      : "—";
    const nearClass = contract.days_to_maturity <= 31 ? "is-near" : "";
    const unavailable = contract.status !== "available" ? " (unavailable)" : "";

    return `
      <tr>
        <td data-label="Contract">
          <span class="contract-name">
            <strong>${escapeHtml(contract.label)}</strong>
            <small>${escapeHtml(contract.code)}${index === 0 ? " · FRONT" : ""}${unavailable}</small>
          </span>
        </td>
        <td class="numeric" data-label="Last"><span class="last-price">${formatPrice(contract.last)}</span></td>
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

    return `
      <tr>
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
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width || !bounds.height || current.length < 1) return;

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
    slot[side] ? [{ x: xAt(index), y: yAt(slot[side].value) }] : []
  );
  const currentPoints = seriesPoints("current");

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

  currentPoints.forEach(({ x, y }, index) => {
    context.beginPath();
    context.arc(x, y, index === 0 ? 5 : 3.5, 0, Math.PI * 2);
    context.fillStyle = index === 0 ? "#000000" : "#ffffff";
    context.fill();
    context.strokeStyle = "#000000";
    context.lineWidth = 2;
    context.stroke();
  });

  drawAxisLabels(context, slots, xAt, width, height);
  state.chartPoints = slots.map((slot, index) => ({
    ...slot,
    x: xAt(index),
    y: yAt((slot.current ?? slot.compare).value),
  }));
  canvas.setAttribute(
    "aria-label",
    `${state.data.pair} futures price curve across ${current.length} maturities, ` +
    `from ${formatPrice(current[0].value)} to ` +
    `${formatPrice(current.at(-1).value)} ${state.data.spot_unit}.${comparisonNote()}`,
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
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width || !bounds.height || contracts.length < 1) {
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
    context.beginPath();
    context.arc(point.x, point.y, isPeak ? 7 : index === 0 ? 5 : 3.5, 0, Math.PI * 2);
    context.fillStyle = isPeak || index === 0 ? "#000000" : "#ffffff";
    context.fill();
    context.strokeStyle = "#000000";
    context.lineWidth = isPeak ? 3 : 2;
    context.stroke();

    if (isPeak) {
      context.beginPath();
      context.arc(point.x, point.y, 11, 0, Math.PI * 2);
      context.strokeStyle = "#000000";
      context.lineWidth = 2;
      context.stroke();
    }
  });

  drawAxisLabels(context, slots, xAt, width, height);
  state.yieldChartPoints = slots.map((slot, index) => ({
    ...slot,
    x: xAt(index),
    y: yAt((slot.current ?? slot.compare).value),
    isPeak: slot.key === peak.key,
  }));

  const peakTag = `HIGHEST ${settings.label.toUpperCase()} · ${formatYield(peak.value)}`;
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
  elements.yieldSummary.textContent =
    `Highest ${settings.label.toLowerCase()} yield is ${formatYield(peak.value)} at ` +
    `${peak.contract.label}. ${settings.label} net yield ranges from ` +
    `${formatYield(Math.min(...values))} to ${formatYield(Math.max(...values))}.`;
  canvas.setAttribute(
    "aria-label",
    `${settings.label} compounded ${state.data.pair} futures net yield by maturity. ` +
    `Highest ${settings.label.toLowerCase()} yield is ${formatYield(peak.value)} at ` +
    `${peak.contract.label}.${comparisonNote()}`,
  );
}

function selectMarket(marketKey, updateUrl = false) {
  const market = state.snapshot?.markets?.[marketKey];
  if (!market) return;

  state.marketKey = marketKey;
  state.data = market;
  state.chartPoints = [];
  state.yieldChartPoints = [];
  elements.tooltip.hidden = true;
  elements.yieldTooltip.hidden = true;

  renderMarketIdentity(market);
  renderSpot(market.spot);
  renderStats(market);
  renderRows(market.contracts);
  renderCarry(market);
  renderComparisonLegends();
  renderChart();
  renderYieldChart();

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
  renderChart();
  renderYieldChart();
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
    state.renderedDate = date;
    renderSnapshot(data);
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

window.addEventListener("resize", () => {
  window.clearTimeout(state.resizeTimer);
  state.resizeTimer = window.setTimeout(() => {
    renderChart();
    renderYieldChart();
  }, 120);
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
  });
});

// Both days' values for one maturity month, and the change from the
// comparison close to the view. A side without the contract reads "not listed".
function comparisonTooltip(point, format, formatDelta) {
  const value = (side) => point[side] ? format(point[side].value) : "not listed";
  const delta = point.current && point.compare
    ? `Δ ${formatDelta(point.current.value - point.compare.value)}`
    : "Δ —";
  const contract = (point.current ?? point.compare).contract;
  const daysLeft = point.current
    ? `${escapeHtml(contract.days_to_maturity)} days left`
    : "Not listed on the viewed day";
  return `<strong>${escapeHtml(point.label)}</strong>` +
    `${formatDay(referenceDate())}: ${value("current")}<br>` +
    `${formatDay(state.compareDate)}: ${value("compare")}<br>` +
    `<span class="tooltip-delta">${delta}</span><br>${daysLeft}`;
}

function placeTooltip(tooltip, point, bounds, minX) {
  tooltip.classList.toggle("is-compare", Boolean(comparisonMarket()));
  tooltip.hidden = false;
  const tooltipX = Math.min(Math.max(point.x + 12, minX), bounds.width - tooltip.offsetWidth - 1);
  const tooltipY = Math.max(point.y - tooltip.offsetHeight - 12, 8);
  tooltip.style.left = `${tooltipX}px`;
  tooltip.style.top = `${tooltipY}px`;
}

elements.canvas.addEventListener("pointermove", (event) => {
  if (!state.chartPoints.length) return;
  const bounds = elements.canvas.getBoundingClientRect();
  const x = event.clientX - bounds.left;
  const nearest = state.chartPoints.reduce((best, point) =>
    Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best
  );

  if (Math.abs(nearest.x - x) > 35) {
    elements.tooltip.hidden = true;
    return;
  }

  elements.tooltip.innerHTML = comparisonMarket()
    ? comparisonTooltip(
      nearest,
      formatPrice,
      (delta) => `${formatSignedPrice(delta)} ${escapeHtml(state.data.spot_unit)}`,
    )
    : `<strong>${escapeHtml(nearest.label)}</strong>` +
      `${formatPrice(nearest.current.value)} · ${escapeHtml(state.data.spot_unit)}<br>` +
      `${escapeHtml(nearest.current.contract.days_to_maturity)} days left`;
  placeTooltip(elements.tooltip, nearest, bounds, 45);
});

elements.canvas.addEventListener("pointerleave", () => {
  elements.tooltip.hidden = true;
});

elements.yieldCanvas.addEventListener("pointermove", (event) => {
  if (!state.yieldChartPoints.length) return;
  const bounds = elements.yieldCanvas.getBoundingClientRect();
  const x = event.clientX - bounds.left;
  const nearest = state.yieldChartPoints.reduce((best, point) =>
    Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best
  );

  if (Math.abs(nearest.x - x) > 35) {
    elements.yieldTooltip.hidden = true;
    return;
  }

  const settings = YIELD_PERIODS[state.yieldPeriod];
  const peakNote = nearest.isPeak ? `<br>Highest ${settings.label.toLowerCase()} yield` : "";
  elements.yieldTooltip.innerHTML = comparisonMarket()
    ? comparisonTooltip(
      nearest,
      (value) => `${formatYield(value)} ${settings.shortLabel}`,
      (delta) => formatYieldDelta(delta),
    ) + peakNote
    : `<strong>${escapeHtml(nearest.label)}</strong>` +
      `${formatYield(nearest.current.value)} ${settings.shortLabel}<br>` +
      `${escapeHtml(nearest.current.contract.days_to_maturity)} days left${peakNote}`;
  placeTooltip(elements.yieldTooltip, nearest, bounds, 60);
});

elements.yieldCanvas.addEventListener("pointerleave", () => {
  elements.yieldTooltip.hidden = true;
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
