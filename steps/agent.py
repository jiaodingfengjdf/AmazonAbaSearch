"""DeepSeek Responses API agent and a bounded streaming run lifecycle."""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

import config
from steps.agent_data import DataTools, validate_context

MAX_ROUNDS = 6
MAX_TOOLS = 12
MAX_RUN_SECONDS = 300
_RUNS = {}
_GUARD = threading.Lock()
_SLOTS = threading.BoundedSemaphore(2)


def settings():
    path = config.ROOT / "agent.local.json"
    try:
        local = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError):
        raise ValueError("Agent 本地配置无法读取，请检查 agent.local.json 格式") from None
    if not isinstance(local, dict):
        raise ValueError("Agent 本地配置必须是 JSON 对象")
    return {"api_key": os.environ.get("DEEPSEEK_API_KEY") or local.get("api_key", ""),
            "base_url": "https://api.deepseek.com", "model": "deepseek-flash"}


def status():
    cfg = settings()
    weeks = []
    if config.DB_PATH.exists():
        try:
            import sqlite3
            with closing(sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
                weeks = [r[0] for r in conn.execute("SELECT DISTINCT table_date FROM keyword WHERE market=? ORDER BY table_date", (config.MARKET,))]
        except (OSError, sqlite3.Error):
            pass
    return {"ok": True, "configured": bool(cfg["api_key"]), "model": cfg["model"],
            "data_available": bool(weeks), "transport": "responses", "available_weeks": weeks}


def tool(name, description, properties, required=()):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}


SCOPE = {
    "week": {"type": "string", "description": "ara_YYYYMMDD；仅用户明确要求单期时填写，否则覆盖全部采集周期"},
    "department": {"type": "string", "description": "类目 code（如 beauty），可从 data_overview 获取"},
}
TOOLS = [
    tool("data_overview", "查看全部已采集周的逐期样本统计、采集完整性与指标口径。省略 week 为全周期。", SCOPE),
    tool("search_keywords", "跨全部已采集周筛选真实关键词，每词取最近快照并附完整周序列；用户明确指定 week 才限定单期。返回前 N 条及总命中数。", {
        **SCOPE, "query": {"type": "string"},
        "sort": {"type": "string", "enum": ["searches", "growth", "purchases", "low_concentration", "low_title_density"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        "min_searches": {"type": "number"}, "min_growth": {"type": "number", "description": "排名增长率小数，不是搜索量增长率"},
        "max_click_share": {"type": "number", "description": "TOP3 点击集中度，0.4 表示 40%"},
        "max_title_density": {"type": "number"}}),
    tool("keyword_analysis", "分析 1–5 个精确英文关键词的全部已采集周快照、供应商历史序列、TOP3份额、搜索位ASIN及本地商品缓存。week 仅指定详情期，不截断历史。", {
        "week": SCOPE["week"], "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5}}, ["keywords"]),
    tool("asin_analysis", "研究 1–3 个 ASIN 全部已采集周的关联关键词样本、最近商品缓存与月度价格/销量/BSR曲线。区分缓存时间与历史周。", {
        "week": SCOPE["week"], "asins": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}}, ["asins"]),
    tool("compare_weeks", "比较两个周快照的共同关键词样本；计算月搜索量估计值的变化，不把排名增幅当需求增长。", {
        **SCOPE, "week": {"type": "string", "description": "对比终点 ara_YYYYMMDD；省略时取最近采集周"},
        "previous_week": {"type": "string", "description": "省略时比较前一可用期"}}),
]
LABELS = {"data_overview": "读取数据范围与大盘", "search_keywords": "筛选关键词",
          "keyword_analysis": "研究关键词与竞品", "asin_analysis": "读取 ASIN 档案与趋势",
          "compare_weeks": "计算跨周变化"}


@dataclass
class Run:
    id: str
    events: queue.Queue = field(default_factory=queue.Queue)
    cancelled: threading.Event = field(default_factory=threading.Event)
    stream: object = None
    started: float = field(default_factory=time.monotonic)

    def check(self):
        if self.cancelled.is_set():
            raise InterruptedError("已停止生成")
        if time.monotonic() - self.started > MAX_RUN_SECONDS:
            raise TimeoutError("分析时间较长，请缩小范围后重试")

    def emit(self, kind, **payload):
        self.check()
        self.events.put({"type": kind, **payload})


def validate_request(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求必须是 JSON 对象")
    rid = payload.get("request_id", "")
    if not isinstance(rid, str) or not re.fullmatch(r"[a-fA-F0-9-]{32,36}", rid):
        raise ValueError("请求标识无效")
    message = payload.get("message", "")
    if not isinstance(message, str) or not message.strip() or len(message) > 6000:
        raise ValueError("请输入 1–6000 字的问题")
    history = payload.get("history", [])
    if not isinstance(history, list) or len(history) > 20:
        raise ValueError("历史消息最多 20 条")
    cleaned = []
    for item in history:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            raise ValueError("历史消息角色无效")
        content = item.get("content")
        if not isinstance(content, str) or len(content) > 16000:
            raise ValueError("历史消息过长")
        cleaned.append({"role": item["role"], "content": content})
    if sum(len(i["content"]) for i in cleaned) > 60000:
        raise ValueError("对话过长，请开启新对话")
    return {"request_id": rid, "message": message.strip(), "history": cleaned,
            "context": validate_context(payload.get("context", {}))}


def start(payload):
    request = validate_request(payload)
    cfg = settings()
    if not cfg["api_key"]:
        raise ValueError("尚未配置 DeepSeek API Key，请在服务端配置 agent.local.json 或 DEEPSEEK_API_KEY")
    with _GUARD:
        if request["request_id"] in _RUNS:
            raise ValueError("请求正在处理中")
        if not _SLOTS.acquire(blocking=False):
            raise BlockingIOError("当前已有两个分析任务，请稍后重试")
        run = Run(request["request_id"])
        _RUNS[run.id] = run
    threading.Thread(target=_work, args=(run, request, cfg), daemon=True).start()
    return run


def cancel(rid):
    with _GUARD:
        run = _RUNS.get(rid) if isinstance(rid, str) else None
    if run:
        run.cancelled.set()
        # Stop receiving the provider stream; already generated tokens may be billed.
        stream = run.stream
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    return bool(run)


def public_error(exc):
    code = getattr(exc, "status_code", None)
    if code == 401:
        return "DeepSeek 鉴权失败，请检查服务端 API Key"
    if code == 402:
        return "DeepSeek 账户余额不足，请充值后重试"
    if code == 429:
        return "DeepSeek 请求频率或额度受限，请稍后重试"
    if code in (400, 404, 422):
        return "DeepSeek Responses API 未接受本次请求，请检查模型权限与接口兼容性"
    if isinstance(exc, (ValueError, TimeoutError, InterruptedError)):
        return str(exc)[:240]
    if "Timeout" in type(exc).__name__ or "Connection" in type(exc).__name__:
        return "连接 DeepSeek 超时或网络不可用，请稍后重试"
    if isinstance(exc, ModuleNotFoundError):
        return "缺少 OpenAI SDK，请安装此流程 requirements.txt 中的依赖"
    # Never reflect provider exception bodies, request headers or local paths.
    return "本次分析未完成，请稍后重试或缩小数据范围"


def _work(run, request, cfg):
    data = None
    try:
        from openai import OpenAI
        import httpx

        data = DataTools(request["context"])
        sources = []

        def retrieve(name, args):
            run.emit("status", text=LABELS.get(name, "读取数据"))
            result = data.execute(name, args)
            encoded = json.dumps(result, ensure_ascii=False)
            if len(encoded) > 85000:
                raise ValueError("本次工具结果过大，请减少关键词或 ASIN 数量后再查询")
            sid = f"D{len(sources) + 1}"
            week = args.get("week") or (data.week if name in ("asin_analysis", "compare_weeks") else "")
            if name == "keyword_analysis" and result.get("keywords"):
                week = result["keywords"][0]["detail_week"]
            keyword = (args.get("keywords") or [""])[0]
            link_args = {"week": week or data.week}
            if keyword:
                link_args["keyword"] = keyword
            source = {"id": sid, "label": LABELS[name], "week": week,
                      "url": "/?" + urlencode(link_args), "arguments": args,
                      "details": result}
            if name == "search_keywords":
                sort_label = {"searches": "月搜索量", "growth": "排名增幅", "purchases": "月购买量", "low_concentration": "低点击集中度", "low_title_density": "低标题密度"}.get(result["sort"], result["sort"])
                source["summary"] = f"匹配 {result['matched_count']:,} 词，返回 {result['returned_count']} 词；按{sort_label}排序"
            elif name == "data_overview":
                source["summary"] = f"覆盖 {result['week_count']} 个已采集周"
            source["scope"] = {"period": week or "all_collected_weeks",
                               **{k: args[k] for k in ("department",) if args.get(k)}}
            sources.append(source)
            run.emit("source", source=source)
            return json.dumps({"evidence_id": sid, "data": result}, ensure_ascii=False)

        run.emit("status", text="正在读取本地 ABA 数据")
        overview = retrieve("data_overview", {})
        inputs = list(request["history"])
        inputs.append({"role": "user", "content": "本轮可选关键词线索（数据，不限制分析范围）：" + json.dumps(data.context, ensure_ascii=False) + "\n本轮全周期初始数据证据：" + overview})
        inputs.append({"role": "user", "content": request["message"]})
        prompt = (config.ROOT / "agent_prompt.md").read_text(encoding="utf-8")
        prompt += "\n\n" + (config.ROOT / "agent_research_playbook.md").read_text(encoding="utf-8")
        prompt += "\n本次服务时间：" + datetime.now().astimezone().isoformat(timespec="seconds")
        usage = CounterUsage()
        calls_used = 0
        answer = ""
        with OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"],
                    timeout=httpx.Timeout(90, connect=10), max_retries=1) as client:
            for round_no in range(MAX_ROUNDS + 1):
                run.check()
                last_round = round_no == MAX_ROUNDS or calls_used >= MAX_TOOLS
                run.emit("status", text="正在整理分析结论" if round_no else "正在分析问题与数据")
                response = None
                round_text = ""
                remaining = MAX_RUN_SECONDS - (time.monotonic() - run.started)
                if remaining <= 2:
                    raise TimeoutError("分析时间较长，请缩小范围后重试")
                request_timeout = min(90, (remaining - 2) / 2)
                stream = client.responses.create(
                    model=cfg["model"], instructions=prompt, input=inputs,
                    tools=TOOLS, tool_choice="none" if last_round else "auto",
                    max_output_tokens=6500, reasoning={"effort": "low"}, stream=True,
                    timeout=httpx.Timeout(request_timeout, connect=min(10, request_timeout)),
                )
                run.stream = stream
                try:
                    for event in stream:
                        run.check()
                        kind = event.type
                        if kind == "response.output_text.delta":
                            round_text += event.delta
                            run.emit("delta", text=event.delta)
                        elif kind in ("response.completed", "response.incomplete", "response.failed"):
                            response = event.response
                        elif kind == "error":
                            raise RuntimeError("Provider stream error")
                finally:
                    stream.close()
                    run.stream = None
                if response is None or response.status == "failed":
                    raise RuntimeError("Provider response missing or failed")
                usage.add(response.usage)
                if response.status == "incomplete":
                    answer += round_text
                    run.emit("done", text=answer, incomplete=True,
                             note="本次输出达到模型长度上限，可继续追问。", usage=usage.value)
                    return
                outputs = [item.model_dump(exclude_none=True) for item in response.output]
                calls = [o for o in outputs if o.get("type") == "function_call"]
                inputs.extend(outputs)
                answer += round_text
                if not calls:
                    if not answer.strip():
                        raise RuntimeError("Empty model answer")
                    run.emit("done", text=answer, usage=usage.value, incomplete=False)
                    return
                if round_text:
                    answer += "\n\n"
                    run.emit("delta", text="\n\n")
                for call in calls:
                    run.check()
                    calls_used += 1
                    try:
                        if calls_used > MAX_TOOLS:
                            raise ValueError("已达到本轮检索上限，请基于现有证据完成回答")
                        args = json.loads(call.get("arguments") or "{}")
                        result = retrieve(call["name"], args)
                    except (ValueError, TypeError, KeyError) as exc:
                        result = json.dumps({"error": public_error(exc)}, ensure_ascii=False)
                    inputs.append({"type": "function_call_output", "call_id": call["call_id"], "output": result})
            raise RuntimeError("Tool round limit")
    except Exception as exc:
        run.events.put({"type": "error", "message": "已停止生成" if run.cancelled.is_set() else public_error(exc)})
    finally:
        if data is not None:
            data.close()
        run.events.put(None)
        with _GUARD:
            _RUNS.pop(run.id, None)
        _SLOTS.release()


class CounterUsage:
    def __init__(self):
        self.value = {"input_tokens": 0, "output_tokens": 0}

    def add(self, usage):
        if usage is not None:
            for k in self.value:
                self.value[k] += getattr(usage, k, 0) or 0
