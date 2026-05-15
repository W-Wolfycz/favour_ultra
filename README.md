# 好感度管理插件

魔改自 [astrbot_plugin_Favour_Ultra](https://github.com/nuomicici/astrbot_plugin_Favour_Ultra)

1. 为指令添加了更细粒度的权限设置，同时根据不同权限展示不同的指令帮助
2. 添加了数值 -> 关系的映射配置，优化了之前的关系逻辑

插件安装后需手动下载 Chromium 浏览器渲染引擎。找到 AstrBot 使用的 `python.exe` 路径，在终端中执行：

```powershell
# 替换为实际的 python.exe 路径
C:\path\to\python.exe -m playwright install chromium
```