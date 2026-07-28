/**
 * Enterprise AI Cluster — Public Dashboard Logic
 * ==================================================
 * Fetch data dari /api/public/nodes & /api/public/stats setiap 5 detik,
 * render sebagai "rack" node dengan bar VRAM/CPU/RAM bergaya LED hardware.
 * Murni vanilla JS, tanpa framework, agar dashboard tetap ringan & cepat load.
 */

const REFRESH_INTERVAL_MS = 5000;

const STATUS_LABEL = {
  online: "ONLINE",
  offline: "OFFLINE",
  unauthorized: "AUTH ERROR",
  error: "ERROR",
  unknown: "UNKNOWN",
};

function formatGb(value) {
  if (value === null || value === undefined) return "–";
  return `${Number(value).toFixed(1)}GB`;
}

function formatPct(value) {
  if (value === null || value === undefined) return "–";
  return `${Number(value).toFixed(0)}%`;
}

function barFillClass(pct, baseClass) {
  if (pct === null || pct === undefined) return baseClass;
  if (pct >= 90) return "crit";
  if (pct >= 75) return "warn";
  return baseClass;
}

function renderClock() {
  const el = document.getElementById("clock");
  if (!el) return;
  const now = new Date();
  el.textContent = now.toLocaleTimeString("id-ID", { hour12: false });
}

function renderNodeRow(node) {
  const statusClass = node.status || "unknown";
  const statusLabel = STATUS_LABEL[statusClass] || statusClass.toUpperCase();
  const disabledClass = node.enabled ? "" : "is-disabled";

  const vramPct = node.vram_used_pct;
  const cpuPct = node.cpu_usage;
  const ramPct = node.ram_usage;

  const gpuChip = node.has_gpu
    ? `<span class="gpu-chip">${node.gpu_name || "GPU"}</span>`
    : `<span class="gpu-chip cpu-only">CPU-ONLY</span>`;

  const safeBadge = node.safe_mode ? `<span class="badge-safe">SAFE MODE</span>` : "";

  const barsHtml = node.status === "online" ? `
    <div class="bars">
      <div class="bar-block">
        <div class="bar-labels">
          <span>VRAM</span>
          <span class="val">${formatGb(node.vram_free_gb)} / ${formatGb(node.vram_total_gb)}</span>
        </div>
        <div class="bar-track">
          <div class="bar-fill ${node.has_gpu ? barFillClass(vramPct, 'vram') : ''}" style="width:${node.has_gpu ? (vramPct || 0) : 0}%"></div>
        </div>
      </div>
      <div class="bar-block">
        <div class="bar-labels">
          <span>CPU / RAM</span>
          <span class="val">${formatPct(cpuPct)} · ${formatPct(ramPct)}</span>
        </div>
        <div class="bar-track">
          <div class="bar-fill ${barFillClass(cpuPct, 'cpu')}" style="width:${cpuPct || 0}%"></div>
        </div>
      </div>
    </div>
    <div class="model-counts">
      <span class="ready-count">● ${node.models_ready_count ?? 0} ready</span>
      <span class="locked-count">○ ${node.models_locked_count ?? 0} locked</span>
      <span>task aktif: <strong style="color:var(--text-hi)">${node.active_tasks ?? 0}</strong></span>
      <span>latency: ${node.latency_ms != null ? node.latency_ms.toFixed(0) + "ms" : "–"}</span>
    </div>
  ` : `<div class="model-counts"><span>Node tidak online, data hardware tidak tersedia.</span></div>`;

  return `
    <div class="node-row ${disabledClass}">
      <div class="node-row-top">
        <div class="node-identity">
          <span class="led ${statusClass}"></span>
          <div>
            <div class="node-name">${node.name}${node.enabled ? "" : " (disabled)"}</div>
            <div class="node-host">${node.host_masked}</div>
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:10px;">
          ${safeBadge}
          ${gpuChip}
          <span class="node-status-text ${statusClass}">${statusLabel}</span>
        </div>
      </div>
      ${barsHtml}
    </div>
  `;
}

async function refreshStats() {
  try {
    const res = await fetch("/api/public/stats");
    if (!res.ok) throw new Error("stats fetch failed");
    const data = await res.json();
    document.getElementById("statOnline").textContent = `${data.online_nodes}/${data.total_nodes}`;
    document.getElementById("statGpu").textContent = data.gpu_nodes;
    document.getElementById("statVram").textContent = `${data.total_vram_free_gb}GB`;
    document.getElementById("statTasks").textContent = data.total_active_tasks;
    document.getElementById("statModels").textContent = data.unique_models_ready;
  } catch (e) {
    console.error("Gagal memuat statistik cluster:", e);
  }
}

async function refreshNodes() {
  const rack = document.getElementById("rack");
  try {
    const res = await fetch("/api/public/nodes");
    if (!res.ok) throw new Error("nodes fetch failed");
    const data = await res.json();

    if (!data.nodes || data.nodes.length === 0) {
      rack.innerHTML = `<div class="empty-state">Belum ada Worker Node terdaftar di cluster.</div>`;
      return;
    }

    rack.innerHTML = data.nodes.map(renderNodeRow).join("");
    document.getElementById("lastUpdated").textContent =
      "Terakhir diperbarui: " + new Date().toLocaleTimeString("id-ID", { hour12: false });
  } catch (e) {
    rack.innerHTML = `<div class="empty-state" style="color:var(--red)">Gagal memuat status cluster. Mencoba lagi...</div>`;
    console.error(e);
  }
}

async function refreshAll() {
  await Promise.all([refreshStats(), refreshNodes()]);
}

renderClock();
setInterval(renderClock, 1000);

refreshAll();
setInterval(refreshAll, REFRESH_INTERVAL_MS);
