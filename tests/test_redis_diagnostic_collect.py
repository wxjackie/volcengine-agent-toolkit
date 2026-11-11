import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'plugins/volcengine-database/skills/volcengine-redis/scripts'
sys.path.insert(0, str(SCRIPTS))
import diagnostic_collect as m
from diagnostic_plan import build_plan


def response(items=None, **extra):
    return {'ResponseMetadata': {'RequestId': 'test-request'},
            'Result': {'Rows': [] if items is None else items, **extra}}


class CollectTest(unittest.TestCase):
    def plan(self, sources=('slow',), metric=None):
        return build_plan('cn-test', 'redis-test', '2025-01-01T00:00:00Z',
                          '2025-01-01T01:00:00Z', sources, metric)

    def contracts(self, plan):
        return {r['action']: dict(source='synthetic fixture contract', items='Result.Rows',
                                  cursor='Result.Context' if r['action']=='DescribeSlowLogs' else 'Result.NextToken',
                                  done='Result.ListOver', total='Result.Total') for r in plan['requests']}

    def run_pages(self, pages, sources=('slow',), max_pages=10, metric=None):
        plan = self.plan(sources, metric)
        calls = []
        def call(*args):
            calls.append(copy.deepcopy(args))
            return pages[len(calls)-1]
        result = m.collect(plan, self.contracts(plan), call, max_pages)
        return result['sources'][0], calls

    def test_write_and_tampered_scope_rejected_before_call(self):
        for mutate in (lambda p: p['requests'][0].update(action='CreateBackup'),
                       lambda p: p['requests'][0]['body'].update(InstanceId='redis-other'),
                       lambda p: p['requests'][0].update(service='other'),
                       lambda p: p['requests'][0]['body'].update(PageSize='100')):
            plan = self.plan(); contracts = self.contracts(plan); mutate(plan)
            with patch.object(m, 've_call') as call, self.assertRaises(ValueError):
                m.collect(plan, contracts, call)
            call.assert_not_called()

    def test_argv_never_executed(self):
        plan = self.plan(); plan['requests'][0]['argv'] = ['ve', 'redis', 'CreateBackup']
        with patch.object(m, 've_call', return_value=response(ListOver=True)) as call:
            m.collect(plan, self.contracts(plan), call)
            self.assertEqual(call.call_args.args[1], 'DescribeSlowLogs')

    def test_cursor_and_scope_preserved(self):
        result, calls = self.run_pages([response([1], ListOver=False, Context='c'), response([2], ListOver=True)])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(calls[1][3]['Context'], 'c')
        self.assertEqual(calls[1][3]['InstanceId'], 'redis-test')
        self.assertEqual(calls[0][3]['QueryStartTime'], calls[1][3]['QueryStartTime'])
        self.assertEqual(len(result['pages']), 2)
        self.assertIn('+00:00', result['pages'][0]['observed_at'])

    def test_errors_never_become_empty_success_and_no_retry(self):
        for bad in ({}, [], {'ResponseMetadata': {'Error': {'Code': 'Denied'}}},
                    {'ResponseMetadata': {}, 'Result': {}}, response(Rows=None), response(ListOver='true')):
            result, calls = self.run_pages([bad])
            self.assertIn(result['status'], ('error', 'partial'))
            self.assertEqual(len(calls), 1)

    def test_empty_valid_list(self):
        result, _ = self.run_pages([response(ListOver=True)])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['pages'][0]['items'], [])

    def test_cursor_failures_and_budget(self):
        for pages in ([response([1], ListOver=False)],
                      [response([1], ListOver=False, Context='c'), response([2], ListOver=False, Context='c')],
                      [response([1], ListOver=False, Context='c'), response([1], ListOver=False, Context='d')]):
            result, _ = self.run_pages(pages)
            self.assertEqual(result['status'], 'partial')
        result, calls = self.run_pages([response([1], ListOver=False, Context='c')], max_pages=1)
        self.assertEqual(result['reason'], 'page_budget_exhausted')
        self.assertEqual(len(calls), 1)

    def test_total_pagination_and_drift(self):
        result, calls = self.run_pages([response([1], Total=2), response([2], Total=2)], ('tasks',))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(calls[1][3]['PageNumber'], 2)
        for last in (response([2], Total=3), response([], Total=2)):
            result, _ = self.run_pages([response([1], Total=2), last], ('tasks',))
            self.assertEqual(result['status'], 'partial')

    def test_audit_missing_end_is_partial(self):
        result, calls = self.run_pages([response([1], NextToken='c'), response([2], NextToken='')], ('audit',))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(calls[1][3]['NextToken'], 'c')
        result, _ = self.run_pages([response()], ('audit',))
        self.assertEqual(result['status'], 'partial')

    def test_samples_and_metrics_never_claim_health(self):
        for source in ('big', 'hot'):
            result, _ = self.run_pages([response()], (source,))
            self.assertEqual(result['status'], 'sample')
        metric = dict(Namespace='VCM_Redis', SubNamespace='server', MetricName='MemUtil', Period='1m', StatisticsMethods=['avg'])
        result, _ = self.run_pages([response()], ('metrics',), metric=metric)
        self.assertEqual(result['status'], 'unverified_coverage')

    def test_typed_body_no_confirmation_bypass(self):
        with patch.object(m.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = json.dumps(response(ListOver=True))
            m.ve_call('redis', 'DescribeSlowLogs', 'cn-test', {'PageSize': 100})
            argv = run.call_args.args[0]
            self.assertEqual(json.loads(argv[argv.index('--body')+1])['PageSize'], 100)
            self.assertNotIn('--yes', argv)
            self.assertNotIn('env', run.call_args.kwargs)

    def test_failed_process_does_not_publish_stderr(self):
        with patch.object(m.subprocess, 'run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = 'sensitive-example'
            with self.assertRaisesRegex(ValueError, '^CLI failed; raw stderr omitted$'):
                m.ve_call('redis', 'DescribeSlowLogs', 'cn-test', {})

    def test_private_output_and_refuse_requery(self):
        import tempfile
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); plan = self.plan()
            (root/'plan.json').write_text(json.dumps(plan))
            (root/'contracts.json').write_text(json.dumps(self.contracts(plan)))
            out = root/'evidence.json'
            argv = ['collect', '--plan', str(root/'plan.json'), '--contracts', str(root/'contracts.json'), '--output', str(out)]
            env = {'VE_CALLER_TYPE': 'ai_agent', 'VE_CALLER_NAME': 'test-host', 'VE_SKILL_NAME': 'volcengine-redis'}
            with patch.object(sys, 'argv', argv), patch.dict(m.os.environ, env, clear=True), patch.object(m, 've_call', return_value=response(ListOver=True)) as call, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m.main(), 0)
                self.assertEqual(out.stat().st_mode & 0o777, 0o600)
                self.assertEqual(m.main(), 1)
                self.assertEqual(call.call_count, 1)
                out.unlink(); out.symlink_to(root/'plan.json')
                self.assertEqual(m.main(), 1)
                self.assertEqual(call.call_count, 1)

    def test_all_requests_validated_before_first_call(self):
        plan = self.plan(('slow', 'tasks')); contracts = self.contracts(plan)
        plan['requests'][1]['action'] = 'CreateBackup'
        with patch.object(m, 've_call') as call, self.assertRaises(ValueError):
            m.collect(plan, contracts, call)
        call.assert_not_called()
