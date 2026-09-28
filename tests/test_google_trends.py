import pathlib
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from aba_client import AbaApiError
from steps import google_trends


class GoogleTrendsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.raw = {'timeLineData': [
            {'time': 1632009600000, 'value': 0, 'hasData': True, 'fomattedValue': '<1'},
            {'time': 1632614400000, 'value': 0, 'hasData': False},
            {'time': 1633219200000, 'value': 100, 'hasData': True},
        ]}

    def test_zero_and_missing_are_distinct(self):
        points = google_trends.normalize(self.raw)
        self.assertEqual([p['value'] for p in points], [0, None, 100])
        self.assertEqual(points[0]['date'], '2021-09-19')
        self.assertEqual(points[0]['label'], '<1')

    def test_keyword_cache_shared_across_calls_but_separate_by_market(self):
        with patch.object(google_trends, 'fetch', return_value=self.raw) as fetch:
            first = google_trends.payload(self.conn, keyword='a & b', market='COM')
            cached = google_trends.payload(self.conn, keyword='a & b', market='COM')
            google_trends.payload(self.conn, keyword='a & b', market='UK')
        self.assertTrue(first['ok'])
        self.assertEqual(cached['source'], 'cache')
        self.assertEqual(fetch.call_count, 2)

    def test_expired_cache_survives_session_failure(self):
        with patch.object(google_trends, 'fetch', return_value=self.raw):
            google_trends.payload(self.conn, keyword='owala', market='COM')
        self.conn.execute('UPDATE google_trend SET fetched_at=0')
        with patch.object(google_trends, 'fetch', side_effect=AbaApiError('ERR_USER_NOT_LOGIN')):
            data = google_trends.payload(self.conn, keyword='owala', market='COM')
        self.assertTrue(data['ok'])
        self.assertTrue(data['stale'])
        self.assertEqual(data['errorCode'], 'ERR_USER_NOT_LOGIN')
        self.assertEqual(len(data['trend']), 3)

    def test_failure_without_cache_does_not_claim_empty_success(self):
        with patch.object(google_trends, 'fetch', side_effect=TimeoutError()):
            data = google_trends.payload(self.conn, keyword='owala', market='COM')
        self.assertFalse(data['ok'])
        self.assertEqual(data['trend'], [])

    def test_unexpected_response_is_not_cached(self):
        with patch.object(google_trends, 'fetch', return_value={'unexpected': []}):
            data = google_trends.payload(self.conn, keyword='owala', market='COM')
        self.assertFalse(data['ok'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM google_trend').fetchone()[0], 0)

    def test_request_uses_user_endpoint_parameters(self):
        with patch.object(google_trends, 'make_client') as factory:
            client = factory.return_value
            client._get.return_value = {'data': self.raw}
            google_trends.fetch('a & b', 'COM')
        client._get.assert_called_once_with('/v2/keyword/google-trends.json', {
            'station': 'COM', 'keyword': 'a & b', 'gprop': '', 'intervalYear': 5,
            'gv': 'false', 'monthly': 'false',
        })
        client.session.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
