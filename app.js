const DATA_URL = "data/market.json";
const ISTANBUL_TIMEZONE = "Europe/Istanbul";

const elements = {
  dataState: document.querySelector("#data-state"),
  refreshButton: document.querySelector("#refresh-button"),
  spotValue: document.querySelector("#spot-value"),
  spotChange: document.querySelector("#spot-change"),
  spotLow: document.querySelector("#spot-low"),
  spotHigh: document.querySelector("#spot-high"),
  marketDate: document.querySelector("#market-date"),
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
  canvas: document.querySelector("#curve-chart"),
  tooltip: document.querySelector("#chart-tooltip"),
};

const state = {
  data: null,
  chartPoints: [],
  yieldChartPoints: [],
  yieldPeriod: "daily",
  refreshTimer: null,
  resizeTimer: null,
};

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

const priceFormat = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
});

const percentFormat = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: "always",
});

const signedPriceFormat = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
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

function signedClass(value) {
  if (!hasNumber(value) || value === 0) return "";
  return value > 0 ? "positive" : "negative";
}

function formatPrice(value) {
  return hasNumber(value) ? priceFormat.format(value) : "—";
}

function formatPercent(value) {
  return hasNumber(value) ? `${percentFormat.format(value)}%` : "—";
}

function yieldValue(contract, period = state.yieldPeriod) {
  const settings = YIELD_PERIODS[period];
  if (hasNumber(contract[settings.field])) return contract[settings.field];

  let factor = contract.daily_yield_factor;
  if (
    !hasNumber(factor) &&
    hasNumber(contract.last) &&
    hasNumber(state.data?.spot?.last) &&
    contract.last > 0 &&
    state.data.spot.last > 0 &&
    contract.days_to_maturity > 0
  ) {
    factor = Math.pow(
      contract.last / state.data.spot.last,
      1 / contract.days_to_maturity,
    );
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

function parseMarketDate(value) {
  return new Date(`${value}T12:00:00+03:00`);
}

function updateStatus(generatedAt) {
  const minutesOld = Math.max(0, (Date.now() - new Date(generatedAt).getTime()) / 60000);
  elements.dataState.classList.remove("is-fresh", "is-delayed", "is-error");
  if (minutesOld <= 45) {
    elements.dataState.classList.add("is-fresh");
    elements.dataState.lastChild.textContent = " Latest snapshot";
  } else {
    elements.dataState.classList.add("is-delayed");
    elements.dataState.lastChild.textContent = ` Update delayed · ${Math.floor(minutesOld)}m old`;
  }
}

function renderSpot(spot) {
  elements.spotValue.textContent = formatPrice(spot.last);
  elements.spotValue.classList.remove("loading-value");
  elements.spotLow.textContent = formatPrice(spot.low);
  elements.spotHigh.textContent = formatPrice(spot.high);

  elements.spotChange.className = `change-value ${signedClass(spot.change_percent)}`;
  const absolute = hasNumber(spot.change) ? signedPriceFormat.format(spot.change) : "—";
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
      `${formatPrice(front.last)} to ${formatPrice(final.last)} TRY per USD.`;
  }
}

function renderRows(contracts) {
  if (!contracts.length) {
    elements.rows.innerHTML = '<tr class="empty-row"><td colspan="8">No active contracts found.</td></tr>';
    return;
  }

  elements.rows.innerHTML = contracts.map((contract, index) => {
    const changeClass = signedClass(contract.change_percent);
    const premiumClass = signedClass(contract.premium_percent);
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
            <strong>${contract.label}</strong>
            <small>${contract.code}${index === 0 ? " · FRONT" : ""}${unavailable}</small>
          </span>
        </td>
        <td class="numeric" data-label="Last"><span class="last-price">${formatPrice(contract.last)}</span></td>
        <td class="numeric ${changeClass}" data-label="Change">
          <span>${formatPercent(contract.change_percent)}</span>
        </td>
        <td class="numeric" data-label="Bid / Ask"><span class="cell-subtle">${spread}</span></td>
        <td data-label="Maturity">${maturity}</td>
        <td class="numeric" data-label="Days left">
          <span class="days-badge ${nearClass}">${contract.days_to_maturity}</span>
        </td>
        <td class="numeric ${premiumClass}" data-label="vs. spot">${formatPercent(contract.premium_percent)}</td>
        <td class="numeric" data-label="Volume">${volume}</td>
      </tr>`;
  }).join("");
}

function renderChart() {
  if (!state.data) return;
  const contracts = state.data.contracts.filter((contract) => hasNumber(contract.last));
  const canvas = elements.canvas;
  const bounds = canvas.getBoundingClientRect();
  if (!bounds.width || !bounds.height || contracts.length < 1) return;

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
  const values = contracts.map((contract) => contract.last);
  const rawMin = Math.min(...values, state.data.spot.last);
  const rawMax = Math.max(...values, state.data.spot.last);
  const padding = Math.max((rawMax - rawMin) * 0.12, 0.1);
  const min = rawMin - padding;
  const max = rawMax + padding;

  const xAt = (index) => margin.left + (contracts.length === 1 ? chartWidth / 2 : index * chartWidth / (contracts.length - 1));
  const yAt = (value) => margin.top + (max - value) / (max - min) * chartHeight;

  context.font = '10px "IBM Plex Mono", monospace';
  context.textBaseline = "middle";
  context.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const value = max - ((max - min) * i / 4);
    const y = margin.top + chartHeight * i / 4;
    context.strokeStyle = "rgba(241, 239, 230, 0.11)";
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(width - margin.right, y);
    context.stroke();
    context.fillStyle = "rgba(241, 239, 230, 0.48)";
    context.textAlign = "right";
    context.fillText(value.toFixed(2), margin.left - 10, y);
  }

  const spotY = yAt(state.data.spot.last);
  context.save();
  context.setLineDash([6, 5]);
  context.strokeStyle = "rgba(255, 75, 47, 0.8)";
  context.beginPath();
  context.moveTo(margin.left, spotY);
  context.lineTo(width - margin.right, spotY);
  context.stroke();
  context.restore();
  context.fillStyle = "#ff715b";
  context.textAlign = "left";
  context.fillText("SPOT", margin.left + 7, spotY - 10);

  const gradient = context.createLinearGradient(0, margin.top, 0, height - margin.bottom);
  gradient.addColorStop(0, "rgba(184, 255, 69, 0.22)");
  gradient.addColorStop(1, "rgba(184, 255, 69, 0)");
  context.beginPath();
  contracts.forEach((contract, index) => {
    const x = xAt(index);
    const y = yAt(contract.last);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.lineTo(xAt(contracts.length - 1), height - margin.bottom);
  context.lineTo(xAt(0), height - margin.bottom);
  context.closePath();
  context.fillStyle = gradient;
  context.fill();

  context.beginPath();
  contracts.forEach((contract, index) => {
    const x = xAt(index);
    const y = yAt(contract.last);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.strokeStyle = "#b8ff45";
  context.lineWidth = 2;
  context.stroke();

  const labelEvery = width < 620 ? Math.ceil(contracts.length / 4) : Math.ceil(contracts.length / 8);
  state.chartPoints = contracts.map((contract, index) => {
    const x = xAt(index);
    const y = yAt(contract.last);
    context.beginPath();
    context.arc(x, y, index === 0 ? 5 : 3.5, 0, Math.PI * 2);
    context.fillStyle = index === 0 ? "#ff4b2f" : "#24271f";
    context.fill();
    context.strokeStyle = index === 0 ? "#ff715b" : "#b8ff45";
    context.lineWidth = 2;
    context.stroke();

    if (index % labelEvery === 0 || index === contracts.length - 1) {
      context.fillStyle = "rgba(241, 239, 230, 0.58)";
      context.textAlign = index === contracts.length - 1 ? "right" : index === 0 ? "left" : "center";
      context.fillText(contract.label.replace(" 20", " ’"), x, height - 20);
    }
    return { x, y, contract };
  });
}

function renderYieldChart() {
  if (!state.data) return;
  const settings = YIELD_PERIODS[state.yieldPeriod];
  const contracts = state.data.contracts
    .map((contract) => ({ contract, value: yieldValue(contract) }))
    .filter((point) => hasNumber(point.value));
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
  const rawMin = Math.min(0, ...values);
  const rawMax = Math.max(0, ...values);
  const range = rawMax - rawMin;
  const padding = Math.max(range * 0.12, state.yieldPeriod === "daily" ? 0.001 : 0.05);
  const min = rawMin - padding;
  const max = rawMax + padding;

  const xAt = (index) => margin.left + (
    contracts.length === 1
      ? chartWidth / 2
      : index * chartWidth / (contracts.length - 1)
  );
  const yAt = (value) => margin.top + (max - value) / (max - min) * chartHeight;

  context.font = '10px "IBM Plex Mono", monospace';
  context.textBaseline = "middle";
  context.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const value = max - ((max - min) * i / 4);
    const y = margin.top + chartHeight * i / 4;
    context.strokeStyle = "rgba(23, 25, 20, 0.13)";
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(width - margin.right, y);
    context.stroke();
    context.fillStyle = "rgba(23, 25, 20, 0.58)";
    context.textAlign = "right";
    context.fillText(
      `${value.toFixed(state.yieldPeriod === "daily" ? 3 : 1)}%`,
      margin.left - 10,
      y,
    );
  }

  const zeroY = yAt(0);
  context.strokeStyle = "rgba(23, 25, 20, 0.72)";
  context.lineWidth = 1.5;
  context.beginPath();
  context.moveTo(margin.left, zeroY);
  context.lineTo(width - margin.right, zeroY);
  context.stroke();

  const gradient = context.createLinearGradient(0, margin.top, 0, zeroY);
  gradient.addColorStop(0, "rgba(255, 75, 47, 0.28)");
  gradient.addColorStop(1, "rgba(255, 75, 47, 0.02)");
  context.beginPath();
  contracts.forEach((point, index) => {
    const x = xAt(index);
    const y = yAt(point.value);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.lineTo(xAt(contracts.length - 1), zeroY);
  context.lineTo(xAt(0), zeroY);
  context.closePath();
  context.fillStyle = gradient;
  context.fill();

  context.beginPath();
  contracts.forEach((point, index) => {
    const x = xAt(index);
    const y = yAt(point.value);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.strokeStyle = "#ff4b2f";
  context.lineWidth = 2.5;
  context.stroke();

  const peak = contracts.reduce(
    (highest, point, index) =>
      point.value > highest.value ? { ...point, index } : highest,
    { ...contracts[0], index: 0 },
  );
  const peakX = xAt(peak.index);
  const peakY = yAt(peak.value);

  context.save();
  context.setLineDash([4, 5]);
  context.strokeStyle = "rgba(255, 75, 47, 0.72)";
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(peakX, margin.top);
  context.lineTo(peakX, height - margin.bottom);
  context.stroke();
  context.restore();

  const labelEvery = width < 620
    ? Math.ceil(contracts.length / 4)
    : Math.ceil(contracts.length / 8);
  state.yieldChartPoints = contracts.map((point, index) => {
    const x = xAt(index);
    const y = yAt(point.value);
    const isPeak = index === peak.index;
    context.beginPath();
    context.arc(x, y, isPeak ? 7 : index === 0 ? 5 : 3.5, 0, Math.PI * 2);
    context.fillStyle = isPeak ? "#ff4b2f" : index === 0 ? "#171914" : "#e5e1d4";
    context.fill();
    context.strokeStyle = isPeak || index === 0 ? "#171914" : "#ff4b2f";
    context.lineWidth = isPeak ? 3 : 2;
    context.stroke();

    if (isPeak) {
      context.beginPath();
      context.arc(x, y, 11, 0, Math.PI * 2);
      context.strokeStyle = "rgba(255, 75, 47, 0.38)";
      context.lineWidth = 2;
      context.stroke();
    }

    if (index % labelEvery === 0 || index === contracts.length - 1) {
      context.fillStyle = "rgba(23, 25, 20, 0.62)";
      context.textAlign = index === contracts.length - 1
        ? "right"
        : index === 0 ? "left" : "center";
      context.fillText(
        point.contract.label.replace(" 20", " ’"),
        x,
        height - 20,
      );
    }
    return { x, y, isPeak, ...point };
  });

  const peakTag = `HIGHEST ${settings.label.toUpperCase()} · ${formatYield(peak.value)}`;
  context.font = '600 10px "IBM Plex Mono", monospace';
  const peakTagWidth = context.measureText(peakTag).width + 18;
  const peakTagX = Math.min(
    Math.max(peakX - peakTagWidth / 2, margin.left),
    width - margin.right - peakTagWidth,
  );
  const peakTagY = peakY < margin.top + 42 ? peakY + 17 : peakY - 34;
  context.fillStyle = "#171914";
  context.fillRect(peakTagX, peakTagY, peakTagWidth, 22);
  context.fillStyle = "#b8ff45";
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
    `${settings.label} compounded USD/TRY futures net yield by maturity. ` +
    `Highest ${settings.label.toLowerCase()} yield is ${formatYield(peak.value)} at ` +
    `${peak.contract.label}.`,
  );
}

function render(data) {
  state.data = data;
  renderSpot(data.spot);
  renderStats(data);
  renderRows(data.contracts);
  renderChart();
  renderYieldChart();

  elements.marketDate.textContent = dateFormat.format(parseMarketDate(data.market_date));
  elements.updatedAt.textContent = `${dateTimeFormat.format(new Date(data.generated_at))} TRT`;
  updateStatus(data.generated_at);
}

async function loadData() {
  elements.refreshButton.classList.add("is-loading");
  elements.refreshButton.disabled = true;
  try {
    const response = await fetch(`${DATA_URL}?v=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Market snapshot returned ${response.status}`);
    const data = await response.json();
    if (!data.spot || !Array.isArray(data.contracts)) throw new Error("Invalid market snapshot");
    render(data);
  } catch (error) {
    console.error(error);
    elements.dataState.classList.remove("is-fresh", "is-delayed");
    elements.dataState.classList.add("is-error");
    elements.dataState.lastChild.textContent = " Data unavailable";
    if (!state.data) {
      elements.rows.innerHTML = '<tr class="empty-row"><td colspan="8">The market snapshot could not be loaded. Try refreshing shortly.</td></tr>';
    }
  } finally {
    elements.refreshButton.classList.remove("is-loading");
    elements.refreshButton.disabled = false;
  }
}

elements.refreshButton.addEventListener("click", loadData);

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

  elements.tooltip.innerHTML = `<strong>${nearest.contract.label}</strong>${formatPrice(nearest.contract.last)} TRY<br>${nearest.contract.days_to_maturity} days left`;
  const tooltipX = Math.min(Math.max(nearest.x + 12, 45), bounds.width - 155);
  const tooltipY = Math.max(nearest.y - 72, 8);
  elements.tooltip.style.left = `${tooltipX}px`;
  elements.tooltip.style.top = `${tooltipY}px`;
  elements.tooltip.hidden = false;
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
  elements.yieldTooltip.innerHTML =
    `<strong>${nearest.contract.label}</strong>` +
    `${formatYield(nearest.value)} ${settings.shortLabel}<br>` +
    `${nearest.contract.days_to_maturity} days left` +
    `${nearest.isPeak ? `<br>Highest ${settings.label.toLowerCase()} yield` : ""}`;
  const tooltipX = Math.min(Math.max(nearest.x + 12, 60), bounds.width - 165);
  const tooltipY = Math.max(nearest.y - 72, 8);
  elements.yieldTooltip.style.left = `${tooltipX}px`;
  elements.yieldTooltip.style.top = `${tooltipY}px`;
  elements.yieldTooltip.hidden = false;
});

elements.yieldCanvas.addEventListener("pointerleave", () => {
  elements.yieldTooltip.hidden = true;
});

loadData();
state.refreshTimer = window.setInterval(loadData, 60_000);
