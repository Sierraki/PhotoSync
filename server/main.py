"""
PhotoSync PC Server
局域网/USB 手机相册同步到电脑的接收端服务器
使用本地数据库进行快速索引，同步时与实际文件交叉验证
"""
import json
import os
import io
import sys
import hashlib
import subprocess
import socket
import threading
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import qrcode

# ─── 配置 ───────────────────────────────────────────────
# 支持 PyInstaller 打包后的路径
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys._MEIPASS)
    EXE_DIR = Path(sys.executable).parent
    CONFIG_FILE = EXE_DIR / "config.json"
    DB_FILE = EXE_DIR / "sync_db.json"
    DEFAULT_STORAGE = EXE_DIR / "photos"
else:
    BASE_DIR = Path(__file__).parent
    CONFIG_FILE = BASE_DIR / "config.json"
    DB_FILE = BASE_DIR / "sync_db.json"
    DEFAULT_STORAGE = BASE_DIR / "photos"

SERVER_PORT = 8920


# ─── 配置管理 ────────────────────────────────────────────

class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "storage_path": str(DEFAULT_STORAGE),
            "adb_path": "",
        }
        self.load()

    def load(self):
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                self.data.update(saved)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    @property
    def storage_path(self) -> Path:
        return Path(self.data["storage_path"])

    @property
    def adb_executable(self) -> str:
        adb_dir = self.data.get("adb_path", "")
        if adb_dir:
            for name in ("adb.exe", "adb"):
                p = Path(adb_dir) / name
                if p.exists():
                    return str(p)
        return "adb"


config = Config(CONFIG_FILE)

# 启动时验证存储路径，无效则回退到默认路径
try:
    PHOTOS_DIR = config.storage_path
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
except (OSError, FileNotFoundError):
    print(f"[警告] 存储路径无效: {config.storage_path}，使用默认路径")
    config.data["storage_path"] = str(DEFAULT_STORAGE)
    config.save()
    PHOTOS_DIR = config.storage_path
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


# ─── 数据库 ──────────────────────────────────────────────
class SyncDB:
    """本地 JSON 数据库，用于快速索引"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "albums": {},     # album -> {md5: {filename, size, mtime}}
            "stats": {
                "total": 0,
                "last_scan": None,
            }
        }
        self.lock = threading.Lock()
        self.load()

    def load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        # 兼容旧格式转换
        if "files" in self.data and "albums" not in self.data:
            # 旧格式：files = {path: {md5, ...}}
            # 转换为：albums = {album: {md5: {filename, ...}}}
            albums = {}
            for path, info in self.data["files"].items():
                parts = path.replace("\\", "/").split("/")
                if len(parts) >= 2:
                    album = parts[0]
                    filename = "/".join(parts[1:])
                else:
                    album = "unsorted"
                    filename = path
                md5 = info.get("md5", "")
                if album not in albums:
                    albums[album] = {}
                if md5:
                    albums[album][md5] = {
                        "filename": filename,
                        "size": info.get("size", 0),
                        "mtime": info.get("mtime", 0),
                    }
            self.data["albums"] = albums
            del self.data["files"]
            self.data["stats"]["total"] = sum(len(a) for a in albums.values())
            self.save()

        # 兼容更旧的格式
        if "synced_files" in self.data:
            del self.data["synced_files"]

    def save(self):
        with self.lock:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

    def has_in_album(self, album: str, md5: str) -> bool:
        """检查相册内是否有该MD5"""
        return md5 in self.data.get("albums", {}).get(album, {})

    def add_to_album(self, album: str, md5: str, filename: str, size: int = 0, mtime: float = 0):
        """添加到相册"""
        with self.lock:
            if "albums" not in self.data:
                self.data["albums"] = {}
            if album not in self.data["albums"]:
                self.data["albums"][album] = {}
            self.data["albums"][album][md5] = {
                "filename": filename,
                "size": size,
                "mtime": mtime,
            }
            self.data["stats"]["total"] = sum(len(a) for a in self.data["albums"].values())

    def remove_from_album(self, album: str, md5: str):
        """从相册删除"""
        with self.lock:
            if album in self.data.get("albums", {}) and md5 in self.data["albums"][album]:
                del self.data["albums"][album][md5]
                self.data["stats"]["total"] = sum(len(a) for a in self.data["albums"].values())

    def get_all_paths(self) -> set:
        """获取所有路径（兼容旧接口）"""
        paths = set()
        for album, md5s in self.data.get("albums", {}).items():
            for md5, info in md5s.items():
                paths.add(f"{album}/{info['filename']}")
        return paths

    def get_count(self) -> int:
        return self.data.get("stats", {}).get("total", 0)

    def set_last_scan(self, timestamp: str):
        self.data["stats"]["last_scan"] = timestamp
        self.save()

    def get_stats(self) -> dict:
        return self.data["stats"]


db = SyncDB(DB_FILE)


# ─── 扫描同步 ────────────────────────────────────────────
scan_status = {
    "running": False,
    "phase": "",
    "total": 0,
    "current": "",
    "scanned": 0,
    "added": 0,
    "removed": 0,
    "start_time": None,
    "log": [],
}


def _verify_and_clean_db():
    """验证数据库记录，删除实际不存在的文件记录"""
    photos_dir = get_photos_dir()
    removed = 0

    for album, md5s in list(db.data.get("albums", {}).items()):
        for md5 in list(md5s.keys()):
            info = md5s[md5]
            filename = info.get("filename", "")
            file_path = photos_dir / album / filename
            if not file_path.exists():
                db.remove_from_album(album, md5)
                removed += 1

    if removed > 0:
        db.save()
        print(f"[数据库] 清理了 {removed} 个不存在的文件记录")


def _scan_local_files():
    """扫描本地照片目录，同步数据库与实际文件"""
    global scan_status
    photos_dir = get_photos_dir()

    scan_status.update({
        "running": True,
        "phase": "scanning",
        "total": 0,
        "current": "正在扫描...",
        "scanned": 0,
        "added": 0,
        "removed": 0,
        "start_time": datetime.now().timestamp(),
        "log": [],
    })

    scan_status["log"].append(f"开始扫描: {photos_dir}")

    # 1. 扫描实际文件（按相册组织）
    # album -> {md5: {filename, size, mtime}}
    actual_albums: dict[str, dict] = {}
    scanned = 0

    for root, _dirs, files in os.walk(str(photos_dir)):
        for fname in files:
            if Path(fname).suffix.lower() not in PHOTO_EXTS:
                continue
            filepath = Path(root) / fname
            try:
                stat = filepath.stat()
                content = filepath.read_bytes()
                md5 = hashlib.md5(content).hexdigest()
                rel_path = filepath.relative_to(photos_dir)
                parts = rel_path.as_posix().split("/")
                if len(parts) >= 2:
                    album = parts[0]
                    filename = "/".join(parts[1:])
                else:
                    album = "unsorted"
                    filename = parts[0] if parts else fname

                if album not in actual_albums:
                    actual_albums[album] = {}
                actual_albums[album][md5] = {
                    "filename": filename,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
                scanned += 1
                scan_status["scanned"] = scanned
                scan_status["current"] = f"扫描中 ({scanned}): {fname[:30]}"
            except Exception as e:
                scan_status["log"].append(f"错误: {filepath} - {e}")

    scan_status["total"] = scanned
    scan_status["log"].append(f"扫描完成，实际文件: {scanned} 个")

    # 2. 与数据库对比（只增不减）
    db_albums = db.data.get("albums", {})
    added = 0

    for album, md5s in actual_albums.items():
        if album not in db_albums:
            db_albums[album] = {}
        for md5, info in md5s.items():
            if md5 not in db_albums[album]:
                db.add_to_album(album, md5, info["filename"], info["size"], info["mtime"])
                added += 1

    db.save()
    db.set_last_scan(datetime.now().isoformat())

    scan_status["log"].append(f"数据库记录: {db.get_count()} 个")
    scan_status["log"].append(f"需新增: {added} 个")
    scan_status["added"] = added
    scan_status["phase"] = "done"
    scan_status["running"] = False
    scan_status["current"] = f"完成 - 数据库: {db.get_count()} 个"


def start_local_scan():
    """启动本地扫描"""
    if scan_status["running"]:
        return False
    t = threading.Thread(target=_scan_local_files, daemon=True)
    t.start()
    return True


# ─── 工具函数 ─────────────────────────────────────────────
PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".avi", ".mkv", ".heic", ".heif",
}


def count_pc_photos() -> int:
    """快速统计电脑端照片文件数量（基于数据库）"""
    return db.get_count()


def is_in_album_synced(album: str, md5: str) -> bool:
    """检查相册内是否已有该MD5，并验证文件真实存在"""
    if not db.has_in_album(album, md5):
        return False
    # 验证文件是否存在
    album_data = db.data.get("albums", {}).get(album, {})
    if md5 in album_data:
        filename = album_data[md5].get("filename", "")
        file_path = get_photos_dir() / album / filename
        if file_path.exists():
            return True
        else:
            db.remove_from_album(album, md5)
            db.save()
            print(f"[验证] 文件不存在已清理: {album}/{filename}")
    return False


def add_to_album_index(album: str, md5: str, filename: str, size: int = 0):
    """上传新文件后，将其加入数据库"""
    mtime = datetime.now().timestamp()
    db.add_to_album(album, md5, filename, size, mtime)
    db.save()


def get_pc_path_count() -> int:
    """获取数据库中的文件数量"""
    return db.get_count()


# 旧函数名兼容
def is_hash_synced(md5: str) -> bool:
    """旧接口：检查 MD5 是否存在（遍历查找）"""
    for path, info in db.data["files"].items():
        if info.get("md5") == md5:
            file_path = get_photos_dir() / path
            if file_path.exists():
                return True
            else:
                db.remove_by_path(path)
                db.save()
                return False
    return False


def add_hash_to_index(md5: str, rel_path: str, size: int = 0):
    """旧接口兼容"""
    add_path_to_index(rel_path, md5, size)


def get_pc_hash_count() -> int:
    """获取数据库中的文件数量"""
    return db.get_count()


# ─── 工具函数 ─────────────────────────────────────────────
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_all_local_ips() -> list[str]:
    """获取本机所有局域网 IP（过滤虚拟网卡）"""
    VIRTUAL_PREFIXES = ("172.17.", "172.18.", "172.19.", "172.25.", "172.26.", "172.27.", "172.28.")
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            if any(ip.startswith(p) for p in VIRTUAL_PREFIXES):
                continue
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    primary = get_local_ip()
    if primary in ips:
        ips.remove(primary)
    ips.insert(0, primary)
    return ips


def get_photos_dir() -> Path:
    d = config.storage_path
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── ADB 工具 ────────────────────────────────────────────
def _run_adb(*args, timeout=10) -> subprocess.CompletedProcess:
    cmd = [config.adb_executable] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def check_adb() -> bool:
    try:
        return _run_adb("version", timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def get_adb_devices(include_emulators: bool = False) -> list[dict]:
    """返回 [{"serial": "xxx", "model": "Pixel 7", "is_emulator": False}]"""
    try:
        result = _run_adb("devices", "-l", timeout=5)
        devices = []
        for line in result.stdout.strip().split("\n")[1:]:
            if "\tdevice" not in line and " device " not in line:
                continue
            parts = line.split()
            serial = parts[0]
            model = ""
            for p in parts:
                if p.startswith("model:"):
                    model = p.split(":", 1)[1].replace("_", " ")
            if not model:
                try:
                    r = _run_adb("-s", serial, "shell", "getprop", "ro.product.model", timeout=5)
                    model = r.stdout.strip()
                except Exception:
                    model = serial
            is_emulator = (
                serial.startswith("emulator-")
                or "emulator" in model.lower()
                or "sdk_gphone" in model.lower()
                or "gphone" in model.lower()
            )
            if not include_emulators and is_emulator:
                continue
            devices.append({
                "serial": serial,
                "model": model or serial,
                "is_emulator": is_emulator,
            })
        return devices
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def setup_adb_reverse(serial: str = None) -> bool:
    """为指定设备设置 ADB reverse 端口转发，返回是否成功"""
    try:
        if serial:
            result = _run_adb(
                "-s",
                serial,
                "reverse",
                f"tcp:{SERVER_PORT}",
                f"tcp:{SERVER_PORT}",
                timeout=5)
        else:
            result = _run_adb("reverse", f"tcp:{SERVER_PORT}", f"tcp:{SERVER_PORT}", timeout=5)
        success = result.returncode == 0
        if success:
            print(f"ADB reverse 端口转发已设置: tcp:{SERVER_PORT} -> tcp:{SERVER_PORT}")
        else:
            print(f"ADB reverse 设置失败: {result.stderr}")
        return success
    except Exception as e:
        print(f"ADB reverse 设置异常: {e}")
        return False


# ─── ADB 直接拉取照片 ────────────────────────────────────
adb_sync_status = {
    "running": False,
    "phase": "",
    "pc_total": 0,
    "phone_total": 0,
    "need_sync": 0,
    "synced": 0,
    "skipped": 0,
    "failed": 0,
    "current": "",
    "device": "",
    "start_time": None,
    "speed": 0.0,
    "eta": 0,
    "log": [],
}

# ─── WiFi 同步状态（手机端上传时更新）─────────────────────
wifi_sync_status = {
    "running": False,
    "phase": "",
    "pc_total": 0,
    "device": "",
    "phone_total": 0,
    "need_sync": 0,
    "synced": 0,
    "skipped": 0,
    "failed": 0,
    "current": "",
    "start_time": None,
    "speed": 0.0,
    "eta": 0,
}

PHONE_PHOTO_DIRS = [
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Pictures/Screenshots",
]


def _adb_sync_log(msg: str):
    adb_sync_status["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    if len(adb_sync_status["log"]) > 500:
        adb_sync_status["log"] = adb_sync_status["log"][-300:]


def _adb_list_files(serial: str, remote_dir: str) -> list[str]:
    """列出手机目录中的文件"""
    try:
        result = _run_adb(
            "-s", serial, "shell",
            f"find {remote_dir} -maxdepth 3 -type f 2>/dev/null",
            timeout=30)
        files = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            ext = Path(line).suffix.lower()
            if ext in PHOTO_EXTS:
                files.append(line)
        return files
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []


def _adb_get_md5(serial: str, remote_path: str) -> str:
    """获取手机文件的 MD5"""
    try:
        result = _run_adb("-s", serial, "shell", f"md5sum '{remote_path}'", timeout=30)
        return result.stdout.strip().split()[0] if result.stdout.strip() else ""
    except Exception:
        return ""


def _adb_pull_file(serial: str, remote_path: str, local_path: str) -> bool:
    """从手机拉取文件到 PC"""
    try:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        result = _run_adb("-s", serial, "pull", remote_path, local_path, timeout=120)
        return result.returncode == 0
    except Exception:
        return False


def _run_adb_sync(serial: str, device_name: str):
    """在后台线程执行 ADB 同步（先扫描再同步）"""
    global adb_sync_status

    adb_sync_status.update({
        "running": True,
        "phase": "scanning",
        "pc_total": 0,
        "phone_total": 0,
        "need_sync": 0,
        "synced": 0,
        "skipped": 0,
        "failed": 0,
        "current": "正在扫描手机...",
        "device": device_name,
        "start_time": None,
        "speed": 0.0,
        "eta": 0,
        "log": [],
    })

    _adb_sync_log(f"开始同步设备: {device_name} ({serial})")
    _adb_sync_log("正在扫描电脑端文件索引...")
    photos_dir = get_photos_dir()

    # ─── 阶段1: 扫描手机照片 ───
    all_files = []
    for dir_path in PHONE_PHOTO_DIRS:
        if not adb_sync_status["running"]:
            break
        _adb_sync_log(f"扫描: {dir_path}")
        files = _adb_list_files(serial, dir_path)
        all_files.extend(files)
        _adb_sync_log(f"  发现 {len(files)} 个文件")

    all_files = list(dict.fromkeys(all_files))  # 去重
    adb_sync_status["phone_total"] = len(all_files)
    _adb_sync_log(f"手机总数: {len(all_files)} 张，正在校验...")

    if not all_files:
        _adb_sync_log("未发现照片文件")
        adb_sync_status["running"] = False
        return

    # 等待 PC 索引重建完成
    _adb_sync_log("等待电脑端文件索引就绪...")
    pc_total = get_pc_hash_count()
    adb_sync_status["pc_total"] = pc_total
    _adb_sync_log(f"电脑端已有照片: {pc_total} 张")

    # ─── 阶段2: 校验哪些需要同步（相册内去重） ───
    need_sync_files = []
    need_sync_md5 = {}
    skipped = 0

    for i, remote_path in enumerate(all_files):
        if not adb_sync_status["running"]:
            _adb_sync_log("同步已取消")
            return

        filename = Path(remote_path).name
        adb_sync_status["current"] = f"校验 ({i + 1}/{len(all_files)}): {filename}"

        md5 = _adb_get_md5(serial, remote_path)
        if not md5:
            continue

        # 从路径提取相册名
        parts = remote_path.split("/")
        album = "unsorted"
        for j, part in enumerate(parts):
            if part in ("DCIM", "Pictures") and j + 1 < len(parts):
                album = parts[j + 1]
                break
        if album == "Camera":
            album = "Camera"

        # 相册内去重检查
        if is_in_album_synced(album, md5):
            skipped += 1
            adb_sync_status["skipped"] = skipped
        else:
            need_sync_files.append((remote_path, album))
            need_sync_md5[remote_path] = md5

    adb_sync_status["need_sync"] = len(need_sync_files)
    _adb_sync_log(f"校验完成！需同步: {len(need_sync_files)}，已存在: {skipped}")

    if not need_sync_files:
        _adb_sync_log("所有照片已同步完成，无需更新")
        adb_sync_status["phase"] = "done"
        adb_sync_status["current"] = "同步完成"
        adb_sync_status["running"] = False
        return

    # ─── 阶段3: 同步文件 ───
    adb_sync_status["phase"] = "syncing"
    adb_sync_status["start_time"] = datetime.now().timestamp()
    _adb_sync_log(f"开始同步 {len(need_sync_files)} 个文件...")

    for remote_path, album in need_sync_files:
        if not adb_sync_status["running"]:
            _adb_sync_log("同步已取消")
            break

        filename = Path(remote_path).name
        adb_sync_status["current"] = filename

        md5 = need_sync_md5.get(remote_path, "")
        if not md5:
            md5 = _adb_get_md5(serial, remote_path)
            if not md5:
                adb_sync_status["failed"] += 1
                _adb_sync_log(f"MD5计算失败: {filename}")
                continue

        # 相册内去重再次检查
        if is_in_album_synced(album, md5):
            continue

        save_dir = photos_dir / album
        save_path = save_dir / filename
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            counter = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        if _adb_pull_file(serial, remote_path, str(save_path)):
            add_to_album_index(album, md5, save_path.name)
            adb_sync_status["synced"] += 1
            adb_sync_status["pc_total"] = db.get_count()
            _adb_sync_log(f"已同步: {album}/{filename}")
        else:
            adb_sync_status["failed"] += 1
            _adb_sync_log(f"失败: {filename}")

        # 计算速度和ETA
        start_time = adb_sync_status.get("start_time")
        synced = adb_sync_status["synced"]
        if start_time and synced > 0:
            elapsed = datetime.now().timestamp() - start_time
            if elapsed > 0:
                speed = synced / elapsed
                adb_sync_status["speed"] = round(speed, 2)
                remaining = len(need_sync_files) - synced - adb_sync_status["failed"]
                adb_sync_status["eta"] = int(remaining / speed) if speed > 0 else 0

    db.set_last_scan(datetime.now().isoformat())
    adb_sync_status["phase"] = "done"
    adb_sync_status["current"] = "同步完成"
    _adb_sync_log(
        f"同步完成！电脑端: {adb_sync_status['pc_total']}，"
        f"本次同步: {adb_sync_status['synced']}，"
        f"失败: {adb_sync_status['failed']}"
    )
    adb_sync_status["running"] = False


# ─── 文件夹选择器 ────────────────────────────────────────
folder_select_result: Optional[str] = None
folder_select_event = threading.Event()


def _open_folder_dialog():
    global folder_select_result
    try:
        import tkinter as tk
        from tkinter import filedialog
        # 创建隐藏的 Tk 窗口
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        # 打开文件夹选择对话框
        path = filedialog.askdirectory(title="选择文件夹")
        folder_select_result = path if path else None
        root.destroy()
    except Exception as e:
        print(f"文件夹选择对话框错误: {e}")
        folder_select_result = None
    finally:
        folder_select_event.set()


# ─── FastAPI 应用 ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="PhotoSync Server", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ─── API 路由 ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/status")
async def get_status():
    ip = get_local_ip()
    all_ips = get_all_local_ips()
    adb_available = check_adb()
    adb_devices = get_adb_devices(include_emulators=False) if adb_available else []
    all_adb_devices = get_adb_devices(include_emulators=True) if adb_available else []

    # 使用数据库中的数量
    total_synced = db.get_count()
    stats = db.get_stats()

    return {
        "server_ip": ip,
        "server_port": SERVER_PORT,
        "server_url": f"http://{ip}:{SERVER_PORT}",
        "all_urls": [f"http://{i}:{SERVER_PORT}" for i in all_ips],
        "adb_available": adb_available,
        "adb_devices": adb_devices,
        "all_adb_devices": all_adb_devices,
        "adb_path": config.data.get("adb_path", ""),
        "total_synced": total_synced,
        "storage_path": str(get_photos_dir().resolve()),
    }


@app.get("/api/qrcode")
async def get_qrcode(url: str = ""):
    """生成服务器地址二维码"""
    if not url:
        ip = get_local_ip()
        url = f"http://{ip}:{SERVER_PORT}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/api/settings/storage")
async def set_storage_path(path: str = Form(...)):
    try:
        new_path = Path(path)
        new_path.mkdir(parents=True, exist_ok=True)
        resolved = str(new_path.resolve())
        config.data["storage_path"] = resolved
        config.save()
        return {"status": "ok", "message": "存储路径已更新", "path": resolved}
    except Exception as e:
        return {"status": "error", "message": f"设置失败: {e}"}


@app.post("/api/settings/browse")
async def browse_folder():
    global folder_select_result
    folder_select_result = None
    folder_select_event.clear()
    t = threading.Thread(target=_open_folder_dialog, daemon=True)
    t.start()
    folder_select_event.wait(timeout=60)
    if folder_select_result:
        return {"status": "ok", "path": folder_select_result}
    return {"status": "cancelled", "message": "未选择文件夹"}


@app.post("/api/settings/adb_path")
async def set_adb_path(path: str = Form(...)):
    try:
        if path:
            p = Path(path)
            if not p.exists():
                return {"status": "error", "message": f"路径不存在: {path}"}
        config.data["adb_path"] = path
        config.save()
        adb_ok = check_adb()
        return {
            "status": "ok",
            "message": "ADB 路径已更新" + (" (ADB 可用)" if adb_ok else " (ADB 不可用)"),
            "adb_available": adb_ok,
        }
    except Exception as e:
        return {"status": "error", "message": f"设置失败: {e}"}


# ─── 本地扫描 API ────────────────────────────────────────
@app.post("/api/scan/start")
async def start_scan():
    """启动本地扫描，同步数据库与实际文件"""
    if scan_status["running"]:
        return {"status": "error", "message": "扫描正在进行中"}
    ok = start_local_scan()
    if ok:
        return {"status": "ok", "message": "开始扫描本地文件"}
    return {"status": "error", "message": "扫描启动失败"}


@app.get("/api/scan/status")
async def get_scan_status():
    """获取扫描进度"""
    stats = db.get_stats()
    return {
        "running": scan_status["running"],
        "phase": scan_status["phase"],
        "total": scan_status["total"],
        "scanned": scan_status["scanned"],
        "added": scan_status["added"],
        "removed": scan_status["removed"],
        "current": scan_status["current"],
        "log": scan_status["log"][-20:] if scan_status["log"] else [],
        "db_total": db.get_count(),
        "db_last_scan": stats.get("last_scan"),
    }


# ─── 统计计数器 ────────────────────────────────────────────
check_stats = {"total": 0, "synced": 0, "not_synced": 0}


@app.post("/api/check_album")
async def check_album(items: list[dict]):
    """批量检查相册内是否已存在（相册内去重）
    输入: [{"album": "Camera", "md5": "xxx"}, ...]
    输出: {"album|md5": true/false, ...}
    """
    global check_stats
    if not items:
        return {}

    results = {}
    synced_count = 0
    not_synced_count = 0

    for item in items:
        album = item.get("album", "unsorted")
        md5 = item.get("md5", "")
        key = f"{album}|{md5}"

        if not md5:
            results[key] = False
            not_synced_count += 1
            continue

        # 检查该相册内是否有该MD5
        in_album = db.has_in_album(album, md5)

        if in_album:
            # 验证文件是否存在
            album_data = db.data.get("albums", {}).get(album, {})
            if md5 in album_data:
                filename = album_data[md5].get("filename", "")
                file_path = get_photos_dir() / album / filename
                if file_path.exists():
                    results[key] = True
                    synced_count += 1
                else:
                    db.remove_from_album(album, md5)
                    db.save()
                    results[key] = False
                    not_synced_count += 1
            else:
                results[key] = False
                not_synced_count += 1
        else:
            results[key] = False
            not_synced_count += 1

    # 累计统计
    check_stats["total"] += len(items)
    check_stats["synced"] += synced_count
    check_stats["not_synced"] += not_synced_count

    if not_synced_count > 0:
        print(
            f"[相册检查] 总计: {
                check_stats['total']}, 已同步: {
                check_stats['synced']}, 需同步: {
                check_stats['not_synced']}")

    return results


@app.post("/api/check")
async def check_files(hashes: list[str]):
    """旧接口：批量检查MD5是否已存在（兼容）"""
    global check_stats
    if not hashes:
        return {}

    results = {}
    synced_count = 0
    not_synced_count = 0

    for h in hashes:
        # 遍历所有相册检查
        found = False
        for album, md5s in db.data.get("albums", {}).items():
            if h in md5s:
                filename = md5s[h].get("filename", "")
                file_path = get_photos_dir() / album / filename
                if file_path.exists():
                    found = True
                    break
                else:
                    db.remove_from_album(album, h)
                    db.save()

        results[h] = found
        if found:
            synced_count += 1
        else:
            not_synced_count += 1

    check_stats["total"] += len(hashes)
    check_stats["synced"] += synced_count
    check_stats["not_synced"] += not_synced_count

    return results


@app.get("/api/check/stats")
async def get_check_stats():
    """获取检查统计"""
    return check_stats


# ─── WiFi 同步 API ────────────────────────────────────────
@app.post("/api/wifi/scan")
async def wifi_scan_progress(
    device: str = Form(""),
    phase: str = Form("scanning"),
    scanned: int = Form(0),
    total: int = Form(0),
):
    """手机端扫描进度更新 - 自动同步本地文件与数据库"""
    # 首次扫描时，验证并清理数据库中不存在的文件记录
    if scanned == 1:
        _verify_and_clean_db()

    # 使用数据库中的数量
    pc_total = db.get_count()

    wifi_sync_status.update({
        "running": True,
        "phase": "scanning",
        "pc_total": pc_total,
        "device": device or "未知设备",
        "phone_total": scanned,
        "need_sync": 0,
        "synced": 0,
        "skipped": 0,
        "failed": 0,
        "current": f"扫描中 {scanned}/{total}...",
        "start_time": None,
        "speed": 0.0,
        "eta": 0,
    })
    return {"status": "ok"}


@app.post("/api/wifi/start")
async def wifi_sync_start(
    device: str = Form(""),
    phone_total: int = Form(0),
    need_sync: int = Form(0),
):
    """手机端开始同步时调用，报告统计信息"""
    pc_total = db.get_count()

    wifi_sync_status.update({
        "running": True,
        "phase": "syncing",
        "pc_total": pc_total,
        "device": device or "未知设备",
        "phone_total": phone_total,
        "need_sync": need_sync,
        "synced": 0,
        "skipped": 0,
        "failed": 0,
        "current": "",
        "start_time": datetime.now().timestamp(),
        "speed": 0.0,
        "eta": 0,
    })
    return {"status": "ok", "message": "同步已开始"}


@app.post("/api/wifi/progress")
async def wifi_sync_progress(
    current: str = Form(""),
    synced: int = Form(0),
    skipped: int = Form(0),
    failed: int = Form(0),
):
    """手机端上传过程中更新进度"""
    wifi_sync_status["current"] = current
    wifi_sync_status["synced"] = synced
    wifi_sync_status["skipped"] = skipped
    wifi_sync_status["failed"] = failed

    # 更新 PC 文件数量
    wifi_sync_status["pc_total"] = db.get_count()

    # 计算速度和剩余时间
    start = wifi_sync_status.get("start_time")
    if start and synced > 0:
        elapsed = datetime.now().timestamp() - start
        if elapsed > 0:
            speed = synced / elapsed
            wifi_sync_status["speed"] = round(speed, 2)
            remaining = wifi_sync_status["need_sync"] - synced - failed
            wifi_sync_status["eta"] = int(remaining / speed) if speed > 0 else 0

    return {"status": "ok"}


@app.post("/api/wifi/stop")
async def wifi_sync_stop(message: str = Form("")):
    """手机端同步完成或取消时调用"""
    wifi_sync_status["running"] = False
    wifi_sync_status["phase"] = "done"
    wifi_sync_status["current"] = message or "同步已完成"
    db.set_last_scan(datetime.now().isoformat())
    return {"status": "ok"}


@app.get("/api/wifi/status")
async def wifi_sync_get_status():
    """获取 WiFi 同步进度"""
    return wifi_sync_status


@app.post("/api/upload")
async def upload_photo(
    file: UploadFile = File(...),
    file_hash: str = Form(""),
    original_name: str = Form(...),
    taken_date: str = Form(""),
    album: str = Form(""),
):
    try:
        photos_dir = get_photos_dir()

        if album:
            sub_dir = album.replace("\\", "/").strip("/")
        else:
            sub_dir = "unsorted"

        save_dir = photos_dir / sub_dir
        save_dir.mkdir(parents=True, exist_ok=True)

        # 先读取内容计算MD5
        content = await file.read()
        actual_hash = hashlib.md5(content).hexdigest()
        final_hash = file_hash if file_hash else actual_hash

        # 检查该相册内是否已有该MD5（相册内去重）
        if is_in_album_synced(sub_dir, final_hash):
            print(f"[上传] 跳过（相册内已存在）: {sub_dir}/{original_name}")
            return {"status": "skipped", "message": "相册内已存在相同文件"}

        # 确定保存路径
        save_path = save_dir / original_name
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            counter = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        filename = save_path.name

        with open(save_path, "wb") as f:
            f.write(content)

        add_to_album_index(sub_dir, final_hash, filename, len(content))
        print(f"[上传] 成功: {sub_dir}/{filename}")

        return {"status": "ok", "message": "上传成功", "path": f"{sub_dir}/{filename}"}
    except ConnectionResetError:
        return {"status": "error", "message": "连接被重置"}
    except Exception as e:
        return {"status": "error", "message": f"上传失败: {e}"}
        return {"status": "error", "message": f"上传失败: {e}"}


@app.get("/api/photos")
async def list_photos(page: int = 1, per_page: int = 50):
    """列出已同步的照片（直接扫描文件系统）"""
    photos_dir = get_photos_dir()
    all_files = []
    for root, _dirs, files in os.walk(str(photos_dir)):
        for fname in files:
            if Path(fname).suffix.lower() not in PHOTO_EXTS:
                continue
            filepath = Path(root) / fname
            try:
                rel_path = filepath.relative_to(photos_dir).as_posix()
                mtime = filepath.stat().st_mtime
                size = filepath.stat().st_size
                all_files.append({
                    "filename": rel_path,
                    "name": fname,
                    "size": size,
                    "mtime": mtime,
                })
            except Exception:
                pass

    # 按修改时间倒序
    all_files.sort(key=lambda x: x["mtime"], reverse=True)

    total = len(all_files)
    start_idx = (page - 1) * per_page
    page_files = all_files[start_idx:start_idx + per_page]

    photos = []
    for f in page_files:
        photos.append({
            "filename": f["filename"],
            "name": f["name"],
            "size": f["size"],
            "url": f"/api/photo/{f['filename']}",
        })

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page if total > 0 else 0,
        "photos": photos,
    }


@app.get("/api/photo/{path:path}")
async def get_photo(path: str):
    """按相对路径获取照片"""
    photo_path = get_photos_dir() / path
    if not photo_path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(str(photo_path))


# ─── ADB 同步 API ────────────────────────────────────────
@app.post("/api/adb/sync")
async def adb_sync_start(serial: str = Form(default="")):
    """启动 ADB USB 同步（先扫描再自动同步）"""
    if adb_sync_status["running"]:
        return {"status": "error", "message": "同步正在进行中"}
    if not check_adb():
        return {"status": "error", "message": "ADB 不可用"}
    devices = get_adb_devices(include_emulators=False)
    if not devices:
        return {"status": "error", "message": "未检测到真机设备（模拟器已过滤）"}

    device = None
    if serial:
        for d in devices:
            if d["serial"] == serial:
                device = d
                break
        if not device:
            return {"status": "error", "message": f"未找到设备: {serial}"}
    else:
        device = devices[0]

    t = threading.Thread(
        target=_run_adb_sync,
        args=(device["serial"], device["model"]),
        daemon=True,
    )
    t.start()
    return {"status": "ok", "message": f"开始同步设备: {device['model']}", "device": device}


@app.get("/api/adb/devices")
async def adb_list_devices(include_emulators: bool = False):
    """刷新并返回 ADB 设备列表"""
    if not check_adb():
        return {"status": "error", "devices": [], "message": "ADB 不可用"}
    devices = get_adb_devices(include_emulators=include_emulators)
    return {"status": "ok", "devices": devices}


@app.post("/api/adb/stop")
async def adb_sync_stop():
    """停止 ADB 同步"""
    adb_sync_status["running"] = False
    return {"status": "ok", "message": "正在停止同步"}


@app.get("/api/adb/status")
async def adb_sync_get_status():
    """获取 ADB 同步进度"""
    return adb_sync_status


@app.post("/api/adb/setup-reverse")
async def adb_setup_reverse():
    """为所有连接的设备设置 ADB reverse 端口转发"""
    if not check_adb():
        return {"status": "error", "message": "ADB 不可用"}

    devices = get_adb_devices()
    if not devices:
        return {"status": "error", "message": "未检测到 ADB 设备"}

    results = []
    for d in devices:
        success = setup_adb_reverse(d["serial"])
        results.append({
            "serial": d["serial"],
            "model": d["model"],
            "success": success,
        })

    success_count = sum(1 for r in results if r["success"])
    return {
        "status": "ok",
        "message": f"已为 {success_count}/{len(devices)} 个设备设置端口转发",
        "results": results,
    }


# ─── 启动入口 ────────────────────────────────────────────
if __name__ == "__main__":
    import logging

    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # 启动时清理不存在的文件记录
    _verify_and_clean_db()

    ip = get_local_ip()
    print(f"{'=' * 50}")
    print(f"  PhotoSync 服务器")
    print(f"  局域网地址: http://{ip}:{SERVER_PORT}")
    print(f"  照片存储: {get_photos_dir().resolve()}")
    print(f"  数据库: {db.get_count()} 个文件")

    # 自动设置 ADB reverse 端口转发
    adb_ok = check_adb()
    print(f"  ADB 可用: {adb_ok}")
    if adb_ok:
        devices = get_adb_devices()
        if devices:
            print(f"  ADB 设备: {len(devices)} 个")
            for d in devices:
                if setup_adb_reverse(d["serial"]):
                    print(f"    - {d['model']} ({d['serial']}) 端口转发已设置")
        else:
            print("  ADB 设备: 未检测到（连接后自动设置）")

    print(f"{'=' * 50}")
    print("服务器已启动，等待连接...")

    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT, log_level="warning")
