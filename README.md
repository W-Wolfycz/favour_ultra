# 好感度管理插件

魔改自 [astrbot_plugin_Favour_Ultra](https://github.com/nuomicici/astrbot_plugin_Favour_Ultra)

## 功能

- 细粒度权限控制，不同角色可用不同指令
- 数值 → 关系映射（简单模式 / 高级模式）
- 按人格 ID（persona_id）隔离数据库，不同人格独立好感度
- 后台 LLM 好感度结算（可配置裁判模型）
- 人设感知的好感度评估
- 框架管理员/特使初始好感度与关系覆盖
- Playwright 文转图渲染

## 安装

插件安装后需手动下载 Chromium 浏览器渲染引擎。找到 AstrBot 使用的 `python` 路径，执行：

```bash
python -m playwright install chromium
```

## 配置项

| 配置 | 说明 |
|---|---|
| 好感度判定模式 | galgame（宽容）/ realistic（严格） |
| 裁判模型 | 判断好感度变化的 LLM，留空使用默认模型 |
| 关系映射模式 | simple（自动均分）/ advance（自定义区间） |
| 管理员/特使关系 | 留空则按映射表，非空则管理员固定使用该关系 |
| 权限配置 | 按角色控制可用指令 |
