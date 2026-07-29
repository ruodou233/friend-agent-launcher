import { invoke } from "@tauri-apps/api/core";
import "./styles.css";

const variant = __BUILD_VARIANT__;
const isClaude = variant === "claude";
const productName = isClaude ? "Friend Claude" : "Friend Codex";
const officialName = isClaude ? "Claude Desktop" : "ChatGPT / Codex";
const defaultGateway = __DEFAULT_GATEWAY_URL__;
const defaultModel = __DEFAULT_MODEL__;

document.title = productName;

const root = document.querySelector("#app");
root.innerHTML = `
  <main class="shell">
    <section class="card">
      <header class="brand">
        <span class="logo">${isClaude ? "C" : "X"}</span>
        <div>
          <h1>${productName}</h1>
          <p>给原版 ${officialName} 配好模型线路</p>
        </div>
        <span class="unofficial">非官方启动器</span>
      </header>

      <div class="status" id="status">
        <span class="dot"></span>
        <div><strong>正在检查原版 App…</strong><small>稍等片刻</small></div>
      </div>

      <form id="setup-form">
        <label class="key-field">
          <span>你的 Key</span>
          <input id="secret" type="password" autocomplete="new-password" placeholder="粘贴后点“开始使用”" required />
          <small>输入后不会回显，也不会进入日志。</small>
        </label>

        <details id="advanced">
          <summary>高级设置</summary>
          <div class="advanced-grid">
            <label>
              <span>API 地址</span>
              <input id="endpoint" type="url" autocomplete="url" placeholder="https://gateway.example.com" />
            </label>
            <label>
              <span>模型</span>
              <div class="model-row">
                <input id="model" list="model-options" autocomplete="off" placeholder="${isClaude ? "Claude 模型名" : "Codex 模型名"}" />
                <button id="discover-models" type="button">获取列表</button>
              </div>
              <datalist id="model-options"></datalist>
              <small>只显示去重后的模型名；切换上游线路由服务端处理。</small>
            </label>
          </div>
        </details>

        <div class="error" id="error" role="alert" hidden></div>
        <button class="primary" id="start" type="submit">开始使用</button>
      </form>

      <nav class="secondary">
        <button id="download" type="button">下载原版 App</button>
        <button id="restore" type="button">恢复官方模式</button>
        <button id="key-help" type="button">没有 Key？</button>
      </nav>

      <section class="key-options" id="key-options" hidden>
        <button data-link="free-token"><b>1</b><span>免费 Token<small>查看可免费注册的平台</small></span></button>
        <button data-link="invite"><b>2</b><span>邀请朋友<small>登记第一笔购买奖励</small></span></button>
        <button data-link="our-gateway"><b>3</b><span>我们的中转站<small>获取独立、有限额度 Key</small></span></button>
      </section>

      <footer>
        <p>安装后，日常只需点击 ${productName}；线路配置会在后台完成。</p>
        <p>本工具不包含、不修改也不冒充 Anthropic 或 OpenAI 官方 App。</p>
      </footer>
    </section>
  </main>
`;

const byId = (id) => document.getElementById(id);
const status = byId("status");
const errorBox = byId("error");
const secretInput = byId("secret");
const endpointInput = byId("endpoint");
const modelInput = byId("model");
const startButton = byId("start");
let officialAppRunning = false;

endpointInput.value = defaultGateway;
modelInput.value = defaultModel;

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
    `原版 ${officialName} 正在运行。切换线路需要正常退出并重新打开，请先保存正在进行的任务。现在继续吗？`,
  );
}

async function bootstrap() {
  try {
    const data = await invoke("launcher_status");
    officialAppRunning = data.official_app_running;
    status.classList.toggle("ready", data.official_app_installed);
    status.innerHTML = data.official_app_installed
      ? `<span class="dot"></span><div><strong>原版 ${officialName} 已安装</strong><small>${data.official_app_version || "可以开始使用"}</small></div>`
      : `<span class="dot"></span><div><strong>尚未安装原版 ${officialName}</strong><small>先点“下载原版 App”，安装后再回来</small></div>`;
    if (data.has_saved_secret) {
      secretInput.required = false;
      secretInput.placeholder = "Key 已保存；不更换可留空";
    }
    if (!endpointInput.value && data.endpoint) endpointInput.value = data.endpoint;
    if (!modelInput.value && data.model) modelInput.value = data.model;
  } catch (error) {
    showMessage(errorText(error));
  }
}

byId("setup-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showMessage();
  if (!confirmRestartIfNeeded()) return;
  startButton.disabled = true;
  startButton.textContent = "正在检查线路…";
  try {
    const endpoint = endpointInput.value.trim().replace(/\/+$/, "");
    const model = modelInput.value.trim();
    const secret = secretInput.value.trim();
    if (!endpoint || !model) {
      byId("advanced").open = true;
      throw new Error("发行包还没有预设 API 地址或模型，请在高级设置中填写。");
    }
    await invoke("configure_and_launch", { request: { endpoint, model, secret } });
    secretInput.value = "";
    secretInput.required = false;
    secretInput.placeholder = "Key 已保存；不更换可留空";
    officialAppRunning = true;
    startButton.textContent = `已打开 ${officialName}`;
    showMessage("配置完成。以后直接从这个图标进入即可。", "success");
  } catch (error) {
    showMessage(errorText(error));
    startButton.textContent = "开始使用";
  } finally {
    startButton.disabled = false;
  }
});

byId("download").addEventListener("click", async () => {
  try {
    await invoke("open_allowed_link", { target: "official-download" });
  } catch (error) {
    showMessage(errorText(error));
  }
});

byId("discover-models").addEventListener("click", async () => {
  showMessage();
  const button = byId("discover-models");
  button.disabled = true;
  button.textContent = "获取中…";
  try {
    const endpoint = endpointInput.value.trim().replace(/\/+$/, "");
    const secret = secretInput.value.trim();
    if (!endpoint) throw new Error("请先填写 API 地址。");
    const models = await invoke("discover_models", { request: { endpoint, secret } });
    byId("model-options").replaceChildren(
      ...models.map((name) => {
        const option = document.createElement("option");
        option.value = name;
        return option;
      }),
    );
    if (!modelInput.value && models.length) modelInput.value = models[0];
    showMessage(`已获取 ${models.length} 个不重复模型。`, "success");
  } catch (error) {
    showMessage(errorText(error));
  } finally {
    button.disabled = false;
    button.textContent = "获取列表";
  }
});

byId("restore").addEventListener("click", async () => {
  try {
    if (!confirmRestartIfNeeded()) return;
    const restored = await invoke("restore_official_mode");
    if (restored) officialAppRunning = true;
    showMessage(
      restored ? `已恢复安装前的官方配置，并重新打开 ${officialName}。` : "没有找到需要恢复的备份。",
      "success",
    );
  } catch (error) {
    showMessage(errorText(error));
  }
});

byId("key-help").addEventListener("click", () => {
  byId("key-options").hidden = !byId("key-options").hidden;
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

void bootstrap();
