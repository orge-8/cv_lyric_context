"""VCPedia 抓取客户端：Anubis PoW 解题 + MediaWiki API 取 wikitext。

VCPedia（https://vcpedia.cn）是 MediaWiki 站点，全站启用 Anubis 1.27 反爬，
明文请求（含 api.php）一律返回 403 挑战页。取数流程：

1. GET / （跟随重定向）→ 403 返回含 <script id="anubis_challenge"> 的 JSON；
2. 解析 randomData / difficulty，暴力搜索 nonce 使
   sha256(randomData + str(nonce)) 的十六进制前 difficulty 位为 0；
3. GET /.within.website/x/cmd/anubis/api/pass-challenge
   ?id=<challenge id>&response=<hash>&nonce=<n>&redir=<url>&elapsedTime=<ms>
   服务端下发 techaro.lol-anubis-auth-* cookie（JWT，有效期约一周）；
4. 之后所有请求带上该 cookie 即可正常访问 api.php。

注意：pass-challenge 的 response 参数必须传**真实 hash**。Anubis 1.27 会校验，
传固定值（如 1）会返回 "invalid response."。

仅依赖标准库，无第三方依赖。
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0 Safari/537.36"
)

_CHALLENGE_SCRIPT_ID = "anubis_challenge"
_PASS_CHALLENGE_PATH = "/.within.website/x/cmd/anubis/api/pass-challenge"

# PoW 搜索上限：difficulty=4 约 6 万次（毫秒级），difficulty=6 约 1600 万次。
# 超过上限直接放弃，避免把 CPU 打满。
_POW_MAX_TRIES = 20_000_000

_DEFAULT_LOGGER = logging.getLogger("vcpedia_client")


class VCPediaError(Exception):
    """VCPedia 抓取失败（网络异常、反爬未通过等）。"""


class AnubisError(VCPediaError):
    """Anubis 挑战未通过。"""


def _solve_pow(random_data: str, difficulty: int, max_tries: int = _POW_MAX_TRIES) -> tuple[int, str]:
    """找 nonce 使 sha256(randomData + nonce) 的 hex 前 difficulty 位为 0。

    返回 (nonce, hash)。与 Anubis 前端 sha256 worker 的判定一致。
    """
    target = "0" * difficulty
    for nonce in range(1, max_tries + 1):
        digest = hashlib.sha256((random_data + str(nonce)).encode("utf-8")).hexdigest()
        if digest.startswith(target):
            return nonce, digest
    raise AnubisError(f"Anubis PoW 未能在 {max_tries} 次内解出（difficulty={difficulty}）")


class VCPediaClient:
    """带 Anubis 认证的 VCPedia API 客户端。

    线程不安全：同步过程请单线程使用。cookie 落盘复用，失效时自动重解。
    """

    def __init__(
        self,
        base_url: str = "https://vcpedia.cn",
        cookie_file: Optional[Path] = None,
        timeout: float = 20.0,
        interval: float = 0.8,
        max_retries: int = 3,
        logger: Optional[logging.Logger] = None,
        verify_ssl: bool = True,
        ca_bundle: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.interval = interval
        # 有些网络出口（公司代理、部分安全软件）会做 TLS 中间人，用自签证书
        # 替换站点证书，Python 默认校验就会报 CERTIFICATE_VERIFY_FAILED。
        # 这种情况把 ca_bundle 指到代理根证书即可；不想折腾也可以关掉校验。
        self.verify_ssl = bool(verify_ssl)
        self.ca_bundle = str(ca_bundle or "").strip()
        # 站点偶发返回 403/连接超时（反爬限流或网络抖动），失败若干次后重试
        self.max_retries = max(1, int(max_retries))
        self.logger = logger or _DEFAULT_LOGGER
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self._jar = http.cookiejar.LWPCookieJar()
        self._load_cookies()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPSHandler(context=self._build_ssl_context()),
        )
        self._last_request_at = 0.0
        self._auth_checked = False

    def _build_ssl_context(self) -> ssl.SSLContext:
        """按配置构造 SSL 上下文。

        优先级：verify_ssl=false（总开关）> ca_bundle（追加信任）> 系统根证书库。
        顺序不能反：verify_ssl 是用户明确表达的意图，ca_bundle 是修补手段，
        被修补手段盖掉总开关会导致「我明明关了校验怎么还在报证书错」。
        """
        if not self.verify_ssl:
            if self.ca_bundle:
                self.logger.warning(
                    "VCPedia: verify_ssl=false 已生效，ca_bundle（%s）本次被忽略",
                    self.ca_bundle,
                )
            self.logger.warning(
                "VCPedia: 已关闭 SSL 证书校验（verify_ssl=false）。"
                "只有在确认网络出口是自己的代理时才该这么做，否则有被中间人窃听的风险。"
            )
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        if self.ca_bundle:
            path = Path(self.ca_bundle).expanduser()
            if not path.is_file():
                self.logger.warning(
                    "VCPedia: ca_bundle 指向的文件不存在（%s），回退到系统证书", path
                )
            else:
                try:
                    # 注意：不能写成 create_default_context(cafile=...)。
                    # CPython 里只要传了 cafile/capath/cadata，就不会再调用
                    # load_default_certs()，等于把 Windows 系统根证书库整个丢掉，
                    # 只信任这一个文件。这里要先建默认上下文（含系统根），
                    # 再用 load_verify_locations 把自定义证书“追加”进去。
                    ctx = ssl.create_default_context()
                    ctx.load_verify_locations(cafile=str(path))
                    self.logger.info(
                        "VCPedia: 已追加 ca_bundle（系统根证书库仍然生效）: %s", path
                    )
                    return ctx
                except (ssl.SSLError, OSError) as exc:
                    self.logger.warning("VCPedia: ca_bundle 加载失败（%s），回退到系统证书", exc)
        self.logger.info(
            "VCPedia: 使用系统根证书库校验（未配置 crawler.ca_bundle）"
        )
        return ssl.create_default_context()

    # ── cookie 持久化 ───────────────────────────────────────────

    def _load_cookies(self) -> None:
        if not self.cookie_file or not self.cookie_file.exists():
            return
        try:
            self._jar.load(str(self.cookie_file), ignore_discard=True, ignore_expires=True)
            self.logger.info("VCPedia: 已载入 Anubis auth cookie 缓存")
        except Exception as exc:  # noqa: BLE001 - 缓存损坏不影响主流程
            self.logger.warning("VCPedia: cookie 缓存读取失败，将重新解题: %s", exc)

    def _save_cookies(self) -> None:
        if not self.cookie_file:
            return
        try:
            self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
            self._jar.save(str(self.cookie_file), ignore_discard=True, ignore_expires=True)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("VCPedia: cookie 缓存保存失败: %s", exc)

    def _has_auth_cookie(self) -> bool:
        now = time.time()
        return any(
            "anubis-auth" in cookie.name
            and (cookie.expires is None or cookie.expires > now)
            for cookie in self._jar
        )

    # ── HTTP 基础 ───────────────────────────────────────────────

    def _throttle(self) -> None:
        """按 interval 限速，避免打爆站点。"""
        elapsed = time.time() - self._last_request_at
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self._last_request_at = time.time()

    def _raw_get(
        self, url: str, headers: Optional[Dict[str, str]] = None
    ) -> tuple[int, str, str]:
        """发起 GET，返回 (状态码, 响应体, 最终URL)。

        最终 URL 必须是重定向后的地址：站点根路径会 301 到 /首页，
        而 Anubis 的 challenge 是在 /首页 下发的，pass-challenge 的 redir
        参数必须与它一致，否则校验不通过。
        """
        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,*/*",
        }
        if headers:
            req_headers.update(headers)
        self._throttle()
        request = urllib.request.Request(url, headers=req_headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "ignore"), resp.geturl()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
            return exc.code, body, url

    @staticmethod
    def _is_challenge_body(body: str) -> bool:
        return bool(body) and f'id="{_CHALLENGE_SCRIPT_ID}"' in body

    # ── Anubis 挑战 ─────────────────────────────────────────────

    def _parse_challenge(self, body: str) -> Dict[str, Any]:
        m = re.search(
            rf'<script id="{_CHALLENGE_SCRIPT_ID}"\s*[^>]*>(.*?)</script>', body, re.S
        )
        if not m:
            raise AnubisError("未找到 Anubis challenge 脚本（站点结构可能已变化）")
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError as exc:
            raise AnubisError(f"challenge JSON 解析失败: {exc}") from exc
        challenge = data.get("challenge") or {}
        rules = data.get("rules") or {}
        cid = challenge.get("id")
        random_data = challenge.get("randomData")
        if not cid or not random_data:
            raise AnubisError("challenge 缺少 id / randomData")
        return {
            "id": str(cid),
            "randomData": str(random_data),
            "difficulty": int(rules.get("difficulty") or challenge.get("difficulty") or 4),
        }

    def solve_challenge(self) -> bool:
        """走一遍 Anubis 挑战并取回 auth cookie，成功返回 True。"""
        entry_url = f"{self.base_url}/"
        status, body, challenge_url = self._raw_get(entry_url)
        if status != 403 or not self._is_challenge_body(body):
            self.logger.warning(
                "VCPedia: 挑战入口返回 %s，未命中 Anubis 挑战（反爬可能已关闭）", status
            )
            self._auth_checked = True
            return status == 200

        info = self._parse_challenge(body)
        started = time.time()
        nonce, digest = _solve_pow(info["randomData"], info["difficulty"])
        elapsed_ms = int((time.time() - started) * 1000)
        self.logger.info(
            "VCPedia: Anubis 解题完成（difficulty=%d, nonce=%d, 耗时 %dms）",
            info["difficulty"], nonce, elapsed_ms,
        )

        query = urllib.parse.urlencode({
            "id": info["id"],
            "response": digest,
            "nonce": str(nonce),
            "redir": challenge_url,
            "elapsedTime": str(elapsed_ms),
        })
        pass_url = f"{self.base_url}{_PASS_CHALLENGE_PATH}?{query}"
        status, body, _ = self._raw_get(pass_url, headers={"Referer": challenge_url})
        if status != 200:
            detail = ""
            m = re.search(r"(?is)<p[^>]*>(.*?)</p>", body)
            if m:
                detail = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:200]
            raise AnubisError(f"pass-challenge 返回 {status}（{detail or '无详情'}）")

        self._save_cookies()
        if not self._has_auth_cookie():
            raise AnubisError("pass-challenge 成功但未取得 auth cookie")
        self._auth_checked = True
        self.logger.info("VCPedia: Anubis auth cookie 已获取并缓存")
        return True

    def _ensure_auth(self, force: bool = False) -> None:
        if not force and self._auth_checked and self._has_auth_cookie():
            return
        if not force and self._has_auth_cookie():
            self._auth_checked = True
            return
        self.solve_challenge()

    # ── 对外请求 ────────────────────────────────────────────────

    def get(self, url: str, retry_on_challenge: bool = True) -> str:
        """带认证的 GET，返回响应体。

        被挑战拦截时重新解题；连接超时/5xx 等瞬时故障按退避重试。
        站点在请求较密时会偶发丢包或重发挑战，重试是必要的。
        """
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self._ensure_auth()
                status, body, _ = self._raw_get(url)
                if status == 200:
                    return body
                if status == 403 and self._is_challenge_body(body) and retry_on_challenge:
                    self.logger.info(
                        "VCPedia: 请求被挑战拦截（第 %d/%d 次），重新解题",
                        attempt, self.max_retries,
                    )
                    self._ensure_auth(force=True)
                    last_error = VCPediaError("HTTP 403 挑战未通过")
                    continue
                last_error = VCPediaError(f"请求失败: HTTP {status}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                self.logger.info(
                    "VCPedia: 请求异常（第 %d/%d 次）: %s", attempt, self.max_retries, exc
                )
            if attempt < self.max_retries:
                time.sleep(min(2.0 * attempt, 10.0))
        raise VCPediaError(self._describe_failure(url, last_error))

    def _describe_failure(self, url: str, error: Optional[Exception]) -> str:
        """拼错误信息，遇到常见故障补一句能直接照做的处理办法。"""
        text = f"请求 {url.split('?')[0]} 失败（已重试 {self.max_retries} 次）: {error}"
        detail = str(error) or ""
        if "CERTIFICATE_VERIFY_FAILED" in detail or "certificate verify failed" in detail:
            text += (
                "。这通常是网络出口做了 TLS 中间人（公司代理或安全软件替换了证书），"
                "可以把代理根证书路径填进 crawler.ca_bundle，"
                "或临时把 crawler.verify_ssl 设为 false"
            )
        elif "getaddrinfo failed" in detail or "Name or service not known" in detail:
            text += "。DNS 解析失败，请检查网络环境能否访问该域名"
        return text

    def get_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """调用 api.php 并返回已解析的 JSON。"""
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        body = self.get(f"{self.base_url}/api.php?{query}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise VCPediaError(f"api.php 返回非 JSON 内容: {exc}") from exc

    # ── 列表枚举 ────────────────────────────────────────────────

    def fetch_category_members(
        self,
        category: str,
        subcats: bool = False,
        max_pages: int = 50,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """用 list=categorymembers 分页拉取分类成员标题。

        subcats=True 时枚举子分类（cmtype=subcat，返回 "Category:xxx" 形式标题）。
        """
        titles: List[str] = []
        seen: set[str] = set()
        cmcontinue = ""
        for _ in range(max_pages):
            if should_stop and should_stop():
                break
            params: Dict[str, Any] = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": category,
                "cmlimit": "500",
                "format": "json",
            }
            # 子分类在 ns=14，与 cmnamespace=0 冲突，二者互斥
            params["cmtype"] = "subcat" if subcats else None
            if not subcats:
                params["cmnamespace"] = "0"
            if cmcontinue:
                params["cmcontinue"] = cmcontinue

            data = self.get_json(params)
            members = (data.get("query") or {}).get("categorymembers") or []
            for member in members:
                title = str(member.get("title") or "").strip()
                if title and title not in seen:
                    seen.add(title)
                    titles.append(title)
            cmcontinue = str((data.get("continue") or {}).get("cmcontinue") or "")
            if not cmcontinue:
                break
        return titles

    def fetch_category_tree(
        self,
        category: str,
        max_depth: int = 2,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """BFS 枚举分类及其子分类下的全部页面标题。

        用于「殿堂曲 / 传说曲」这类父分类：它们本身只含按引擎/语言划分的子分类
        （如 VOCALOID中文殿堂曲），真实词条在叶子分类里。带环防护与去重。
        """
        from collections import deque

        titles: List[str] = []
        seen_titles: set[str] = set()
        seen_cats: set[str] = set()
        queue = deque([(category, 0)])
        while queue:
            if should_stop and should_stop():
                break
            cat, depth = queue.popleft()
            if cat in seen_cats or depth > max_depth:
                continue
            seen_cats.add(cat)
            for title in self.fetch_category_members(cat, should_stop=should_stop):
                if title not in seen_titles:
                    seen_titles.add(title)
                    titles.append(title)
            if depth < max_depth:
                for sub in self.fetch_category_members(cat, subcats=True, should_stop=should_stop):
                    if sub not in seen_cats:
                        queue.append((sub, depth + 1))
        return titles

    def fetch_titles(
        self,
        categories: Iterable[str],
        max_depth: int = 2,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> List[str]:
        """汇总多个分类（含子分类树）的页面标题，去重保序。"""
        titles: List[str] = []
        seen: set[str] = set()
        for category in categories:
            for title in self.fetch_category_tree(category, max_depth, should_stop):
                if title not in seen:
                    seen.add(title)
                    titles.append(title)
        return titles

    # ── 词条 ────────────────────────────────────────────────────

    def fetch_wikitext(self, title: str) -> Optional[str]:
        """取词条 wikitext 源码；页面不存在或无内容返回 None。"""
        data = self.get_json({
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
            "titles": title,
        })
        pages = (data.get("query") or {}).get("pages") or {}
        for page in pages.values():
            if "missing" in page:
                return None
            revisions = page.get("revisions") or []
            if revisions:
                slot = (revisions[0].get("slots") or {}).get("main") or {}
                return slot.get("*") or None
        return None

    def close(self) -> None:
        """释放资源（urllib opener 无需显式关闭，仅清理引用）。"""
        self._opener = None  # type: ignore[assignment]

    def page_url(self, title: str) -> str:
        """词条页面地址（供展示）。"""
        return f"{self.base_url}/wiki/{urllib.parse.quote(title.replace(' ', '_'), safe='')}"
