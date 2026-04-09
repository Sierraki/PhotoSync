"""
PhotoSync PC Server
局域网/USB 手机相册同步到电脑的接收端服务器
使用本地 SQLite 数据库进行快速索引，同步时与实际文件交叉验证
使用 SHA-256 替代 MD5，提高安全性和碰撞抗性
"""
import time
import asyncio
import json
import os
import io
import sys
import hashlib
import sqlite3
import subprocess
import socket
import threading
from pathlib import Path
from datetime import datetime
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
import qrcode

# ─── 配置 ───────────────────────────────────────────────
# 支持 PyInstaller 打包后的路径
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    EXE_DIR = Path(sys.executable).parent
    CONFIG_FILE = EXE_DIR / "config.json"
    DB_FILE = EXE_DIR / "sync_db.sqlite"  # SQLite 数据库文件
    DB_JSON_FILE = EXE_DIR / "sync_db.json"  # 旧 JSON 备份（用于迁移）
    DEFAULT_STORAGE = EXE_DIR / "photos"
else:
    BASE_DIR = Path(__file__).parent
    CONFIG_FILE = BASE_DIR / "config.json"
    DB_FILE = BASE_DIR / "sync_db.sqlite"  # SQLite 数据库文件
    DB_JSON_FILE = BASE_DIR / "sync_db.json"  # 旧 JSON 备份（用于迁移）
    DEFAULT_STORAGE = BASE_DIR / "photos"

DEFAULT_SERVER_PORT = 8920


# ─── 配置管理 ────────────────────────────────────────────

class Config:
    def __init__(self, path: Path):
        self.path = path
        self.data = {
            "storage_path": str(DEFAULT_STORAGE),
            "connection_type": "wifi",  # "wifi" 或 "adb"
            "server_port": DEFAULT_SERVER_PORT,
            "upload_rate_limit_kbps": 0,  # 0 表示不限速
            "ui_theme": "",  # "dark" | "light" | ""(follow system)
            "tls_enabled": False,
            "tls_cert_file": "",
            "tls_key_file": "",
            "enforce_https": False,
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
    def server_port(self) -> int:
        return int(self.data.get("server_port", DEFAULT_SERVER_PORT))

    @property
    def adb_executable(self) -> str:
        # 使用内置 ADB
        builtin_adb = Path(__file__).parent.parent / "ADB" / "adb.exe"
        if builtin_adb.exists():
            return str(builtin_adb)
        # 备用：尝试系统 PATH 中的 adb
        return "adb"


config = Config(CONFIG_FILE)


def get_server_port() -> int:
    """统一从配置读取当前端口（运行中展示/逻辑使用）。"""
    return config.server_port


def resolve_config_path(path_text: str) -> Path:
    """将配置中的路径解析为绝对路径。"""
    p = Path(path_text)
    if p.is_absolute():
        return p
    base = EXE_DIR if getattr(sys, 'frozen', False) else BASE_DIR
    return (base / p).resolve()


def get_server_scheme() -> str:
    """根据配置判断对外展示协议。"""
    if bool(config.data.get("tls_enabled", False)):
        return "https"
    return "http"


def get_upload_rate_limit_bps() -> int:
    """获取上传限速（字节/秒），0 表示不限速。"""
    try:
        kbps = int(config.data.get("upload_rate_limit_kbps", 0) or 0)
    except (TypeError, ValueError):
        kbps = 0
    return max(0, kbps) * 1024


def is_port_available(host: str, port: int) -> bool:
    """检查端口是否可用。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_available_port(start_port: int, max_tries: int = 30) -> Optional[int]:
    """从起始端口向后寻找可用端口。"""
    for p in range(start_port, start_port + max_tries):
        if 1024 <= p <= 65535 and is_port_available("0.0.0.0", p):
            return p
    return None

# 启动时验证存储路径，无效则回退到默认路径
try:
    PHOTOS_DIR = config.storage_path
except (OSError, FileNotFoundError):
    print(f"[警告] 存储路径无效: {config.storage_path}，使用默认路径")
    config.data["storage_path"] = str(DEFAULT_STORAGE)
    config.save()
    PHOTOS_DIR = config.storage_path


# ─── 哈希工具（使用 SHA-256 替代 MD5）─────────────────────
def compute_file_hash(file_path: Path) -> str:
    """计算文件的 SHA-256 哈希值"""
    sha256 = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as e:
        print(f"[哈希] 计算失败: {file_path} - {e}")
        return ""


# ─── 数据库（SQLite 版本）──────────────────────────────────
class SyncDB:
    """SQLite 数据库，用于高效索引和并发安全"""

    def __init__(self, db_path: Path, json_backup_path: Optional[Path] = None):
        self.db_path = db_path
        self.json_backup_path = json_backup_path
        self.lock = threading.RLock()  # 可重入锁，支持同一线程多次获取
        self._journal_mode = "WAL"
        self._wal_unavailable = False

        # 初始化数据库
        self._init_db()

        # 从旧 JSON 迁移数据（如果存在）
        if json_backup_path and json_backup_path.exists():
            self._migrate_from_json(json_backup_path)

    def _init_db(self):
        """初始化数据库表"""
        with self.lock:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            self._apply_journal_mode(conn)
            try:
                cursor = conn.cursor()

                # 文件表：存储相册内的文件索引
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS files (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        album TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        filename TEXT NOT NULL,
                        size INTEGER DEFAULT 0,
                        mtime REAL DEFAULT 0,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(album, sha256)
                    )
                """)

                # 创建索引，加速查询
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_album_sha256 ON files(album, sha256)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sha256 ON files(sha256)"
                )

                # 统计表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS stats (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)

                # 初始化统计
                cursor.execute("INSERT OR IGNORE INTO stats VALUES ('total', '0')")
                cursor.execute("INSERT OR IGNORE INTO stats VALUES ('last_scan', NULL)")

                conn.commit()
            finally:
                conn.close()

    def _apply_journal_mode(self, conn: sqlite3.Connection):
        """尝试设置数据库日志模式，WAL 不可用时自动降级。"""
        if self._wal_unavailable:
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                pass
            return

        try:
            conn.execute(f"PRAGMA journal_mode={self._journal_mode}")
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "disk i/o error" in msg or "readonly" in msg or "read-only" in msg:
                self._wal_unavailable = True
                self._journal_mode = "DELETE"
                print("[数据库] WAL 模式不可用，已自动降级为 DELETE")
                try:
                    conn.execute("PRAGMA journal_mode=DELETE")
                except sqlite3.Error:
                    pass
                return
            raise

    def _migrate_from_json(self, json_path: Path):
        """从旧的 JSON 格式迁移数据"""
        try:
            print(f"[迁移] 从 JSON 迁移数据: {json_path}")
            with open(json_path, 'r', encoding='utf-8') as f:
                old_data = json.load(f)

            albums = old_data.get("albums", {})
            if not albums:
                return

            with self.lock:
                conn = sqlite3.connect(str(self.db_path), timeout=10)
                try:
                    cursor = conn.cursor()
                    migrated_count = 0

                    for album, md5_dict in albums.items():
                        for md5_hash, info in md5_dict.items():
                            # MD5 -> SHA-256 迁移：使用原有的 MD5 作为占位符
                            # 生成一个虚拟的 "md5:" 前缀以便识别
                            sha256 = f"md5:{md5_hash}"

                            cursor.execute(
                                """INSERT OR REPLACE INTO files
                                   (album, sha256, filename, size, mtime)
                                   VALUES (?, ?, ?, ?, ?)""",
                                (album, sha256, info.get("filename", ""),
                                 info.get("size", 0), info.get("mtime", 0))
                            )
                            migrated_count += 1

                    conn.commit()
                    print(f"[迁移] 已迁移 {migrated_count} 条记录")

                    # 备份原文件
                    backup_path = json_path.with_suffix('.json.bak')
                    json_path.rename(backup_path)
                    print(f"[迁移] 原文件已备份至: {backup_path}")
                finally:
                    conn.close()
        except Exception as e:
            print(f"[迁移] 失败: {e}")

    @contextmanager
    def _get_connection(self):
        """获取数据库连接（上下文管理器）"""
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        self._apply_journal_mode(conn)
        try:
            yield conn
        finally:
            conn.close()

    def has_in_album(self, album: str, sha256: str) -> bool:
        """检查相册内是否有该 SHA-256"""
        if not sha256:
            return False
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT 1 FROM files WHERE album = ? AND sha256 = ? LIMIT 1",
                    (album, sha256)
                )
                return cursor.fetchone() is not None

    def add_to_album(self, album: str, sha256: str, filename: str,
                     size: int = 0, mtime: float = 0) -> bool:
        """添加到相册（原子操作）"""
        with self.lock:
            with self._get_connection() as conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        """INSERT OR REPLACE INTO files
                           (album, sha256, filename, size, mtime)
                           VALUES (?, ?, ?, ?, ?)""",
                        (album, sha256, filename, size, mtime)
                    )
                    cursor.execute("SELECT COUNT(*) FROM files")
                    total = cursor.fetchone()[0]
                    cursor.execute(
                        "INSERT OR REPLACE INTO stats VALUES ('total', ?)",
                        (str(total),)
                    )
                    conn.commit()
                    return True
                except Exception as e:
                    print(f"[数据库] 添加失败: {e}")
                    return False

    def remove_from_album(self, album: str, sha256: str) -> bool:
        """从相册删除"""
        with self.lock:
            with self._get_connection() as conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "DELETE FROM files WHERE album = ? AND sha256 = ?",
                        (album, sha256)
                    )
                    cursor.execute("SELECT COUNT(*) FROM files")
                    total = cursor.fetchone()[0]
                    cursor.execute(
                        "INSERT OR REPLACE INTO stats VALUES ('total', ?)",
                        (str(total),)
                    )
                    conn.commit()
                    return True
                except Exception as e:
                    print(f"[数据库] 删除失败: {e}")
                    return False

    def get_all_paths(self) -> set:
        """获取所有路径"""
        with self.lock:
            paths = set()
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT album, filename FROM files ORDER BY album, filename"
                )
                for album, filename in cursor.fetchall():
                    paths.add(f"{album}/{filename}")
            return paths

    def get_count(self) -> int:
        """获取文件总数"""
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM files")
                return cursor.fetchone()[0]

    def set_last_scan(self, timestamp: str):
        """设置最后扫描时间"""
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO stats VALUES ('last_scan', ?)",
                    (timestamp,)
                )
                conn.commit()

    def get_stats(self) -> dict:
        """获取统计信息"""
        with self.lock:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM stats")
                stats = dict(cursor.fetchall())
            return {
                "total": int(stats.get("total", 0)),
                "last_scan": stats.get("last_scan")
            }

db = SyncDB(DB_FILE, DB_JSON_FILE)

def _verify_and_clean_db():
    """验证数据库记录，删除实际不存在的文件记录"""
    photos_dir = get_photos_dir()
    removed = 0

    with db.lock:
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT album, sha256, filename FROM files")
            rows = cursor.fetchall()

            for album, sha256, filename in rows:
                file_path = photos_dir / album / filename
                if not file_path.exists():
                    db.remove_from_album(album, sha256)
                    removed += 1

    if removed > 0:
        print(f"[数据库] 清理了 {removed} 个不存在的文件记录")

    return removed


# ─── 工具函数 ─────────────────────────────────────────────
PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".mp4", ".mov", ".avi", ".mkv", ".heic", ".heif",
}


def add_to_album_index(album: str, sha256: str, filename: str, size: int = 0):
    """上传新文件后，将其加入数据库（原子操作）"""
    mtime = datetime.now().timestamp()
    db.add_to_album(album, sha256, filename, size, mtime)


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
    """获取本机可用的 IPv4 地址列表（去重，优先展示非回环地址）。"""
    ips = set()

    # 优先加入默认出口 IP
    primary_ip = get_local_ip()
    if primary_ip:
        ips.add(primary_ip)

    # 再从主机名解析补充可用地址
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            ip = item[4][0]
            if ip and ip != "0.0.0.0":
                ips.add(ip)
    except OSError:
        pass

    # 确保至少有一个可返回地址
    if not ips:
        ips.add("127.0.0.1")

    # 非回环地址优先，回环地址放最后
    return sorted(ips, key=lambda ip: (ip.startswith("127."), ip))


def get_photos_dir() -> Path:
    # 仅返回当前配置路径，不在读取状态时隐式创建目录。
    # 目录创建应由“保存并确认”或实际写入流程触发。
    return config.storage_path


def is_in_album_synced(album: str, hash_value: str) -> bool:
    """检查相册内是否已有该哈希，并验证文件真实存在。"""
    if not hash_value or not db.has_in_album(album, hash_value):
        return False

    with db.lock:
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT filename FROM files WHERE album = ? AND sha256 = ?",
                (album, hash_value)
            )
            row = cursor.fetchone()
            if row:
                filename = row[0]
                file_path = get_photos_dir() / album / filename
                if file_path.exists():
                    return True
                db.remove_from_album(album, hash_value)
                print(f"[验证] 文件不存在已清理: {album}/{filename}")

    return False


def normalize_client_hashes(raw_hash: str) -> list[str]:
    """将客户端上报哈希规范化为可在电脑端数据库查询的候选值。"""
    if not raw_hash:
        return []

    value = raw_hash.strip().lower()
    if not value:
        return []

    # 兼容三种存储格式：sha256(64)、md5(32)、md5:xxxx
    if value.startswith("md5:") and len(value) == 36:
        return [value, value[4:]]
    if len(value) == 64:
        return [value]
    if len(value) == 32:
        return [f"md5:{value}", value]
    return [value]


def sanitize_filename(name: str) -> str:
    """清理客户端上传文件名，禁止路径穿越。"""
    if not name:
        return ""
    normalized = name.replace("\\", "/")
    base = normalized.split("/")[-1].strip()
    if base in ("", ".", ".."):
        return ""
    return base


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


def setup_adb_reverse(serial: Optional[str] = None) -> bool:
    """为指定设备设置 ADB reverse 端口转发，返回是否成功"""
    try:
        host_port = int(get_server_port())

        # Android 端 USB/ADB 模式历史默认使用 localhost:8920。
        # 为了兼容“服务器实际运行端口 != 8920”的情况，增加 8920 -> host_port 的跨端口 reverse。
        legacy_device_port = 8920

        mappings: list[tuple[int, int]] = []
        if host_port != legacy_device_port:
            mappings.append((legacy_device_port, host_port))
        # 同端口映射也保留：当手机端配置为与服务端一致的端口时仍可用。
        mappings.append((host_port, host_port))

        adb_prefix = ["-s", serial] if serial else []

        all_ok = True
        for device_port, target_port in mappings:
            # 先尝试移除旧映射，避免重复/冲突（移除失败不影响后续设置）
            try:
                _run_adb(*adb_prefix, "reverse", "--remove", f"tcp:{device_port}", timeout=5)
            except Exception:
                pass

            result = _run_adb(
                *adb_prefix,
                "reverse",
                f"tcp:{device_port}",
                f"tcp:{target_port}",
                timeout=5,
            )
            if result.returncode == 0:
                print(f"ADB reverse 端口转发已设置: tcp:{device_port} -> tcp:{target_port}")
            else:
                all_ok = False
                print(f"ADB reverse 设置失败 (tcp:{device_port} -> tcp:{target_port}): {result.stderr}")

        return all_ok
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
    "speed": 0.0,      # MB/s
    "bytes_sent": 0,   # 已传输字节数
    "eta": 0,
    "log": [],
}
adb_status_lock = threading.Lock()

# ─── WiFi 同步状态（手机端上传时更新）─────────────────────
wifi_sync_status = {
    "running": False,
    "phase": "",
    "connected": False,        # 手机是否已连接
    "connection_type": "",    # "wifi" 或 "adb"
    # 控制权：谁发起同步，谁拥有“请求/模式切换”等控制权。
    # phone 仍会持续上报进度，但不应在 pc 发起时覆盖关键控制字段。
    "control_owner": "",      # "pc" | "phone" | ""
    "pc_request_sync": False,  # PC 端请求手机开始同步
    "pc_request_stop": False,  # PC 端请求手机停止同步
    "stop_requested_at": None,  # 记录停止请求时间，避免长期卡在 stopping
    "requested_sync_mode": "incremental",  # PC 请求的同步模式
    "sync_mode": "incremental",  # 当前进行中的同步模式
    "pc_total": 0,
    "device": "",
    "phone_total": 0,
    "need_sync": 0,
    "synced": 0,
    "skipped": 0,
    "failed": 0,
    "current": "",
    "start_time": None,
    "speed": 0.0,          # MB/s
    "bytes_sent": 0,       # 已传输字节数
    "eta": 0,
    "log": [],
    "phone_log": [],
}
wifi_status_lock = threading.RLock()
full_prepare_token = 0


def _wifi_is_active_locked(status: dict) -> bool:
    """判断 WiFi 同步状态机是否处于活跃/占用态（需要控制权）。"""
    phase = (status.get("phase") or "").strip().lower()
    running = bool(status.get("running", False))
    if running:
        return True
    return phase in {"requested", "preparing_full", "scanning", "syncing", "stopping"}

# 本次同步的照片列表（最多保留 50 条）
recent_synced_photos = []
recent_photos_lock = threading.Lock()

PHONE_PHOTO_DIRS = [
    "/sdcard/DCIM/Camera",
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Pictures/Screenshots",
]


def _adb_sync_log(msg: str):
    with adb_status_lock:
        adb_sync_status["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(adb_sync_status["log"]) > 500:
            adb_sync_status["log"] = adb_sync_status["log"][-300:]


def _wifi_sync_log(msg: str):
    with wifi_status_lock:
        log_list = wifi_sync_status.setdefault("log", [])
        log_list.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(log_list) > 500:
            wifi_sync_status["log"] = log_list[-300:]


def _wifi_phone_log(msg: str):
    with wifi_status_lock:
        log_list = wifi_sync_status.setdefault("phone_log", [])
        log_list.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(log_list) > 500:
            wifi_sync_status["phone_log"] = log_list[-300:]


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


def _adb_get_hash(serial: str, remote_path: str) -> str:
    """获取手机文件的 SHA-256（使用 busybox sha256sum）

    如果设备不支持 sha256sum，回退到 md5sum
    在计算后自动转换为统一格式
    """
    try:
        # 尝试使用 SHA-256
        result = _run_adb("-s", serial, "shell", f"sha256sum '{remote_path}'", timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]

        # 回退到 MD5（兼容旧设备）
        result = _run_adb("-s", serial, "shell", f"md5sum '{remote_path}'", timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            md5_hash = result.stdout.strip().split()[0]
            # 转换为 md5: 前缀格式，便于后续识别迁移
            return f"md5:{md5_hash}"

        return ""
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

    with adb_status_lock:
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
            "bytes_sent": 0,
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
    need_sync_hash = {}
    skipped = 0

    for i, remote_path in enumerate(all_files):
        if not adb_sync_status["running"]:
            _adb_sync_log("同步已取消")
            return

        filename = Path(remote_path).name
        adb_sync_status["current"] = f"校验 ({i + 1}/{len(all_files)}): {filename}"

        file_hash = _adb_get_hash(serial, remote_path)
        if not file_hash:
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
        if is_in_album_synced(album, file_hash):
            skipped += 1
            adb_sync_status["skipped"] = skipped
        else:
            need_sync_files.append((remote_path, album))
            need_sync_hash[remote_path] = file_hash

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
    adb_sync_status["bytes_sent"] = 0
    _adb_sync_log(f"开始同步 {len(need_sync_files)} 个文件...")

    for remote_path, album in need_sync_files:
        if not adb_sync_status["running"]:
            _adb_sync_log("同步已取消")
            break

        filename = Path(remote_path).name
        adb_sync_status["current"] = filename

        file_hash = need_sync_hash.get(remote_path, "")
        if not file_hash:
            file_hash = _adb_get_hash(serial, remote_path)
            if not file_hash:
                adb_sync_status["failed"] += 1
                _adb_sync_log(f"哈希计算失败: {filename}")
                continue

        # 相册内去重再次检查
        if is_in_album_synced(album, file_hash):
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
            # 获取文件大小
            file_size = save_path.stat().st_size if save_path.exists() else 0
            adb_sync_status["bytes_sent"] += file_size
            add_to_album_index(album, file_hash, save_path.name, file_size)
            adb_sync_status["synced"] += 1
            adb_sync_status["pc_total"] = db.get_count()
            _adb_sync_log(f"已同步: {album}/{filename}")
        else:
            adb_sync_status["failed"] += 1
            _adb_sync_log(f"失败: {filename}")

        # 计算速度和ETA (MB/s)
        start_time = adb_sync_status.get("start_time")
        bytes_sent = adb_sync_status["bytes_sent"]
        if start_time and bytes_sent > 0:
            elapsed = datetime.now().timestamp() - start_time
            if elapsed > 0:
                speed_mb = (bytes_sent / 1024 / 1024) / elapsed
                adb_sync_status["speed"] = round(speed_mb, 2)
                remaining = len(need_sync_files) - \
                    adb_sync_status["synced"] - adb_sync_status["failed"]
                if speed_mb > 0:
                    avg_bytes = bytes_sent / \
                        adb_sync_status["synced"] if adb_sync_status["synced"] > 0 else 0
                    remaining_bytes = avg_bytes * remaining
                    adb_sync_status["eta"] = int(remaining_bytes / 1024 / 1024 / speed_mb)

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


def _request_is_https(request: Request) -> bool:
    """判断请求是否来自 HTTPS（兼容反向代理头）。"""
    if request.url.scheme.lower() == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https"


@app.middleware("http")
async def enforce_https_middleware(request: Request, call_next):
    if bool(config.data.get("enforce_https", False)):
        if not _request_is_https(request):
            host = request.url.hostname or get_local_ip()
            target = f"https://{host}:{get_server_port()}{request.url.path}"
            if request.url.query:
                target = f"{target}?{request.url.query}"
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    status_code=426,
                    content={"status": "error", "message": "HTTPS required", "redirect": target},
                )
            return RedirectResponse(url=target, status_code=307)
    return await call_next(request)


# ─── API 路由 ────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    raw_theme = (config.data.get("ui_theme") or "").strip().lower()
    ui_theme = raw_theme if raw_theme in {"dark", "light"} else ""
    raw_conn = (config.data.get("connection_type") or "wifi").strip().lower()
    preferred_conn_type = raw_conn if raw_conn in {"wifi", "adb"} else "wifi"
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "ui_theme": ui_theme, "preferred_conn_type": preferred_conn_type},
    )


@app.get("/api/status")
async def get_status():
    ip = get_local_ip()
    port = get_server_port()
    scheme = get_server_scheme()
    all_ips = get_all_local_ips()
    adb_available = check_adb()
    adb_devices = get_adb_devices(include_emulators=False) if adb_available else []
    all_adb_devices = get_adb_devices(include_emulators=True) if adb_available else []

    # 使用数据库中的数量
    total_synced = db.get_count()
    stats = db.get_stats()

    return {
        "server_ip": ip,
        "server_port": port,
        "server_url": f"{scheme}://{ip}:{port}",
        "all_urls": [f"{scheme}://{i}:{port}" for i in all_ips],
        "adb_available": adb_available,
        "adb_devices": adb_devices,
        "all_adb_devices": all_adb_devices,
        "connection_type": config.data.get("connection_type", "wifi"),
        "ui_theme": (config.data.get("ui_theme") or ""),
        "total_synced": total_synced,
        "storage_path": str(get_photos_dir().resolve()),
        "upload_rate_limit_kbps": int(config.data.get("upload_rate_limit_kbps", 0) or 0),
        "tls_enabled": bool(config.data.get("tls_enabled", False)),
        "enforce_https": bool(config.data.get("enforce_https", False)),
    }


@app.post("/api/settings/theme")
async def set_ui_theme(theme: str = Form("")):
    """保存 Web UI 主题偏好（服务重启后仍生效）。"""
    t = (theme or "").strip().lower()
    if t not in {"dark", "light", ""}:
        return {"status": "error", "message": "theme 必须是 dark 或 light"}
    config.data["ui_theme"] = t
    config.save()
    return {"status": "ok", "message": "主题已保存", "ui_theme": t}


@app.get("/api/qrcode")
async def get_qrcode(url: str = ""):
    """生成服务器地址二维码"""
    if not url:
        ip = get_local_ip()
        url = f"{get_server_scheme()}://{ip}:{get_server_port()}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/api/settings/port")
async def set_server_port(port: str = Form("")):
    """设置服务器端口"""
    try:
        p = int(port)
        if p < 1024 or p > 65535:
            return {"status": "error", "message": "端口号应在 1024-65535 之间"}
        config.data["server_port"] = p
        config.save()
        print(f"[设置] 端口已保存为: {p}")

        # 更新 ADB 端口转发
        if check_adb():
            devices = get_adb_devices()
            for device in devices:
                setup_adb_reverse(device.get("serial", device))

        return {
            "status": "ok",
            "message": f"端口已保存为 {p}。ADB 端口转发已更新，WiFi 连接需要手机重新连接"
        }
    except Exception as e:
        print(f"[错误] 保存端口失败: {e}")
        return {"status": "error", "message": f"设置失败: {e}"}


@app.post("/api/settings/upload-limit")
async def set_upload_limit(limit_kbps: str = Form("0")):
    """设置上传速度限制（KB/s），0 表示不限速。"""
    try:
        v = int(limit_kbps)
        if v < 0:
            return {"status": "error", "message": "限速不能小于 0"}
        config.data["upload_rate_limit_kbps"] = v
        config.save()
        return {
            "status": "ok",
            "message": f"上传限速已设置为 {v} KB/s",
            "upload_rate_limit_kbps": v,
        }
    except ValueError:
        return {"status": "error", "message": "请输入有效整数"}


@app.post("/api/settings/tls")
async def set_tls_settings(
    enabled: str = Form("false"),
    cert_file: str = Form(""),
    key_file: str = Form(""),
    enforce_https: str = Form("false"),
):
    """设置 TLS 参数（重启后生效）。"""
    enabled_flag = enabled.strip().lower() in ("1", "true", "yes", "on")
    enforce_flag = enforce_https.strip().lower() in ("1", "true", "yes", "on")
    cert_text = cert_file.strip()
    key_text = key_file.strip()

    if enabled_flag:
        if not cert_text or not key_text:
            return {"status": "error", "message": "启用 TLS 需要证书和私钥路径"}
        cert_path = resolve_config_path(cert_text)
        key_path = resolve_config_path(key_text)
        if not cert_path.exists() or not key_path.exists():
            return {"status": "error", "message": "证书或私钥文件不存在"}

    config.data["tls_enabled"] = enabled_flag
    config.data["enforce_https"] = enforce_flag if enabled_flag else False
    config.data["tls_cert_file"] = cert_text
    config.data["tls_key_file"] = key_text
    config.save()

    return {
        "status": "ok",
        "message": "TLS 设置已保存，重启服务后生效",
        "tls_enabled": enabled_flag,
        "enforce_https": config.data["enforce_https"],
    }


@app.post("/api/settings/storage")
async def set_storage_path(
    path: str = Form(""),
    confirm_create: str = Form("false"),
):
    try:
        if path:
            new_path = Path(path)
            path_key = str(new_path.resolve(strict=False))
            should_confirm_create = confirm_create.strip().lower() == "true"

            # 两步确认：第一次永远返回 need_confirm，第二次明确确认才创建
            pending = getattr(set_storage_path, "_pending_create_paths", set())
            setattr(set_storage_path, "_pending_create_paths", pending)

            if not new_path.exists():
                if (not should_confirm_create) or (path_key not in pending):
                    pending.add(path_key)
                    return {
                        "status": "need_confirm",
                        "message": f"路径不存在：{new_path}，是否创建该目录？",
                        "path": str(new_path),
                    }
                pending.discard(path_key)
            else:
                pending.discard(path_key)

            new_path.mkdir(parents=True, exist_ok=True)
            resolved = str(new_path.resolve())
            config.data["storage_path"] = resolved
        else:
            # 空路径使用默认
            config.data["storage_path"] = str(DEFAULT_STORAGE)
        config.save()
        return {"status": "ok", "message": "存储路径已更新", "path": config.data["storage_path"]}
    except Exception as e:
        return {"status": "error", "message": f"设置失败: {e}"}


@app.post("/api/settings/connection")
async def set_connection_type(conn_type: str = Form(...)):
    """设置连接方式：wifi 或 adb"""
    if conn_type not in ("wifi", "adb"):
        return {"status": "error", "message": "无效的连接类型"}
    config.data["connection_type"] = conn_type
    config.save()
    return {"status": "ok", "message": f"已切换到 {conn_type.upper()} 连接"}


@app.post("/api/phone/register")
async def phone_register(device: str = Form(""), connection_type: str = Form("wifi")):
    """手机端注册连接状态"""
    with wifi_status_lock:
        wifi_sync_status["connected"] = True
        wifi_sync_status["connection_type"] = connection_type
        wifi_sync_status["device"] = device or "未知设备"
    return {"status": "ok", "message": "已注册连接"}


@app.post("/api/phone/unregister")
async def phone_unregister():
    """手机端断开连接"""
    with wifi_status_lock:
        wifi_sync_status["connected"] = False
        wifi_sync_status["connection_type"] = ""
        wifi_sync_status["device"] = ""
    return {"status": "ok", "message": "已断开连接"}


@app.post("/api/test-connection")
async def test_connection(conn_type: str = Form(...), device_serial: str = Form("")):
    """测试连接是否稳定"""
    if conn_type == "wifi":
        # WiFi 模式：检查手机是否已连接
        with wifi_status_lock:
            connected = wifi_sync_status.get("connected")
            conn_value = wifi_sync_status.get("connection_type", "wifi")
        if not connected:
            return {"status": "error", "message": "手机未连接，请在手机端打开 App 连接"}

        # 只有手机实际是 WiFi 连接时，WiFi 测试才算成功；避免出现“WiFi 正常 (ADB)”的误导。
        if str(conn_value).lower() != "wifi":
            return {
                "status": "error",
                "message": "手机当前为 USB ADB 连接，请选择“有线 ADB”或在手机端重新用 WiFi 连接",
            }

        return {"status": "ok", "message": "WiFi 连接正常"}

    elif conn_type == "adb":
        # ADB 模式：测试设备连接
        if not check_adb():
            return {"status": "error", "message": "ADB 不可用"}

        if not device_serial:
            return {"status": "error", "message": "请先选择设备"}

        try:
            result = _run_adb("-s", device_serial, "shell", "echo", "ok", timeout=5)
            if result.returncode == 0 and "ok" in result.stdout:
                # 顺便设置 reverse，确保手机端 USB 模式可通过 localhost:<port> 访问到 PC 服务
                reverse_ok = setup_adb_reverse(device_serial)
                if reverse_ok:
                    return {"status": "ok", "message": f"ADB 连接正常，端口转发已设置 ({device_serial})"}
                return {"status": "error", "message": f"ADB 连接正常，但端口转发设置失败 ({device_serial})"}
            else:
                return {"status": "error", "message": "设备无响应"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "连接超时"}
        except Exception as e:
            return {"status": "error", "message": f"连接失败: {e}"}

    return {"status": "error", "message": "未知的连接类型"}


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



# 扫描进度（统一用于 /api/settings/scan-* 与 /api/scan/*）
scan_progress = {
    "running": False,
    "phase": "",
    "current": "",
    "total": 0,
    "scanned": 0,
    "added": 0,
    "removed": 0,
    "start_time": None,
    "log": [],
}


def scan_local_photos():
    """后台扫描当前存储路径，重建数据库索引（使用 SHA-256）"""
    global scan_progress
    photos_dir = get_photos_dir()

    if not photos_dir.exists():
        scan_progress["running"] = False
        return

    scan_progress["phase"] = "scanning"
    scan_progress["start_time"] = datetime.now().timestamp()
    scan_progress["log"] = [f"开始扫描: {photos_dir}"]

    # 收集所有图片文件
    extensions = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".mp4", ".mov", ".avi"}
    local_files = {}  # (album, sha256) -> {filename, size, mtime}
    all_files = []

    for root, _dirs, files in os.walk(str(photos_dir)):
        for f in files:
            ext = Path(f).suffix.lower()
            if ext in extensions:
                all_files.append(Path(root) / f)

    scan_progress["total"] = len(all_files)
    scan_progress["scanned"] = 0
    scan_progress["added"] = 0
    scan_progress["removed"] = 0

    # 第一步：扫描本地所有文件，计算 SHA-256
    for file_path in all_files:
        if not scan_progress["running"]:
            break
        try:
            sha256 = compute_file_hash(file_path)
            if not sha256:
                continue

            rel_path = file_path.relative_to(photos_dir)
            parts = str(rel_path).replace("\\", "/").split("/")
            album = parts[0] if len(parts) > 1 else "unsorted"
            filename = "/".join(parts[1:]) if len(parts) > 1 else parts[0]

            local_files[(album, sha256)] = {
                "filename": filename,
                "size": file_path.stat().st_size,
                "mtime": file_path.stat().st_mtime
            }

            scan_progress["scanned"] += 1
            scan_progress["current"] = filename
        except Exception:
            continue

    # 第二步：重建数据库（清空旧索引，再写入当前路径扫描结果）
    with db.lock:
        with db._get_connection() as conn:
            cursor = conn.cursor()

            # 记录重建前条数，用于统计删除数量
            cursor.execute("SELECT COUNT(*) FROM files")
            old_total = cursor.fetchone()[0]

            cursor.execute("DELETE FROM files")

            for (album, sha256), info in local_files.items():
                cursor.execute(
                    """INSERT OR REPLACE INTO files
                       (album, sha256, filename, size, mtime)
                       VALUES (?, ?, ?, ?, ?)""",
                    (album, sha256, info["filename"], info["size"], info["mtime"])
                )

            new_total = len(local_files)
            cursor.execute(
                "INSERT OR REPLACE INTO stats VALUES ('total', ?)",
                (str(new_total),)
            )
            conn.commit()

    added = new_total
    removed = old_total

    db.set_last_scan(datetime.now().isoformat())

    scan_progress["added"] = added
    scan_progress["removed"] = removed
    scan_progress["phase"] = "done"
    scan_progress["running"] = False
    scan_progress["current"] = f"完成 - 重建后总数: {new_total}"
    scan_progress["log"].append(
        f"扫描完成，已按当前路径重建索引。原记录: {old_total}，现记录: {new_total}"
    )


@app.post("/api/settings/scan-local")
async def scan_local_photos_api():
    """扫描本地照片并更新数据库"""
    global scan_progress

    if scan_progress["running"]:
        return {"status": "error", "message": "扫描正在进行中"}

    scan_progress["running"] = True
    scan_progress["phase"] = "scanning"
    scan_progress["current"] = "开始扫描..."
    scan_progress["total"] = 0
    scan_progress["scanned"] = 0
    scan_progress["added"] = 0
    scan_progress["removed"] = 0
    scan_progress["start_time"] = datetime.now().timestamp()
    scan_progress["log"] = []

    # 后台执行扫描
    t = threading.Thread(target=scan_local_photos, daemon=True)
    t.start()

    return {"status": "ok", "message": "扫描已开始"}


@app.get("/api/settings/scan-status")
async def get_scan_status_settings():
    """获取扫描状态"""
    return scan_progress


# ─── 本地扫描 API ────────────────────────────────────────
@app.post("/api/scan/start")
async def start_scan():
    """启动本地扫描，同步数据库与实际文件"""
    if scan_progress["running"]:
        return {"status": "error", "message": "扫描正在进行中"}
    return await scan_local_photos_api()


@app.get("/api/scan/status")
async def get_scan_status():
    """获取扫描进度"""
    stats = db.get_stats()
    return {
        "running": scan_progress["running"],
        "phase": scan_progress["phase"],
        "total": scan_progress["total"],
        "scanned": scan_progress["scanned"],
        "added": scan_progress["added"],
        "removed": scan_progress["removed"],
        "current": scan_progress["current"],
        "log": scan_progress["log"][-20:] if scan_progress["log"] else [],
        "db_total": db.get_count(),
        "db_last_scan": stats.get("last_scan"),
    }


# ─── 统计计数器 ────────────────────────────────────────────
check_stats = {"total": 0, "synced": 0, "not_synced": 0}
check_last_print = 0  # 上次打印时的计数
upload_stats = {"total": 0, "skipped": 0, "success": 0}
upload_last_print = 0  # 上次打印时的计数


def reset_stats():
    """重置统计计数器和最近同步列表"""
    global check_stats, check_last_print, upload_stats, upload_last_print, recent_synced_photos
    check_stats = {"total": 0, "synced": 0, "not_synced": 0}
    check_last_print = 0
    upload_stats = {"total": 0, "skipped": 0, "success": 0}
    upload_last_print = 0
    recent_synced_photos = []  # 清空本次同步的照片列表
    print()  # 换行，开始新的同步


@app.post("/api/check_album")
async def check_album(items: list[dict]):
    """批量检查相册内是否已存在（相册内去重，支持 SHA-256）

    输入: [{"album": "Camera", "sha256": "xxx"}, ...] (推荐)
    或   [{"album": "Camera", "md5": "xxx"}, ...] (兼容旧版本)

    输出: {"album|hash": true/false, ...}
    """
    global check_stats, check_last_print
    if not items:
        return {}

    results = {}
    synced_count = 0
    not_synced_count = 0

    for item in items:
        album = item.get("album", "unsorted")

        # 兼容：优先 sha256，回退到 md5
        hash_value = item.get("sha256") or item.get("md5", "")
        key = f"{album}|{hash_value}"

        if not hash_value:
            results[key] = False
            not_synced_count += 1
            continue

        # 检查该相册内是否有该哈希
        in_album = db.has_in_album(album, hash_value)

        if in_album:
            # 验证文件是否存在
            with db.lock:
                with db._get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT filename FROM files WHERE album = ? AND sha256 = ?",
                        (album, hash_value)
                    )
                    row = cursor.fetchone()
                    if row:
                        filename = row[0]
                        file_path = get_photos_dir() / album / filename
                        if file_path.exists():
                            results[key] = True
                            synced_count += 1
                        else:
                            db.remove_from_album(album, hash_value)
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

    # 每100张输出一次进度
    total = check_stats["total"]
    if total - check_last_print >= 100 or (total > 0 and check_last_print == 0):
        print(
            f"[相册检查] 总计: {total}, 已同步: {check_stats['synced']}, 需同步: {check_stats['not_synced']}"
        )
        check_last_print = total

    return results


@app.post("/api/check")
async def check_files(hashes: list[str]):
    """旧接口：批量检查哈希是否已存在（兼容旧客户端）"""
    global check_stats
    if not hashes:
        return {}

    results = {}
    synced_count = 0
    not_synced_count = 0

    for h in hashes:
        found = False
        with db.lock:
            with db._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT album, filename FROM files WHERE sha256 = ?",
                    (h,)
                )
                rows = cursor.fetchall()

                for album, filename in rows:
                    file_path = get_photos_dir() / album / filename
                    if file_path.exists():
                        found = True
                        break

                # 清理已失效记录
                if not found and rows:
                    for album, _filename in rows:
                        db.remove_from_album(album, h)

        results[h] = found
        if found:
            synced_count += 1
        else:
            not_synced_count += 1

    check_stats["total"] += len(hashes)
    check_stats["synced"] += synced_count
    check_stats["not_synced"] += not_synced_count

    return results


@app.post("/api/check_manifest")
async def check_manifest(items: list[dict]):
    """按清单比对电脑端数据库是否已存在（album + filename + size）。"""
    if not items:
        return {}

    results = {}

    with db.lock:
        with db._get_connection() as conn:
            cursor = conn.cursor()
            for item in items:
                album = (item.get("album") or "unsorted").strip() or "unsorted"
                filename = (item.get("filename") or "").strip()
                try:
                    size = int(item.get("size") or 0)
                except (TypeError, ValueError):
                    size = 0

                key = f"{album}|{filename}|{size}"
                if not filename:
                    results[key] = False
                    continue

                cursor.execute(
                    "SELECT filename, size FROM files WHERE album = ? AND filename = ? LIMIT 1",
                    (album, filename),
                )
                row = cursor.fetchone()
                if not row:
                    results[key] = False
                    continue

                db_size = int(row[1] or 0)
                # 以文件名 + 文件大小为准做存在性判断，避免仅按数量误判
                results[key] = (db_size == size)

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
    sync_mode: str = Form("incremental"),
):
    """手机端扫描进度更新 - 自动同步本地文件与数据库"""
    # 首次扫描时，验证并清理数据库中不存在的文件记录
    if scanned == 1:
        _verify_and_clean_db()

    # 使用数据库中的数量
    pc_total = db.get_count()
    mode_value = (sync_mode or "incremental").strip().lower()
    if mode_value not in ("incremental", "full"):
        mode_value = "incremental"

    with wifi_status_lock:
        if scanned <= 1:
            wifi_sync_status["log"] = []
            wifi_sync_status["phone_log"] = []
        wifi_sync_status.update({
            "running": True,
            "phase": "scanning",
            "sync_mode": mode_value,
            "requested_sync_mode": mode_value,
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
            "bytes_sent": 0,
            "eta": 0,
        })

    if scanned <= 1:
        _wifi_sync_log(f"手机端开始扫描（{mode_value}）")
    elif total > 0 and (scanned == total or scanned % 500 == 0):
        _wifi_sync_log(f"扫描进度: {scanned}/{total}")
    return {"status": "ok"}


@app.post("/api/wifi/start")
async def wifi_sync_start(
    device: str = Form(""),
    phone_total: int = Form(0),
    need_sync: int = Form(0),
    connection_type: str = Form("wifi"),
    sync_mode: str = Form("incremental"),
):
    """手机端开始同步时调用，报告统计信息"""
    # 重置统计计数器
    reset_stats()

    pc_total = db.get_count()

    # 更新连接状态
    mode_value = (sync_mode or "incremental").strip().lower()
    if mode_value not in ("incremental", "full"):
        mode_value = "incremental"

    with wifi_status_lock:
        wifi_sync_status["log"] = []
        wifi_sync_status["phone_log"] = []
        wifi_sync_status["connected"] = True
        wifi_sync_status["pc_request_stop"] = False
        # 只有在“手机端发起同步”时才允许用 start 上报覆盖 connection_type。
        # 若 control_owner=pc（PC 发起），则保持 PC/手机 register/unregister 维护的实际连接来源。
        if wifi_sync_status.get("control_owner") != "pc":
            wifi_sync_status["control_owner"] = "phone"
            wifi_sync_status["connection_type"] = connection_type
        wifi_sync_status.update({
            "running": True,
            "phase": "syncing",
            "sync_mode": mode_value,
            "requested_sync_mode": mode_value,
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
    _wifi_sync_log(
        f"同步开始（{mode_value}）: 设备={device or '未知设备'}，手机总数={phone_total}，需同步={need_sync}"
    )
    return {"status": "ok", "message": "同步已开始"}


@app.post("/api/wifi/progress")
async def wifi_sync_progress(
    current: str = Form(""),
    synced: int = Form(0),
    skipped: int = Form(0),
    failed: int = Form(0),
    bytes_sent: int = Form(0),
    speed_mbps: float = Form(-1.0),
    eta_seconds: int = Form(-1),
):
    """手机端上传过程中更新进度"""
    with wifi_status_lock:
        wifi_sync_status["current"] = current
        wifi_sync_status["synced"] = synced
        wifi_sync_status["skipped"] = skipped
        wifi_sync_status["failed"] = failed
        wifi_sync_status["bytes_sent"] = bytes_sent

    # 更新 PC 文件数量
    with wifi_status_lock:
        wifi_sync_status["pc_total"] = db.get_count()

    # 优先使用手机端实时上报的上传速度/剩余上传时间（两端统一口径）
    if speed_mbps >= 0 and eta_seconds >= 0:
        with wifi_status_lock:
            wifi_sync_status["speed"] = round(float(speed_mbps), 2)
            wifi_sync_status["eta"] = int(eta_seconds)
        return {"status": "ok"}

    # 计算速度和剩余时间 (MB/s)
    with wifi_status_lock:
        start = wifi_sync_status.get("start_time")
    if start and bytes_sent > 0:
        elapsed = datetime.now().timestamp() - start
        if elapsed > 0:
            # 字节转换为 MB/s
            speed_mb = (bytes_sent / 1024 / 1024) / elapsed
            with wifi_status_lock:
                wifi_sync_status["speed"] = round(speed_mb, 2)
            # 剩余时间基于文件数量估算
            with wifi_status_lock:
                remaining = wifi_sync_status["need_sync"] - synced - skipped - failed
                if remaining < 0:
                    remaining = 0
            if speed_mb > 0:
                # 估算平均每个文件的字节数
                avg_bytes = bytes_sent / synced if synced > 0 else 0
                remaining_bytes = avg_bytes * remaining
                with wifi_status_lock:
                    wifi_sync_status["eta"] = int(
                        remaining_bytes / 1024 / 1024 / speed_mb) if speed_mb > 0 else 0

    return {"status": "ok"}


@app.post("/api/wifi/stop")
async def wifi_sync_stop(message: str = Form("")):
    """手机端同步完成或取消时调用"""
    with wifi_status_lock:
        wifi_sync_status["running"] = False
        wifi_sync_status["phase"] = "done"
        wifi_sync_status["current"] = message or "同步已完成"
        # 停止同步不代表断开连接；连接状态由 register/unregister 维护
        wifi_sync_status["pc_request_stop"] = False
        wifi_sync_status["stop_requested_at"] = None
        wifi_sync_status["pc_request_sync"] = False
        # 同步结束后释放控制权，允许另一端重新发起来改变优先级。
        wifi_sync_status["control_owner"] = ""
    _wifi_sync_log(message or "同步已完成")
    _wifi_phone_log(message or "同步已完成")
    db.set_last_scan(datetime.now().isoformat())
    return {"status": "ok"}


@app.post("/api/wifi/log")
async def wifi_sync_log(message: str = Form("")):
    """手机端上报同步日志，供网页端显示与手机一致的内容。"""
    msg = (message or "").strip()
    if msg:
        _wifi_phone_log(msg)
    return {"status": "ok"}


def _prepare_full_sync_then_request(conn_type: str, prepare_token: int):
    """后台执行全量同步前置刷新，完成后再向手机发起同步请求。"""
    try:
        scan_local_photos()

        with wifi_status_lock:
            # 若期间被取消或被新的请求抢占，直接退出，不覆盖当前状态
            if prepare_token != full_prepare_token:
                return

            # 若手机端已发起并占用控制权，则 PC 端全量准备线程不应再覆盖状态。
            if _wifi_is_active_locked(wifi_sync_status) and wifi_sync_status.get(
                    "control_owner") == "phone":
                return

            wifi_sync_status["pc_request_sync"] = True
            # PC 端发起同步请求不应覆盖手机实际连接方式；
            # connection_type 由手机端 register/unregister 维护。
            wifi_sync_status["control_owner"] = "pc"
            wifi_sync_status["requested_sync_mode"] = "full"
            wifi_sync_status["sync_mode"] = "full"
            wifi_sync_status["running"] = False
            wifi_sync_status["phase"] = "requested"
            wifi_sync_status["phone_total"] = 0
            wifi_sync_status["need_sync"] = 0
            wifi_sync_status["synced"] = 0
            wifi_sync_status["skipped"] = 0
            wifi_sync_status["failed"] = 0
            wifi_sync_status["speed"] = 0.0
            wifi_sync_status["eta"] = 0
            wifi_sync_status["bytes_sent"] = 0
            wifi_sync_status["current"] = (
                f"数据库刷新完成（{scan_progress.get('scanned', 0)}/{scan_progress.get('total', 0)}），"
                "等待手机开始全量同步..."
            )
        _wifi_sync_log("全量同步前置数据库刷新完成，已通知手机开始同步")
    except Exception as e:
        with wifi_status_lock:
            if prepare_token != full_prepare_token:
                return
            wifi_sync_status["running"] = False
            wifi_sync_status["phase"] = ""
            wifi_sync_status["current"] = f"全量同步准备失败: {e}"
        _wifi_sync_log(f"全量同步准备失败: {e}")


@app.post("/api/wifi/request-sync")
async def request_sync(
    conn_type: str = Form(""),
    sync_mode: str = Form("incremental"),
):
    """PC 端请求手机开始同步"""
    global full_prepare_token
    mode_value = (sync_mode or "incremental").strip().lower()
    if mode_value not in ("incremental", "full"):
        return {"status": "error", "message": "无效的同步模式"}

    if mode_value == "full":
        # 若手机端正在同步（手机发起），PC 端不能抢占；请先停止再从 PC 端发起。
        with wifi_status_lock:
            if _wifi_is_active_locked(wifi_sync_status) and wifi_sync_status.get(
                    "control_owner") == "phone":
                return {"status": "error", "message": "手机端正在同步（手机发起），请先在手机端停止同步"}

        if scan_progress["running"]:
            return {"status": "error", "message": "全量同步前正在刷新数据库，请稍候"}

        # 全量模式：后台先刷新数据库，完成后再发请求给手机
        scan_progress["running"] = True
        scan_progress["phase"] = "scanning"
        scan_progress["current"] = "开始扫描..."
        scan_progress["total"] = 0
        scan_progress["scanned"] = 0
        scan_progress["added"] = 0
        scan_progress["removed"] = 0
        scan_progress["start_time"] = datetime.now().timestamp()
        scan_progress["log"] = []

        with wifi_status_lock:
            full_prepare_token += 1
            current_prepare_token = full_prepare_token

            wifi_sync_status["pc_request_sync"] = False
            wifi_sync_status["pc_request_stop"] = False
            wifi_sync_status["control_owner"] = "pc"
            wifi_sync_status["requested_sync_mode"] = "full"
            wifi_sync_status["sync_mode"] = "full"
            wifi_sync_status["running"] = False
            wifi_sync_status["phase"] = "preparing_full"
            wifi_sync_status["phone_total"] = 0
            wifi_sync_status["need_sync"] = 0
            wifi_sync_status["synced"] = 0
            wifi_sync_status["skipped"] = 0
            wifi_sync_status["failed"] = 0
            wifi_sync_status["speed"] = 0.0
            wifi_sync_status["eta"] = 0
            wifi_sync_status["bytes_sent"] = 0
            wifi_sync_status["log"] = []
            wifi_sync_status["phone_log"] = []
            wifi_sync_status["current"] = "正在刷新数据库 0/0..."

        _wifi_sync_log("PC 端发起全量同步请求，开始刷新数据库")

        t = threading.Thread(
            target=_prepare_full_sync_then_request,
            args=(conn_type, current_prepare_token),
            daemon=True,
        )
        t.start()

        return {"status": "ok", "message": "已开始刷新数据库，完成后将自动发起全量同步请求"}

    # 增量模式：如果正在全量准备，先取消该准备流程，避免状态被后台线程覆盖
    with wifi_status_lock:
        preparing_full = wifi_sync_status.get("phase") == "preparing_full"
    if scan_progress.get("running") and preparing_full:
        scan_progress["running"] = False
        with wifi_status_lock:
            full_prepare_token += 1

    # 若手机端正在同步（手机发起），PC 端不能抢占；请先停止再从 PC 端发起。
    with wifi_status_lock:
        if _wifi_is_active_locked(wifi_sync_status) and wifi_sync_status.get(
                "control_owner") == "phone":
            return {"status": "error", "message": "手机端正在同步（手机发起），请先在手机端停止同步"}

    # 设置请求标志，手机端轮询时会收到
    with wifi_status_lock:
        wifi_sync_status["pc_request_sync"] = True
        wifi_sync_status["pc_request_stop"] = False
        # PC 端点击“同步”仅表示发起请求，不应强行指定/覆盖手机的连接方式。
        # connection_type 用于展示实际连接来源，由手机端 register/unregister 更新。
        wifi_sync_status["control_owner"] = "pc"
        wifi_sync_status["requested_sync_mode"] = mode_value
        wifi_sync_status["sync_mode"] = mode_value

        # 进入请求中状态，避免沿用上次 done 造成“秒完成”错觉
        wifi_sync_status["running"] = False
        wifi_sync_status["phase"] = "requested"
        wifi_sync_status["phone_total"] = 0
        wifi_sync_status["need_sync"] = 0
        wifi_sync_status["synced"] = 0
        wifi_sync_status["skipped"] = 0
        wifi_sync_status["failed"] = 0
        wifi_sync_status["speed"] = 0.0
        wifi_sync_status["eta"] = 0
        wifi_sync_status["bytes_sent"] = 0
        wifi_sync_status["log"] = []
        wifi_sync_status["phone_log"] = []
        if mode_value == "full":
            wifi_sync_status["current"] = "已完成数据库刷新，等待手机开始全量同步..."
        else:
            wifi_sync_status["current"] = "已发送增量同步请求，等待手机响应..."

    _wifi_sync_log(f"PC 端发起{('全量' if mode_value == 'full' else '增量')}同步请求")

    action_text = "全量同步" if mode_value == "full" else "增量同步"
    return {"status": "ok", "message": f"已向手机发送{action_text}请求"}


@app.post("/api/wifi/cancel-request")
async def cancel_sync_request():
    """取消 PC 端发起但尚未开始的同步请求"""
    global full_prepare_token
    with wifi_status_lock:
        # 若当前控制权属于手机端，则 PC 端不允许取消/改写状态；只能先停止再重新发起。
        if _wifi_is_active_locked(wifi_sync_status) and wifi_sync_status.get(
                "control_owner") == "phone":
            return {"status": "error", "message": "手机端发起的同步进行中，电脑端无法取消；请先在手机端停止"}

        if wifi_sync_status.get("phase") == "preparing_full":
            scan_progress["running"] = False
            full_prepare_token += 1
            wifi_sync_status["pc_request_sync"] = False
            wifi_sync_status["pc_request_stop"] = False
            wifi_sync_status["phase"] = ""
            wifi_sync_status["current"] = "已取消全量准备"
            wifi_sync_status["requested_sync_mode"] = "incremental"
            wifi_sync_status["sync_mode"] = "incremental"
            wifi_sync_status["control_owner"] = ""
            return {"status": "ok", "message": "已取消全量同步准备"}

        if wifi_sync_status.get("phase") == "requested" and not wifi_sync_status.get("running"):
            wifi_sync_status["pc_request_sync"] = False
            wifi_sync_status["pc_request_stop"] = False
            wifi_sync_status["phase"] = ""
            wifi_sync_status["current"] = "已取消同步请求"
            wifi_sync_status["requested_sync_mode"] = "incremental"
            wifi_sync_status["sync_mode"] = "incremental"
            wifi_sync_status["control_owner"] = ""
            return {"status": "ok", "message": "已取消同步请求"}

        if wifi_sync_status.get("running"):
            return {"status": "error", "message": "同步已开始，请在手机端点击停止"}

    return {"status": "ok", "message": "当前无待处理同步请求"}


@app.post("/api/wifi/request-stop")
async def request_sync_stop():
    """PC 端请求手机停止同步。"""
    log_message = ""
    response = {"status": "ok", "message": "当前无进行中的同步任务"}

    with wifi_status_lock:
        phase = wifi_sync_status.get("phase", "")
        running = bool(wifi_sync_status.get("running", False))

        if phase in ("requested", "preparing_full") and not running:
            wifi_sync_status["pc_request_sync"] = False
            wifi_sync_status["pc_request_stop"] = False
            wifi_sync_status["phase"] = ""
            wifi_sync_status["current"] = "已取消同步请求"
            log_message = "PC 端取消了待执行的同步请求"
            response = {"status": "ok", "message": "已取消同步请求"}

        elif running or phase in ("syncing", "scanning"):
            wifi_sync_status["pc_request_stop"] = True
            wifi_sync_status["phase"] = "stopping"
            wifi_sync_status["current"] = "已请求停止，等待手机端确认..."
            wifi_sync_status["stop_requested_at"] = datetime.now().timestamp()
            log_message = "PC 端发起停止请求，等待手机端确认"
            response = {"status": "ok", "message": "已发送停止请求"}

    if log_message:
        _wifi_sync_log(log_message)

    return response


@app.get("/api/wifi/check-request")
async def check_sync_request():
    """手机端轮询检查是否需要开始同步"""
    with wifi_status_lock:
        request = wifi_sync_status.get("pc_request_sync", False)
        requested_mode = wifi_sync_status.get("requested_sync_mode", "incremental")
    if request:
        # 清除请求标志
        with wifi_status_lock:
            wifi_sync_status["pc_request_sync"] = False
    return {"request_sync": request, "sync_mode": requested_mode}


@app.get("/api/wifi/check-stop-request")
async def check_stop_request():
    """手机端轮询检查是否需要停止同步。"""
    with wifi_status_lock:
        request_stop = bool(wifi_sync_status.get("pc_request_stop", False))
    if request_stop:
        with wifi_status_lock:
            wifi_sync_status["pc_request_stop"] = False
    return {"request_stop": request_stop}


@app.get("/api/wifi/status")
async def wifi_sync_get_status():
    """获取 WiFi 同步进度"""
    with wifi_status_lock:
        # 兜底：若停止请求长时间未被手机确认，自动收敛状态，避免前端长期卡在“停止中”
        if wifi_sync_status.get("phase") == "stopping":
            stop_ts = wifi_sync_status.get("stop_requested_at")
            elapsed = (datetime.now().timestamp() - stop_ts) if stop_ts else 0
            if elapsed >= 3:
                wifi_sync_status["running"] = False
                wifi_sync_status["pc_request_stop"] = False
                wifi_sync_status["pc_request_sync"] = False
                wifi_sync_status["phase"] = "done"
                wifi_sync_status["current"] = "停止超过3秒无响应，已强制结束本次同步"
                wifi_sync_status["stop_requested_at"] = None
                wifi_sync_status["control_owner"] = ""

        snapshot = dict(wifi_sync_status)
    with recent_photos_lock:
        recent = recent_synced_photos[-10:]
    return {
        **snapshot,
        "log": snapshot.get("log", [])[-200:],
        "phone_log": snapshot.get("phone_log", [])[-200:],
        "refresh_running": scan_progress.get("running", False),
        "refresh_total": scan_progress.get("total", 0),
        "refresh_scanned": scan_progress.get("scanned", 0),
        "refresh_current": scan_progress.get("current", ""),
        "recent_photos": recent,  # 最近 10 张
    }


@app.post("/api/upload")
async def upload_photo(
    file: UploadFile = File(...),
    file_hash: str = Form(""),
    original_name: str = Form(...),
    taken_date: str = Form(""),
    album: str = Form(""),
):
    """
    上传照片到特定相册（查重全部在电脑端完成）

    原子操作流程：
    1. 电脑端按客户端上报哈希做预检查（可跳过上传）
    2. 流式保存文件并由电脑端实算 SHA-256
    3. 电脑端按实算 SHA-256 终检查重（并发安全）
    4. 原子性地更新数据库
    """
    global upload_stats, upload_last_print, recent_synced_photos
    try:
        photos_dir = get_photos_dir()
        if album:
            sub_dir = album.replace("\\", "/").strip("/")
        else:
            sub_dir = "unsorted"

        save_dir = photos_dir / sub_dir
        save_dir.mkdir(parents=True, exist_ok=True)

        # 相册名规范化（防止路径遍历攻击）
        if ".." in sub_dir or sub_dir.startswith("/"):
            return {"status": "error", "message": "非法的相册名"}

        # 客户端哈希只用于电脑端预检查，最终仍以电脑端实算 SHA-256 为准
        candidate_hashes = normalize_client_hashes(file_hash)
        for h in candidate_hashes:
            if is_in_album_synced(sub_dir, h):
                upload_stats["total"] += 1
                upload_stats["skipped"] += 1
                return {"status": "skipped", "message": "相册内已存在相同文件"}

        safe_name = sanitize_filename(original_name)
        if not safe_name:
            return {"status": "error", "message": "非法文件名"}

        # 确定保存路径
        save_path = save_dir / safe_name
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            counter = 1
            while save_path.exists():
                save_path = save_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        filename = save_path.name

        # ─── 阶段2: 流式写入文件 + 计算 SHA-256 ───
        sha256_hash = hashlib.sha256()
        total_size = 0
        rate_limit_bps = get_upload_rate_limit_bps()
        throttle_start = time.monotonic()
        throttled_bytes = 0

        try:
            with open(save_path, "wb") as f:
                while True:
                    chunk = await file.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha256_hash.update(chunk)
                    total_size += len(chunk)

                    if rate_limit_bps > 0:
                        throttled_bytes += len(chunk)
                        expected_elapsed = throttled_bytes / rate_limit_bps
                        actual_elapsed = time.monotonic() - throttle_start
                        if expected_elapsed > actual_elapsed:
                            await asyncio.sleep(expected_elapsed - actual_elapsed)
        except Exception as write_err:
            # 写入失败，清理文件
            if save_path.exists():
                save_path.unlink()
            return {"status": "error", "message": f"文件保存失败: {write_err}"}

        # ─── 阶段3: 确定最终哈希值 ───
        # 最终哈希始终以服务端实算结果为准
        final_hash = sha256_hash.hexdigest()

        if candidate_hashes and final_hash not in candidate_hashes:
            print(f"[上传] 客户端哈希与服务端 SHA-256 不同，已按服务端结果处理: {safe_name}")

        # 再次检查去重（防止并发冲突）
        if is_in_album_synced(sub_dir, final_hash):
            save_path.unlink()  # 删除重复文件
            upload_stats["total"] += 1
            upload_stats["skipped"] += 1
            return {"status": "skipped", "message": "相册内已存在相同文件"}

        # ─── 阶段4: 原子性地更新数据库 ───
        # 使用数据库锁确保并发安全
        file_mtime = save_path.stat().st_mtime
        success = db.add_to_album(sub_dir, final_hash, filename, total_size, file_mtime)
        if not success:
            # 数据库添加失败，删除已保存的文件
            save_path.unlink()
            upload_stats["total"] += 1
            upload_stats["failed"] = upload_stats.get("failed", 0) + 1
            return {"status": "error", "message": "数据库更新失败"}

        # 添加到最近同步列表（最多保留 50 条）
        with recent_photos_lock:
            recent_synced_photos.append(f"{sub_dir}/{filename}")
            if len(recent_synced_photos) > 50:
                recent_synced_photos.pop(0)

        # 统计成功
        upload_stats["total"] += 1
        upload_stats["success"] += 1

        # 每100张输出一次进度
        total = upload_stats["total"]
        if total - upload_last_print >= 100 or (total > 0 and upload_last_print == 0):
            print(
                f"[上传统计] 总计: {total}, 成功: {upload_stats['success']}, 跳过: {upload_stats['skipped']}"
            )
            upload_last_print = total

        return {
            "status": "ok",
            "message": "上传成功",
            "path": f"{sub_dir}/{filename}",
            "sha256": final_hash,
            "size": total_size
        }
    except ConnectionResetError:
        return {"status": "error", "message": "连接被重置"}
    except Exception as e:
        print(f"[上传] 异常: {e}")
        return {"status": "error", "message": f"上传失败: {str(e)[:100]}"}


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
    photos_root = get_photos_dir().resolve()
    photo_path = (photos_root / path).resolve()

    try:
        photo_path.relative_to(photos_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法路径")

    if not photo_path.exists() or not photo_path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(str(photo_path))


# ─── ADB 同步 API ────────────────────────────────────────
@app.post("/api/adb/sync")
async def adb_sync_start(serial: str = Form(default="")):
    """启动 ADB USB 同步（先扫描再自动同步）"""
    with adb_status_lock:
        is_running = adb_sync_status["running"]
    if is_running:
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
    if serial and device is None:
        return {"status": "error", "message": f"未找到设备: {serial}"}
    if device is None:
        device = devices[0]

    t = threading.Thread(
        target=_run_adb_sync,
        args=(device["serial"], device["model"]),
        daemon=True,
    )
    t.start()

    return {
        "status": "ok",
        "message": f"已开始同步: {device['model']} ({device['serial']})",
        "device": device,
    }


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
    with adb_status_lock:
        adb_sync_status["running"] = False
    return {"status": "ok", "message": "正在停止同步"}


@app.get("/api/adb/status")
async def adb_sync_get_status():
    """获取 ADB 同步进度"""
    with adb_status_lock:
        return dict(adb_sync_status)


@app.post("/api/adb/setup-reverse")
async def adb_setup_reverse(serial: str = Form("")):
    """为指定设备设置 ADB reverse 端口转发"""
    if not check_adb():
        return {"status": "error", "message": "ADB 不可用"}

    if serial:
        # 为指定设备设置
        devices = [d for d in get_adb_devices(include_emulators=True) if d["serial"] == serial]
        if not devices:
            return {"status": "error", "message": f"设备 {serial} 未连接"}
    else:
        # 为所有设备设置
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

    # 启动时读取监听端口（监听端口运行中不可热切换）
    requested_port = get_server_port()
    run_port = requested_port

    # 端口冲突自动回退，避免 WinError 10048 直接退出
    if not is_port_available("0.0.0.0", run_port):
        fallback_port = find_available_port(run_port + 1, max_tries=100)
        if fallback_port is None:
            raise RuntimeError(f"端口 {run_port} 已被占用，且未找到可用回退端口")
        run_port = fallback_port
        config.data["server_port"] = run_port
        config.save()
        print(f"[启动] 端口 {requested_port} 被占用，已自动切换到 {run_port}")
    run_scheme = get_server_scheme()

    # 启动时清理不存在的文件记录
    _verify_and_clean_db()

    ip = get_local_ip()
    print(f"{'=' * 50}")
    print(f"  PhotoSync 服务器")
    print(f"  局域网地址: {run_scheme}://{ip}:{run_port}")
    print(f"  照片存储: {get_photos_dir().resolve()}")
    print(f"  数据库: {db.get_count()} 个文件")
    print(f"  上传限速: {int(config.data.get('upload_rate_limit_kbps', 0) or 0)} KB/s")
    print(f"  HTTPS 强制: {bool(config.data.get('enforce_https', False))}")

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

    uvicorn_kwargs = {
        "app": app,
        "host": "0.0.0.0",
        "port": run_port,
        "log_level": "warning",
    }

    if bool(config.data.get("tls_enabled", False)):
        cert_text = str(config.data.get("tls_cert_file", "") or "").strip()
        key_text = str(config.data.get("tls_key_file", "") or "").strip()
        cert_path = resolve_config_path(cert_text) if cert_text else None
        key_path = resolve_config_path(key_text) if key_text else None

        if cert_path and key_path and cert_path.exists() and key_path.exists():
            uvicorn_kwargs["ssl_certfile"] = str(cert_path)
            uvicorn_kwargs["ssl_keyfile"] = str(key_path)
            print(f"  TLS: 已启用 ({cert_path.name})")
        else:
            print("  TLS: 已配置但证书无效，已回退 HTTP")

    uvicorn.run(**uvicorn_kwargs)
