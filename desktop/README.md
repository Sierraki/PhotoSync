# PhotoSync Material 3 Desktop

- 双击自动启动本地服务并打开 WebView2 桌面窗口。
- 不显示命令行窗口。
- 关闭应用窗口时优雅停止 Uvicorn、SQLite 和单实例锁。
- 网页功能、WiFi 同步和 ADB 同步保持不变。
- 最终产物位于 `sol/output/PhotoSync.exe`。

构建：

```powershell
.\.venv\Scripts\python.exe sol\build.py
```
