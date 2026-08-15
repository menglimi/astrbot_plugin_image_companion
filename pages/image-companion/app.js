const API = "/api/plug/astrbot_plugin_image_companion/page";
const $ = (id) => document.getElementById(id);
let bridgePromise = null;

async function pageBridge(timeoutMs = 3000) {
  if (!bridgePromise) {
    bridgePromise = (async () => {
      const startedAt = Date.now();
      while (Date.now() - startedAt < timeoutMs) {
        const bridge = window.AstrBotPluginPage;
        if (bridge?.apiGet) {
          if (bridge.ready) await bridge.ready();
          return bridge;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 80));
      }
      return null;
    })();
  }
  return bridgePromise;
}

async function status() {
  const bridge = await pageBridge();
  const payload = bridge
    ? await bridge.apiGet("page/status")
    : await fetch(`${API}/status`, { credentials: "same-origin" }).then((response) => {
        if (!response.ok) throw new Error(`请求失败（${response.status}）`);
        return response.json();
      });
  if (payload?.ok === false) throw new Error(payload.message || "请求失败");
  return payload?.data || {};
}

function showPage(id) {
  ["loading-page", "managed-page", "status-page", "error-page"].forEach((pageId) => {
    $(pageId).hidden = pageId !== id;
  });
}

function render(data) {
  $("enabled").textContent = data.enabled ? "已启用" : "已停用";
  $("mode").textContent = data.state === "independent" ? "完全独立" : "兼容迁移";
  $("count").textContent = String(Number(data.generation_count || 0));
  const latest = data.last_generation || {};
  $("workflow").textContent = latest.workflow_kind || "暂无记录";
  $("backend").textContent = latest.backend || "-";
  $("result").textContent = latest.success === true ? "成功" : latest.success === false ? "失败" : "-";
  $("note").textContent = latest.note || "-";
}

async function load() {
  showPage("loading-page");
  try {
    const data = await status();
    if (data.managed_by_private_companion) {
      showPage("managed-page");
      return;
    }
    render(data);
    showPage("status-page");
  } catch (error) {
    $("error-message").textContent = error.message || "未知错误";
    showPage("error-page");
  }
}

function openPluginManager() {
  try { window.top.location.assign("/#/plugins"); }
  catch (_) { window.location.assign("/#/plugins"); }
}

$("open-companion").addEventListener("click", openPluginManager);
$("refresh").addEventListener("click", load);
$("retry").addEventListener("click", load);
load();
