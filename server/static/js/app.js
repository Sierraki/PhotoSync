let currentPage = 1;
let syncPollTimer = null;

async function fetchJSON(url, options) {
    const resp = await fetch(url, options);
    return resp.json();
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

        // ADB 路径
        const adbInput = document.getElementById("adb-path-input");
        if (adbInput && data.adb_path !== undefined) {
            adbInput.value = data.adb_path;
        }
        const adbStatus = document.getElementById("adb-status");
        if (adbStatus) {
            adbStatus.textContent = data.adb_available ? "✓ ADB 可用" : "✗ ADB 不可用";
            adbStatus.style.color = data.adb_available ? "green" : "red";
        }
    } catch (e) {
        console.error("加载状态失败:", e);
    }
}

// ─── 照片列表 ──────────────────────────────────
async function loadPhotos() {
    try {
        // 加载所有照片，用滚动条显示
        const data = await fetchJSON(`/api/photos?page=1&per_page=10000`);
        document.getElementById("photo-count").textContent = data.total;
        const grid = document.getElementById("photo-grid");

        if (data.total === 0) {
            grid.innerHTML = `<div class="empty-state"><p>暂无同步的照片</p><p class="hint">通过手机 App 开始同步</p></div>`;
            return;
        }
        grid.innerHTML = data.photos.map(photo => {
            return `<div class="photo-item"><div class="photo-name" title="${photo.name}">${photo.name}</div></div>`;
        }).join("");

        // 滚动到顶部
        grid.scrollTop = 0;
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

async function setupAdbReverse() {
    const statusEl = document.getElementById("adb-reverse-status");
    statusEl.textContent = "正在设置...";
    statusEl.style.color = "#6B7280";
    try {
        const data = await fetchJSON("/api/adb/setup-reverse", { method: "POST" });
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

// ─── 本地扫描 ──────────────────────────────────
let scanPollTimer = null;

async function startScan() {
    try {
        const data = await fetchJSON("/api/scan/start", { method: "POST" });
        if (data.status === "ok") {
            document.getElementById("scan-progress").style.display = "block";
            pollScanStatus();
        } else {
            alert(data.message || "扫描启动失败");
        }
    } catch (e) {
        alert("扫描启动失败");
    }
}

async function pollScanStatus() {
    try {
        const data = await fetchJSON("/api/scan/status");

        // 更新进度
        if (data.scanned > 0 && data.total > 0) {
            const pct = Math.round((data.scanned / data.total) * 100);
            document.getElementById("scan-progress-bar").style.width = pct + "%";
        }

        document.getElementById("scan-progress-text").textContent = data.current || "处理中...";
        document.getElementById("db-info").textContent = `数据库: ${data.db_total} 个文件`;

        // 显示日志
        const logEl = document.getElementById("scan-log");
        if (data.log && data.log.length > 0) {
            logEl.innerHTML = data.log.map(l => `<div>${l}</div>`).join("");
            logEl.scrollTop = logEl.scrollHeight;
        }

        if (data.running) {
            scanPollTimer = setTimeout(pollScanStatus, 1000);
        } else {
            // 扫描完成
            document.getElementById("scan-progress-bar").style.width = "100%";
            document.getElementById("scan-progress-text").textContent =
                `完成 - 数据库: ${data.db_total} 个文件`;
            // 刷新状态
            loadStatus();
            // 更新同步面板的电脑端数量
            document.getElementById("sync-pc-total").textContent = data.db_total;
        }
    } catch (e) {
        console.error("获取扫描状态失败:", e);
    }
}

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

            // 更新同步统计数据
            document.getElementById("sync-scanned").textContent = s.phone_total || 0;
            document.getElementById("sync-need-sync").textContent = s.need_sync || 0;
            document.getElementById("sync-synced").textContent = s.synced || 0;
            document.getElementById("sync-speed").textContent = s.speed > 0 ? s.speed.toFixed(1) + " 个/秒" : "--";

            // 进度条
            const needSync = s.need_sync || 1;
            const done = (s.synced || 0) + (s.failed || 0);
            const pct = needSync > 0 ? Math.round(done * 100 / needSync) : (s.need_sync === 0 ? 100 : 0);
            document.getElementById("sync-progress-bar").style.width = pct + "%";

            // 进度文本
            if (isRunning) {
                const speedText = s.speed > 0 ? s.speed.toFixed(1) + " 个/秒" : "计算中...";
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
loadPhotos();
startSyncPoll();
setInterval(() => { loadStatus(); loadPhotos(); }, 5000);
