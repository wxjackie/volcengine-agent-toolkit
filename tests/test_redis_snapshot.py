"""Read-only pagination, projection, and drift behavior using synthetic cloud responses."""
import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'plugins/volcengine-database/skills/volcengine-redis/scripts/redis_snapshot.py'
spec = importlib.util.spec_from_file_location('redis_snapshot', PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def page(items, total=None, error=None):
    return {'ResponseMetadata': {'RequestId': 'synthetic-request', 'Region': 'cn-test', 'Error': error},
            'Result': {'Instances': items, 'TotalInstancesNum': len(items) if total is None else total}}


def item(i='redis-test', **kw):
    return {'InstanceId': i, 'EngineVersion': '6.0', 'Status': 'Running',
            'Capacity': {'Total': 1024, 'Used': 68}, **kw}


def snap(rows):
    return mod.collect('cn-test', [], 'test-project', lambda _: page(rows))


class RedisSnapshotTests(unittest.TestCase):
    def test_pages_do_not_stop_at_default_page(self):
        calls = []
        def fetch(params):
            calls.append(params)
            return page([item('redis-a')] if params['PageNumber'] == 1 else [item('redis-b')], 2)
        result = mod.collect('cn-test', [], 'test-project', fetch)
        self.assertEqual(len(result['instances']), 2)
        self.assertEqual([c['PageNumber'] for c in calls], [1, 2])
        self.assertTrue(all(c['ProjectName'] == 'test-project' for c in calls))

    def test_empty_and_forbidden_are_distinct(self):
        empty = mod.collect('cn-test', ['redis-test'], None, lambda _: page([]))
        self.assertEqual(empty['not_found_in_scope'], ['redis-test'])
        with self.assertRaises(mod.EvidenceError):
            mod.collect('cn-test', ['redis-test'], None, lambda _: page([], error={'Code': 'AccessDenied'}))

    def test_repeated_page_and_total_drift_fail(self):
        for responses in ([page([item()], 2), page([item()], 2)],
                          [page([item()], 2), page([item('redis-b')], 3)]):
            it = iter(responses)
            with self.assertRaises(mod.EvidenceError):
                mod.collect('cn-test', [], 'test-project', lambda _: next(it))

    def test_page_limit_and_empty_premature_page_fail(self):
        with self.assertRaises(mod.EvidenceError):
            mod.collect('cn-test', [], 'test-project', lambda _: page([item()], 2), max_pages=1)
        with self.assertRaises(mod.EvidenceError):
            mod.collect('cn-test', [], 'test-project', lambda _: page([], 1))

    def test_exact_id_and_region_must_match(self):
        for response in (page([item('redis-wrong')]), page([item(RegionId='cn-other')])):
            with self.assertRaises(mod.EvidenceError):
                mod.collect('cn-test', ['redis-test'], None, lambda _: response)

    def test_missing_schema_does_not_become_empty_success(self):
        for response in ({}, {'Result': {}}, page([{}]), page([], total='0')):
            with self.assertRaises(mod.EvidenceError):
                mod.collect('cn-test', [], 'test-project', lambda _: response)

    def test_projection_omits_unrelated_secrets(self):
        value = snap([item(Password='synthetic-secret', AccessKey='synthetic-key')])
        self.assertNotIn('synthetic-secret', json.dumps(value))
        self.assertNotIn('synthetic-key', json.dumps(value))

    def test_usage_is_not_resize_and_state_not_config(self):
        before = snap([item()])
        after = snap([item(Status='Updating', Capacity={'Total': 1024, 'Used': 69})])
        changes = mod.compare(before, after)['changes']
        self.assertEqual({c['kind'] for c in changes}, {'usage', 'state'})
        after['instances'][0]['ShardNumber'] = 4
        before['instances'][0]['ShardNumber'] = 2
        self.assertIn('configuration', {c['kind'] for c in mod.compare(before, after)['changes']})

    def test_missing_field_and_missing_instance_are_not_deletion(self):
        before = snap([item(), item('redis-second')]);after = snap([item()])
        del after['instances'][0]['EngineVersion']
        result = mod.compare(before, after)
        self.assertEqual(result['changes'], [])
        self.assertEqual(result['no_longer_observed'], ['redis-second'])
        self.assertEqual(result['unknown_fields'], [{'id': 'redis-test', 'field': 'EngineVersion'}])

    def test_scope_mismatch_and_incomplete_rejected(self):
        b = snap([item()]);a = snap([item()]);a['scope']['region'] = 'cn-other'
        with self.assertRaises(mod.EvidenceError):mod.compare(b, a)
        a = snap([item()]);a['complete'] = False
        with self.assertRaises(mod.EvidenceError):mod.compare(b, a)

    def test_metadata_policy_and_no_shell(self):
        env = {'VE_CALLER_TYPE': 'ai_agent', 'VE_CALLER_NAME': 'test-host',
               'VE_SKILL_NAME': 'volcengine-redis', 'VOLCENGINE_SAFETY_POLICY_ENABLED': 'true'}
        response = subprocess.CompletedProcess([], 0, json.dumps(page([])), '')
        with patch.dict(mod.os.environ, env, clear=True), patch.object(mod.subprocess, 'run', return_value=response) as run:
            mod.ve_fetch('cn-test', 5)({'InstanceId': 'redis-test', 'PageNumber': 1})
            argv = run.call_args.args[0]
            self.assertEqual(json.loads(argv[argv.index('--body')+1])['PageNumber'], 1)
            self.assertEqual(run.call_args.args[0][:3], ['ve', 'redis', 'DescribeDBInstances'])
            self.assertEqual(run.call_args.kwargs['env'], env)
            self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
            self.assertNotIn('--yes', run.call_args.args[0])
            self.assertNotIn('shell', run.call_args.kwargs)
        with patch.dict(mod.os.environ, {}, clear=True):
            with self.assertRaises(mod.EvidenceError):mod.ve_fetch('cn-test', 5)

    def test_cli_failures_not_parsed_or_retried(self):
        env = {'VE_CALLER_TYPE': 'ai_agent', 'VE_CALLER_NAME': 'test-host', 'VE_SKILL_NAME': 'volcengine-redis'}
        for response in (subprocess.CompletedProcess([], 1, '', 'secret'), subprocess.CompletedProcess([], 0, 'not-json', '')):
            with patch.dict(mod.os.environ, env, clear=True), patch.object(mod.subprocess, 'run', return_value=response) as run:
                with self.assertRaises(mod.EvidenceError):mod.ve_fetch('cn-test', 5)({})
                self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
