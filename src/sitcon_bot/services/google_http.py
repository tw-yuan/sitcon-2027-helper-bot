"""Google API client 共用建構（EC-9）。

google-api-python-client 底層用 httplib2，**httplib2.Http 非執行緒安全**，且一旦某次請求逾時/中斷，
該連線會殘留在「Request-sent」狀態——後續在同一 Http 物件上的呼叫會拋 ResponseNotReady；多執行緒
共用同一條 TLS 連線還會出現「SSL record layer failure」。asyncio.to_thread 會在多個 worker 執行緒間
輪替，且可能有並行的 Drive 搜尋，故**不可共用單一 Http**。

作法：service（含 discovery，可安全重用）建一次；但**每次 execute() 帶一個全新、帶 timeout 的
AuthorizedHttp**（見 request_http），確保連線不共用、壞連線不重用。timeout 用自訂 httplib2.Http
設定（不可用 socket.setdefaulttimeout，會一併縮短 Telegram 長輪詢逾時）；num_retries 供 5xx/限流退避。
"""

from __future__ import annotations

from typing import Any

GOOGLE_HTTP_TIMEOUT = 30  # 秒：連線/讀取逾時
GOOGLE_NUM_RETRIES = 3  # execute() 對 5xx／429 的自動退避重試次數


def build_google_service(
    api: str, version: str, sa_json_path: str, scopes: list[str], subject: str | None = None
) -> tuple[Any, Any]:
    """回傳 (service, creds)。service 僅用來組 request 並可重用；I/O 一律走 request_http(creds)。

    subject：domain-wide delegation 冒用的使用者 email（Calendar 用）；需在 Workspace 管理後台
    對此 service account 的 client ID 授權對應 scope，否則 API 會回 unauthorized_client。
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(sa_json_path, scopes=scopes)
    if subject:
        creds = creds.with_subject(subject)
    # service 內建的 http 不會被實際使用（每次 execute 帶自己的 http）；用 credentials 建即可。
    service = build(api, version, credentials=creds, cache_discovery=False)
    return service, creds


def request_http(creds: Any) -> Any:
    """每次 API 呼叫用全新、帶 timeout 的 AuthorizedHttp（避免執行緒共用與壞連線重用）。"""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp

    return AuthorizedHttp(creds, http=httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT))
