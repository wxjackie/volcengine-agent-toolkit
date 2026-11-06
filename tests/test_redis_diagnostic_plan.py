import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'plugins/volcengine-database/skills/volcengine-redis/scripts/diagnostic_plan.py'
spec = importlib.util.spec_from_file_location('diagnostic_plan', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DiagnosticPlanTest(unittest.TestCase):
    def plan(self, sources, **kwargs):
        return module.build_plan('cn-test', 'redis-test', kwargs.pop('start', '2025-01-01T08:00:00+08:00'),
                                 kwargs.pop('end', '2025-01-01T09:00:00+08:00'), sources, **kwargs)

    def test_time_contracts_and_read_only_actions(self):
        plan = self.plan(['slow', 'big', 'hot', 'tasks', 'audit'])
        req = plan['requests']
        self.assertEqual(req[0]['body']['QueryStartTime'], '2025-01-01T00:00:00Z')
        self.assertEqual(req[4]['body']['StartTime'], 1735689600)
        self.assertEqual(plan['status'], 'plan_only_not_executed')
        self.assertTrue(all(r['action'].startswith(('Describe', 'Lookup')) for r in req))
        self.assertNotIn('PageNumber', req[1]['body'])
        self.assertNotIn('PageNumber', req[2]['body'])
        self.assertEqual(req[3]['body']['PageNumber'], 1)

    def test_invalid_windows(self):
        for start, end in [('2025-01-01T08:00:00', '2025-01-01T09:00:00Z'),
                           ('2025-01-02T00:00:00Z', '2025-01-01T00:00:00Z'),
                           ('2025-01-01T00:00:00Z', '2025-01-03T00:00:00Z'),
                           ('2099-01-01T00:00:00Z', '2099-01-01T01:00:00Z')]:
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.plan(['slow'], start=start, end=end)

    def test_metric_requires_verified_spec(self):
        with self.assertRaises(ValueError):
            self.plan(['metrics'])

    def test_metric_binding_and_no_scope_override(self):
        metric = dict(Namespace='VCM_Redis', SubNamespace='server', MetricName='MemUtil',
                      Period='1m', StatisticsMethods=['avg'])
        body = self.plan(['metrics'], metric=metric)['requests'][0]['body']
        self.assertEqual(body['Instances'][0]['Dimensions'], [{'Name': 'ResourceID', 'Value': 'redis-test'}])
        metric['Dimensions'] = [{'Name': 'ResourceID', 'Value': 'other'}]
        with self.assertRaises(ValueError):
            self.plan(['metrics'], metric=metric)

    def test_invalid_dimension_or_stat(self):
        metric = dict(Namespace='VCM_Redis', SubNamespace='server', MetricName='MemUtil',
                      Period='1m', StatisticsMethods=['p99'])
        with self.assertRaises(ValueError):
            self.plan(['metrics'], metric=metric)
        metric['StatisticsMethods'] = ['avg']
        metric['Dimensions'] = [{'Name': 'NodeID', 'Value': 'a'}, {'Name': 'NodeID', 'Value': 'b'}]
        with self.assertRaises(ValueError):
            self.plan(['metrics'], metric=metric)

    def test_selective_sources(self):
        self.assertEqual(len(self.plan(['slow', 'slow'])['requests']), 1)
        for sources in ([], ['create']):
            with self.assertRaises(ValueError):
                self.plan(sources)
