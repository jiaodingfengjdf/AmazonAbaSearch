import json
import sqlite3
import unittest
from unittest.mock import Mock

import test_agent
from contextlib import closing
from steps import agent


class MarketCoverageTest(unittest.TestCase):
    setUp = test_agent.AgentDataTest.setUp
    tearDown = test_agent.AgentDataTest.tearDown

    def write(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(sql, params)

    def test_all_keyword_fields_and_week_category_snapshots(self):
        self.write('UPDATE keyword SET clicks=123, impressions=999, phrase_ppc=1.25, broad_ppc=0.75, ad_products_7=12 WHERE keyword="serum"')
        data = self.data.keyword_analysis(keywords=['serum'])['keywords'][0]
        self.assertEqual(data['snapshots'][0]['clicks'], 123)
        self.assertEqual(data['snapshots'][0]['phrase_ppc'], 1.25)
        snapshots = self.data.execute('keyword_snapshots', {'keywords': ['serum'], 'limit': 1})
        self.assertEqual(snapshots['matched_count'], 2)
        self.assertIsNotNone(snapshots['next_offset'])
        self.assertIn('departments', snapshots['rows'][0])

    def test_keyword_pagination_reaches_records_beyond_first_thirty(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executemany('INSERT INTO keyword(keyword,market,table_date,searches) VALUES (?,"COM","ara_20260919",6000)', [(f'other-{i:02}',) for i in range(35)])
        first = self.data.search_keywords(limit=30)
        second = self.data.search_keywords(limit=30, offset=first['next_offset'])
        keys = [r['keyword'] for r in first['rows'] + second['rows']]
        self.assertEqual(len(set(keys)), 39)
        self.assertEqual(second['next_offset'], None)

    def test_category_series_counts_overlapping_categories_without_inflating_each(self):
        self.write('INSERT INTO keyword_department VALUES ("brush","COM","ara_20260919","beauty",1) ON CONFLICT DO NOTHING')
        data = self.data.execute('category_analysis', {})
        current = [r for r in data['rows'] if r['week'] == 'ara_20260919']
        beauty = next(r for r in current if r['department'] == 'beauty')
        self.assertEqual(beauty['keywords'], 2)
        self.assertEqual(beauty['monthly_searches_sum'], 20000)
        self.assertTrue(any(r['department'] == '__none__' for r in current))
        self.assertTrue(any(r['department'] == 'pets' for r in current))

    def test_complete_product_fields_and_monthly_history(self):
        self.write('UPDATE asin_keyword SET data=?', (json.dumps({'title': 'Serum', 'sellerName': 'Store', 'profit': 45, 'lqs': 95, 'imageUrl': 'https://example.com/image.png', 'api_key': 'never export'}),))
        points = [{'date': f'{2020+i//12}-{i%12+1:02}', 'price': i, 'unit': 100, 'rating': 4.5} for i in range(40)]
        self.write('INSERT INTO asin_trend VALUES ("B012345678","COM",?,"2026-09-28")', (json.dumps({'trend': points, 'partial': True, 'reason': 'local estimate'}),))
        data = self.data.asin_analysis(asins=['B012345678'])['asins'][0]
        self.assertEqual(len(data['monthly_trend']), 40)
        self.assertEqual(data['cached_detail']['data']['sellerName'], 'Store')
        self.assertEqual(data['cached_detail']['data']['profit'], 45)
        self.assertNotIn('api_key', data['cached_detail']['data'])
        self.assertTrue(data['trend_metadata']['partial'])
        history = self.data.execute('product_history', {'asins': ['B012345678']})
        self.assertEqual(history['rows'][0]['fetched_at'], '2026-09-23')

    def test_asin_search_includes_snapshot_only_products_and_has_pagination(self):
        self.write('UPDATE keyword SET gk_asins=? WHERE keyword="brush" AND table_date="ara_20260919"', (json.dumps([{'asin': 'B087654321', 'title': 'Brush product', 'price': 8}]),))
        result = self.data.execute('search_asins', {'query': 'Brush', 'limit': 1})
        self.assertEqual(result['rows'][0]['asin'], 'B087654321')
        self.assertFalse(result['rows'][0]['has_cached_detail'])

    def test_live_details_and_trends_use_dashboard_loader_with_validated_args(self):
        loader = Mock(return_value={'ok': True, 'source': 'cache', 'asins': [], 'trend': [{'date': '2026-08', 'price': 12}], 'partial': True})
        self.data.market_loader = loader
        detail = self.data.execute('keyword_asins', {'keywords': ['serum']})
        self.assertEqual(detail['keywords'][0]['source'], 'cache')
        self.data.execute('asin_trends', {'asins': ['B012345678']})
        self.assertEqual([c.args[0] for c in loader.call_args_list], ['keyword_asins', 'asin_trend'])
        loader.reset_mock()
        with self.assertRaises(ValueError):
            self.data.execute('asin_trends', {'asins': ['B012345678', '../../file']})
        loader.assert_not_called()

    def test_catalog_and_knowledge_expose_actual_accessible_sources(self):
        catalog = self.data.execute('data_catalog', {})
        datasets = {d['id']: d for d in catalog['datasets']}
        self.assertEqual(datasets['keyword']['records'], 6)
        self.assertIn('clicks', datasets['keyword']['fields'])
        self.assertIn('sellerName', datasets['asin_keyword']['fields'])
        knowledge = self.data.execute('market_knowledge', {'query': 'Rufus'})
        self.assertGreater(knowledge['matched_count'], 0)
        self.assertTrue(all('source' in row for row in knowledge['rows']))
        self.assertTrue({'data_catalog','category_analysis','keyword_snapshots','keyword_history','search_asins','product_history','keyword_asins','asin_trends','market_knowledge'} <= {t['name'] for t in agent.TOOLS})

    def test_dashboard_samples_and_collection_records_are_explicitly_paginated(self):
        week = self.root / 'ara_20260919'
        week.mkdir()
        (week / 'summary.json').write_text(json.dumps({'meta': {'generatedAt': '2026-09-28'},
            'kpis': {'keywords': 4, 'medianGrowth': .2},
            'deptScatter': {'__all__': [['serum',12000,.2,50,0,.3], ['brush',8000,.8,20,0,None]]}}), encoding='utf-8')
        result = self.data.execute('dashboard_view', {'section': 'kpis'})
        self.assertEqual(result['data']['medianGrowth'], .2)
        result = self.data.execute('dashboard_view', {'section': 'deptScatter', 'limit': 1})
        self.assertEqual(result['matched_count'], 2)
        self.assertEqual(result['next_offset'], 1)
        self.write('INSERT INTO fetch_log VALUES ("beauty","COM","ara_20260919",1,100,100,"2026-09-28")')
        records = self.data.execute('collection_history', {})
        self.assertEqual(records['rows'][0]['item_count'], 100)

    def test_keyword_history_pagination_preserves_supplied_period_and_zero(self):
        self.write('INSERT INTO keyword_trend VALUES ("serum","COM","ara_20260919",0,"2026-01",0,10,NULL,NULL)')
        self.write('INSERT INTO keyword_trend VALUES ("serum","COM","ara_20260919",1,"2026-02",500,5,NULL,NULL)')
        result = self.data.execute('keyword_history', {'keyword': 'serum', 'limit': 1})
        self.assertEqual(result['rows'][0]['searches'], 0)
        self.assertEqual(result['next_offset'], 1)
        self.assertEqual(result['rows'][0]['source_week'], 'ara_20260919')

    def test_google_trends_in_running_agent_uses_same_dashboard_provider(self):
        loader = Mock(return_value={'ok': True, 'keyword': 'serum', 'station': 'COM', 'source': 'cache', 'fetchedAt': 1, 'trend': []})
        self.data.market_loader = loader
        result = self.data.google_trends(keywords=['serum'])
        loader.assert_called_once_with('google_trends', {'keyword': 'serum'})
        self.assertTrue(result['keywords'][0]['ok'])


if __name__ == '__main__':
    unittest.main()
