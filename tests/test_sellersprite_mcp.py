"""Review-only MCP transport and evidence regression tests; no paid network calls."""
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from steps import agent, agent_skills


URL = 'https://mcp.sellersprite.com/mcp?secret-key=fixture-secret'


class ReviewMCPTest(unittest.TestCase):
    def invoke(self, args, tools=None, result=None, sse=False):
        from steps import sellersprite_mcp
        calls = []
        def handle(request):
            body = json.loads(request.content)
            calls.append((body, dict(request.headers)))
            method = body['method']
            if method == 'initialize':
                payload = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
                return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': payload}, headers={'Mcp-Session-Id': 'fixture-session'})
            self.assertEqual(request.headers['mcp-session-id'], 'fixture-session')
            self.assertEqual(request.headers['mcp-protocol-version'], '2025-03-26')
            if method == 'notifications/initialized':
                return httpx.Response(202)
            if method == 'tools/list':
                payload = {'tools': tools if tools is not None else [{'name': 'review', 'inputSchema': {'type': 'object'}}]}
            elif method == 'tools/call':
                payload = result if result is not None else {'content': [{'type': 'text', 'text': json.dumps({'data': {'total': 22, 'list': [{'title': 'Leaks', 'content': 'Cap leaks when tilted', 'star': 2, 'date': 1772380800000, 'verified': True, 'vine': False, 'secret-key': 'fixture-secret'}]}})}]}
            else:
                self.fail('unapproved method ' + method)
            response = {'jsonrpc': '2.0', 'id': body['id'], 'result': payload}
            if sse:
                notification = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress', 'params': {}})
                return httpx.Response(200, text='data: ' + notification + '\n\nevent: message\ndata: ' + json.dumps(response) + '\n\n', headers={'content-type': 'text/event-stream'})
            return httpx.Response(200, json=response)
        value = sellersprite_mcp.reviews(args, url=URL, transport=httpx.MockTransport(handle))
        return value, calls

    def test_skill_and_agent_tool_are_registered(self):
        self.assertIn('reviews', [x['id'] for x in agent_skills.catalog()])
        skill = agent_skills.resolve('/reviews b012345678')
        self.assertEqual(skill['arguments'], 'b012345678')
        self.assertIn('sellersprite_reviews', skill['instructions'])
        self.assertIn('样本', skill['instructions'])
        self.assertIn('额度', skill['instructions'])
        self.assertIn('sellersprite_reviews', [x['name'] for x in agent.TOOLS])

    def test_real_protocol_session_mapping_filters_and_clean_evidence(self):
        result, calls = self.invoke({'asin': 'b012345678', 'marketplace': 'US', 'page': 2, 'size': 10, 'star_list': [1, 2, 3], 'type_list': [3]})
        call = next(body for body, _ in calls if body['method'] == 'tools/call')
        self.assertEqual(call['params'], {'name': 'review', 'arguments': {'asin': 'B012345678', 'marketplace': 'US', 'page': 2, 'size': 10, 'starList': [1, 2, 3], 'typeList': [3]}})
        self.assertTrue(result['ok'])
        self.assertEqual(result['returned_count'], 1)
        self.assertEqual(result['total_count'], 22)
        self.assertTrue(result['has_more'])
        self.assertEqual(result['reviews'][0]['content'], 'Cap leaks when tilted')
        self.assertTrue(result['reviews'][0]['verified'])
        self.assertIn('fetched_at', result)
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertIn('样本', result['coverage_note'])

    def test_sse_response_is_supported(self):
        result, _ = self.invoke({'asin': 'B012345678'}, sse=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['returned_count'], 1)

    def test_exhausted_key_does_not_call_any_tool_or_invent_empty_success(self):
        result, calls = self.invoke({'asin': 'B012345678'}, tools=[{'name': 'secret_no_remaining', 'description': '当前没有可使用的次数'}])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'quota_exhausted')
        self.assertIn('次数', result['error'])
        self.assertEqual(result['reviews'], [])
        self.assertNotIn('tools/call', [body['method'] for body, _ in calls])

    def test_other_remote_tools_are_not_exposed_or_called(self):
        result, calls = self.invoke({'asin': 'B012345678'}, tools=[{'name': 'product_research'}])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'review_unavailable')
        self.assertNotIn('tools/call', [body['method'] for body, _ in calls])

    def test_upstream_ok_string_code_is_success_not_failure(self):
        # 上游 review 返回 {"code":"OK","message":"成功","data":{...}}；字符串 OK 是成功码，不能被当成额度/权限失败。
        page = {'code': 'OK', 'message': '成功', 'data': {'page': 1, 'size': 10, 'total': 0, 'items': [], 'hasNextPage': False}}
        result, _ = self.invoke({'asin': 'B012345678'}, result={'content': [{'type': 'text', 'text': json.dumps(page)}], 'isError': False})
        self.assertTrue(result['ok'])
        self.assertIsNone(result.get('error_code'))
        self.assertEqual(result['returned_count'], 0)
        self.assertEqual(result['total_count'], 0)

    def test_upstream_ok_string_code_with_reviews_is_returned(self):
        page = {'code': 'OK', 'data': {'total': 725, 'items': [
            {'title': 'Bottom was smashed in', 'content': 'Just doesn’t work', 'star': 1, 'date': 1772380800000, 'verified': True}]}}
        result, _ = self.invoke({'asin': 'B0D3LGT8M3'}, result={'content': [{'type': 'text', 'text': json.dumps(page)}], 'isError': False})
        self.assertTrue(result['ok'])
        self.assertEqual(result['returned_count'], 1)
        self.assertEqual(result['total_count'], 725)
        self.assertEqual(result['reviews'][0]['content'], 'Just doesn’t work')
        self.assertTrue(result['has_more'])

    def test_structured_empty_page_and_business_failure_are_distinct(self):
        result, _ = self.invoke({'asin': 'B012345678'}, result={'structuredContent': {'data': {'list': [], 'total': 0}}, 'content': []})
        self.assertTrue(result['ok'])
        self.assertEqual(result['returned_count'], 0)
        self.assertFalse(result['has_more'])
        result, _ = self.invoke({'asin': 'B012345678'}, result={'isError': True, 'content': [{'type': 'text', 'text': URL}]})
        self.assertFalse(result['ok'])
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_invalid_inputs_never_reach_network(self):
        from steps import sellersprite_mcp
        cases = ({'asin': 'bad'}, {'asin': 'B012345678', 'size': 11}, {'asin': 'B012345678', 'page': True}, {'asin': 'B012345678', 'star_list': [0]}, {'asin': 'B012345678', 'type_list': [5]}, {'asin': 'B012345678', 'marketplace': 'UNKNOWN'}, {'asin': 'B012345678', 'url': 'https://evil.invalid'}, {'asin': 'B012345678', 'star_list': [True]})
        with patch('httpx.Client', side_effect=AssertionError('invalid input must not connect')):
            for args in cases:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    sellersprite_mcp.reviews(args, url=URL)
            for url in ('http://mcp.sellersprite.com/mcp?secret-key=x', 'https://evil.invalid/mcp?secret-key=x', 'https://mcp.sellersprite.com/other?secret-key=x'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    sellersprite_mcp.reviews({'asin': 'B012345678'}, url=url)

    def test_transport_exception_is_sanitized(self):
        from steps import sellersprite_mcp
        def fail(request):
            raise httpx.ConnectError(URL, request=request)
        result = sellersprite_mcp.reviews({'asin': 'B012345678'}, url=URL, transport=httpx.MockTransport(fail))
        self.assertFalse(result['ok'])
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(result['error_code'], 'network_error')

    def test_public_status_contains_no_config_url_or_key(self):
        with patch.object(agent, 'settings', return_value={'api_key': 'deepseek-fixture', 'model': 'deepseek-flash', 'sellersprite_mcp_url': URL}):
            value = agent.status()
        self.assertTrue(value['integrations']['sellersprite_mcp']['configured'])
        self.assertNotIn('fixture-secret', json.dumps(value))
        self.assertNotIn('deepseek-fixture', json.dumps(value))

    def test_size_limit_stops_stream_before_buffering_a_giant_single_line(self):
        from steps import sellersprite_mcp
        delivered = []
        class Body(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(30):
                    delivered.append(1)
                    yield b'x' * 1000
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Body(), headers={'content-type': 'application/json'}))
        with httpx.Client(transport=transport) as client, patch.object(sellersprite_mcp, 'MAX_BYTES', 5000):
            rpc = sellersprite_mcp._Client(client, URL)
            with self.assertRaises(sellersprite_mcp.MCPError) as error:
                rpc.rpc('initialize')
            self.assertEqual(error.exception.code, 'result_too_large')
        self.assertLessEqual(len(delivered), 6)

    def test_review_skill_roundtrip_without_market_database_and_sample_source(self):
        captured = []
        class Item:
            def __init__(self, value): self.value = value
            def model_dump(self, **kwargs): return self.value
        class Stream:
            def __init__(self, events): self.events = events
            def __iter__(self): return iter(self.events)
            def close(self): pass
        class Client:
            def __init__(self, **kwargs): self.responses = self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def create(self, **kwargs):
                captured.append({**kwargs, 'input': list(kwargs['input'])})
                if len(captured) == 1:
                    output = [Item({'type': 'function_call', 'call_id': 'fc-review', 'name': 'sellersprite_reviews', 'arguments': '{"asin":"B012345678","page":2}'})]
                    events = []
                else:
                    output = [Item({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '盖子漏水 [D1]'}]})]
                    events = [SimpleNamespace(type='response.output_text.delta', delta='盖子漏水 [D1]')]
                events.append(SimpleNamespace(type='response.completed', response=SimpleNamespace(status='completed', output=output, usage=None)))
                return Stream(events)
        result, _ = self.invoke({'asin': 'B012345678', 'page': 2})
        from steps import sellersprite_mcp
        request = {'context': {}, 'history': [], 'message': '/reviews B012345678', 'skill': agent_skills.resolve('/reviews B012345678')}
        run = agent.Run('e' * 32)
        agent._SLOTS.acquire()
        with patch('openai.OpenAI', Client), patch.object(agent, 'DataTools', side_effect=AssertionError('review-only research must not open market DB')), patch.object(sellersprite_mcp, 'reviews', return_value=result) as remote:
            agent._work(run, request, {'api_key': 'fixture', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'sellersprite_mcp_url': URL})
        events = []
        while (event := run.events.get_nowait()) is not None:
            events.append(event)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('[D1]', events[-1]['text'])
        source = next(event['source'] for event in events if event['type'] == 'source')
        self.assertEqual(source['scope']['period'], 'live_review_sample')
        self.assertEqual(source['scope']['asin'], 'B012345678')
        self.assertEqual(source['scope']['page'], 2)
        self.assertEqual(source['url'], '')
        self.assertNotIn('fixture-secret', json.dumps(events))
        self.assertNotIn('fixture-secret', json.dumps(captured, default=str))
        remote.assert_called_once_with({'asin': 'B012345678', 'page': 2}, url=URL)
        evidence = next(x for x in captured[1]['input'] if x.get('type') == 'function_call_output')
        self.assertEqual(json.loads(evidence['output'])['data']['reviews'][0]['star'], 2)


class GenericToolChannelTest(unittest.TestCase):
    """The generic MCP channel: catalog projection, bounded calls, scrubbing."""

    def transport(self, tools=None, call_result=None, status=200):
        catalog = tools if tools is not None else [
            {'name': 'aba_research_trend', 'description': 'trend', 'inputSchema': {'type': 'object', 'properties': {'marketplace': {'type': 'string'}, 'keyword': {'type': 'string'}}, 'required': ['marketplace', 'keyword']}},
            {'name': 'market_price_distribution', 'description': 'price', 'inputSchema': {'type': 'object', 'properties': {'request': {'type': 'object'}}}},
        ]
        def handle(request):
            body = json.loads(request.content)
            method = body['method']
            if method == 'initialize':
                payload = {'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {}}
                return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': payload}, headers={'Mcp-Session-Id': 's1'})
            if method == 'notifications/initialized':
                return httpx.Response(202)
            if method == 'tools/list':
                return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': {'tools': catalog}})
            if method == 'tools/call':
                payload = call_result if call_result is not None else {'content': [{'type': 'text', 'text': json.dumps({'code': 'OK', 'data': {'list': [1, 2]}})}]}
                return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': payload})
            raise AssertionError('unapproved ' + method)
        return httpx.MockTransport(handle)

    def test_list_tools_projects_catalog_with_prefix_no_collision(self):
        from steps import sellersprite_mcp, agent
        value = sellersprite_mcp.list_tools(url=URL, transport=self.transport(), force=True)
        self.assertTrue(value['ok'])
        self.assertEqual(len(value['tools']), 2)
        specs, labels, listed = agent.mcp_tool_specs(url=URL, transport=self.transport(), force=True)
        names = [s['name'] for s in specs]
        self.assertEqual(names, ['sellersprite_aba_research_trend', 'sellersprite_market_price_distribution'])
        static = {t['name'] for t in agent.TOOLS}
        self.assertFalse(static & set(names))
        self.assertIn('required', specs[0]['parameters'])

    def test_call_returns_data_and_scrubs_secret(self):
        from steps import sellersprite_mcp
        result = sellersprite_mcp.call('aba_research_trend', {'marketplace': 'US', 'keyword': 'x'}, url=URL, transport=self.transport())
        self.assertTrue(result['ok'])
        self.assertEqual(result['remote_tool'], 'aba_research_trend')
        self.assertEqual(result['data']['code'], 'OK')
        self.assertNotIn('fixture-secret', json.dumps(result))

    def test_call_rejects_unknown_tool_and_bad_arguments(self):
        from steps import sellersprite_mcp
        result = sellersprite_mcp.call('not_in_catalog', {}, url=URL, transport=self.transport())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'tool_unavailable')
        deep = {'a': {}}
        for _ in range(12):
            deep = {'a': deep}
        result = sellersprite_mcp.call('aba_research_trend', deep, url=URL, transport=self.transport())
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'invalid_arguments')

    def test_call_requires_configuration_and_valid_url(self):
        from steps import sellersprite_mcp
        self.assertEqual(sellersprite_mcp.call('review', {}, url='')['error_code'], 'not_configured')
        self.assertEqual(sellersprite_mcp.call('review', {}, url='https://evil.invalid/mcp?secret-key=x')['error_code'], 'invalid_configuration')

    def test_iserror_result_is_not_reported_as_zero(self):
        from steps import sellersprite_mcp
        failed = {'isError': True, 'content': [{'type': 'text', 'text': 'quota'}]}
        result = sellersprite_mcp.call('aba_research_trend', {'marketplace': 'US', 'keyword': 'x'}, url=URL, transport=self.transport(call_result=failed))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'tool_failed')
        self.assertNotIn('data', result)

    def test_catalog_quota_sentinel_stops_listing(self):
        from steps import sellersprite_mcp
        sentinel = [{'name': 'secret_no_remaining', 'inputSchema': {'type': 'object'}}]
        value = sellersprite_mcp.list_tools(url=URL, transport=self.transport(tools=sentinel), force=True)
        self.assertFalse(value['ok'])
        self.assertEqual(value['error_code'], 'quota_exhausted')


class McpToolInjectionTest(unittest.TestCase):
    """Ordinary research (no /reviews skill) can reach MCP tools via the loop."""

    def test_plain_question_can_invoke_generic_mcp_tool(self):
        from steps import sellersprite_mcp
        captured = []
        class Item:
            def __init__(self, value): self.value = value
            def model_dump(self, **kwargs): return self.value
        class Stream:
            def __init__(self, events): self.events = events
            def __iter__(self): return iter(self.events)
            def close(self): pass
        class Client:
            def __init__(self, **kwargs): self.responses = self
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def create(self, **kwargs):
                captured.append({**kwargs, 'input': list(kwargs['input'])})
                if len(captured) == 1:
                    output = [Item({'type': 'function_call', 'call_id': 'fc-1', 'name': 'sellersprite_aba_research_trend', 'arguments': '{"marketplace":"US","keyword":"owala"}'})]
                    events = []
                else:
                    output = [Item({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': '趋势上升 [D1]'}]})]
                    events = [SimpleNamespace(type='response.output_text.delta', delta='趋势上升 [D1]')]
                events.append(SimpleNamespace(type='response.completed', response=SimpleNamespace(status='completed', output=output, usage=None)))
                return Stream(events)
        specs = [{'type': 'function', 'name': 'sellersprite_aba_research_trend', 'description': 'x', 'parameters': {'type': 'object', 'properties': {}}}]
        run = agent.Run('f' * 32)
        request = {'context': {}, 'history': [], 'message': '帮我看 owala 的趋势', 'attachment_ids': [], 'skill': None}
        agent._SLOTS.acquire()
        fake = {'ok': True, 'remote_tool': 'aba_research_trend', 'data': {'code': 'OK', 'data': [{'label': '202601', 'searches': 10}]}, 'fetched_at': 'now'}
        with patch('openai.OpenAI', Client), \
             patch.object(agent, 'DataTools') as data_cls, \
             patch.object(agent, 'mcp_tool_specs', return_value=(specs, {'sellersprite_aba_research_trend': '卖家精灵'}, {'ok': True})), \
             patch.object(sellersprite_mcp, 'call', return_value=fake) as remote:
            data_cls.return_value.execute.return_value = {'period': 'all_collected_weeks', 'week_count': 0,
                                                          'available_weeks': [], 'scope': {}, 'weekly_statistics': []}
            data_cls.return_value.market_loader = None
            data_cls.return_value.week = ''
            data_cls.return_value.market_loader = None
            agent._work(run, request, {'api_key': 'k', 'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash', 'sellersprite_mcp_url': URL})
        events = []
        while (event := run.events.get_nowait()) is not None:
            events.append(event)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertIn('[D1]', events[-1]['text'])
        remote.assert_called_once_with('aba_research_trend', {'marketplace': 'US', 'keyword': 'owala'}, url=URL)
        source = next(e['source'] for e in events if e['type'] == 'source' and 'live_sellersprite_query' == e['source']['scope'].get('period'))
        self.assertEqual(source['scope']['remote_tool'], 'aba_research_trend')
        self.assertNotIn('fixture-secret', json.dumps(events))
        self.assertIn('sellersprite_aba_research_trend', captured[0]['tools'][-1]['name'])


if __name__ == '__main__':
    unittest.main()
