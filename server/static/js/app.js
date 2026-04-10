let syncPollTimer = null;
let selectedDeviceSerial = "";
let pendingAdbDeviceSelection = false;
let activeWifiSyncMode = "";
let activeWifiSyncPhase = "";
let lastWifiLogSnapshot = "";
let wifiStoppingSinceMs = 0;
let wifiStatusErrorStreak = 0;
let localWifiSyncLogs = [];
let lastPhoneConnected = false;
let preferredConnType = "wifi";
let userSelectedConnType = false;
let activeAdbSyncRunning = false;
let activeAdbSyncMode = "";

const STOPPING_UI_TIMEOUT_MS = 4000;
const WIFI_STATUS_ERROR_RESET_THRESHOLD = 3;

const THEME_STORAGE_KEY = "photosync_theme";

function getEffectiveTheme() {
    const explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "dark" || explicit === "light") return explicit;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light";
}

function setTheme(theme) {
    if (theme === "dark" || theme === "light") {
        document.documentElement.setAttribute("data-theme", theme);
    } else {
        document.documentElement.removeAttribute("data-theme");
    }
}

function updateThemeToggleLabel() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    const effective = getEffectiveTheme();
    btn.textContent = effective === "dark" ? "浅色模式" : "深色模式";
}

function initThemeToggle() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;

    try {
        const saved = localStorage.getItem(THEME_STORAGE_KEY);
        if (saved === "dark" || saved === "light") {
            setTheme(saved);
        }
    } catch {
        // ignore storage errors
    }

    updateThemeToggleLabel();

    btn.addEventListener("click", () => {
        const current = getEffectiveTheme();
        const next = current === "dark" ? "light" : "dark";
        setTheme(next);
        try {
            localStorage.setItem(THEME_STORAGE_KEY, next);
        } catch {
            // ignore storage errors
        }
        // 同步保存到服务端配置，确保“重启服务”后也能记住
        try {
            const fd = new FormData();
            fd.append("theme", next);
            fetch("/api/settings/theme", { method: "POST", body: fd });
        } catch {
            // ignore network errors
        }
        updateThemeToggleLabel();
    });

    const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    if (!mq) return;

    const onChange = () => {
        // 只有在用户未显式选择主题时，才随系统变化更新按钮文案
        const explicit = document.documentElement.getAttribute("data-theme");
        if (explicit === "dark" || explicit === "light") return;
        updateThemeToggleLabel();
    };

    if (typeof mq.addEventListener === "function") {
        mq.addEventListener("change", onChange);
    } else if (typeof mq.addListener === "function") {
        mq.addListener(onChange);
    }
}

function addWifiSyncLogMessage(msg) {
    const timestamp = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const fullMsg = `[${timestamp}] ${msg}`;
    localWifiSyncLogs.push(fullMsg);
    const logEl = document.getElementById("wifi-sync-log");
    if (logEl) {
        if (logEl.textContent === "等待同步日志..." || logEl.textContent === "") {
            logEl.textContent = fullMsg;
        } else {
            logEl.textContent += "\n" + fullMsg;
        }
        logEl.scrollTop = logEl.scrollHeight;
    }
}

async function fetchJSON(url, options) {
    const resp = await fetch(url, options);
    return resp.json();
}

// ─── 手机连接状态 ──────────────────────────────────
async function loadConnectionStatus() {
    try {
        const data = await fetchJSON("/api/wifi/status");

        const statusEl = document.getElementById("connection-status");
        const methodEl = document.getElementById("connection-method");

        const selectedMethod = preferredConnType === "adb" ? "USB ADB" : "WiFi 局域网";

        if (data.connected) {
            lastPhoneConnected = true;
            statusEl.textContent = "已连接";
            statusEl.style.color = "green";

            // 连接方式显示只与 Web 当前选择有关。
            methodEl.textContent = selectedMethod;
        } else {
            lastPhoneConnected = false;
            statusEl.textContent = "未连接";
            statusEl.style.color = "gray";
            // 未连接时也展示“当前选择的连接方式”，避免与设置面板不一致。
            methodEl.textContent = selectedMethod;
        }

        // 更新最近同步的照片列表
        const recentPhotos = data.recent_photos || [];
        const recentListEl = document.getElementById("recent-photos-list");
        if (recentPhotos.length > 0) {
            recentListEl.innerHTML = recentPhotos.map(p =>
                `<span class="recent-photo-item">${p}</span>`
            ).join("");
            // 更新本次同步计数
            document.getElementById("sync-count").textContent = data.synced || 0;
        } else if (!data.running && data.phase !== "syncing") {
            recentListEl.innerHTML = '<div class="empty-state"><p class="hint">同步开始后显示</p></div>';
            document.getElementById("sync-count").textContent = "0";
        }
    } catch (e) {
        console.error("加载连接状态失败:", e);
    }
}

function applyConnectionTypeUi(connType, opts = {}) {
    const btnWifi = document.getElementById("btn-wifi");
    const btnAdb = document.getElementById("btn-adb");

    if (!btnWifi || !btnAdb) return;

    setUsbControlsEnabled((connType || "").toString().toLowerCase() === "adb");

    if (opts.clearFailed) {
        btnWifi.classList.remove("btn-failed");
        btnAdb.classList.remove("btn-failed");
    }

    btnWifi.classList.remove("btn-success");
    btnAdb.classList.remove("btn-success");

    const t = (connType || "wifi").toString().toLowerCase() === "adb" ? "adb" : "wifi";
    if (t === "adb") {
        btnAdb.classList.add("btn-success");
        refreshDevices();
    } else {
        btnWifi.classList.add("btn-success");
    }
}

function setUsbControlsEnabled(enabled) {
    const usbSettings = document.getElementById("usb-settings");
    if (!usbSettings) return;
    const selectEl = usbSettings.querySelector("#adb-device-select");
    const buttons = usbSettings.querySelectorAll("button");
    if (selectEl) {
        selectEl.disabled = !enabled;
    }
    buttons.forEach((b) => {
        b.disabled = !enabled;
    });
}

async function setupAdbReverseForSerial(deviceSerial, opts = {}) {
    const silent = Boolean(opts.silent);
    if (!deviceSerial) {
        if (!silent) alert("请先选择设备");
        return;
    }

    const statusEl = document.getElementById("adb-reverse-status");
    if (statusEl) {
        statusEl.textContent = "正在设置...";
        statusEl.style.color = "#6B7280";
    }
    try {
        const fd = new FormData();
        fd.append("serial", deviceSerial);
        const data = await fetchJSON("/api/adb/setup-reverse", { method: "POST", body: fd });
        if (statusEl) {
            if (data.status === "ok") {
                statusEl.textContent = "✓ " + data.message;
                statusEl.style.color = "green";
            } else {
                statusEl.textContent = "✗ " + data.message;
                statusEl.style.color = "red";
            }
        }
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = "✗ 设置失败";
            statusEl.style.color = "red";
        }
    }
}

async function persistPreferredConnectionType(connType) {
    const t = (connType || "").toString().toLowerCase();
    if (t !== "wifi" && t !== "adb") return;
    try {
        const fd = new FormData();
        fd.append("conn_type", t);
        await fetch("/api/settings/connection", { method: "POST", body: fd });
    } catch {
        // ignore network errors
    }
}

function updateSyncActionButtons() {
    const btnInc = document.getElementById("btn-sync-incremental");
    const btnFull = document.getElementById("btn-sync-full");
    if (!btnInc || !btnFull) return;

    maybeRecoverFromStaleWifiStopping();

    btnInc.classList.remove("btn-stop");
    btnFull.classList.remove("btn-stop");
    btnInc.textContent = "增量同步";
    btnFull.textContent = "全量同步";
    btnInc.disabled = false;
    btnFull.disabled = false;

    // ADB 同步运行时：按钮进入“停止”态（与 WiFi 状态机独立）。
    if (activeAdbSyncRunning) {
        const mode = activeAdbSyncMode === "full" ? "full" : "incremental";
        if (mode === "full") {
            btnFull.textContent = "停止同步";
            btnFull.classList.add("btn-stop");
            btnInc.disabled = true;
            btnInc.textContent = "当前：全量";
        } else {
            btnInc.textContent = "停止同步";
            btnInc.classList.add("btn-stop");
            btnFull.disabled = true;
            btnFull.textContent = "当前：增量";
        }
        return;
    }

    const hasPendingOrRunning =
        activeWifiSyncPhase === "requested" ||
        activeWifiSyncPhase === "scanning" ||
        activeWifiSyncPhase === "syncing" ||
        activeWifiSyncPhase === "preparing_full" ||
        activeWifiSyncPhase === "stopping";
    if (!hasPendingOrRunning) return;

    if (activeWifiSyncMode === "full") {
        btnFull.textContent = "停止同步";
        btnFull.classList.add("btn-stop");
        btnInc.disabled = true;
        btnInc.textContent = "当前：全量";
    } else {
        btnInc.textContent = "停止同步";
        btnInc.classList.add("btn-stop");
        btnFull.disabled = true;
        btnFull.textContent = "当前：增量";
    }
}

function maybeRecoverFromStaleWifiStopping() {
    if (activeWifiSyncPhase !== "stopping") return;
    if (!wifiStoppingSinceMs) return;

    const elapsed = Date.now() - wifiStoppingSinceMs;
    if (elapsed < STOPPING_UI_TIMEOUT_MS) return;

    resetWifiSyncUiState();
}

function markWifiStoppingIfNeeded() {
    if (activeWifiSyncPhase === "stopping") {
        if (!wifiStoppingSinceMs) {
            wifiStoppingSinceMs = Date.now();
        }
    } else {
        wifiStoppingSinceMs = 0;
    }
}

function resetWifiSyncUiState() {
    activeWifiSyncMode = "";
    activeWifiSyncPhase = "";
    wifiStoppingSinceMs = 0;
}

function normalizeWifiMode(raw) {
    return raw === "full" ? "full" : "incremental";
}

function getEffectiveWifiMode(status) {
    const phase = status.phase || "";
    const syncMode = status.sync_mode;
    const requestedMode = status.requested_sync_mode;

    // 运行态优先使用手机端实际上报的 sync_mode；请求态再看 requested_sync_mode。
    if (phase === "scanning" || phase === "syncing" || status.running) {
        if (syncMode === "full" || syncMode === "incremental") {
            return normalizeWifiMode(syncMode);
        }
    }
    if (phase === "requested" || phase === "preparing_full" || phase === "stopping") {
        if (requestedMode === "full" || requestedMode === "incremental") {
            return normalizeWifiMode(requestedMode);
        }
    }

    if (syncMode === "full" || syncMode === "incremental") {
        return normalizeWifiMode(syncMode);
    }
    if (requestedMode === "full" || requestedMode === "incremental") {
        return normalizeWifiMode(requestedMode);
    }
    return "";
}

async function requestSync(mode = "incremental") {
    // 获取当前选中的连接方式（WiFi 还是 ADB）
    const btnWifi = document.getElementById("btn-wifi");
    const btnAdb = document.getElementById("btn-adb");
    let connType = "wifi"; // 默认 WiFi

    if (btnAdb?.classList.contains("btn-success")) {
        connType = "adb";
    } else if (btnWifi?.classList.contains("btn-success")) {
        connType = "wifi";
    }

    const normalizedMode = mode === "full" ? "full" : "incremental";
    const btnInc = document.getElementById("btn-sync-incremental");
    const btnFull = document.getElementById("btn-sync-full");

    // ADB 模式：走 USB 同步接口（不再误发 WiFi 同步请求）。
    if (connType === "adb") {
        try {
            const adbStatus = await fetchJSON("/api/adb/status");
            if (adbStatus && adbStatus.running) {
                const stopData = await fetchJSON("/api/adb/stop", { method: "POST" });
                addWifiSyncLogMessage(stopData.message || "已发送停止请求");
                return;
            }
        } catch {
            // ignore status errors
        }

        btnInc.disabled = true;
        btnFull.disabled = true;
        if (normalizedMode === "full") {
            btnFull.textContent = "准备全量...";
        } else {
            btnInc.textContent = "发送请求...";
        }

        try {
            const fd = new FormData();
            if (selectedDeviceSerial) {
                fd.append("serial", selectedDeviceSerial);
            }
            const adbData = await fetchJSON("/api/adb/sync", { method: "POST", body: fd });

            if (adbData.status === "ok") {
                activeAdbSyncRunning = true;
                activeAdbSyncMode = normalizedMode;
                updateSyncActionButtons();
                addWifiSyncLogMessage(adbData.message || "已开始 ADB 同步");
            } else {
                addWifiSyncLogMessage(adbData.message || "ADB 同步请求失败");
            }
        } catch (e) {
            addWifiSyncLogMessage("ADB 同步请求失败: " + e.message);
        } finally {
            updateSyncActionButtons();
        }
        return;
    }

    // 当前模式已处于请求中/同步中，再点则尝试停止（请求中可取消）
    if (activeWifiSyncMode === normalizedMode &&
        (activeWifiSyncPhase === "requested" || activeWifiSyncPhase === "scanning" || activeWifiSyncPhase === "syncing" || activeWifiSyncPhase === "preparing_full" || activeWifiSyncPhase === "stopping")) {
        if (activeWifiSyncPhase === "requested" || activeWifiSyncPhase === "preparing_full") {
            try {
                const data = await fetchJSON("/api/wifi/request-stop", { method: "POST" });
                addWifiSyncLogMessage(data.message || "已发送停止请求");
            } catch (e) {
                addWifiSyncLogMessage("停止失败: " + e.message);
            }
            return;
        }

        if (activeWifiSyncPhase === "stopping") {
            addWifiSyncLogMessage("正在停止中，请稍候...");
            return;
        }

        try {
            const data = await fetchJSON("/api/wifi/request-stop", { method: "POST" });
            addWifiSyncLogMessage(data.message || "已发送停止请求");
        } catch (e) {
            addWifiSyncLogMessage("停止失败: " + e.message);
        }
        return;
    }

    btnInc.disabled = true;
    btnFull.disabled = true;
    if (normalizedMode === "full") {
        btnFull.textContent = "准备全量...";
    } else {
        btnInc.textContent = "发送请求...";
    }

    try {
        const syncFd = new FormData();
        syncFd.append("conn_type", connType);
        syncFd.append("sync_mode", normalizedMode);
        const syncData = await fetchJSON("/api/wifi/request-sync", { method: "POST", body: syncFd });

        if (syncData.status === "ok") {
            activeWifiSyncMode = normalizedMode;
            activeWifiSyncPhase = normalizedMode === "full" ? "preparing_full" : "requested";
            updateSyncActionButtons();
            addWifiSyncLogMessage(syncData.message || "已发送同步请求");
        } else {
            addWifiSyncLogMessage(syncData.message || "请求失败");
        }
    } catch (e) {
        addWifiSyncLogMessage("请求失败: " + e.message);
    } finally {
        updateSyncActionButtons();
    }
}

// ─── 服务器状态 ──────────────────────────────────
async function loadStatus() {
    try {
        const data = await fetchJSON("/api/status");
        document.getElementById("server-url").textContent = data.server_url;

        // 这两个元素已被删除，添加 null 检查
        const totalSyncedEl = document.getElementById("total-synced");
        if (totalSyncedEl) totalSyncedEl.textContent = data.total_synced;

        const lastSyncEl = document.getElementById("last-sync");
        if (lastSyncEl) lastSyncEl.textContent = data.last_sync ? new Date(data.last_sync).toLocaleString("zh-CN") : "从未";

        // 更新同步状态面板的电脑端照片数量
        document.getElementById("sync-pc-total").textContent = data.total_synced;

        // 显示备用地址（多网卡时）
        const altUrls = data.all_urls || [];
        const altArea = document.getElementById("alt-urls");
        const altList = document.getElementById("alt-urls-list");
        if (altArea && altList) {
            if (altUrls.length > 1) {
                altArea.style.display = "";
                altList.innerHTML = altUrls.slice(1).map(u =>
                    `<span class="alt-url" onclick="switchUrl('${u}')" title="点击切换">${u}</span>`
                ).join("");
            } else {
                altArea.style.display = "none";
            }
        }

        // 存储路径
        const pathInput = document.getElementById("storage-path-input");
        if (!pathInput._userEdited) pathInput.value = data.storage_path;

        // 服务器端口
        const portInput = document.getElementById("server-port-input");
        const currentPort = data.server_port || 8920;
        if (!portInput._userEdited) {
            portInput.value = currentPort;
        }

        // ADB 路径（已移除，使用内置 ADB）

        // 连接方式
        const connType = data.connection_type || "wifi";
        const serverPreferred = (connType || "wifi").toString().toLowerCase() === "adb" ? "adb" : "wifi";
        // 如果用户已经手动选过，就不要再被轮询覆盖；否则使用服务端记忆的偏好。
        if (!userSelectedConnType) {
            preferredConnType = serverPreferred;
        }
        applyConnectionTypeUi(preferredConnType);
        updateConnectionUI(preferredConnType);
    } catch (e) {
        console.error("加载状态失败:", e);
    }
}

// ─── 照片列表 ──────────────────────────────────
async function loadPhotos() {
    try {
        const data = await fetchJSON("/api/skipped?per_page=200");
        document.getElementById("photo-count").textContent = data.total;
        const grid = document.getElementById("photo-grid");

        if (data.total === 0) {
            grid.innerHTML = `<div class="empty-state"><p>暂无跳过的照片</p><p class="hint">仅显示本次同步产生的跳过</p></div>`;
            return;
        }
        grid.innerHTML = data.photos.map(photo => {
            return `<div class="photo-item"><div class="photo-name" title="${photo.name}">${photo.name}</div></div>`;
        }).join("");
    } catch (e) {
        console.error("加载照片失败:", e);
    }
}

// ─── 复制地址 ──────────────────────────────────
function copyAddress() {
    const url = document.getElementById("server-url").textContent;
    if (!url || url === "加载中...") {
        alert("地址加载中，请稍候");
        return;
    }

    // 使用传统的复制方法，兼容 HTTP 环境
    const textarea = document.createElement("textarea");
    textarea.value = url;
    document.body.appendChild(textarea);
    textarea.select();
    try {
        document.execCommand("copy");
        const toast = document.getElementById("copy-toast");
        toast.classList.add("show");
        setTimeout(() => toast.classList.remove("show"), 2000);
        console.log("已复制:", url);
    } catch (err) {
        console.error("复制失败:", err);
        alert("复制失败: " + err.message);
    } finally {
        document.body.removeChild(textarea);
    }
}

function switchUrl(url) {
    document.getElementById("server-url").textContent = url;

    // 使用传统的复制方法，兼容 HTTP 环境
    const textarea = document.createElement("textarea");
    textarea.value = url;
    document.body.appendChild(textarea);
    textarea.select();
    try {
        document.execCommand("copy");
        const toast = document.getElementById("copy-toast");
        toast.textContent = "已切换并复制: " + url;
        toast.classList.add("show");
        setTimeout(() => {
            toast.classList.remove("show");
            toast.textContent = "已复制到剪贴板";
        }, 2000);
    } catch (err) {
        console.error("复制失败:", err);
    } finally {
        document.body.removeChild(textarea);
    }
}

// ─── 设置 ──────────────────────────────────
async function browseFolder() {
    try {
        const data = await fetchJSON("/api/settings/browse", { method: "POST" });
        if (data.status === "ok" && data.path) {
            document.getElementById("storage-path-input").value = data.path;
            document.getElementById("storage-path-input")._userEdited = true;
        } else if (data.status === "cancelled") {
            // 用户取消了选择，不做任何事
        } else {
            // 浏览失败，提示手动输入
            alert("无法打开文件夹选择器，请手动输入路径");
        }
    } catch (e) {
        alert("浏览文件夹失败，请手动输入路径");
    }
}

async function browseAdbFolder() {
    try {
        const data = await fetchJSON("/api/settings/browse", { method: "POST" });
        if (data.status === "ok" && data.path) {
            document.getElementById("adb-path-input").value = data.path;
        } else if (data.status === "cancelled") {
            // 用户取消了选择，不做任何事
        } else {
            alert("无法打开文件夹选择器，请手动输入路径");
        }
    } catch (e) {
        alert("浏览文件夹失败，请手动输入路径");
    }
}

async function savePath() {
    const path = document.getElementById("storage-path-input").value.trim();
    if (!path) {
        addWifiSyncLogMessage("请输入有效路径");
        return;
    }
    try {
        const buildForm = (confirmCreate) => {
            const form = new FormData();
            form.append("path", path);
            if (confirmCreate) {
                form.append("confirm_create", "true");
            }
            return form;
        };

        let data = await fetchJSON("/api/settings/storage", {
            method: "POST",
            body: buildForm(false),
        });

        if (data.status === "need_confirm") {
            const ok = confirm(data.message || `路径不存在，是否创建？\n${path}`);
            if (!ok) {
                return;
            }
            data = await fetchJSON("/api/settings/storage", {
                method: "POST",
                body: buildForm(true),
            });
        }

        alert(data.message || "保存成功");
        document.getElementById("storage-path-input")._userEdited = false;
        loadStatus();
    } catch (e) {
        alert("保存失败");
    }
}

async function savePort() {
    const port = parseInt(document.getElementById("server-port-input").value);
    if (!port || port < 1024 || port > 65535) {
        return alert("请输入有效端口号 (1024-65535)");
    }
    try {
        const fd = new FormData();
        fd.append("port", port.toString());
        const data = await fetchJSON("/api/settings/port", { method: "POST", body: fd });
        addWifiSyncLogMessage(data.message || (data.status === "ok" ? "保存成功" : "保存失败"));
        document.getElementById("server-port-input")._userEdited = false;
        loadStatus();
    } catch (e) {
        alert("保存失败: " + e.message);
    }
}

// ─── 本地数据库扫描 ──────────────────────────────────
let scanPollTimer = null;

async function scanLocalDatabase() {
    const statusEl = document.getElementById("scan-status");
    const btn = event.target;

    btn.disabled = true;
    btn.textContent = "扫描中...";
    statusEl.textContent = "开始扫描...";
    statusEl.style.color = "#6B7280";

    try {
        const data = await fetchJSON("/api/settings/scan-local", { method: "POST" });
        if (data.status === "ok") {
            statusEl.textContent = "扫描中...";
            statusEl.style.color = "#6B7280";
            // 开始轮询扫描状态
            scanPollTimer = setInterval(async () => {
                const statusData = await fetchJSON("/api/settings/scan-status");
                if (statusData.running) {
                    statusEl.textContent = `已扫描: ${statusData.scanned}/${statusData.total}`;
                } else {
                    clearInterval(scanPollTimer);
                    btn.disabled = false;
                    btn.textContent = "刷新数据库";
                    // 获取数据库总数
                    const status = await fetchJSON("/api/status");
                    const detail = statusData.current ? ` | ${statusData.current}` : "";
                    statusEl.textContent = `数据库总计: ${status.total_synced} 个文件${detail}`;
                    statusEl.style.color = "green";
                    loadStatus(); // 刷新显示的照片数量
                }
            }, 1000);
        } else {
            statusEl.textContent = data.message || "扫描失败";
            statusEl.style.color = "red";
            btn.disabled = false;
            btn.textContent = "刷新数据库";
        }
    } catch (e) {
        statusEl.textContent = "扫描失败";
        statusEl.style.color = "red";
        btn.disabled = false;
        btn.textContent = "刷新数据库";
    }
}

async function saveAdbPath() {
    const path = document.getElementById("adb-path-input").value.trim();
    try {
        const fd = new FormData();
        fd.append("path", path);
        const data = await fetchJSON("/api/settings/adb_path", { method: "POST", body: fd });
        alert(data.message || "保存成功");
        loadStatus();
    } catch (e) {
        alert("保存失败");
    }
}

// ─── USB 设备选择 ──────────────────────────────────
async function refreshDevices() {
    const selectEl = document.getElementById("adb-device-select");
    try {
        const data = await fetchJSON("/api/status");
        const devices = data.all_adb_devices || [];

        selectEl.innerHTML = '<option value="">-- 选择设备 --</option>';
        for (const dev of devices) {
            const label = dev.model ? `${dev.model} (${dev.serial})` : dev.serial;
            const option = document.createElement("option");
            option.value = dev.serial;
            option.textContent = label;
            if (dev.serial === selectedDeviceSerial) {
                option.selected = true;
            }
            selectEl.appendChild(option);
        }

        if (devices.length === 0) {
            selectEl.innerHTML = '<option value="">-- 未检测到设备 --</option>';
        }
    } catch (e) {
        console.error("刷新设备失败:", e);
    }
}

function onDeviceSelected() {
    const selectEl = document.getElementById("adb-device-select");
    selectedDeviceSerial = selectEl.value;
    console.log("选中设备:", selectedDeviceSerial);

    // 仅在“点击了 ADB 测试按钮后等待用户选择设备”的场景下，才自动触发连接测试。
    if (pendingAdbDeviceSelection && selectedDeviceSerial) {
        pendingAdbDeviceSelection = false;
        setupAdbReverseForSerial(selectedDeviceSerial, { silent: true }).finally(() => {
            performConnectionTest("adb", selectedDeviceSerial);
        });
    }
}

// ─── 连接方式选择 ──────────────────────────────────
async function onConnectionTypeChanged() {
    const selectEl = document.getElementById("connection-type-select");
    const connType = selectEl.value;
    const statusEl = document.getElementById("connection-type-status");
    const usbSettings = document.getElementById("usb-settings");

    try {
        const fd = new FormData();
        fd.append("conn_type", connType);
        const data = await fetchJSON("/api/settings/connection", { method: "POST", body: fd });

        if (data.status === "ok") {
            statusEl.textContent = "✓ " + data.message;
            statusEl.style.color = "green";
            // 根据连接类型显示/隐藏 USB 设置
            usbSettings.style.display = connType === "adb" ? "flex" : "none";
            // 切换到 ADB 时自动刷新设备列表
            if (connType === "adb") {
                await refreshDevices();
            }
        } else {
            statusEl.textContent = "✗ " + data.message;
            statusEl.style.color = "red";
        }
    } catch (e) {
        statusEl.textContent = "✗ 设置失败";
        statusEl.style.color = "red";
    }
}

async function testConnection() {
    const connType = document.getElementById("connection-type-select").value;
    const statusEl = document.getElementById("connection-test-status");
    const deviceSerial = document.getElementById("adb-device-select")?.value || "";

    statusEl.textContent = "测试中...";
    statusEl.style.color = "#6B7280";

    try {
        const fd = new FormData();
        fd.append("conn_type", connType);
        fd.append("device_serial", deviceSerial);
        const data = await fetchJSON("/api/test-connection", { method: "POST", body: fd });

        if (data.status === "ok") {
            statusEl.textContent = "✓ " + data.message;
            statusEl.style.color = "green";
        } else {
            statusEl.textContent = "✗ " + data.message;
            statusEl.style.color = "red";
        }
    } catch (e) {
        statusEl.textContent = "✗ 测试失败";
        statusEl.style.color = "red";
    }
}

function updateConnectionUI(connType) {
    const selectEl = document.getElementById("connection-type-select");

    if (selectEl) {
        selectEl.value = connType;
    }
    setUsbControlsEnabled((connType || "").toString().toLowerCase() === "adb");
    // 如果是 ADB 模式，刷新设备列表
    if ((connType || "").toString().toLowerCase() === "adb") {
        refreshDevices();
    }
}

async function setupAdbReverse() {
    const selectEl = document.getElementById("adb-device-select");
    const deviceSerial = selectEl.value;
    await setupAdbReverseForSerial(deviceSerial);
}

// 删除旧的扫描代码，避免重复

// ─── 同步状态轮询 ──────────────────────────────────
function startSyncPoll() {
    if (syncPollTimer) clearInterval(syncPollTimer);
    pollSyncStatus();  // 立即执行一次
    syncPollTimer = setInterval(pollSyncStatus, 1000);
}

async function pollSyncStatus() {
    try {
        const [wifiStatus, adbStatus, serverStatus, refreshStatus] = await Promise.all([
            fetchJSON("/api/wifi/status"),
            fetchJSON("/api/adb/status"),
            fetchJSON("/api/status"),
            fetchJSON("/api/settings/scan-status")
        ]);

        activeAdbSyncRunning = Boolean(adbStatus && adbStatus.running);

        wifiStatusErrorStreak = 0;

        const syncDot = document.getElementById("sync-dot");
        const syncStatusText = document.getElementById("sync-status-text");
        const syncProgressArea = document.getElementById("sync-progress-area");

        activeWifiSyncPhase = wifiStatus.phase || "";
        activeWifiSyncMode = getEffectiveWifiMode(wifiStatus);
        markWifiStoppingIfNeeded();

        if (!wifiStatus.running && (activeWifiSyncPhase === "" || activeWifiSyncPhase === "done")) {
            resetWifiSyncUiState();
        }

        updateSyncActionButtons();
        renderWifiSyncLog(wifiStatus.phone_log || wifiStatus.log || []);

        // 始终更新电脑端照片数量
        document.getElementById("sync-pc-total").textContent = serverStatus.total_synced || 0;
        const effectiveMode = getEffectiveWifiMode(wifiStatus);
        const modeText = effectiveMode === "full"
            ? "全量"
            : (effectiveMode === "incremental" ? "增量" : "-");
        document.getElementById("sync-mode").textContent = modeText;

        // 判断当前同步状态
        let s = null;
        let isRunning = false;
        let syncSource = "";

        const wifiLinkType = preferredConnType === "adb" ? "ADB" : "WiFi";

        if (adbStatus && adbStatus.running) {
            s = adbStatus;
            isRunning = true;
            syncSource = "ADB";
        } else if (wifiStatus.phase === "preparing_full") {
            s = {
                ...wifiStatus,
                phone_total: refreshStatus.total || 0,
                need_sync: refreshStatus.total || 0,
                synced: refreshStatus.scanned || 0,
                current: refreshStatus.current || wifiStatus.current || "正在刷新数据库...",
            };
            isRunning = true;
            syncSource = wifiLinkType;
        } else if (wifiStatus.running) {
            s = wifiStatus;
            isRunning = true;
            syncSource = wifiLinkType;
        } else if (wifiStatus.phase === "requested") {
            s = wifiStatus;
            isRunning = false;
            syncSource = wifiLinkType;
        } else if (adbStatus && adbStatus.phase === "done" && adbStatus.phone_total > 0) {
            // ADB 同步刚完成，显示最终结果
            s = adbStatus;
            isRunning = false;
            syncSource = "ADB";
        } else if (wifiStatus.phase === "done" && wifiStatus.phone_total > 0) {
            // WiFi 同步刚完成，显示最终结果
            s = wifiStatus;
            isRunning = false;
            syncSource = wifiLinkType;
        }

        if (s) {
            if (s.phase === "preparing_full") {
                syncDot.className = "dot yellow";
                syncStatusText.textContent = `全量准备中(${syncSource})`;
            } else if (s.phase === "requested") {
                syncDot.className = "dot gray";
                syncStatusText.textContent = `等待手机响应(${syncSource})`;
            } else if (isRunning) {
                syncDot.className = "dot green";
                syncStatusText.textContent = `同步中(${syncSource}): ${s.device || "未知设备"}`;
            } else {
                syncDot.className = "dot gray";
                syncStatusText.textContent = `同步完成(${syncSource}): ${s.device || "未知设备"}`;
            }
            syncProgressArea.style.display = "";

            // 动态显示速度单位 (>=1MB显示MB/s，否则显示KB/s)
            function formatSpeed(mbSpeed, running = false, done = 0) {
                const v = Number(mbSpeed || 0);
                if (v >= 1) {
                    return v.toFixed(1) + " MB/s";
                } else if (v > 0) {
                    return (v * 1024).toFixed(0) + " KB/s";
                }
                if (!running) return "--";
                return done > 0 ? "0 KB/s" : "等待首个文件...";
            }

            // 更新同步统计数据
            document.getElementById("sync-scanned").textContent = s.phone_total || 0;
            document.getElementById("sync-need-sync").textContent = s.phone_total || 0;
            document.getElementById("sync-synced").textContent = s.synced || 0;
            const done = (s.synced || 0) + (s.skipped || 0) + (s.failed || 0);
            document.getElementById("sync-speed").textContent = formatSpeed(s.speed, isRunning, done);

            // 进度条
            const needSync = s.need_sync || 1;
            const pct = needSync > 0 ? Math.round(done * 100 / needSync) : (s.need_sync === 0 ? 100 : 0);
            document.getElementById("sync-progress-bar").style.width = pct + "%";

            // 进度文本
            if (s.phase === "preparing_full") {
                const scanned = s.synced || 0;
                const total = s.need_sync || 0;
                document.getElementById("sync-progress-text").textContent =
                    `正在刷新数据库 ${scanned}/${total} | 当前: ${s.current || "..."}`;
            } else if (s.phase === "requested") {
                document.getElementById("sync-progress-text").textContent =
                    s.current || "已发送同步请求，等待手机开始扫描...";
            } else if (isRunning) {
                document.getElementById("sync-progress-text").textContent =
                    `上传进度: ${done}/${s.need_sync || 0} (${pct}%) | 当前: ${s.current || "..."}`;
            } else {
                document.getElementById("sync-progress-text").textContent =
                    `上传完成: ${done}/${s.need_sync || 0} | ${s.current || "同步完成"}`;
            }
        } else {
            syncDot.className = "dot gray";
            syncStatusText.textContent = "等待同步...";
            syncProgressArea.style.display = "none";
            document.getElementById("sync-scanned").textContent = "-";
            document.getElementById("sync-need-sync").textContent = "-";
            document.getElementById("sync-synced").textContent = "-";
            document.getElementById("sync-mode").textContent = "-";
            document.getElementById("sync-speed").textContent = "--";
        }
    } catch (e) {
        console.error("轮询状态失败:", e);
        wifiStatusErrorStreak += 1;
        if (wifiStatusErrorStreak >= WIFI_STATUS_ERROR_RESET_THRESHOLD) {
            resetWifiSyncUiState();
            updateSyncActionButtons();
        }
    }
}

function renderWifiSyncLog(logs) {
    const logEl = document.getElementById("wifi-sync-log");
    if (!logEl) return;

    if (!Array.isArray(logs) || logs.length === 0) {
        if (lastWifiLogSnapshot !== "__empty__") {
            logEl.textContent = "等待同步日志...";
            lastWifiLogSnapshot = "__empty__";
        }
        return;
    }

    const snapshot = logs.join("\n");
    if (snapshot === lastWifiLogSnapshot) {
        return;
    }

    logEl.textContent = snapshot;
    logEl.scrollTop = logEl.scrollHeight;
    lastWifiLogSnapshot = snapshot;
}

// ─── 测试连接 ──────────────────────────────────
async function testConnectionType(type) {
    const btnWifi = document.getElementById("btn-wifi");
    const btnAdb = document.getElementById("btn-adb");
    const statusEl = document.getElementById("connection-test-status");

    // 用户点击即锁定选择：除非用户再点另一个，否则永远不要自动改回。
    preferredConnType = (type || "wifi").toString().toLowerCase() === "adb" ? "adb" : "wifi";
    userSelectedConnType = true;
    applyConnectionTypeUi(preferredConnType, { clearFailed: true });
    // 立即持久化偏好，避免定时刷新把按钮刷回旧值。
    persistPreferredConnectionType(preferredConnType);

    // 如果是 ADB，先弹出设备选择
    if (type === "adb") {
        await showAdbDeviceSelector();
        return;
    }

    // WiFi 模式直接测试
    performConnectionTest(type, "");
}

async function showAdbDeviceSelector() {
    const statusEl = document.getElementById("connection-test-status");
    statusEl.textContent = "正在刷新设备列表...";
    statusEl.style.color = "#6b7280";

    setUsbControlsEnabled(true);

    const selectEl = document.getElementById("adb-device-select");
    if (selectEl) {
        selectEl.innerHTML = '<option value="">-- 选择设备 --</option>';
    }

    try {
        // 调用刷新设备列表
        const resp = await fetch("/api/adb/devices");
        const data = await resp.json();

        if (!data.devices || data.devices.length === 0) {
            statusEl.textContent = "✗ 未找到设备，请确保手机已连接";
            statusEl.style.color = "#ef4444";
            pendingAdbDeviceSelection = false;
            if (selectEl) {
                selectEl.innerHTML = '<option value="">-- 未检测到设备 --</option>';
            }
            return;
        }

        // 如果只有一个设备，直接使用
        if (data.devices.length === 1) {
            const deviceSerial = data.devices[0].serial || data.devices[0];
            pendingAdbDeviceSelection = false;
            if (selectEl) {
                const label = data.devices[0].model
                    ? `${data.devices[0].model} (${deviceSerial})`
                    : deviceSerial;
                selectEl.innerHTML = `<option value="${deviceSerial}">${label}</option>`;
                selectEl.value = deviceSerial;
            }
            selectedDeviceSerial = deviceSerial;
            await setupAdbReverseForSerial(deviceSerial, { silent: true });
            performConnectionTest("adb", deviceSerial);
            return;
        }

        // 多个设备：在页面内联展示下拉选择（不使用 prompt 弹窗）
        pendingAdbDeviceSelection = true;
        statusEl.textContent = "请选择要使用的 ADB 设备（选择后会自动测试连接）";
        statusEl.style.color = "#6b7280";

        if (selectEl) {
            selectEl.innerHTML = '<option value="">-- 选择设备 --</option>';
            for (const dev of data.devices) {
                const serial = dev.serial || dev;
                const label = dev.model ? `${dev.model} (${serial})` : serial;
                const option = document.createElement("option");
                option.value = serial;
                option.textContent = label;
                if (serial === selectedDeviceSerial) {
                    option.selected = true;
                }
                selectEl.appendChild(option);
            }
            selectEl.focus();
        }
    } catch (e) {
        statusEl.textContent = `✗ 获取设备列表失败: ${e.message}`;
        statusEl.style.color = "#ef4444";
        pendingAdbDeviceSelection = false;
    }
}

async function performConnectionTest(type, deviceSerial) {
    const btnWifi = document.getElementById("btn-wifi");
    const btnAdb = document.getElementById("btn-adb");
    const statusEl = document.getElementById("connection-test-status");

    // 重置按钮状态
    btnWifi?.classList.remove("btn-success", "btn-failed");
    btnAdb?.classList.remove("btn-success", "btn-failed");
    statusEl.textContent = "测试连接中...";
    statusEl.style.color = "#6b7280";

    try {
        const fd = new FormData();
        fd.append("conn_type", type);
        fd.append("device_serial", deviceSerial || "");

        const resp = await fetch("/api/test-connection", {
            method: "POST",
            body: fd
        });
        const data = await resp.json();

        if (data.status === "ok") {
            // 测试成功
            const activeBtn = type === "adb" ? btnAdb : btnWifi;
            const inactiveBtn = type === "adb" ? btnWifi : btnAdb;

            // 成功时只高亮选中的那个；不要把另一个强行标红（避免 UI 误导）。
            activeBtn?.classList.remove("btn-failed");
            activeBtn?.classList.add("btn-success");
            inactiveBtn?.classList.remove("btn-success", "btn-failed");

            applyConnectionTypeUi(type);
            await persistPreferredConnectionType(type);

            statusEl.textContent = `✓ ${data.message}`;
            statusEl.style.color = "#22c55e";
        } else {
            // 测试失败
            const btn = type === "adb" ? btnAdb : btnWifi;
            btn?.classList.remove("btn-success");
            btn?.classList.add("btn-failed");

            statusEl.textContent = `✗ ${data.message || "连接失败"}`;
            statusEl.style.color = "#ef4444";
        }
    } catch (e) {
        // 测试异常
        const btn = type === "adb" ? btnAdb : btnWifi;
        btn?.classList.remove("btn-success");
        btn?.classList.add("btn-failed");

        statusEl.textContent = `✗ 连接测试异常: ${e.message}`;
        statusEl.style.color = "#ef4444";
    }
}

// ─── 初始化 ──────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    const pathInput = document.getElementById("storage-path-input");
    if (pathInput) pathInput.addEventListener("input", () => { pathInput._userEdited = true; });

    const portInput = document.getElementById("server-port-input");
    if (portInput) portInput.addEventListener("input", () => { portInput._userEdited = true; });

    initThemeToggle();

    // 首屏从服务端注入的偏好连接方式初始化（重启服务后仍可记住）。
    const injected = (document.documentElement.getAttribute("data-preferred-conn") || "").toLowerCase();
    if (!userSelectedConnType && (injected === "wifi" || injected === "adb")) {
        preferredConnType = injected;
        applyConnectionTypeUi(preferredConnType);
    }
});

loadStatus();
refreshDevices();
loadConnectionStatus();
loadPhotos();
startSyncPoll();
setInterval(() => { loadStatus(); loadConnectionStatus(); loadPhotos(); }, 5000);
