import { invoke } from "@tauri-apps/api/core";
import "./styles.css";

const variant = __BUILD_VARIANT__;
const isClaude = variant === "claude";
const buildGatewayConfigured = __BUILD_GATEWAY_CONFIGURED__;
const productName = isClaude ? "Friend Claude" : "Friend Codex（暂不开放）";
const officialName = isClaude ? "Claude Desktop" : "ChatGPT / Codex";

document.title = productName;

const root = document.querySelector("#app");
root.innerHTML = `
  <main class="shell">
    <section class="card">
      <header class="brand">
        <span class="logo">${isClaude ? "C" : "X"}</span>
        <div>
          <h1>${productName}</h1>
          <p>保留原版 ${officialName}，只接入受信的 Friend 目录</p>
        </div>
        <span class="unofficial">非官方启动器</span>
      </header>

      <div class="status" id="status" aria-live="polite">
        <span class="dot"></span>
        <div><strong>正在检查原版 App…</strong><small>稍等片刻</small></div>
      </div>

      <section class="disclosure" aria-labelledby="disclosure-title">
        <div class="section-kicker">开始前请知悉</div>
        <h2 id="disclosure-title">数据告知</h2>
        <p>我方默认不记录请求/响应正文和 Key；请求会经过我方网关和上游，上游的数据策略另行适用。额度与扣费按我方账户和 New API 记录，Key 可被撤销。请勿输入不应发送到该服务的数据。</p>
        <label class="consent-row">
          <input id="consent" type="checkbox" />
          <span>我已阅读并同意以上数据告知</span>
        </label>
      </section>

      <form id="setup-form">
        <fieldset id="key-fieldset" disabled>
          <legend>我方 Key</legend>
          <label class="key-field">
            <span>一次性输入 Friend Key</span>
            <input id="secret" type="password" autocomplete="new-password" placeholder="粘贴后验证目录" />
            <small>提交后立即清空；不会回显、落盘或进入日志。</small>
          </label>
        </fieldset>

        <section class="catalog-panel" aria-labelledby="catalog-title">
          <div class="section-kicker">受信配置</div>
          <h2 id="catalog-title">选择目录项</h2>
          <select id="catalog" disabled>
            <option value="">先提交 Key 获取受信目录</option>
          </select>
          <p class="catalog-meta" id="catalog-meta">目录由固定 Friend 网关返回；不接受手填 Endpoint、Provider 或裸模型名。</p>
        </section>

        <section class="balance" aria-live="polite">
          <div>
            <div class="section-kicker">账户额度</div>
            <h2>余额</h2>
          </div>
          <strong id="balance-value">未读取</strong>
        </section>

        <div class="error" id="error" role="alert" hidden></div>
        <button class="primary" id="start" type="submit">验证 Key 并获取目录</button>
        <button class="quiet-action" id="cancel-flow" type="button" hidden>取消本次配置</button>
      </form>

      <section class="entry-section" aria-labelledby="entry-title">
        <div class="section-kicker">三类入口</div>
        <h2 id="entry-title">选择适合你的方式</h2>
        <div class="entry-grid">
          <button class="entry-card active" data-entry="our-key" type="button">
            <b>我方 Key</b><small>手工输入，按账户与安装控制额度</small>
          </button>
          <button class="entry-card" data-link="invite" type="button">
            <b>邀请</b><small>人工登记状态，暂不承诺自动到账</small>
          </button>
          <button class="entry-card" data-link="free-token" type="button">
            <b>免费第三方</b><small>只打开外链，不接受第三方 Key 或 Endpoint</small>
          </button>
        </div>
      </section>

      <nav class="secondary">
        <button id="download" type="button">下载原版 App</button>
        <button id="restore" type="button">恢复官方模式</button>
      </nav>

      <footer>
        <p id="gateway-note">${buildGatewayConfigured ? "发行构建已带固定网关配置。" : "当前构建未带远端网关配置；发行模式会失败关闭。"}</p>
        <p>安装后，日常只需点击 ${productName}；本工具不包含、不修改也不冒充官方 App。</p>
      </footer>
    </section>
  </main>
`;

const byId = (id) => document.getElementById(id);
const status = byId("status");
const errorBox = byId("error");
const consent = byId("consent");
const keyFieldset = byId("key-fieldset");
const secretInput = byId("secret");
const catalogSelect = byId("catalog");
const catalogMeta = byId("catalog-meta");
const balanceValue = byId("balance-value");
const startButton = byId("start");
const cancelButton = byId("cancel-flow");
let officialAppRunning = false;
let flowReady = false;
let catalogVersion = "";

function errorText(error) {
  if (typeof error === "string") return error;
  return error?.message || "操作失败，请稍后重试。";
}

function showMessage(message = "", kind = "error") {
  errorBox.hidden = !message;
  errorBox.dataset.kind = kind;
  errorBox.textContent = message;
}

function confirmRestartIfNeeded() {
  if (!officialAppRunning) return true;
  return window.confirm(
    `原版 ${officialName} 正在运行。切换配置需要正常退出并重新打开，请先保存正在进行的任务。现在继续吗？`,
  );
}

function setConsentState() {
  keyFieldset.disabled = !consent.checked || !isClaude || flowReady;
  if (consent.checked && isClaude && !flowReady) secretInput.focus();
}

function renderBalance(balance) {
  if (!balance || typeof balance.amount !== "string") {
    balanceValue.textContent = "未读取";
    return;
  }
  balanceValue.textContent = `${balance.amount} ${balance.currency || ""}`.trim();
}

function renderCatalog(data) {
  catalogVersion = data.catalog_version;
  catalogSelect.replaceChildren();
  for (const item of data.catalog || []) {
    const option = document.createElement("option");
    option.value = item.canonical_id;
    option.textContent = `${item.display_name} · ${item.billing_label}`;
    catalogSelect.append(option);
  }
  catalogSelect.disabled = false;
  catalogMeta.textContent = `受信目录 ${data.catalog_version} · 有效至 ${data.expires_at} · 固定网关；服务端模型引用不会暴露给前端。`;
  renderBalance(data.balance);
  setConsentState();
}

function resetFlowView() {
  flowReady = false;
  catalogVersion = "";
  catalogSelect.disabled = true;
  catalogSelect.replaceChildren(new Option("先提交 Key 获取受信目录", ""));
  catalogMeta.textContent = "目录由固定 Friend 网关返回；不接受手填 Endpoint、Provider 或裸模型名。";
  cancelButton.hidden = true;
  startButton.textContent = "验证 Key 并获取目录";
  setConsentState();
}

async function bootstrap() {
  try {
    const data = await invoke("launcher_status");
    officialAppRunning = Boolean(data.official_app_running);
    status.classList.toggle("ready", Boolean(data.official_app_installed));
    status.innerHTML = data.official_app_installed
      ? `<span class="dot"></span><div><strong>原版 ${officialName} 已安装</strong><small>${data.official_app_version || "可以开始使用"}</small></div>`
      : `<span class="dot"></span><div><strong>尚未安装原版 ${officialName}</strong><small>先点“下载原版 App”，安装后再回来</small></div>`;
    if (!data.gateway_configured && isClaude) {
      showMessage("当前发行构建没有固定网关配置，已失败关闭。请使用正确的现场测试构建。", "error");
    }
    if (isClaude && data.gateway_configured) {
      try {
        renderBalance(await invoke("refresh_friend_balance"));
      } catch {
        // 没有 Friend 自有 profile 时保持“未读取”，不打扰首次配置。
      }
    }
  } catch (error) {
    showMessage(errorText(error));
  }
}

consent.addEventListener("change", setConsentState);

byId("setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage();
  if (!isClaude) {
    showMessage("Codex 新流程在 V1A 阶段未开放。", "error");
    return;
  }
  const configuring = flowReady;
  const secret = secretInput.value.trim();
  // Clear before the IPC call and again in finally. This covers success, failure,
  // validation errors, cancellation and any future command rejection path.
  secretInput.value = "";
  if (!confirmRestartIfNeeded()) {
    showMessage("本次配置已取消。", "success");
    return;
  }
  startButton.disabled = true;
  cancelButton.hidden = false;
  startButton.textContent = configuring ? "正在应用目录配置…" : "正在验证 Key…";

  try {
    if (!configuring) {
      if (!secret) throw new Error("请先输入 Friend Key。");
      const data = await invoke("begin_friend_flow", { request: { secret } });
      renderCatalog(data);
      flowReady = true;
      setConsentState();
      startButton.textContent = "应用所选目录配置";
      showMessage("目录已验证，请确认目录项后应用配置。", "success");
    } else {
      const canonicalId = catalogSelect.value;
      if (!canonicalId || !catalogVersion) throw new Error("请先选择受信目录项。");
      await invoke("configure_and_launch", {
        request: { canonical_id: canonicalId, catalog_version: catalogVersion },
      });
      resetFlowView();
      officialAppRunning = true;
      startButton.textContent = "已打开 Claude Desktop";
      showMessage("配置已提交并打开原版 Claude。", "success");
    }
  } catch (error) {
    resetFlowView();
    showMessage(errorText(error));
  } finally {
    secretInput.value = "";
    startButton.disabled = false;
    cancelButton.hidden = !flowReady;
    if (!flowReady) startButton.textContent = "验证 Key 并获取目录";
  }
});

cancelButton.addEventListener("click", async () => {
  secretInput.value = "";
  try {
    await invoke("cancel_friend_flow");
  } catch (error) {
    showMessage(errorText(error));
  } finally {
    resetFlowView();
    showMessage("本次配置已取消。", "success");
  }
});

byId("download").addEventListener("click", async () => {
  try {
    await invoke("open_allowed_link", { target: "official-download" });
  } catch (error) {
    showMessage(errorText(error));
  }
});

byId("restore").addEventListener("click", async () => {
  secretInput.value = "";
  try {
    await invoke("cancel_friend_flow");
    resetFlowView();
    if (!confirmRestartIfNeeded()) return;
    const restored = await invoke("restore_official_mode");
    if (restored) officialAppRunning = true;
    showMessage(
      restored ? `已恢复 Friend 之前的官方配置，并重新打开 ${officialName}。` : "没有找到可安全恢复的 Friend 代际配置。",
      "success",
    );
  } catch (error) {
    showMessage(errorText(error));
  }
});

document.querySelectorAll("[data-entry]").forEach((button) => {
  button.addEventListener("click", () => {
    consent.checked = true;
    setConsentState();
    secretInput.scrollIntoView({ behavior: "smooth", block: "center" });
    secretInput.focus();
  });
});

document.querySelectorAll("[data-link]").forEach((button) => {
  button.addEventListener("click", async () => {
    try {
      await invoke("open_allowed_link", { target: button.dataset.link });
    } catch (error) {
      showMessage(errorText(error));
    }
  });
});

setConsentState();
void bootstrap();
