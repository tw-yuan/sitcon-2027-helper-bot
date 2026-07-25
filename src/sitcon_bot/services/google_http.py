"""Google API client 共用建構（EC-9）。

google-api-python-client 預設無 socket timeout、`execute()` 預設不重試。這裡以自訂 httplib2.Http
設定連線/讀取 timeout（避免任一呼叫永久卡住），並提供統一的 num_retries 供各 `execute()` 對
5xx／限流做指數退避。注意：不可用 socket.setdefaulttimeout（會一併縮短 Telegram 長輪詢逾時）。
"""

from __future__ import annotations

from typing import Any

GOOGLE_HTTP_TIMEOUT = 30  # 秒：連線/讀取逾時
GOOGLE_NUM_RETRIES = 3  # execute() 對 5xx／429 的自動退避重試次數


def build_google_service(api: str, version: str, sa_json_path: str, scopes: list[str]) -> Any:
    """建立帶 timeout 的 Google API service。"""
    import httplib2
    from google.oauth2 import service_account
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(sa_json_path, scopes=scopes)
    authed = AuthorizedHttp(creds, http=httplib2.Http(timeout=GOOGLE_HTTP_TIMEOUT))
    return build(api, version, http=authed, cache_discovery=False)
