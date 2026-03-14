let syncPollTimer = null;
let selectedDeviceSerial = "";

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

        if (data.connected) {
            statusEl.textContent = "已连接";
            statusEl.style.color = "green";

            const connType = data.connection_type || "wifi";
            methodEl.textContent = connType === "adb" ? "USB ADB" : "WiFi 局域网";
        } else {
            statusEl.textContent = "未连接";
            statusEl.style.color = "gray";
            methodEl.textContent = "-";
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

async function requestSync() {
    const connType = document.getElementById("connection-type-select").value;
    const btnSync = document.getElementById("btn-start-sync");

    // 禁用按钮，防止重复点击
    btnSync.disabled = true;
    btnSync.textContent = "发送请求...";

    try {
        // 直接发送同步请求，不管连接状态
        const syncFd = new FormData();
        syncFd.append("conn_type", connType);
        const syncData = await fetchJSON("/api/wifi/request-sync", { method: "POST", body: syncFd });

        if (syncData.status === "ok") {
            alert("已发送同步请求");
        } else {
            alert(syncData.message || "请求失败");
        }
    } catch (e) {
        alert("请求失败: " + e.message);
    } finally {
        btnSync.disabled = false;
        btnSync.textContent = "开始同步";
    }
}

// ─── 工具函数 ──────────────────────────────────
function formatEta(seconds) {
    if (!seconds || seconds <= 0) return "--";
    if (seconds < 60) return seconds + " 秒";
    if (seconds < 3600) return Math.floor(seconds / 60) + " 分 " + (seconds % 60) + " 秒";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return h + " 时 " + m + " 分";
}

// ─── 服务器状态 ──────────────────────────────────
async function loadStatus() {
    try {
        const data = await fetchJSON("/api/status");
        document.getElementById("server-url").textContent = data.server_url;
        document.getElementById("total-synced").textContent = data.total_synced;
        document.getElementById("last-sync").textContent =
            data.last_sync ? new Date(data.last_sync).toLocaleString("zh-CN") : "从未";

        // 更新同步状态面板的电脑端照片数量
        document.getElementById("sync-pc-total").textContent = data.total_synced;

        // 显示备用地址（多网卡时）
        const altUrls = data.all_urls || [];
        const altArea = document.getElementById("alt-urls");
        const altList = document.getElementById("alt-urls-list");
        if (altUrls.length > 1) {
            altArea.style.display = "";
            altList.innerHTML = altUrls.slice(1).map(u =>
                `<span class="alt-url" onclick="switchUrl('${u}')" title="点击切换">${u}</span>`
            ).join("");
        } else {
            altArea.style.display = "none";
        }

        // 存储路径
        const pathInput = document.getElementById("storage-path-input");
        if (!pathInput._userEdited) pathInput.value = data.storage_path;

        // 服务器端口
        const portInput = document.getElementById("server-port-input");
        if (!portInput._userEdited) portInput.value = data.server_port || 8920;

        // ADB 路径（已移除，使用内置 ADB）

        // 连接方式
        const connType = data.connection_type || "wifi";
        updateConnectionUI(connType);
    } catch (e) {
        console.error("加载状态失败:", e);
    }
}

// ─── 照片列表 ──────────────────────────────────
async function loadPhotos() {
    try {
        const data = await fetchJSON("/api/photos?per_page=200");
        document.getElementById("photo-count").textContent = data.total;
        const grid = document.getElementById("photo-grid");

        if (data.total === 0) {
            grid.innerHTML = `<div class="empty-state"><p>暂无同步的照片</p><p class="hint">通过手机 App 开始同步</p></div>`;
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
    navigator.clipboard.writeText(url).then(() => {
        const toast = document.getElementById("copy-toast");
        toast.classList.add("show");
        setTimeout(() => toast.classList.remove("show"), 2000);
    });
}

function switchUrl(url) {
    document.getElementById("server-url").textContent = url;
    navigator.clipboard.writeText(url).then(() => {
        const toast = document.getElementById("copy-toast");
        toast.textContent = "已切换并复制: " + url;
        toast.classList.add("show");
        setTimeout(() => {
            toast.classList.remove("show");
            toast.textContent = "已复制到剪贴板";
        }, 2000);
    });
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
    if (!path) return alert("请输入有效路径");
    try {
        const fd = new FormData();
        fd.append("path", path);
        const data = await fetchJSON("/api/settings/storage", { method: "POST", body: fd });
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
        alert(data.message || (data.status === "ok" ? "保存成功" : "保存失败"));
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
                    const diffText = [];
                    if (statusData.added > 0) diffText.push(`新增: ${statusData.added}`);
                    if (statusData.removed > 0) diffText.push(`删除: ${statusData.removed}`);
                    const diffInfo = diffText.length > 0 ? ` (${diffText.join(", ")})` : "";
                    statusEl.textContent = `数据库总计: ${status.total_synced} 个文件${diffInfo}`;
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
    const usbSettings = document.getElementById("usb-settings");

    if (selectEl) {
        selectEl.value = connType;
    }
    if (usbSettings) {
        usbSettings.style.display = connType === "adb" ? "flex" : "none";
        // 如果是 ADB 模式，刷新设备列表
        if (connType === "adb") {
            refreshDevices();
        }
    }
}

async function setupAdbReverse() {
    const selectEl = document.getElementById("adb-device-select");
    const deviceSerial = selectEl.value;

    if (!deviceSerial) {
        alert("请先选择设备");
        return;
    }

    const statusEl = document.getElementById("adb-reverse-status");
    statusEl.textContent = "正在设置...";
    statusEl.style.color = "#6B7280";
    try {
        const fd = new FormData();
        fd.append("serial", deviceSerial);
        const data = await fetchJSON("/api/adb/setup-reverse", { method: "POST", body: fd });
        if (data.status === "ok") {
            statusEl.textContent = "✓ " + data.message;
            statusEl.style.color = "green";
        } else {
            statusEl.textContent = "✗ " + data.message;
            statusEl.style.color = "red";
        }
    } catch (e) {
        statusEl.textContent = "✗ 设置失败";
        statusEl.style.color = "red";
    }
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
        const [wifiStatus, serverStatus] = await Promise.all([
            fetchJSON("/api/wifi/status"),
            fetchJSON("/api/status")
        ]);

        const syncDot = document.getElementById("sync-dot");
        const syncStatusText = document.getElementById("sync-status-text");
        const syncProgressArea = document.getElementById("sync-progress-area");

        // 始终更新电脑端照片数量
        document.getElementById("sync-pc-total").textContent = serverStatus.total_synced || 0;

        // 判断当前同步状态
        let s = null;
        let isRunning = false;

        if (wifiStatus.running) {
            s = wifiStatus;
            isRunning = true;
        } else if (wifiStatus.phase === "done" && wifiStatus.phone_total > 0) {
            // 同步刚完成，显示最终结果
            s = wifiStatus;
            isRunning = false;
        }

        if (s) {
            if (isRunning) {
                syncDot.className = "dot green";
                syncStatusText.textContent = `同步中: ${s.device || "未知设备"}`;
            } else {
                syncDot.className = "dot gray";
                syncStatusText.textContent = `同步完成: ${s.device || "未知设备"}`;
            }
            syncProgressArea.style.display = "";

            // 动态显示速度单位 (>=1MB显示MB/s，否则显示KB/s)
            function formatSpeed(mbSpeed) {
                if (mbSpeed >= 1) {
                    return mbSpeed.toFixed(1) + " MB/s";
                } else if (mbSpeed > 0) {
                    return (mbSpeed * 1024).toFixed(0) + " KB/s";
                }
                return "--";
            }

            // 更新同步统计数据
            document.getElementById("sync-scanned").textContent = s.phone_total || 0;
            document.getElementById("sync-need-sync").textContent = s.need_sync || 0;
            document.getElementById("sync-synced").textContent = s.synced || 0;
            document.getElementById("sync-speed").textContent = formatSpeed(s.speed);

            // 进度条
            const needSync = s.need_sync || 1;
            const done = (s.synced || 0) + (s.failed || 0);
            const pct = needSync > 0 ? Math.round(done * 100 / needSync) : (s.need_sync === 0 ? 100 : 0);
            document.getElementById("sync-progress-bar").style.width = pct + "%";

            // 进度文本
            if (isRunning) {
                const speedText = formatSpeed(s.speed);
                const etaText = formatEta(s.eta);
                document.getElementById("sync-progress-text").textContent =
                    `${s.current || "..."} | 速度: ${speedText} | 剩余: ${etaText} | ${pct}%`;
            } else {
                document.getElementById("sync-progress-text").textContent = s.current || "同步完成";
            }
        } else {
            syncDot.className = "dot gray";
            syncStatusText.textContent = "等待同步...";
            syncProgressArea.style.display = "none";
            document.getElementById("sync-scanned").textContent = "-";
            document.getElementById("sync-need-sync").textContent = "-";
            document.getElementById("sync-synced").textContent = "-";
            document.getElementById("sync-speed").textContent = "--";
        }
    } catch (e) {
        console.error("轮询状态失败:", e);
    }
}

// ─── 初始化 ──────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    const pathInput = document.getElementById("storage-path-input");
    if (pathInput) pathInput.addEventListener("input", () => { pathInput._userEdited = true; });
});

loadStatus();
refreshDevices();
loadConnectionStatus();
loadPhotos();
startSyncPoll();
setInterval(() => { loadStatus(); loadConnectionStatus(); loadPhotos(); }, 5000);
