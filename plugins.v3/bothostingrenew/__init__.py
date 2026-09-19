"""
BotHostingRenew - bot-hosting.net 容器自动续期插件（MoviePilot V3）。

站点机制（来自 bot-hosting.net 面板实际行为）：
- 官方 API 没有续期端点，续期只能打开 /a/billings 页面点击 "Renew for 4 days" 按钮；
- 按钮受 Cloudflare Turnstile 人机验证保护，因此必须使用宿主 CloakBrowser 真实渲染；
- 每次续 4 天，24 小时冷却，冷却中按钮显示 "Renew in HH:MM:SS" 倒计时；
- 登录态依赖 Cookie 中的 session_token（JWT，会过期，需定期从浏览器重新复制）。

流程：注入 Cookie -> 打开账单页 -> 判断登录/到期/按钮状态 -> 等待或点击 Renew ->
验证按钮变为倒计时即成功。Turnstile 验证期间轮询等待，最多重试 3 次。

辅助能力（与 GitHub Actions 方案 Auto-Renew-Bothosting 同源的三个凭据）：
- session_token：面板登录态 Cookie，约 7 天过期；
- DISCORD_TOKEN：session_token 失效时，走 Discord OAuth 自动重登换取新 token；
- GH_TOKEN：拿到新 session_token 后回写 GitHub 仓库 Secrets（SESSION_TOKEN），
  保持 Auto-Renew-Bothosting 工作流同步可用。

注意：本插件配置中保存了敏感凭据，仓库请保持私有，不要发布到公开市场。
"""
import base64
import re
import threading
import time
from datetime import datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import EventType, NotificationType

BASE_URL = "https://bot-hosting.net"
BILLINGS_URL = f"{BASE_URL}/a/billings"
DISCORD_LOGIN_URL = "https://discord.com/login"
GH_API_BASE = "https://api.github.com"

# Renew 按钮可点击状态用模式匹配（站点曾把文案从 "Renew for 4 days"
# 改成 "Renew free plan"，不能再写死具体文案）；
# 冷却中按钮显示 "Renew in HH:MM:SS" 倒计时
RENEW_READY_PATTERN = re.compile(r"\bRenew\b(?!\s+in)")
RENEW_COOLDOWN_PATTERN = re.compile(r"Renew in\s+[\d:]+")
HUMAN_VERIFY_TEXT = "verify you are human"
MAX_VERIFY_ATTEMPTS = 3
VERIFY_WAIT_SECONDS = 10
PAGE_LOAD_TIMEOUT = 60000
GOTO_RETRY = 2

# 默认 Git 仓库（Discord 令牌不硬编码进源码，请在插件配置中填写）
DEFAULT_GH_REPO = "jixinlei6/Auto-Renew-Bothosting"


class BotHostingRenew(_PluginBase):
    """bot-hosting.net 容器自动续期。"""

    plugin_name = "BotHosting自动续期"
    plugin_desc = "自动打开 bot-hosting.net 账单页并点击 Renew 按钮续期容器，支持 Discord 重登与 GitHub Secrets 同步。"
    plugin_icon = "cloud.png"
    plugin_version = "1.3.2"
    plugin_author = "jixinlei"
    author_url = "https://github.com/jixinlei6"
    plugin_config_prefix = "bothostingrenew_"
    plugin_order = 51
    auth_level = 1

    # 运行状态
    _enabled = False
    _onlyonce = False
    _notify = False
    _cron = "0 9 * * *"
    _session_token = ""
    _discord_token = ""
    _gh_token = ""
    _gh_repo = DEFAULT_GH_REPO
    _proxy = ""
    _headed = False
    _lock = threading.Lock()

    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置；立即运行一次的请求转为后台线程执行。"""
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._cron = str(config.get("cron") or "0 9 * * *")
        self._session_token = str(config.get("session_token") or "").strip()
        self._discord_token = str(
            config.get("discord_token") or ""
        ).strip()
        self._gh_token = str(config.get("gh_token") or "").strip()
        self._gh_repo = str(config.get("gh_repo") or DEFAULT_GH_REPO).strip()
        self._proxy = str(config.get("proxy") or "").strip()
        self._headed = bool(config.get("headed", False))

        if config.get("onlyonce"):
            self.update_config(self._current_config(enabled=self._enabled))
            logger.info("收到立即运行请求，后台启动续期任务")
            threading.Thread(target=self.renew, daemon=True).start()

        logger.info(f"BotHosting自动续期插件初始化完成，启用状态: {self._enabled}")

    def _current_config(self, enabled: bool) -> dict:
        """组装当前插件配置（onlyonce 不落盘，避免重复触发）。"""
        return {
            "enabled": enabled,
            "notify": self._notify,
            "cron": self._cron,
            "session_token": self._session_token,
            "discord_token": self._discord_token,
            "gh_token": self._gh_token,
            "gh_repo": self._gh_repo,
            "proxy": self._proxy,
            "headed": self._headed,
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
                "cmd": "/bh_renew",
                "event": EventType.PluginAction,
                "desc": "执行BotHosting续期",
                "category": "工具",
                "data": {"action": "bh_renew_run"},
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        """注册插件动态 API。"""
        return [
            {
                "path": "/renew",
                "endpoint": self._renew_api,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行续期",
            },
            {
                "path": "/history",
                "endpoint": self._history_api,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取续期历史",
            },
        ]

    def get_service(self) -> list[dict[str, Any]]:
        """注册定时服务（续期有 24h 冷却，每天跑一次即可）。"""
        if not self.get_state() or not self._session_token:
            return []
        try:
            return [
                {
                    "id": "BotHostingRenew.Renew",
                    "name": "BotHosting自动续期",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.renew,
                    "kwargs": {},
                }
            ]
        except Exception as e:
            logger.error(f"定时服务配置错误: {e}")
            return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        """返回配置页面和默认配置。"""
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
                                            "placeholder": "0 9 * * *",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "proxy",
                                            "label": "代理服务器（可选）",
                                            "placeholder": "http://127.0.0.1:7890",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "headed",
                                            "label": "有头模式（Turnstile 过不了时开启）",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "session_token",
                                            "label": "session_token Cookie",
                                            "placeholder": "从浏览器登录后的 Cookie 中复制 session_token 的值",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "discord_token",
                                            "label": "DISCORD_TOKEN（可选，用于自动重登）",
                                            "placeholder": "session_token 失效时自动通过 Discord OAuth 重新登录",
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
                                            "model": "gh_token",
                                            "label": "GH_TOKEN（可选，同步 Secrets）",
                                            "placeholder": "ghp_ 开头的 PAT，需 repo 权限",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "gh_repo",
                                            "label": "GitHub 仓库",
                                            "placeholder": DEFAULT_GH_REPO,
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "📌 使用说明：\n1. 浏览器登录 bot-hosting.net 后，F12 -> 应用 -> Cookie 中复制 session_token 的值\n2. 续期每次延长 4 天，24 小时冷却，冷却中插件会跳过并记录\n3. session_token 过期（约 7 天）时若填写了 DISCORD_TOKEN 会自动重登换新 token\n4. 填写 GH_TOKEN 后，新 token 会自动回写 GitHub 仓库的 SESSION_TOKEN Secret\n5. 续期按钮有 Cloudflare Turnstile 验证，由宿主内置浏览器完成",
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
            "cron": "0 9 * * *",
            "session_token": "",
            "discord_token": "",
            "gh_token": "",
            "gh_repo": DEFAULT_GH_REPO,
            "proxy": "",
 "headed": false
        }

    def get_page(self) -> list[dict]:
        """返回插件详情页（续期历史）。"""
        history = self.get_data("history") or []
        if not history:
            return [
                {
                    "component": "VAlert",
                    "props": {"type": "info", "variant": "tonal", "text": "暂无续期记录"},
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
                            "props": {"class": "text-caption py-2 px-3"},
                            "text": record.get("expiry", "-"),
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
                    {"component": "VCardTitle", "text": "续期历史"},
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
                                                        "props": {
                                                            "class": "text-left text-caption"
                                                        },
                                                        "text": "时间",
                                                    },
                                                    {
                                                        "component": "th",
                                                        "props": {
                                                            "class": "text-left text-caption"
                                                        },
                                                        "text": "当前到期",
                                                    },
                                                    {
                                                        "component": "th",
                                                        "props": {
                                                            "class": "text-left text-caption"
                                                        },
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

    # ---------------- 页面状态解析（纯逻辑，便于测试） ----------------

    @staticmethod
    def _parse_state(content: str) -> dict[str, Any]:
        """
        从账单页 HTML 中解析登录与续期状态。

        返回:
            logged_in: 是否处于登录状态
            ready: Renew 按钮是否可点击
            cooldown: 冷却倒计时文本（如 "Renew in 12:34:56"），无则 None
            expiry: 当前到期日期文本，无则 None
        """
        state = {"logged_in": False, "ready": False, "cooldown": None, "expiry": None}
        if not content:
            return state

        # 未登录时站点跳转登录页，页面不会出现 Renew 相关内容
        state["logged_in"] = "Renew" in content or "billing" in content.lower()

        cooldown_match = RENEW_COOLDOWN_PATTERN.search(content)
        if cooldown_match:
            state["cooldown"] = cooldown_match.group(0)
        state["ready"] = bool(RENEW_READY_PATTERN.search(content))

        # 到期日期，兼容 YYYY/MM/DD、MM/DD/YYYY 与 ISO（RenewalDueAt:"2026-09-16T..."）
        expiry_match = re.search(
            r"(\d{4}/\d{2}/\d{2})|(\d{2}/\d{2}/\d{4})|(\d{4}-\d{2}-\d{2})", content
        )
        if expiry_match:
            state["expiry"] = expiry_match.group(0)

        return state

    @staticmethod
    def _find_renew_button(page: Any) -> dict[str, Any]:
        """
        查找可点击的 Renew 按钮。

        匹配 "Renew for 4 days"、"Renew free plan" 等文案，排除冷却倒计时
        "Renew in ..." 与 disabled 按钮。确认框内的续期按钮（"Renew for 4 days"）
        在 Turnstile 人机验证通过前是 disabled 的，因此需等它变成可点。

        返回 {"found": bool, "disabled": bool}。
        """
        try:
            return page.evaluate(
                """() => {
                    const els = [...document.querySelectorAll('button, a, [role="button"]')].filter(e => {
                        const t = (e.textContent || '');
                        return /\\bRenew\\b/i.test(t) && !/renew\\s+in/i.test(t);
                    });
                    if (!els.length) return { found: false, disabled: false };
                    const btn = els[els.length - 1];
                    return { found: true, disabled: !!btn.disabled };
                }"""
            )
        except Exception as e:
            logger.warning(f"查找 Renew 按钮失败: {e}")
            return {"found": False, "disabled": False}

    @staticmethod
    def _click_turnstile(page: Any) -> str:
        """
        尝试真实点击页面中的 Turnstile 人机验证组件（点它的 iframe 中心）。

        JS 无法操作跨域 iframe 内部，因此用 page.click 对 iframe 元素
        发起真实鼠标点击。返回描述结果的字符串用于日志。
        """
        selectors = (
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[src*="challenge"]',
            'iframe[title*="Cloudflare"]',
            'iframe[title*="人机"]',
            'iframe[src*="/cdn-cgi/"]',
            '[class*="cf-turnstile"]',
            '[class*="turnstile"] iframe',
        )
        for selector in selectors:
            try:
                page.click(selector, timeout=3000)
                return f"已点击 {selector}"
            except Exception:
                continue
        return "未找到 Turnstile 组件"

    @staticmethod
    def _dump_iframes(page: Any) -> str:
        """打印页面内全部 iframe 的 src/title/尺寸，用于诊断 Turnstile 渲染形态。"""
        try:
            info = page.evaluate(
                """() => JSON.stringify({
                    iframes: [...document.querySelectorAll('iframe')].map(f => ({
                        src: (f.src || '').slice(0, 90),
                        title: f.title || '',
                        w: f.clientWidth, h: f.clientHeight
                    })),
                    turnstileDivs: [...document.querySelectorAll('[class*="turnstile"], [class*="challenge"]')]
                        .map(d => (d.className || '').toString().slice(0, 60)).slice(0, 5)
                })"""
            )
            return str(info)[:900]
        except Exception as e:
            return f"dump失败: {e}"

    @staticmethod
    def _click_renew_button(page: Any) -> bool:
        """点击最后一个可点击的 Renew 按钮（确认弹窗内的按钮）。"""
        try:
            return bool(
                page.evaluate(
                    """() => {
                        const els = [...document.querySelectorAll('button, a, [role="button"]')].filter(e => {
                            const t = (e.textContent || '');
                            return /\\bRenew\\b/i.test(t) && !/renew\\s+in/i.test(t) && !e.disabled;
                        });
                        if (!els.length) return false;
                        els[els.length - 1].click();
                        return true;
                    }"""
                )
            )
        except Exception as e:
            logger.warning(f"点击 Renew 按钮失败: {e}")
            return False

    # ---------------- 续期主流程 ----------------

    @staticmethod
    def _goto(page: Any, url: str) -> bool:
        """
        导航到指定页面，等待 domcontentloaded（不等完整 load，避免 CF 资源挂起超时）。
        失败重试一次，返回是否成功。
        """
        for attempt in range(GOTO_RETRY):
            try:
                page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_load_state("domcontentloaded")
                return True
            except Exception as e:
                logger.warning(
                    f"打开页面{'重试' if attempt else ''}失败: {url}，错误: {e}"
                )
                time.sleep(5)
        return False

    def _run_renew(self) -> dict[str, Any]:
        """
        执行一次续期，返回 {success, message, expiry}。

        使用宿主 CloakBrowser（app.sdk.browser）渲染账单页并通过 Turnstile 验证。
        """
        from app.sdk.browser import launch_browser_context

        cookie_header = (
            f"session_token={self._session_token}; login=true; theme=system"
        )
        launch_kwargs = {"proxy": self._proxy} if self._proxy else {}

        with launch_browser_context(headless=not self._headed, **launch_kwargs) as context:
            page = context.new_page()
            page.set_extra_http_headers({"cookie": cookie_header})
            page.set_default_timeout(PAGE_LOAD_TIMEOUT)
            if not self._goto(page, BILLINGS_URL):
                return {
                    "success": False,
                    "message": "打开账单页超时，请检查容器网络或在插件中配置代理",
                    "expiry": None,
                    "need_relogin": False,
                }

            # Turnstile 交互式验证可能出现在页面中，等待其自动通过
            for attempt in range(MAX_VERIFY_ATTEMPTS):
                content = page.content()
                state = self._parse_state(content)

                if not state["logged_in"]:
                    return {
                        "success": False,
                        "message": "登录状态无效，session_token 可能已过期",
                        "expiry": None,
                        "need_relogin": True,
                    }

                if state["cooldown"] and not state["ready"]:
                    return {
                        "success": True,
                        "message": f"冷却中，无需续期（{state['cooldown']}）",
                        "expiry": state["expiry"],
                    }

                if state["ready"]:
                    logger.info("检测到可续期的 Renew 按钮，尝试点击")
                    clicked = self._click_renew_button(page)
                    if not clicked:
                        time.sleep(VERIFY_WAIT_SECONDS)
                        continue

                    # 两步确认：第一层点击会弹出确认框。确认框内的按钮在
                    # Turnstile 人机验证通过前是 disabled 的：轮询等待，
                    # 期间主动尝试点击 Turnstile 复选框帮助通过验证。
                    confirm_clicked = False
                    turnstile_clicks = 0
                    last_probe = {"found": False, "disabled": False}
                    for wait_i in range(30):
                        time.sleep(2)
                        last_probe = self._find_renew_button(page)
                        if not last_probe["found"]:
                            # 弹窗已关闭，说明可能已续期或被取消
                            break
                        if not last_probe["disabled"]:
                            confirm_clicked = self._click_renew_button(page)
                            if confirm_clicked:
                                logger.info("Turnstile 已通过，已点击确认按钮")
                            break
                        # 确认按钮仍 disabled：在第 8/20/40 秒尝试点击 Turnstile
                        if turnstile_clicks < 3 and wait_i in (4, 10, 20):
                            turnstile_clicks += 1
                            if turnstile_clicks == 1:
                                logger.info(f"页面 iframe 清单: {self._dump_iframes(page)}")
                            ts_result = self._click_turnstile(page)
                            logger.info(
                                f"尝试点击 Turnstile 人机验证 "
                                f"({turnstile_clicks}/3): {ts_result}"
                            )
                        else:
                            logger.debug("等待 Turnstile 通过，确认按钮尚未可点")

                    # 点击后轮询等待按钮变为倒计时，即为续期成功
                    for _ in range(VERIFY_WAIT_SECONDS):
                        time.sleep(1)
                        after = self._parse_state(page.content())
                        if after["cooldown"] and not after["ready"]:
                            return {
                                "success": True,
                                "message": "续期成功，已延长 4 天",
                                "expiry": after["expiry"],
                            }
                        if after["ready"]:
                            # 按钮仍可点击，可能人机验证未通过，继续等待重试
                            break
                    continue

                # 既不可续期也不在冷却，可能正在人机验证
                if HUMAN_VERIFY_TEXT in content.lower():
                    logger.info(
                        f"等待 Turnstile 人机验证通过（{attempt + 1}/{MAX_VERIFY_ATTEMPTS}）"
                    )
                time.sleep(VERIFY_WAIT_SECONDS)

            # 保存失败时的页面快照，便于定位 Turnstile/按钮问题
            try:
                debug_path = self.get_data_path() / "debug_renew_failure.html"
                debug_path.write_text(page.content(), encoding="utf-8")
                logger.info(f"已保存失败页面快照: {debug_path}")
            except Exception as debug_err:
                logger.debug(f"保存失败快照异常: {debug_err}")

            return {
                "success": False,
                "message": "未能完成续期：Renew 按钮不可用或人机验证未通过",
                "expiry": self._parse_state(page.content()).get("expiry"),
            }

    def renew(self) -> None:
        """定时任务入口：执行续期并保存历史、发送通知。"""
        if not self._lock.acquire(blocking=False):
            logger.warning("续期任务已在运行，跳过本次执行")
            return
        try:
            if not self._session_token:
                self._save_history(False, None, "未配置 session_token")
                self._notify_user("续期失败", "未配置 session_token，请先在插件配置中填写")
                return

            logger.info("开始执行 bot-hosting.net 续期任务")
            result = self._run_renew()

            # session_token 失效时，尝试用 DISCORD_TOKEN 自动重登换新 token 后重试一次
            if not result["success"] and result.get("need_relogin"):
                new_token = self._discord_relogin()
                if new_token:
                    self._save_session_token(new_token)
                    self._sync_gh_secret(new_token)
                    logger.info("已获取新 session_token，使用新 token 重试续期")
                    result = self._run_renew()
                else:
                    result["message"] = (
                        "session_token 已失效且自动重登失败，"
                        "请手动从浏览器重新复制 session_token"
                    )
                    result["need_relogin"] = False

            self._save_history(result["success"], result.get("expiry"), result["message"])
            if result["success"]:
                logger.info(f"续期结果: {result['message']}")
            else:
                logger.error(f"续期失败: {result['message']}")
            self._notify_user(
                "续期成功" if result["success"] else "续期失败",
                result["message"]
                + (f"（当前到期: {result['expiry']}）" if result.get("expiry") else ""),
            )
        except Exception as e:
            logger.error(f"续期任务异常: {e}")
            self._save_history(False, None, f"异常: {str(e)[:80]}")
            self._notify_user("续期异常", str(e)[:200])
        finally:
            self._lock.release()

    def _save_session_token(self, token: str) -> None:
        """把新获取的 session_token 持久化到插件配置。"""
        self._session_token = token
        self.update_config(self._current_config(enabled=self._enabled))
        logger.info("新 session_token 已保存到插件配置")

    def _discord_relogin(self) -> str | None:
        """
        使用 DISCORD_TOKEN 通过 Discord OAuth 重新登录 bot-hosting.net。

        流程：打开 Discord 登录页注入 token -> 回到面板点击 Discord 登录 ->
        在授权页点击授权 -> 从回调后的 Cookie 中提取新的 session_token。
        属于对第三方页面结构的自动化，失败返回 None，不影响主流程。
        """
        if not self._discord_token:
            logger.warning("未配置 DISCORD_TOKEN，跳过自动重登")
            return None
        try:
            from app.sdk.browser import launch_browser_context

            launch_kwargs = {"proxy": self._proxy} if self._proxy else {}
            with launch_browser_context(headless=not self._headed, **launch_kwargs) as context:
                page = context.new_page()
                page.set_default_timeout(30000)

                # 1. Discord 登录态注入：写入 localStorage 后刷新生效
                self._goto(page, DISCORD_LOGIN_URL)
                page.evaluate(
                    """(token) => {
                        localStorage.setItem('token', JSON.stringify(token));
                    }""",
                    self._discord_token,
                )
                self._goto(page, DISCORD_LOGIN_URL)
                time.sleep(5)

                # 2. 回到面板发起 Discord OAuth 登录
                self._goto(page, f"{BASE_URL}/login")
                clicked = page.evaluate(
                    """() => {
                        const els = [...document.querySelectorAll('button, a')];
                        const btn = els.find(e =>
                            /discord/i.test(e.textContent || '') ||
                            /discord/i.test(e.href || ''));
                        if (btn) { btn.click(); return true; }
                        return false;
                    }"""
                )
                if not clicked:
                    logger.error("未找到 Discord 登录入口")
                    return None

                # 3. Discord 授权页：等待加载后点击授权按钮
                time.sleep(5)
                page.evaluate(
                    """() => {
                        const els = [...document.querySelectorAll('button')];
                        const btn = els.find(e =>
                            /authorize|授权/i.test(e.textContent || ''));
                        if (btn) { btn.click(); return true; }
                        return false;
                    }"""
                )

                # 4. 等待回调完成，从 Cookie 中提取新 session_token
                new_token = None
                for _ in range(15):
                    time.sleep(2)
                    try:
                        for cookie in context.cookies():
                            if cookie.get("name") == "session_token":
                                new_token = cookie.get("value")
                                break
                    except Exception:
                        pass
                    if new_token:
                        break

                if new_token:
                    logger.info("Discord 自动重登成功，已获取新 session_token")
                else:
                    logger.error("Discord 自动重登未能获取新 session_token")
                return new_token
        except Exception as e:
            logger.error(f"Discord 自动重登异常: {e}")
            return None

    def _sync_gh_secret(self, token: str) -> None:
        """
        把新 session_token 回写到 GitHub 仓库的 SESSION_TOKEN Secret，
        保持 Auto-Renew-Bothosting 工作流与插件使用同一份有效凭据。
        """
        if not self._gh_token:
            return
        try:
            import requests
            from nacl import encoding, public

            repo = self._gh_repo
            headers = {
                "Authorization": f"Bearer {self._gh_token}",
                "Accept": "application/vnd.github+json",
            }
            # 1. 获取仓库公钥
            key_res = requests.get(
                f"{GH_API_BASE}/repos/{repo}/actions/secrets/public-key",
                headers=headers,
                timeout=30,
            )
            key_res.raise_for_status()
            key_data = key_res.json()

            # 2. 使用 SealedBox 加密 Secrets 值
            public_key = public.PublicKey(
                key_data["key"].encode("utf-8"),
                encoding.Base64Encoder(),
            )
            sealed = public.SealedBox(public_key).encrypt(token.encode("utf-8"))

            # 3. 回写 Secret
            put_res = requests.put(
                f"{GH_API_BASE}/repos/{repo}/actions/secrets/SESSION_TOKEN",
                headers=headers,
                json={
                    "encrypted_value": base64.b64encode(sealed).decode("utf-8"),
                    "key_id": key_data["key_id"],
                },
                timeout=30,
            )
            put_res.raise_for_status()
            logger.info(f"新 session_token 已同步到 GitHub 仓库 {repo} 的 SESSION_TOKEN")
        except ImportError:
            logger.warning(
                "同步 GitHub Secrets 需要 pynacl 和 requests 依赖，请确认插件依赖已安装"
            )
        except Exception as e:
            logger.error(f"同步 GitHub Secrets 失败: {e}")

    # ---------------- 持久化与通知 ----------------

    def _save_history(self, success: bool, expiry: str | None, message: str) -> None:
        """追加一条续期历史记录（保留最近 100 条）。"""
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        history.insert(
            0,
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "success": success,
                "expiry": expiry or "-",
                "message": message,
            },
        )
        self.save_data("history", history[:100])

    def _notify_user(self, title: str, text: str) -> None:
        """按配置发送通知。"""
        if not self._notify:
            return
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"【BotHosting自动续期】{title}",
                text=text,
            )
        except Exception as e:
            logger.error(f"发送通知失败: {e}")

    # ---------------- API 与远程命令 ----------------

    def _renew_api(self) -> dict[str, Any]:
        """POST /renew：后台启动续期任务。"""
        if self._lock.locked():
            return {"success": False, "message": "续期任务正在运行"}
        threading.Thread(target=self.renew, daemon=True).start()
        return {"success": True, "message": "续期任务已启动"}

    def _history_api(self) -> dict[str, Any]:
        """GET /history：返回最近 50 条历史。"""
        history = self.get_data("history") or []
        return {"success": True, "data": history[:50]}

    def _handle_command(self, event) -> None:
        """远程命令处理：只响应属于本插件的动作。"""
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != "bh_renew_run":
            return
        if self._lock.locked():
            self.post_message(
                mtype=NotificationType.Plugin,
                title="【BotHosting自动续期】",
                text="续期任务正在运行，请等待完成",
            )
            return
        threading.Thread(target=self.renew, daemon=True).start()

    # V3 约定：通过事件管理器注册 PluginAction 响应，实现远程命令触发
    run_command = eventmanager.register(EventType.PluginAction)(_handle_command)


__all__ = ["BotHostingRenew"]
