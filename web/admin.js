/**
 * Enterprise AI Cluster — Admin Panel Logic
 * =============================================
 * Semua request memakai HTTP Basic Auth bawaan browser (fetch otomatis
 * menyertakan credential setelah browser prompt sekali di /admin).
 * Vanilla JS, tanpa framework build step.
 */

const REFRESH_INTERVAL_MS = 8000;

function showToast(message, type = "success") {
  const existing = document.querySelector(".toast");
  if (existing) existing.remove();
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let detail = text;
    try { detail = JSON.parse(text).detail || text; } catch (_) { /* ignore */ }
    showToast(`Error ${res.status}: ${detail}`, "error");
    throw new Error(`API error ${res.status}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------
// NODE REGISTRY
// ---------------------------------------------------------------------

function statusColor(status) {
  const map = { online: "var(--cyan)", offline: "var(--red)", unauthorized: "var(--amber)", error: "var(--amber)" };
  return map[status] || "var(--text-faint)";
}

function renderNodeAdminRow(n) {
  const readyCount = (n.models_detail || []).filter(m => m.status === "ready").length;
  const lockedCount = (n.models_detail || []).filter(m => (m.status || "").includes("locked")).length;
  return `
    <div class="admin-node-row">
      <div class="info">
        <div style="font-family:var(--font-mono); font-weight:600; font-size:13px;">
          <span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:${statusColor(n.status)};margin-right:6px;"></span>
          #${n.id} ${n.name} <span style="color:var(--text-faint); font-weight:400;">(${n.status})</span>
        </div>
        <div style="font-family:var(--font-mono); font-size:11px; color:var(--text-dim); margin-top:3px;">
          ${n.host}:${n.port} — key ${n.api_key_masked} — priority ${n.priority}
        </div>
        <div style="font-family:var(--font-mono); font-size:11px; color:var(--text-faint); margin-top:2px;">
          ${readyCount} ready · ${lockedCount} locked ${n.safe_mode ? ' · <span style="color:var(--amber)">SAFE MODE</span>' : ''}
        </div>
      </div>
      <div class="actions">
        <button class="btn btn-ghost btn-sm" onclick="toggleNode(${n.id})">${n.enabled ? "Nonaktifkan" : "Aktifkan"}</button>
        <button class="btn btn-danger btn-sm" onclick="deleteNode(${n.id})">Hapus</button>
      </div>
    </div>
  `;
}

async function loadNodes() {
  try {
    const data = await api("/api/admin/nodes");
    const list = document.getElementById("nodeList");
    list.innerHTML = data.nodes.length
      ? data.nodes.map(renderNodeAdminRow).join("")
      : `<p style="color:var(--text-dim); font-family:var(--font-mono); font-size:12px;">Belum ada node terdaftar.</p>`;

    const online = data.nodes.filter(n => n.status === "online").length;
    document.getElementById("statOnline").textContent = `${online}/${data.nodes.length}`;
    const totalTasks = data.nodes.reduce((sum, n) => sum + (n.active_tasks || 0), 0);
    document.getElementById("statTasks").textContent = totalTasks;
  } catch (e) {
    console.error(e);
  }
}

async function addNode() {
  const name = document.getElementById("f_name").value.trim();
  const host = document.getElementById("f_host").value.trim();
  const port = parseInt(document.getElementById("f_port").value.trim() || "3716", 10);
  const priority = parseInt(document.getElementById("f_priority").value.trim() || "100", 10);
  const api_key = document.getElementById("f_key").value.trim();
  const tags = document.getElementById("f_tags").value.trim();

  if (!name || !host || !api_key) {
    showToast("Nama, host, dan API Key wajib diisi.", "error");
    return;
  }

  await api("/api/admin/nodes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, host, port, api_key, priority, tags }),
  });

  showToast(`Node '${name}' berhasil ditambahkan.`, "success");
  ["f_name", "f_host", "f_key", "f_tags"].forEach(id => (document.getElementById(id).value = ""));
  document.getElementById("f_port").value = "3716";
  document.getElementById("f_priority").value = "100";
  loadNodes();
}

async function toggleNode(id) {
  const result = await api(`/api/admin/nodes/${id}/toggle`, { method: "POST" });
  showToast(`Node #${id} ${result.enabled ? "diaktifkan" : "dinonaktifkan"}.`);
  loadNodes();
}

async function deleteNode(id) {
  if (!confirm(`Hapus node #${id} dari cluster? Tindakan ini tidak bisa dibatalkan.`)) return;
  await api(`/api/admin/nodes/${id}`, { method: "DELETE" });
  showToast(`Node #${id} dihapus dari cluster.`);
  loadNodes();
}

// ---------------------------------------------------------------------
// PULL / UNLOAD MODEL
// ---------------------------------------------------------------------

async function pullModel() {
  const nodeId = document.getElementById("pm_node_id").value.trim();
  const model = document.getElementById("pm_model").value.trim();
  if (!nodeId || !model) {
    showToast("Node ID dan nama model wajib diisi.", "error");
    return;
  }
  showToast(`Memulai pull '${model}' ke node #${nodeId}... (bisa lama untuk model besar)`);
  try {
    const result = await api(`/api/admin/nodes/${nodeId}/pull`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_name: model }),
    });
    showToast(`✅ Model '${model}' berhasil di-pull (${result.elapsed_seconds}s).`, "success");
    loadNodes();
  } catch (e) { /* toast sudah ditampilkan oleh api() */ }
}

async function unloadModel() {
  const nodeId = document.getElementById("pm_node_id").value.trim();
  const model = document.getElementById("pm_model").value.trim();
  if (!nodeId || !model) {
    showToast("Node ID dan nama model wajib diisi.", "error");
    return;
  }
  try {
    await api(`/api/admin/nodes/${nodeId}/unload`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_name: model }),
    });
    showToast(`✅ Model '${model}' berhasil di-unload dari node #${nodeId}.`, "success");
    loadNodes();
  } catch (e) { /* toast sudah ditampilkan oleh api() */ }
}

// ---------------------------------------------------------------------
// QUEUE MONITOR
// ---------------------------------------------------------------------

async function loadQueue() {
  try {
    const data = await api("/api/admin/queue");
    const panel = document.getElementById("queuePanel");

    const nodeLines = data.nodes.map(n => `
      <div style="display:flex; justify-content:space-between; font-family:var(--font-mono); font-size:12px; padding:5px 0; border-bottom:1px solid var(--border-line-soft);">
        <span>${n.name}${n.safe_mode ? ' <span style="color:var(--amber)">⚠</span>' : ''}</span>
        <span style="color:var(--text-hi);">${n.active_tasks} task</span>
      </div>
    `).join("");

    const eventLines = data.recent_events.slice(0, 10).map(ev => {
      const colorMap = { completed: "var(--cyan)", failed: "var(--red)", fallback: "var(--amber)", started: "var(--text-dim)" };
      const color = colorMap[ev.event_type] || "var(--text-dim)";
      return `<div style="font-family:var(--font-mono); font-size:11px; color:${color}; padding:3px 0;">
        [${ev.event_type}] ${ev.model_name}${ev.node_name ? " @" + ev.node_name : ""}
      </div>`;
    }).join("");

    panel.innerHTML = `
      <div style="margin-bottom:14px;">${nodeLines || '<p style="color:var(--text-dim); font-family:var(--font-mono); font-size:12px;">Tidak ada node online.</p>'}</div>
      <div style="font-family:var(--font-mono); font-size:10px; color:var(--text-faint); letter-spacing:0.05em; margin-bottom:6px;">EVENT TERBARU</div>
      ${eventLines || '<p style="color:var(--text-faint); font-family:var(--font-mono); font-size:11px;">Belum ada event.</p>'}
    `;
  } catch (e) {
    console.error(e);
  }
}

// ---------------------------------------------------------------------
// USERS
// ---------------------------------------------------------------------

async function loadUsers() {
  try {
    const data = await api("/api/admin/users?limit=25");
    document.getElementById("statUsers").textContent = data.total;
    const tbody = document.getElementById("userTableBody");
    tbody.innerHTML = data.users.map(u => `
      <tr>
        <td>${u.telegram_id}</td>
        <td>${u.username ? "@" + u.username : "-"}</td>
        <td>${u.model_role}/${u.model_tier}</td>
        <td>${u.is_unlimited ? "∞" : `${u.tokens_used}/${u.token_limit}`}</td>
        <td>${u.is_banned ? '<span style="color:var(--red)">banned</span>' : '<span style="color:var(--cyan)">active</span>'}</td>
      </tr>
    `).join("");
  } catch (e) {
    console.error(e);
  }
}

// ---------------------------------------------------------------------
// INIT
// ---------------------------------------------------------------------

async function refreshAll() {
  await Promise.all([loadNodes(), loadQueue(), loadUsers()]);
  document.getElementById("lastUpdated").textContent =
    "Terakhir diperbarui: " + new Date().toLocaleTimeString("id-ID", { hour12: false });
}

refreshAll();
setInterval(refreshAll, REFRESH_INTERVAL_MS);
