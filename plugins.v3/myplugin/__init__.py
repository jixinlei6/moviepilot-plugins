"""
MyPlugin - MoviePilot V3 最小可运行插件骨架。

对应官方文档 docs/Plugin_Development.md 第 4 节的最小骨架，
在此基础上补充了远程命令、通知和定时服务示例，可按需删减。

注意：V3 新插件统一从 app.sdk 导入宿主能力，
app.core.* / app.helper.* / app.utils.* 旧路径禁止在新插件中使用。
"""
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import EventType, NotificationType


class MyPlugin(_PluginBase):
    """演示 V3 插件的最小生命周期和页面接口。"""

    # 插件元信息（plugin_version 必须与 package.v3.json 的 version 一致）
    plugin_name = "我的插件"
    plugin_desc = "一个最小可运行的 MoviePilot V3 插件。"
    plugin_icon = "Moviepilot_A.png"
    plugin_version = "1.0.0"
    plugin_author = "your-name"
    author_url = "https://github.com/your-name"
    plugin_config_prefix = "myplugin_"
    plugin_order = 50
    auth_level = 1

    # 运行状态（由 init_plugin 按配置建立，不在导入期初始化资源）
    _enabled = False
    _message = "Hello MoviePilot"
    _cron = "0 8 * * *"

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并建立本次运行所需状态。必须允许重复调用。"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._message = str(config.get("message") or "Hello MoviePilot")
        self._cron = str(config.get("cron") or "0 8 * * *")
        logger.info(f"{self.plugin_name} 初始化完成，启用状态: {self._enabled}")

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程命令（如 /my_plugin_run），不需要可返回空列表。"""
        return [
            {
                "cmd": "/my_plugin_run",
                "event": EventType.PluginAction,
                "desc": "执行我的插件",
                "category": "插件命令",
                "data": {"action": "my_plugin_run"},
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """注册插件动态 API，最终路径为 /api/v1/plugin/MyPlugin/hello。"""
        return [
            {
                "path": "/hello",
                "endpoint": self._hello_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "问候接口",
            }
        ]

    def get_service(self) -> list[dict[str, Any]]:
        """注册定时服务（在 设定-服务 中可见、可手动运行）。"""
        if not self.get_state():
            return []
        try:
            return [
                {
                    "id": "MyPlugin.Refresh",
                    "name": "我的插件定时刷新",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.refresh,
                    "kwargs": {},
                }
            ]
        except Exception as e:
            logger.error(f"定时服务配置错误: {e}")
            return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面（Vuetify JSON）和默认配置。"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "message",
                                            "label": "展示文本",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "0 8 * * *",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "message": "Hello MoviePilot",
            "cron": "0 8 * * *",
        }

    def get_page(self) -> list[dict]:
        """返回插件详情页（显示最近一次运行结果）。"""
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": self.get_data("last_result") or self._message,
                },
            }
        ]

    def stop_service(self) -> None:
        """释放插件创建的后台资源。宿主管理的定时服务无需在此处理。"""
        pass

    @staticmethod
    def get_render_mode() -> tuple[str, str]:
        """使用默认 Vuetify 渲染模式（Vue 联邦模式才需要返回 ("vue", ...)）。"""
        return "vuetify", ""

    # ---------------- 业务逻辑 ----------------

    def refresh(self) -> None:
        """定时任务入口：执行业务并把结果写入详情页。"""
        logger.info("开始执行我的插件任务")
        result = f"任务执行完成 @ {self._message}"
        self.save_data("last_result", result)
        self.post_message(
            mtype=NotificationType.Plugin,
            title="【我的插件】",
            text=result,
        )

    def _hello_api(self) -> dict[str, Any]:
        """API 响应方法。"""
        return {"success": True, "data": self._message}

    @eventmanager.register(EventType.PluginAction)
    def run_command(self, event: Event) -> None:
        """远程命令响应：只处理属于当前插件的动作。"""
        event_data = event.event_data or {}
        if event_data.get("action") != "my_plugin_run":
            return
        self.refresh()


__all__ = ["MyPlugin"]
