"""觸發判定與路由（TRIG-1、AUTH-*）——純邏輯，不依賴 Telegram SDK，方便單元測試。

流程：
  classify_trigger(...) 先判斷訊息屬於 指令 / 業務觸發 / 皆非。
  route(...) 依 聊天類型 × 是否管理員 × 是否授權 × 觸發類型 決定要做的動作。
"""

from __future__ import annotations

from enum import Enum, auto


class Kind(Enum):
    """訊息觸發類型。"""

    COMMAND = auto()  # 以 / 開頭且指向本 bot（TRIG-1d）
    BUSINESS = auto()  # @提及 / reply bot / 「小石」前綴（TRIG-1a/b/c）
    NONE = auto()  # 非觸發，應丟棄（不入儲存、不送 LLM）


class Action(Enum):
    """路由後要執行的動作。"""

    IGNORE = auto()  # 沉默丟棄
    PRIVATE_NOTICE = auto()  # AUTH-5 私訊固定回覆
    PRIVATE_AUTHORIZE_REDIRECT = auto()  # EC-18 私訊 /authorize
    CMD_AUTHORIZE = auto()
    CMD_REVOKE = auto()
    CMD_LIST_GROUPS = auto()
    CMD_RELOAD = auto()
    CMD_HELP = auto()
    CMD_START = auto()
    CMD_NOTIFY_ON = auto()  # NT-4：本群訂閱里程碑預告（可指定組別）
    CMD_NOTIFY_OFF = auto()
    CMD_NOTIFY_LIST = auto()
    CMD_NOTIFY_TEST = auto()
    CMD_TAG_LEADERS = auto()  # /tl：tag 全部組長＋總召（不含副組長、不含自己）
    CMD_TAG_ALL = auto()  # /ta：tag 全體工作人員（不含自己）
    BUSINESS = auto()  # 交給 agent 處理


ADMIN_COMMANDS = frozenset(
    {"authorize", "revoke", "list_groups", "reload", "notify_on", "notify_off", "notify_list", "notify_test"}
)
PUBLIC_COMMANDS = frozenset({"help", "start", "tl", "ta"})

_COMMAND_ACTION = {
    "revoke": Action.CMD_REVOKE,
    "list_groups": Action.CMD_LIST_GROUPS,
    "reload": Action.CMD_RELOAD,
    "help": Action.CMD_HELP,
    "start": Action.CMD_START,
    "notify_on": Action.CMD_NOTIFY_ON,
    "notify_off": Action.CMD_NOTIFY_OFF,
    "notify_list": Action.CMD_NOTIFY_LIST,
    "notify_test": Action.CMD_NOTIFY_TEST,
    "tl": Action.CMD_TAG_LEADERS,
    "ta": Action.CMD_TAG_ALL,
}


def parse_command(text: str, bot_username: str | None) -> str | None:
    """自 `/cmd@bot args` 取出正規化指令名。

    指向其他 bot（@ 後綴不符）或空指令回傳 None。
    """
    if not text.startswith("/"):
        return None
    parts = text[1:].split(maxsplit=1)
    if not parts or not parts[0]:
        return None
    token = parts[0]
    name, sep, target = token.partition("@")
    if sep and bot_username and target.lower() != bot_username.lower():
        return None  # 指向其他 bot
    if not name:
        return None
    return name.lower()


def command_args(text: str) -> str:
    """取出指令後方的參數字串（`/notify_on 開發組, 行政組` → `開發組, 行政組`）。

    非指令或無參數回空字串。指令名本身的解析仍由 parse_command 負責。
    """
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return ""
    parts = stripped[1:].split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def classify_trigger(
    text: str,
    *,
    mentions_bot: bool,
    reply_to_bot: bool,
    trigger_name: str,
    bot_username: str | None,
) -> tuple[Kind, str | None]:
    """判斷訊息觸發類型（TRIG-1）。回傳 (Kind, 指令名或 None)。"""
    stripped = (text or "").lstrip()
    if stripped.startswith("/"):
        cmd = parse_command(stripped, bot_username)
        if cmd is None:
            return (Kind.NONE, None)
        return (Kind.COMMAND, cmd)
    if mentions_bot or reply_to_bot or (trigger_name and stripped.startswith(trigger_name)):
        return (Kind.BUSINESS, None)
    return (Kind.NONE, None)


def route(
    *,
    chat_type: str,
    is_admin: bool,
    is_authorized: bool,
    kind: Kind,
    command: str | None,
) -> Action:
    """依脈絡決定動作。

    私訊：一律固定回覆（AUTH-5）；/authorize 導向群組（EC-18）。
    群組：未授權時僅管理員 /authorize 有效（AUTH-4），其餘沉默。
    """
    if chat_type == "private":
        if kind is Kind.COMMAND and command == "authorize":
            return Action.PRIVATE_AUTHORIZE_REDIRECT
        return Action.PRIVATE_NOTICE

    # group / supergroup
    if kind is Kind.COMMAND:
        if command == "authorize":
            return Action.CMD_AUTHORIZE if is_admin else Action.IGNORE
        if not is_authorized:
            return Action.IGNORE  # AUTH-4：未授權群組僅 /authorize 有效
        action = _COMMAND_ACTION.get(command or "")
        if action is None:
            return Action.IGNORE  # 授權群組中的未知指令
        if command in ADMIN_COMMANDS and not is_admin:
            return Action.IGNORE  # 管理指令僅管理員
        return action

    if kind is Kind.BUSINESS:
        return Action.BUSINESS if is_authorized else Action.IGNORE

    return Action.IGNORE
