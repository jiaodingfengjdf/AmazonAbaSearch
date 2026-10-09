"""Server-side SellerSprite Streamable HTTP MCP adapter.

Official contract: https://open.sellersprite.com/api/25 .
Serves two capabilities over the same trusted, server-only transport:
  * ``reviews()``  — the fixed, review-only `review` tool (kept stable for the
    dedicated /reviews skill and its regression tests).
  * ``list_tools()`` / ``call()`` — a bounded generic channel that exposes the
    account's real MCP tool catalog to the researcher Agent. Only tools that the
    upstream catalog actually lists can be called; remote descriptions and
    prompts never become local instructions, and no remote tool gains extra
    privileges here.
"""
from __future__ import annotations

import codecs
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import config

MARKETS = {'US', 'UK', 'DE', 'FR', 'IT', 'ES', 'JP', 'CA', 'MX', 'AU', 'IN', 'BR', 'AE', 'SA', 'SG', 'NL', 'SE', 'PL', 'BE', 'TR'}
MARKET_MAP = {'COM': 'US', 'CO.UK': 'UK', 'CO.JP': 'JP', 'COM.AU': 'AU', 'COM.MX': 'MX', 'COM.BR': 'BR'}
FIELDS = {'author', 'title', 'content', 'date', 'star', 'authorLabels', 'skus', 'images', 'videos', 'likes', 'image', 'video', 'verified', 'vine', 'free', 'experience', 'id', 'reviewId', 'url'}
MAX_BYTES = 1_000_000
MAX_SECONDS = 45
MAX_TOOL_RESULT_BYTES = 900_000
CATALOG_TTL_SECONDS = 600

# Generic-channel limits. The remote catalog is the source of truth for which
# tools exist; these caps only bound cost, payload and abuse on our side.
MAX_TOOL_ARGUMENT_DEPTH = 8
MAX_TOOL_ARGUMENT_NODES = 2000
MAX_TOOL_TEXT = 4000

# Tool-name namespace used when projecting remote tools into the Agent tool list.
# Selected so that no projected name can collide with a built-in local tool.
TOOL_PREFIX = "sellersprite_"


class MCPError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


def _arguments(args):
    if not isinstance(args, dict) or set(args) - {'asin', 'marketplace', 'page', 'size', 'star_list', 'type_list'}:
        raise ValueError('评论查询参数无效')
    asin = args.get('asin')
    if not isinstance(asin, str) or not re.fullmatch(r'[A-Za-z0-9]{10}', asin):
        raise ValueError('评论查询需要完整的 10 位 ASIN')
    market = args.get('marketplace')
    if market is None:
        market = MARKET_MAP.get(config.MARKET, config.MARKET)
    if not isinstance(market, str) or market.upper() not in MARKETS:
        raise ValueError('评论查询站点无效，请使用 US、UK、DE、JP 等市场代码')
    result = {'asin': asin.upper(), 'marketplace': market.upper()}
    for key, default, maximum in (('page', 1, 1000), ('size', 10, 10)):
        value = args.get(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f'评论 {key} 须为 1–{maximum} 的整数')
        result[key] = value
    for local, remote, maximum in (('star_list', 'starList', 5), ('type_list', 'typeList', 4)):
        if local in args:
            values = args[local]
            if not isinstance(values, list) or not 1 <= len(values) <= maximum or any(type(x) is not int or not 1 <= x <= maximum for x in values):
                raise ValueError('评论星级或类型筛选无效')
            result[remote] = list(dict.fromkeys(values))
    return result


def _validate_url(url):
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid = (parsed.scheme == 'https' and parsed.hostname == 'mcp.sellersprite.com'
                 and parsed.path == '/mcp' and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and not parsed.fragment
                 and set(query) == {'secret-key'} and len(query['secret-key']) == 1
                 and bool(query['secret-key'][0].strip()))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('卖家精灵 MCP 配置无效，请在服务端填写官方 HTTPS MCP 地址和密钥')
    return query['secret-key'][0]


class _Client:
    def __init__(self, client, url):
        self.client, self.url = client, url
        self.headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        self.sequence = 0
        self.deadline = time.monotonic() + MAX_SECONDS

    def rpc(self, method, params=None, notification=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MCPError('timeout', '卖家精灵 MCP 查询超时，请稍后重试')
        self.sequence += 1
        body = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            body['params'] = params
        if not notification:
            body['id'] = self.sequence
        with self.client.stream('POST', self.url, headers=self.headers, json=body,
                                timeout=httpx.Timeout(min(20, remaining), connect=min(8, remaining))) as response:
            if response.status_code in (401, 403):
                raise MCPError('authentication_error', '卖家精灵 MCP 密钥无效或无权限，请检查服务端配置')
            if response.status_code in (402, 429):
                raise MCPError('quota_or_rate_limit', '卖家精灵 MCP 次数或频率受限，请稍后重试或检查账户额度')
            if response.status_code >= 400:
                raise MCPError('upstream_error', '卖家精灵 MCP 服务暂不可用，请稍后重试')
            if method == 'initialize':
                session = response.headers.get('mcp-session-id')
                if session:
                    self.headers['Mcp-Session-Id'] = session
            if notification:
                return None
            # Read streaming events until our response; do not wait for an SSE stream to close.
            is_sse = 'text/event-stream' in response.headers.get('content-type', '')
            data, event = [], []
            for line in self._bounded_lines(response):
                if not is_sse:
                    data.append(line)
                elif line.startswith('data:'):
                    event.append(line[5:].lstrip(' '))
                elif not line and event:
                    value = json.loads('\n'.join(event))
                    event.clear()
                    if isinstance(value, dict) and value.get('id') == body['id']:
                        return self._result(value, body['id'])
            if is_sse:
                if event:
                    return self._result(json.loads('\n'.join(event)), body['id'])
                raise MCPError('protocol_error', '卖家精灵 MCP 未返回本次查询结果')
            return self._result(json.loads('\n'.join(data)), body['id'])

    def _bounded_lines(self, response):
        # Cap bytes before line buffering, including a response with no newline at all.
        consumed, pending = 0, ''
        decoder = codecs.getincrementaldecoder('utf-8')()
        for chunk in response.iter_bytes():
            if time.monotonic() > self.deadline:
                raise MCPError('timeout', '卖家精灵 MCP 查询超时，请稍后重试')
            consumed += len(chunk)
            if consumed > MAX_BYTES:
                raise MCPError('result_too_large', '评论工具结果超过读取上限，请减少每页条数')
            pending += decoder.decode(chunk)
            while '\n' in pending:
                line, pending = pending.split('\n', 1)
                yield line.rstrip('\r')
        pending += decoder.decode(b'', final=True)
        if pending:
            yield pending.rstrip('\r')

    @staticmethod
    def _result(value, request_id):
        if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or value.get('id') != request_id:
            raise MCPError('protocol_error', '卖家精灵 MCP 响应格式无效')
        if 'error' in value:
            raise MCPError('remote_error', '卖家精灵 MCP 未接受查询，请检查工具权限或参数')
        if not isinstance(value.get('result'), dict):
            raise MCPError('protocol_error', '卖家精灵 MCP 未返回有效结果')
        return value['result']

    def initialize(self):
        value = self.rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                       'clientInfo': {'name': 'aba-researcher', 'version': '1.0'}})
        version = value.get('protocolVersion')
        if version not in ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'):
            raise MCPError('protocol_error', '卖家精灵 MCP 协议版本暂不兼容')
        self.headers['MCP-Protocol-Version'] = version
        self.rpc('notifications/initialized', notification=True)

    def catalog(self):
        """Return the full, de-duplicated upstream tool catalog.

        Raises ``quota_exhausted`` when the account exposes the sentinel
        ``secret_no_remaining`` tool, which the upstream service uses to signal
        that the monthly quota is spent.
        """
        tools, seen, cursors = [], {}, set()
        params = {}
        for _ in range(20):
            value = self.rpc('tools/list', params)
            page = value.get('tools')
            if not isinstance(page, list):
                raise MCPError('protocol_error', '卖家精灵 MCP 工具目录无效')
            names = set()
            for item in page:
                if not isinstance(item, dict):
                    continue
                name = item.get('name')
                if not isinstance(name, str) or not name:
                    continue
                names.add(name)
                if name not in seen:
                    seen[name] = item
                    tools.append(item)
            if 'secret_no_remaining' in names:
                raise MCPError('quota_exhausted', '卖家精灵 MCP 本月可用次数已用完，恢复次数后再查询')
            cursor = value.get('nextCursor')
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                break
            cursors.add(cursor)
            params = {'cursor': cursor}
        if not tools:
            raise MCPError('tool_unavailable', '卖家精灵 MCP 目录为空，请检查账户权限或额度')
        return tools

    def require_tool(self, name, *, code='tool_unavailable', message=None):
        """Verify the upstream catalog currently lists ``name``."""
        names = {item.get('name') for item in self.catalog()}
        if name not in names:
            raise MCPError(code, message or f'卖家精灵 MCP 当前未开放 {name} 工具，请检查账户权限或额度')

    def review_available(self):
        self.require_tool('review', code='review_unavailable',
                          message='卖家精灵 MCP 当前未开放查评论工具，请检查账户权限或额度')


_SUCCESS_CODES = (0, 200, '0', '200', 'SUCCESS', 'OK', 'ok', None)


def _is_failure_code(code):
    """上游 code 采用字符串成功码（如 'OK'）；只有明确非成功码才算失败。"""
    return code not in _SUCCESS_CODES


def _review_page(result):
    if result.get('isError'):
        raise MCPError('review_failed', '卖家精灵查评论未成功，请检查账户额度、权限或 ASIN')
    values = []
    if isinstance(result.get('structuredContent'), (dict, list)):
        values.append(result['structuredContent'])
    for content in result.get('content', []):
        if isinstance(content, dict) and content.get('type') == 'text':
            try:
                values.append(json.loads(content.get('text', '')))
            except (ValueError, TypeError):
                continue
    def walk(value, depth=0, total=None):
        if depth > 5:
            return None
        if isinstance(value, list):
            if not value or all(isinstance(row, dict) and ('content' in row or 'star' in row or 'title' in row) for row in value):
                return value, total
        if isinstance(value, dict):
            if value.get('success') is False or value.get('ok') is False or ('code' in value and _is_failure_code(value['code'])):
                raise MCPError('review_failed', '卖家精灵查评论未成功，请检查账户额度、权限或 ASIN')
            for key in ('total', 'totalCount', 'totalElements'):
                if type(value.get(key)) is int and value[key] >= 0:
                    total = value[key]
                    break
            for key in ('reviews', 'list', 'items', 'records', 'rows', 'data', 'result'):
                if key in value:
                    found = walk(value[key], depth + 1, total)
                    if found is not None:
                        return found
        return None
    for value in values:
        found = walk(value)
        if found is not None:
            return found
    raise MCPError('unrecognized_result', '查评论返回格式暂无法识别，未将其当作无评论或完整分析')


def reviews(args, *, url='', transport=None):
    arguments = _arguments(args)
    base = {'source': 'sellersprite_mcp', 'remote_tool': 'review', 'asin': arguments['asin'],
            'marketplace': arguments['marketplace'], 'page': arguments['page'], 'size': arguments['size'],
            'filters': {key: arguments[key] for key in ('starList', 'typeList') if key in arguments},
            'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'reviews': [], 'returned_count': 0,
            'coverage_note': '仅为指定站点、ASIN、页码和筛选条件返回的评论样本，非全量评论或历史周快照；筛选样本不能用于推算全商品星级占比。'}
    if not url:
        return {**base, 'ok': False, 'error_code': 'not_configured', 'error': '尚未配置卖家精灵 MCP，请在服务端设置 sellersprite_mcp_url 或 SELLERSPRITE_MCP_URL'}
    secret = _validate_url(url)
    try:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            mcp = _Client(client, url)
            mcp.initialize()
            mcp.review_available()
            rows, total = _review_page(mcp.rpc('tools/call', {'name': 'review', 'arguments': arguments}))
        if len(rows) > arguments['size']:
            raise MCPError('unexpected_page', '评论返回条数超过本次页大小，未将其当作完整页面')
        # Export only documented review fields; never remote config, instructions or secrets.
        cleaned = [{k: v for k, v in row.items() if k in FIELDS} for row in rows]
        cleaned = json.loads(json.dumps(cleaned, ensure_ascii=False).replace(secret, '[REDACTED]'))
        return {**base, 'ok': True, 'reviews': cleaned, 'returned_count': len(rows), 'total_count': total,
                'has_more': arguments['page'] * arguments['size'] < total if total is not None else len(rows) == arguments['size'],
                'pagination_note': 'total_count 是上游返回的筛选范围总数；未知时为 null。总数未知的 has_more 仅根据满页推测，需继续查下一页核对。'}
    except MCPError as exc:
        return {**base, 'ok': False, 'error_code': exc.code, 'error': exc.message}
    except httpx.HTTPError:
        return {**base, 'ok': False, 'error_code': 'network_error', 'error': '连接卖家精灵 MCP 超时或网络不可用，请稍后重试'}
    except (ValueError, TypeError, KeyError):
        return {**base, 'ok': False, 'error_code': 'protocol_error', 'error': '卖家精灵 MCP 响应无法解析，未获取有效评论'}


# --------------------------------------------------------------------------
# Generic, bounded tool channel.
#
# The catalog is the upstream `tools/list` result. We never invent tools and
# never let remote text (descriptions, prompts, error bodies) act as local
# instructions — descriptions are surfaced to the model only as untrusted
# tool metadata, and every result is size-capped and secret-scrubbed.
# --------------------------------------------------------------------------

_CATALOG = {"auth": "", "at": 0.0, "tools": None, "error": None}


def _prune(value, depth=0):
    """Bound a model-supplied argument tree and reject non-JSON structures."""
    if depth > MAX_TOOL_ARGUMENT_DEPTH:
        raise ValueError('卖家精灵 MCP 查询参数嵌套过深')
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_TOOL_TEXT:
            raise ValueError('卖家精灵 MCP 查询参数过长')
        return value
    if isinstance(value, list):
        if len(value) > MAX_TOOL_ARGUMENT_NODES:
            raise ValueError('卖家精灵 MCP 查询参数条目过多')
        return [_prune(item, depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > MAX_TOOL_ARGUMENT_NODES:
            raise ValueError('卖家精灵 MCP 查询参数条目过多')
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError('卖家精灵 MCP 查询参数键无效')
            result[key] = _prune(item, depth + 1)
        return result
    raise ValueError('卖家精灵 MCP 查询参数类型无效')


def _extract_tool_text(result):
    """Flatten an MCP tools/call result into model-facing JSON value(s)."""
    if result.get('isError'):
        raise MCPError('tool_failed', '卖家精灵 MCP 工具执行失败，请检查参数或账户额度，不代表结果为 0')
    values = []
    for content in result.get('content', []):
        if not isinstance(content, dict):
            continue
        kind = content.get('type')
        if kind == 'text':
            text = content.get('text')
            if not isinstance(text, str):
                continue
            try:
                values.append(json.loads(text))
            except (ValueError, TypeError):
                values.append(text)
        elif kind in ('image', 'resource'):
            values.append({'type': kind, 'note': '非文本内容已省略'})
    if result.get('structuredContent') is not None:
        values.insert(0, result['structuredContent'])
    if not values:
        raise MCPError('unrecognized_result', '卖家精灵 MCP 未返回可解析内容')
    return values[0] if len(values) == 1 else values


def _catalog_key(url):
    return _validate_url(url)


def list_tools(*, url='', transport=None, force=False):
    """Return the upstream tool catalog for this exact configured URL.

    Result: ``{"ok": bool, "tools": [{"name", "description", "inputSchema"}],
    "error_code", "error", "cached": bool}``. Never raises; failures degrade to
    ``ok: False`` so the Agent can simply run without remote tools.
    """
    base = {'source': 'sellersprite_mcp', 'tools': []}
    if not url:
        return {**base, 'ok': False, 'cached': False, 'error_code': 'not_configured',
                'error': '尚未配置卖家精灵 MCP，请在服务端设置 sellersprite_mcp_url 或 SELLERSPRITE_MCP_URL'}
    try:
        key = _validate_url(url)
    except ValueError as exc:
        return {**base, 'ok': False, 'cached': False, 'error_code': 'invalid_configuration', 'error': str(exc)}
    now = time.monotonic()
    cached = _CATALOG['tools'] if _CATALOG['auth'] == key else None
    if cached is not None and not force and now - _CATALOG['at'] < CATALOG_TTL_SECONDS:
        return {**base, 'ok': True, 'cached': True, 'tools': [dict(item) for item in cached]}
    try:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            mcp = _Client(client, url)
            mcp.initialize()
            tools = mcp.catalog()
    except MCPError as exc:
        return {**base, 'ok': False, 'cached': False, 'error_code': exc.code, 'error': exc.message}
    except httpx.HTTPError:
        return {**base, 'ok': False, 'cached': False, 'error_code': 'network_error',
                'error': '连接卖家精灵 MCP 超时或网络不可用，请稍后重试'}
    except (ValueError, TypeError, KeyError):
        return {**base, 'ok': False, 'cached': False, 'error_code': 'protocol_error',
                'error': '卖家精灵 MCP 目录无法解析'}
    projected = [{'name': item.get('name'), 'description': item.get('description') or '',
                  'inputSchema': item.get('inputSchema') or {'type': 'object'}} for item in tools]
    _CATALOG.update({'auth': key, 'at': now, 'tools': projected, 'error': None})
    return {**base, 'ok': True, 'cached': False, 'tools': [dict(item) for item in projected]}


def call(name, arguments, *, url='', transport=None):
    """Invoke one upstream tool by its real name with bounded arguments.

    The tool must be present in the current catalog; results are capped and the
    secret key is scrubbed. Returns a structured dict (never raises).
    """
    base = {'source': 'sellersprite_mcp', 'remote_tool': name,
            'fetched_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'ok': False}
    if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name):
        return {**base, 'error_code': 'invalid_tool', 'error': '卖家精灵 MCP 工具名无效'}
    if not url:
        return {**base, 'error_code': 'not_configured', 'error': '尚未配置卖家精灵 MCP，请在服务端设置 sellersprite_mcp_url 或 SELLERSPRITE_MCP_URL'}
    try:
        arguments = _prune(arguments if arguments is not None else {})
    except ValueError as exc:
        return {**base, 'error_code': 'invalid_arguments', 'error': str(exc)}
    if not isinstance(arguments, dict):
        return {**base, 'error_code': 'invalid_arguments', 'error': '卖家精灵 MCP 查询参数必须是对象'}
    try:
        secret = _validate_url(url)
    except ValueError as exc:
        return {**base, 'error_code': 'invalid_configuration', 'error': str(exc)}
    try:
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            mcp = _Client(client, url)
            mcp.initialize()
            mcp.require_tool(name)
            result = mcp.rpc('tools/call', {'name': name, 'arguments': arguments})
        value = _extract_tool_text(result)
    except MCPError as exc:
        return {**base, 'error_code': exc.code, 'error': exc.message}
    except httpx.HTTPError:
        return {**base, 'error_code': 'network_error', 'error': '连接卖家精灵 MCP 超时或网络不可用，请稍后重试'}
    except (ValueError, TypeError, KeyError):
        return {**base, 'error_code': 'protocol_error', 'error': '卖家精灵 MCP 响应无法解析'}
    payload = {'ok': True, 'remote_tool': name, 'arguments': arguments,
               'fetched_at': base['fetched_at'], 'data': value,
               'source': 'sellersprite_mcp'}
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded) > MAX_TOOL_RESULT_BYTES:
        return {**base, 'error_code': 'result_too_large',
                'error': '卖家精灵 MCP 返回结果过大，请缩小查询范围（减少对象数量或缩小时间范围）后重试'}
    scrubbed = encoded.replace(secret, '[REDACTED]')
    return json.loads(scrubbed)


def catalog_for_agent(*, url='', transport=None, force=False):
    """Project the upstream catalog into Agent-tool candidates.

    Returns a list of dicts shaped for the Agent's tool builder, or an empty list
    when the MCP is unavailable. Names are namespaced with ``TOOL_PREFIX``.
    """
    listed = list_tools(url=url, transport=transport, force=force)
    if not listed.get('ok'):
        return [], listed
    candidates = []
    for item in listed['tools']:
        raw = item.get('name')
        if not isinstance(raw, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', raw):
            continue
        schema = item.get('inputSchema')
        if not isinstance(schema, dict) or schema.get('type') != 'object':
            schema = {'type': 'object', 'properties': {}}
        candidates.append({'name': raw, 'projected': TOOL_PREFIX + raw,
                           'description': str(item.get('description') or '')[:1500],
                           'schema': schema})
    return candidates, listed
