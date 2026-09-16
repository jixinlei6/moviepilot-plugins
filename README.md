# movepoiltv3 - MoviePilot V3 插件仓库

按官方 [V3 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md) 组织的第三方插件仓库。

## 目录结构

```
movepoiltv3/
├── package.v3.json              # 插件市场索引（version 必须与插件类 plugin_version 一致）
├── plugins.v3/
│   ├── myplugin/                # 示例插件（最小骨架）
│   │   └── __init__.py
│   ├── bothostingrenew/         # BotHosting 容器自动续期
│   │   └── __init__.py
│   └── nexusformsignin/         # NexusPHP 空表单 POST 型站点签到（慕雪阁/Depth Studio 等）
│       └── __init__.py
├── tests/v3/
│   ├── myplugin/
│   ├── bothostingrenew/
│   └── nexusformsignin/
└── README.md
```

## 本地开发

把 MoviePilot V3 宿主和本仓库放在同级目录，配置环境变量后启动宿主：

```
PLUGIN_LOCAL_REPO_PATHS=<本仓库绝对路径>
PLUGIN_AUTO_RELOAD=true
DEBUG=true
```

源码改动会自动重载；DEBUG 模式会提示旧导入兼容警告。

## 发布

1. 修改插件代码后，同步三处版本号：`__init__.py` 的 `plugin_version`、
   `package.v3.json` 的 `version`、`history` 顶部当前版本记录。
2. 推送到 GitHub 后，在 MoviePilot「设定 → 插件 → 插件市场」添加本仓库地址即可安装。

## 检查清单（发布前）

- [ ] `python -m compileall plugins.v3`
- [ ] `plugin_version` 与 `package.v3.json` 的 `version` 一致
- [ ] 只使用 `app.sdk` 稳定导入，未使用 `app.core.*` 等旧路径
- [ ] 在真实 V3 宿主中完成加载、启用、禁用验证
