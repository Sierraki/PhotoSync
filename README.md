<p align="center">
  <img src="image/README/1775990295672.png" alt="logo" width="100" />
</p>
<h1 align="center">PhotoSync</h1>


一款跨平台的手机相册同步工具。支持通过 **局域网 (Wi-Fi)** 或 **ADB (有线)** 连接，将手机端的照片与视频高速同步至电脑端。

<div align="center">



![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-blue)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://makeapullrequest.com)

</div>

遇到 BUG 请首先确认软件是否为最新版本。如果确认版本最新且问题依旧存在，请前往 [Issues](https://github.com/Sierraki/PhotoSync/issues) 提交反馈。

---

## 主要特性

* **多模连接**：支持局域网链接与 ADB 高速有线传输。
* **增量同步**：智能识别已同步文件，仅传输新增照片。

---



## 🖥️ 桌面端一键启动

1. 进入 `desktop/output/` 目录。
2. 双击 `PhotoSyncServer.exe`，即可直接启动服务器。





### A 局域网链接

```
设置路径：在浏览器界面中，首先设置好电脑端的照片存储路径。
链接方式：可以把链接复制到手机的输入框上也可以直接扫码链接
连接测试：在手机 App 上点击测试链接。
开始同步：连接成功后，在手机上点击开始同步即可。
```

### B ADB链接(有线)

```
基础设置：设置电脑端的照片存储路径。
```

```
准备连接：
文件内置了名为ADB的文件夹，在网页里的ADB路径指向该文件夹。
在手机上打开 USB 调试，并使用数据线连接电脑。
开始同步：在手机 App 点击测试链接，成功后点击开始同步。
```
