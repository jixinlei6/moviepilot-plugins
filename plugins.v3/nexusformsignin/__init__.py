"""
NexusFormSignin - NexusPHP 空表单 POST 型站点自动签到插件（MoviePilot V3）。

适用站点实测（2026-09-14，浏览器实测确认）：
- pt.muxuege.org（慕雪阁）、dstudio.me（Depth Studio）等使用相同签到组件的
  NexusPHP 站：签到页 attendance.php，签到动作 = POST attendance.php，
  请求体为空，无隐藏字段、无 token、无验证码；
- 成功页面关键词（两站完全一致）："签到成功"、"这是您的第 X 次签到，
  已连续签到 X 天，本次签到获得 X 个魔力值"、"今日签到排名：X / X"；
- 未登录/Cookie 失效时请求会 302 到 login.php。

Cookie 来源：按站点域名自动从站点管理读取，因此请先在站点管理中
添加对应站点并保持 Cookie 有效。
"""
import re
import threading
from datetime import datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import EventType, NotificationType

REQUEST_TIMEOUT = 30

# 首次使用时默认勾选的站点（仅当站点管理中存在时生效）。
# 注意：慕雪阁的签到页在 pt 子域，域名必须是完整的 pt.muxuege.org
PREFERRED_SITES = ["pt.muxuege.org", "dstudio.me"]


def _site_oper():
    """获取站点管理操作对象，兼容 V3 不同版本/不同版本的 DB 路径，绝对兜底。"""
    try:
        try:
            from app.db.oper.site import SiteOper
        except ImportError:
            from app.db.site_oper import SiteOper
        return SiteOper()
    except BaseException:
        return None


def _list_site_options() -> list[dict[str, str]]:
    """
    读取站点管理中的全部站点，转为下拉框选项。
    任何异常都返回空列表，绝不阻塞配置页加载。
    """
    try:
        oper = _site_oper()
        if oper is None:
            return []
        sites = oper.list() or []
        return [
            {"title": (site.name or site.domain), "value": site.domain}
            for site in sites
            if site.domain
        ]
    except BaseException:
        return []


def _get_site_info(domain: str) -> dict[str, str]:
    """读取指定域名的站点名称与 Cookie，站点不存在时名称回退为域名。"""
    try:
        oper = _site_oper()
        if oper is None:
            return {"name": domain, "cookie": ""}
        site = oper.get_by_domain(domain)
        if site:
            return {"name": (site.name or domain), "cookie": (site.cookie or "").strip()}
    except BaseException:
        pass
    return {"name": domain, "cookie": ""}


def _normalize_domains(raw: Any) -> list[str]:
    """兼容两种配置格式：新格式为域名列表，旧格式为 域名|名称 多行文本。"""
    if isinstance(raw, list):
        return [str(d).strip() for d in raw if str(d).strip()]
    if isinstance(raw, str):
        domains = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            domain = line.split("|", 1)[0].strip()
            domain = domain.replace("https://", "").replace("http://", "").strip("/")
            if domain:
                domains.append(domain)
        return domains
    return []


class NexusFormSignin(_PluginBase):
    """NexusPHP 空表单 POST 型站点自动签到。"""

    plugin_name = "NexusPHP表单签到"
    plugin_desc = "通用签到插件，适用于慕雪阁、Depth Studio 等 POST attendance.php 空表单型 NexusPHP 站点。"
    plugin_icon = "signin.png"
    plugin_version = "1.1.2"
    plugin_author = "jixinlei"
    author_url = "https://github.com/jixinlei6"
    plugin_config_prefix = "nexusformsignin_"
    plugin_order = 52
    auth_level = 2

    _enabled = False
    _onlyonce = False
    _notify = False
    _cron = "0 8 * * *"
    _sites: list[str] = []
    _lock = threading.Lock()

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置；立即运行一次的请求转为后台线程执行。"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._cron = str(config.get("cron") or "0 8 * * *")
        self._sites = _normalize_domains(config.get("sites"))

        if config.get("onlyonce"):
            self.update_config(self._current_config())
            logger.info("收到立即运行请求，后台启动签到任务")
            threading.Thread(target=self.signin, daemon=True).start()

        logger.info(f"NexusPHP表单签到插件初始化完成，启用状态: {self._enabled}")

    def _current_config(self) -> dict:
        """组装当前插件配置（onlyonce 不落盘，避免重复触发）。"""
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "sites": self._sites,
            "onlyonce": False,
        }

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        """注册远程命令。"""
        return [
            {
                "cmd": "/nx_sign",
                "event": EventType.PluginAction,
                "desc": "执行Nexus站点签到",
                "category": "站点",
                "data": {"action": "nexusform_signin_run"},
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """注册插件动态 API。"""
        return [
            {
                "path": "/sign",
                "endpoint": self._sign_api,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "执行全部站点签到",
            },
            {
                "path": "/history",
                "endpoint": self._history_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取签到历史",
            },
        ]

    def get_service(self) -> list[dict[str, Any]]:
        """注册定时服务。"""
        if not self.get_state():
            return []
        try:
            return [
                {
                    "id": "NexusFormSignin.Sign",
                    "name": "NexusPHP表单签到",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.signin,
                    "kwargs": {},
                }
            ]
        except Exception as e:
            logger.error(f"定时服务配置错误: {e}")
            return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面和默认配置。站点下拉框动态读取站点管理。"""
        site_options = _list_site_options()
        # 首次使用时默认勾选已实测支持的站点
        option_values = {item["value"] for item in site_options}
        default_sites = [d for d in PREFERRED_SITES if d in option_values]
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
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "发送通知"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "onlyonce", "label": "立即运行一次"},
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
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sites",
                                            "label": "签到站点",
                                            "items": site_options,
                                            "multiple": True,
                                            "chips": True,
                                            "clearable": True,
                                            "placeholder": "从站点管理中选择需要签到的站点",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "📌 使用说明：\n1. 站点列表来自站点管理，请先在站点管理中添加站点并保持 Cookie 有效\n2. Cookie 按站点域名自动读取，无需手动填写\n3. 仅适用于签到接口为 POST attendance.php 空表单的 NexusPHP 站点\n4. 已实测支持：pt.muxuege.org（慕雪阁）、dstudio.me（Depth Studio）",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "0 8 * * *",
            "sites": default_sites,
        }

    def get_page(self) -> list[dict]:
        """返回插件详情页（签到历史）。"""
        history = self.get_data("history") or []
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "text": "暂无签到记录"},
                }
            ]

        rows = []
        for record in history[:50]:
            success = record.get("success", False)
            rows.append(
                {
                    "component": "tr",
                    "props": {"style": "border-bottom: 1px solid #f0f0f0;"},
                    "content": [
                        {
                            "component": "td",
                            "props": {"class": "text-caption py-2 px-3"},
                            "text": record.get("time", "-"),
                        },
                        {
                            "component": "td",
                            "props": {"class": "text-caption py-2 px-3 font-weight-medium"},
                            "text": record.get("site", "-"),
                        },
                        {
                            "component": "td",
                            "props": {
                                "class": "text-caption py-2 px-3 text-"
                                + ("success" if success else "error")
                            },
                            "text": ("✅ " if success else "❌ ") + record.get("message", "-"),
                        },
                    ],
                }
            )

        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {"component": "VCardTitle", "text": "签到历史"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "VSimpleTable",
                                "props": {"dense": True},
                                "content": [
                                    {
                                        "component": "thead",
                                        "content": [
                                            {
                                                "component": "tr",
                                                "content": [
                                                    {
                                                        "component": "th",
                                                        "props": {"class": "text-left text-caption"},
                                                        "text": "时间",
                                                    },
                                                    {
                                                        "component": "th",
                                                        "props": {"class": "text-left text-caption"},
                                                        "text": "站点",
                                                    },
                                                    {
                                                        "component": "th",
                                                        "props": {"class": "text-left text-caption"},
                                                        "text": "结果",
                                                    },
                                                ],
                                            }
                                        ],
                                    },
                                    {"component": "tbody", "content": rows},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

    def stop_service(self) -> None:
        """释放插件创建的后台资源。"""
        pass

    # ---------------- 签到核心 ----------------

    @staticmethod
    def _parse_result(text: str) -> dict[str, Any]:
        """
        从签到响应页面解析结果（纯逻辑，便于测试）。

        两站实测成功页面文案一致：
        "签到成功 / 这是您的第 103 次签到，已连续签到 77 天，
         本次签到获得 300 个魔力值 / 今日签到排名：70 / 70"
        """
        result = {"status": "failed", "message": ""}
        if not text:
            return result

        if "签到成功" in text or re.search(r"第\s*\d+\s*次签到", text):
            reward = re.search(r"本次签到获得\s*([\d,]+)\s*个?魔力值", text)
            total = re.search(r"第\s*(\d+)\s*次签到", text)
            streak = re.search(r"已连续签到\s*(\d+)\s*天", text)
            rank = re.search(r"今日签到排名[：:]\s*(\d+)\s*/\s*(\d+)", text)
            parts = []
            if reward:
                parts.append(f"获得 {reward.group(1)} 魔力值")
            if streak and total:
                parts.append(f"连续签到 {streak.group(1)} 天（第 {total.group(1)} 次）")
            if rank:
                parts.append(f"今日排名 {rank.group(1)}/{rank.group(2)}")
            result["status"] = "success"
            result["message"] = ("签到成功，" + "，".join(parts)) if parts else "签到成功"
            return result

        if re.search(r"已(经)?签到|请勿重复签到", text):
            result["status"] = "success"
            result["message"] = "今日已签到"
            return result

        if "登录" in text and "签到" not in text:
            result["message"] = "Cookie 已失效，请更新站点 Cookie"
        return result

    def _sign_one(self, domain: str) -> dict[str, Any]:
        """对单个站点执行签到，返回 {success, message}。"""
        cookie = _get_site_info(domain).get("cookie", "")
        if not cookie:
            return {"success": False, "message": "未获取到 Cookie，请检查站点管理"}
        import requests

        from app.sdk.config import settings

        res = requests.post(
            f"https://{domain}/attendance.php",
            headers={
                "Cookie": cookie,
                "Referer": f"https://{domain}/attendance.php",
                "User-Agent": settings.USER_AGENT,
            },
            data={},
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )

        # 302 到登录页说明 Cookie 失效
        if "login.php" in (res.url or ""):
            return {"success": False, "message": "Cookie 已失效，请更新站点 Cookie"}

        parsed = self._parse_result(res.text)
        if parsed["status"] == "failed" and not parsed["message"]:
            parsed["message"] = f"响应异常，状态码: {res.status_code}"
        return {"success": parsed["status"] == "success", "message": parsed["message"]}

    def signin(self) -> None:
        """定时任务入口：遍历站点签到并保存历史、发送通知。"""
        if not self._lock.acquire(blocking=False):
            logger.warning("签到任务已在运行，跳过本次执行")
            return
        try:
            domains = self._sites
            if not domains:
                logger.warning("未选择签到站点，跳过签到")
                return

            logger.info(f"开始执行 NexusPHP 站点签到任务，共 {len(domains)} 个站点")
            lines = []
            for domain in domains:
                try:
                    site = _get_site_info(domain)
                    name = site.get("name") or domain
                    result = self._sign_one(domain)
                except Exception as e:
                    logger.error(f"[{domain}] 签到异常: {e}")
                    result = {"success": False, "message": f"异常: {str(e)[:60]}"}
                    name = domain
                if result["success"]:
                    logger.info(f"[{name}] {result['message']}")
                else:
                    logger.error(f"[{name}] {result['message']}")
                lines.append(f"{'✅' if result['success'] else '❌'} {name}：{result['message']}")
                self._save_history(result["success"], name, result["message"])

            summary = "\n".join(lines)
            all_success = all(line.startswith("✅") for line in lines)
            self._notify_user(
                "全部成功" if all_success else "部分失败",
                summary,
            )
        except Exception as e:
            logger.error(f"签到任务异常: {e}")
            self._notify_user("签到异常", str(e)[:200])
        finally:
            self._lock.release()

    # ---------------- 持久化与通知 ----------------

    def _save_history(self, success: bool, site: str, message: str) -> None:
        """追加一条签到历史（保留最近 200 条）。"""
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        history.insert(
            0,
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "success": success,
                "site": site,
                "message": message,
            },
        )
        self.save_data("history", history[:200])

    def _notify_user(self, title: str, text: str) -> None:
        """按配置发送通知。"""
        if not self._notify:
            return
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"【NexusPHP表单签到】{title}",
                text=text,
            )
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

    # ---------------- API 与远程命令 ----------------

    def _sign_api(self) -> dict[str, Any]:
        """POST /sign：后台启动签到任务。"""
        if self._lock.locked():
            return {"success": False, "message": "签到任务正在运行"}
        threading.Thread(target=self.signin, daemon=True).start()
        return {"success": True, "message": "签到任务已启动"}

    def _history_api(self) -> dict[str, Any]:
        """GET /history：返回最近 50 条历史。"""
        history = self.get_data("history") or []
        return {"success": True, "data": history[:50]}

    @eventmanager.register(EventType.PluginAction)
    def run_command(self, event: Event) -> None:
        """远程命令响应：只处理属于当前插件的动作。"""
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != "nexusform_signin_run":
            return
        if self._lock.locked():
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【NexusPHP表单签到】",
                text="签到任务正在运行，请等待完成",
            )
            return
        threading.Thread(target=self.signin, daemon=True).start()


__all__ = ["NexusFormSignin"]
