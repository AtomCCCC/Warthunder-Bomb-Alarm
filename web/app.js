"use strict";

const $ = (id) => document.getElementById(id);
const ui = {
  connection: $("connection"), notifyButton: $("notifyButton"), alertPanel: $("alertPanel"),
  alertMessage: $("alertMessage"), countdownValue: $("countdownValue"), mapStage: $("mapStage"),
  mapImage: $("mapImage"), playerMarker: $("playerMarker"), targetMarker: $("targetMarker"),
  routeLine: $("routeLine"), zoneLayer: $("zoneLayer"), allyLayer: $("allyLayer"),
  clearTarget: $("clearTarget"), zoneStatus: $("zoneStatus"), aircraftName: $("aircraftName"),
  eventToast: $("eventToast"), eventToastTitle: $("eventToastTitle"), eventToastBody: $("eventToastBody"),
  tas: $("tas"), altitude: $("altitude"), verticalSpeed: $("verticalSpeed"), roll: $("roll"),
  distance: $("distance"), crossTrack: $("crossTrack"), fallTime: $("fallTime"),
  releaseDistance: $("releaseDistance"), elevation: $("elevation"), retention: $("retention"),
  calibration: $("calibration"), approach: $("approach"), corridor: $("corridor"), modeLabel: $("modeLabel"),
  plannerDetected: $("plannerDetected"), plannerBr: $("plannerBr"), baseMode: $("baseMode"),
  bombName: $("bombName"), bombOptions: $("bombOptions"), bombCount: $("bombCount"),
  plannerTotalWeight: $("plannerTotalWeight"), plannerDetail: $("plannerDetail"),
  plannerStatus: $("plannerStatus"), bombSource: $("bombSource")
};

let saved = {};
try { saved = JSON.parse(localStorage.getItem("wt-bomb-alert") || "{}"); } catch (_) { saved = {}; }
let target = saved.target || null;
let targetZoneId = saved.targetZoneId || null;
let lastStatus = null;
let notificationEnabled = "Notification" in window && Notification.permission === "granted";
let lastMapRefresh = 0;
let audioContext = null;
let polling = false;
let renderedZones = [];
let toastTimer = null;
let bombChart = null;
let bombChartAircraft = null;
let bombChartLoading = false;
const zoneLifecycle = {
  initialized: false,
  generation: null,
  confirmed: new Map(),
  missingSince: new Map(),
  destroyed: new Set()
};
const ZONE_MISSING_GRACE_MS = 1500;

for (const key of ["elevation", "retention", "calibration", "approach", "corridor"]) {
  if (saved[key] !== undefined) ui[key].value = saved[key];
  ui[key].addEventListener("change", saveSettings);
}

if (saved.plannerBr !== undefined) ui.plannerBr.value = saved.plannerBr;
if (saved.plannerBomb !== undefined) ui.bombName.value = saved.plannerBomb;
if (saved.plannerMode !== undefined) ui.baseMode.value = saved.plannerMode;
ui.plannerBr.addEventListener("input", () => { saveSettings(); renderBombPlan(); });
ui.bombName.addEventListener("input", () => { saveSettings(); renderBombPlan(); });
ui.baseMode.addEventListener("change", () => { saveSettings(); renderBombPlan(); });

function saveSettings() {
  localStorage.setItem("wt-bomb-alert", JSON.stringify({
    target, targetZoneId,
    elevation: ui.elevation.value,
    retention: ui.retention.value,
    calibration: ui.calibration.value,
    approach: ui.approach.value,
    corridor: ui.corridor.value,
    plannerBr: ui.plannerBr.value,
    plannerBomb: ui.bombName.value,
    plannerMode: ui.baseMode.value,
    plannerAircraft: bombChartAircraft
  }));
}

function updateNotificationButton() {
  if (!("Notification" in window)) {
    ui.notifyButton.textContent = "浏览器不支持通知";
    ui.notifyButton.disabled = true;
    return;
  }
  if (notificationEnabled) {
    ui.notifyButton.textContent = "通知已启用";
    ui.notifyButton.classList.add("enabled");
  } else if (Notification.permission === "denied") {
    ui.notifyButton.textContent = "通知已被阻止";
    ui.notifyButton.classList.remove("enabled");
  } else {
    ui.notifyButton.textContent = "启用通知";
    ui.notifyButton.classList.remove("enabled");
  }
}

ui.notifyButton.addEventListener("click", async () => {
  const permission = await Notification.requestPermission();
  notificationEnabled = permission === "granted";
  updateNotificationButton();
  if (notificationEnabled) notify("WT 投弹提醒器", "通知测试成功；可以把仪表盘放到后台。", false);
});

function notify(title, body, urgent = false) {
  if (notificationEnabled) {
    new Notification(title, { body, tag: urgent ? "wt-release" : "wt-approach", renotify: true });
  }
  beep(urgent);
}

function showEvent(title, body, urgent = false) {
  ui.eventToastTitle.textContent = title;
  ui.eventToastBody.textContent = body;
  ui.eventToast.classList.toggle("danger", urgent);
  ui.eventToast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ui.eventToast.classList.remove("visible"), 6000);
  notify(title, body, urgent);
}

function clearTargetState() {
  target = null;
  targetZoneId = null;
  lastStatus = null;
  saveSettings();
  renderMarkers(null);
}

function beep(urgent) {
  try {
    audioContext ||= new AudioContext();
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();
    oscillator.type = "square";
    oscillator.frequency.value = urgent ? 930 : 620;
    gain.gain.setValueAtTime(0.07, audioContext.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioContext.currentTime + (urgent ? 0.32 : 0.18));
    oscillator.connect(gain).connect(audioContext.destination);
    oscillator.start();
    oscillator.stop(audioContext.currentTime + (urgent ? 0.32 : 0.18));
  } catch (_) { /* 浏览器可能在首次用户交互前禁止声音。 */ }
}

function selectTarget(event) {
  const rect = ui.mapStage.getBoundingClientRect();
  target = {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))
  };
  targetZoneId = null;
  lastStatus = null;
  saveSettings();
  renderMarkers(null);
}

ui.mapStage.addEventListener("click", selectTarget);
ui.mapStage.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    const rect = ui.mapStage.getBoundingClientRect();
    selectTarget({ clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 });
  }
});
ui.clearTarget.addEventListener("click", clearTargetState);

function format(value, digits = 0) {
  return Number.isFinite(value) ? value.toFixed(digits) : "--";
}

function nearestTier(br) {
  if (!bombChart || !Number.isFinite(br)) return null;
  const exact = bombChart.tiers.find((tier) => br >= tier.min_br && br <= tier.max_br);
  if (exact) return exact;
  return bombChart.tiers.reduce((best, tier) => {
    const distance = br < tier.min_br ? tier.min_br - br : br - tier.max_br;
    return !best || distance < best.distance ? { ...tier, distance } : best;
  }, null);
}

function selectedBomb() {
  if (!bombChart) return null;
  const query = ui.bombName.value.trim().toLocaleLowerCase();
  if (!query) return null;
  const choices = bombChart.availableBombs || bombChart.bombs;
  const exact = choices.find((bomb) =>
    bomb.full_name.toLocaleLowerCase() === query || bomb.chart_name.toLocaleLowerCase() === query
  );
  if (exact) return exact;
  const partial = choices.filter((bomb) =>
    bomb.full_name.toLocaleLowerCase().includes(query) || bomb.chart_name.toLocaleLowerCase().includes(query)
  );
  return partial.length === 1 ? partial[0] : null;
}

function renderBombPlan() {
  if (!bombChart) return;
  const br = Number.parseFloat(ui.plannerBr.value);
  const tier = nearestTier(br);
  const bomb = selectedBomb();
  if (!tier || !bomb) {
    ui.bombCount.textContent = "--";
    ui.plannerTotalWeight.textContent = "--";
    ui.plannerDetail.textContent = !Number.isFinite(br) ? "请填写飞机权重（BR）。" : "请从列表中选择一个明确的炸弹型号。";
    return;
  }

  const tierIndex = bombChart.tiers.findIndex((candidate) => candidate.hp === tier.hp);
  const chartCount = Number(bomb.counts[tierIndex]);
  if (!Number.isFinite(chartCount)) {
    ui.bombCount.textContent = "--";
    ui.plannerTotalWeight.textContent = "--";
    ui.plannerDetail.textContent = `${bomb.full_name} 在该权重区间没有可用计数。`;
    return;
  }

  const multipliers = { four: 1, three: 0.5, arcade: 2 };
  const multiplier = multipliers[ui.baseMode.value] || 1;
  const required = Math.ceil(chartCount * multiplier);
  const totalWeight = Number.isFinite(bomb.actual_weight_kg) ? required * bomb.actual_weight_kg : null;
  const ruleText = ui.baseMode.value === "four" ? "四基地表格原值" :
    (ui.baseMode.value === "three" ? "三基地约半载荷（经验规则）" : "街机不刷新基地约双载荷（经验规则）");
  ui.bombCount.textContent = String(required);
  ui.plannerTotalWeight.textContent = Number.isFinite(totalWeight) ? totalWeight.toFixed(totalWeight >= 100 ? 0 : 1) : "--";
  ui.plannerDetail.textContent = `BR ${br.toFixed(1)} → ${tier.hp.toLocaleString()} HP · ${bomb.full_name} · ${ruleText}。`;
}

async function loadBombChart(aircraft) {
  if (!aircraft || aircraft === bombChartAircraft || bombChartLoading) return;
  bombChartAircraft = aircraft;
  bombChartLoading = true;
  ui.plannerStatus.classList.remove("error");
  ui.plannerStatus.textContent = "正在读取并匹配社区表格…";
  try {
    const response = await fetch(`/api/bomb-chart?aircraft=${encodeURIComponent(aircraft)}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.available) throw new Error(payload.error || "Bomb Chart 不可用");
    bombChart = payload;
    const normalizeBomb = (value) => value.toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");
    const loadoutNames = payload.aircraft_match?.bomb_names || [];
    let choices = payload.bombs;
    if (loadoutNames.length) {
      const normalizedNames = loadoutNames.map(normalizeBomb);
      const compatible = payload.bombs.filter((bomb) => {
        const names = [normalizeBomb(bomb.chart_name), normalizeBomb(bomb.full_name)];
        return normalizedNames.some((available) => names.some((name) => name === available));
      });
      if (compatible.length) choices = compatible;
    }
    bombChart.availableBombs = choices;
    ui.bombOptions.replaceChildren(...choices.map((bomb) => {
      const option = document.createElement("option");
      option.value = bomb.full_name;
      option.label = bomb.chart_name === bomb.full_name ? bomb.type : `${bomb.chart_name} · ${bomb.type}`;
      return option;
    }));
    if (payload.source?.url) ui.bombSource.href = payload.source.url;
    if (payload.aircraft_match) {
      const match = payload.aircraft_match;
      ui.plannerDetected.textContent = `${match.name} · BR ${match.br.toFixed(1)}`;
      if (!ui.plannerBr.value || saved.plannerAircraft !== aircraft) ui.plannerBr.value = match.br.toFixed(1);
      if (saved.plannerAircraft !== aircraft) ui.bombName.value = choices.length === 1 ? choices[0].full_name : "";
      ui.plannerStatus.textContent = `已匹配 ${match.nation} 数据与 ${choices.length} 种表内挂载；可手动校正。`;
    } else {
      ui.plannerDetected.textContent = "未匹配机型";
      ui.plannerStatus.textContent = `8111 型号 ${aircraft} 未在表中精确匹配，请手动填写 BR。`;
    }
    saveSettings();
    renderBombPlan();
  } catch (error) {
    bombChartAircraft = null;
    ui.plannerStatus.classList.add("error");
    ui.plannerStatus.textContent = error.message || "公开 Bomb Chart 读取失败。";
  } finally {
    bombChartLoading = false;
  }
}

function renderMarkers(player) {
  if (target) {
    ui.targetMarker.classList.add("visible");
    ui.targetMarker.style.left = `${target.x * 100}%`;
    ui.targetMarker.style.top = `${target.y * 100}%`;
  } else {
    ui.targetMarker.classList.remove("visible");
  }

  if (player) {
    ui.playerMarker.classList.add("visible");
    ui.playerMarker.style.left = `${player.x * 100}%`;
    ui.playerMarker.style.top = `${player.y * 100}%`;
    ui.playerMarker.style.transform = `translate(-50%,-50%) rotate(${player.heading_deg}deg)`;
  } else {
    ui.playerMarker.classList.remove("visible");
  }

  if (target && player) {
    ui.routeLine.classList.add("visible");
    ui.routeLine.setAttribute("x1", player.x * 1000);
    ui.routeLine.setAttribute("y1", player.y * 1000);
    ui.routeLine.setAttribute("x2", target.x * 1000);
    ui.routeLine.setAttribute("y2", target.y * 1000);
  } else {
    ui.routeLine.classList.remove("visible");
  }

  for (const marker of ui.zoneLayer.querySelectorAll(".zone-marker")) {
    const zone = renderedZones.find((item) => item.id === marker.dataset.zoneId);
    const selected = Boolean(targetZoneId && zone && targetZoneId === zone.id);
    marker.classList.toggle("selected", selected);
  }
}

function renderZones(zones) {
  const signature = JSON.stringify(zones);
  if (signature === ui.zoneLayer.dataset.signature) return;
  ui.zoneLayer.dataset.signature = signature;
  ui.zoneLayer.replaceChildren();
  renderedZones = zones;

  for (const zone of zones) {
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = `zone-marker ${zone.kind} ${zone.team}`;
    marker.dataset.zoneId = zone.id;
    marker.textContent = zone.short_label;
    marker.title = `${zone.label} · 点击设为投弹目标`;
    marker.setAttribute("aria-label", `${zone.label}，点击设为投弹目标`);
    marker.style.left = `${zone.x * 100}%`;
    marker.style.top = `${zone.y * 100}%`;
    marker.addEventListener("click", (event) => {
      event.stopPropagation();
      target = { x: zone.x, y: zone.y };
      targetZoneId = zone.id;
      lastStatus = null;
      saveSettings();
      renderMarkers(null);
    });
    ui.zoneLayer.append(marker);
  }
}

function renderAllies(allies) {
  ui.allyLayer.replaceChildren();
  for (const ally of allies) {
    const marker = document.createElement("span");
    marker.className = "ally-marker";
    marker.title = `${ally.label} · ${ally.aircraft_type}`;
    marker.setAttribute("aria-label", marker.title);
    marker.style.left = `${ally.x * 100}%`;
    marker.style.top = `${ally.y * 100}%`;
    marker.style.transform = `translate(-50%,-50%) rotate(${ally.heading_deg}deg)`;
    ui.allyLayer.append(marker);
  }
}

function initializeZoneLifecycle(live, generation) {
  zoneLifecycle.initialized = true;
  zoneLifecycle.generation = generation;
  zoneLifecycle.confirmed = new Map(live);
  zoneLifecycle.missingSince.clear();
  zoneLifecycle.destroyed.clear();
  if (targetZoneId && !live.has(targetZoneId)) clearTargetState();
}

function trackZoneLifecycle(zones, solution, generation) {
  const live = new Map(zones.filter((zone) => zone.kind === "bombing").map((zone) => [zone.id, zone]));
  if (!solution.player) return;

  if (!zoneLifecycle.initialized) {
    initializeZoneLifecycle(live, generation);
    return;
  }
  if (generation != null && zoneLifecycle.generation != null && generation !== zoneLifecycle.generation) {
    initializeZoneLifecycle(live, generation);
    clearTargetState();
    showEvent("战区地图已重置", "检测到新的地图状态，请重新选择投弹战区。", false);
    return;
  }

  const now = Date.now();
  let targetDestroyed = false;
  for (const id of [...zoneLifecycle.confirmed.keys()]) {
    if (live.has(id)) {
      zoneLifecycle.missingSince.delete(id);
      continue;
    }
    if (!zoneLifecycle.missingSince.has(id)) zoneLifecycle.missingSince.set(id, now);
    if (now - zoneLifecycle.missingSince.get(id) >= ZONE_MISSING_GRACE_MS) {
      zoneLifecycle.confirmed.delete(id);
      zoneLifecycle.missingSince.delete(id);
      zoneLifecycle.destroyed.add(id);
      if (targetZoneId === id) targetDestroyed = true;
    }
  }

  if (targetDestroyed) {
    clearTargetState();
    showEvent("目标战区已被摧毁", "投弹目标已自动清除，请选择仍然活跃的战区。", true);
  }

  const refreshed = [];
  for (const [id, zone] of live) {
    if (!zoneLifecycle.confirmed.has(id)) refreshed.push(zone);
    zoneLifecycle.confirmed.set(id, zone);
    zoneLifecycle.missingSince.delete(id);
  }
  if (refreshed.length) {
    for (const zone of refreshed) zoneLifecycle.destroyed.delete(zone.id);
    clearTargetState();
    showEvent("战区已刷新", `发现 ${refreshed.length} 个新轰炸区，投弹目标已重置。`, false);
  }
}

function renderDisconnected(message) {
  ui.connection.className = "connection offline";
  ui.connection.innerHTML = "<i></i>等待游戏";
  ui.alertPanel.className = "alert-panel waiting";
  ui.alertMessage.textContent = message;
  ui.countdownValue.textContent = "--";
  renderZones([]);
  renderAllies([]);
  ui.zoneStatus.textContent = "战区 -- · 友机 --";
  renderMarkers(null);
}

function renderSnapshot(payload) {
  if (!payload.connected) {
    renderDisconnected(payload.error || "等待游戏遥测");
    return;
  }

  const solution = payload.solution || {};
  const telemetry = solution.telemetry || {};
  ui.connection.className = "connection online";
  ui.connection.innerHTML = `<i></i>${payload.demo ? "演示遥测" : "8111 已连接"}`;
  ui.modeLabel.textContent = payload.demo ? "DEMO MODE" : "LIVE MODE";
  ui.alertPanel.className = `alert-panel ${solution.status || "waiting"}`;
  ui.alertMessage.textContent = solution.message || "等待解算";
  ui.aircraftName.textContent = payload.aircraft || "当前载具";
  loadBombChart(payload.aircraft);

  const countdown = solution.seconds_to_release;
  ui.countdownValue.textContent = Number.isFinite(countdown) ? (countdown >= 0 ? countdown.toFixed(1) : "0.0") : "--";
  ui.tas.textContent = format(telemetry.tas_kmh);
  ui.altitude.textContent = format(telemetry.altitude_m);
  ui.verticalSpeed.textContent = format(telemetry.vertical_speed_mps, 1);
  ui.roll.textContent = format(telemetry.roll_deg, 1);
  ui.distance.textContent = format(solution.distance_m / 1000, 2);
  ui.crossTrack.textContent = format(solution.cross_track_m);
  ui.fallTime.textContent = format(solution.fall_time_s, 1);
  ui.releaseDistance.textContent = format(solution.release_distance_m / 1000, 2);
  const zones = payload.zones || [];
  const allies = payload.allies || [];
  trackZoneLifecycle(zones, solution, payload.map_generation);
  renderZones(zones);
  renderAllies(allies);
  ui.zoneStatus.textContent = `战区 ${zones.filter((zone) => zone.kind === "bombing").length} 活跃 · 友机 ${allies.length}`;
  renderMarkers(solution.player);
  handleAlertTransition(solution.status, solution.message);
}

function handleAlertTransition(status, message) {
  if (status === lastStatus) return;
  if (status === "approaching") notify("进入投弹航线", message, false);
  if (status === "countdown") notify("准备投弹", message, false);
  if (status === "release") notify("立即投弹", "已到达计算投弹点", true);
  lastStatus = status;
}

function queryString() {
  const query = new URLSearchParams({
    elevation: ui.elevation.value || "0",
    retention: String((Number(ui.retention.value) || 92) / 100),
    calibration: ui.calibration.value || "0",
    approach: ui.approach.value || "20",
    corridor: ui.corridor.value || "800"
  });
  if (target) { query.set("tx", target.x); query.set("ty", target.y); }
  return query.toString();
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const response = await fetch(`/api/snapshot?${queryString()}`, { cache: "no-store" });
    renderSnapshot(await response.json());
  } catch (_) {
    renderDisconnected("本地提醒服务连接中断");
  } finally {
    polling = false;
  }
}

function refreshMap() {
  const now = Date.now();
  if (now - lastMapRefresh < 2500) return;
  lastMapRefresh = now;
  const probe = new Image();
  probe.onload = () => {
    ui.mapImage.src = probe.src;
    ui.mapStage.classList.add("map-ready");
  };
  probe.onerror = () => ui.mapStage.classList.remove("map-ready");
  probe.src = `/api/map?t=${now}`;
}

updateNotificationButton();
renderMarkers(null);
poll();
refreshMap();
setInterval(poll, 100);
setInterval(refreshMap, 2500);
