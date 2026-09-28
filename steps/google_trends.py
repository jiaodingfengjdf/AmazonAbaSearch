"""按关键词缓存 Google 网页搜索近五年周趋势。"""
from datetime import datetime, timezone
import json
import math
import sqlite3
import threading
import time

import config

from steps.asin_detail import make_client, SESSION_CODES

MAX_AGE = 24 * 3600
_LOCKS = {}
_GUARD = threading.Lock()


def load(*, keyword, market, live=True, db_path=None):
    """看板与 Agent 共用缓存及并发去重；数据库路径仅由内部代码提供。"""
    path = str(db_path or config.DB_PATH)
    with _GUARD:
        lock = _LOCKS.setdefault((path, market, keyword), threading.Lock())
    with lock:
        conn = sqlite3.connect(path, timeout=30)
        try:
            return payload(conn, keyword=keyword, market=market, live=live)
        finally:
            conn.close()


def fetch(keyword, market):
    client = make_client()
    client.timeout = 30
    client.retries = 0
    try:
        return client._get('/v2/keyword/google-trends.json', {
            'station': market, 'keyword': keyword, 'gprop': '', 'intervalYear': 5,
            'gv': 'false', 'monthly': 'false',
        })['data']
    finally:
        client.session.close()


def normalize(data):
    if not isinstance(data, dict) or not isinstance(data.get('timeLineData'), list):
        raise ValueError('谷歌趋势响应格式异常')
    points = []
    for item in data['timeLineData']:
        stamp = float(item['time'])
        value = item.get('value')
        if isinstance(value, list):
            value = value[0] if value else None
        if item.get('hasData') is False or value is None:
            value = None
        else:
            value = float(value)
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError('谷歌趋势指数异常')
        points.append({
            'date': datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime('%Y-%m-%d'),
            'time': stamp, 'value': value,
            'label': str(item.get('fomattedValue', item.get('formattedValue', value if value is not None else '无数据'))),
        })
    return sorted(points, key=lambda p: p['time'])


def payload(conn, *, keyword, market, live=True):
    conn.execute('CREATE TABLE IF NOT EXISTS google_trend ('
                 'market TEXT, keyword TEXT, fetched_at REAL, payload TEXT, '
                 'PRIMARY KEY (market, keyword))')
    row = conn.execute('SELECT fetched_at, payload FROM google_trend WHERE market=? AND keyword=?',
                       (market, keyword)).fetchone()
    cached = json.loads(row[1]) if row else None
    if cached is not None and time.time() - row[0] < MAX_AGE:
        return {**cached, 'source': 'cache'}
    try:
        if not live:
            raise RuntimeError('登录态正在恢复，请稍后重试')
        raw = fetch(keyword, market)
        result = {'ok': True, 'keyword': keyword, 'station': market,
                  'trend': normalize(raw), 'source': 'live', 'fetchedAt': time.time()}
        conn.execute('INSERT OR REPLACE INTO google_trend VALUES (?, ?, ?, ?)',
                     (market, keyword, result['fetchedAt'], json.dumps(result, ensure_ascii=False)))
        conn.commit()
        return result
    except Exception as exc:
        code = getattr(exc, 'code', 'ERR_GOOGLE_TRENDS_FETCH')
        if code in SESSION_CODES:
            code = 'ERR_USER_NOT_LOGIN'
            message = '卖家精灵登录态失效，请稍后重试'
        else:
            message = '谷歌趋势暂时无法获取，请稍后重试'
        if cached is not None:
            return {**cached, 'source': 'cache', 'stale': True, 'error': message, 'errorCode': code}
        return {'ok': False, 'keyword': keyword, 'station': market, 'trend': [],
                'error': message, 'errorCode': code}
