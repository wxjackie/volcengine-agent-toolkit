#!/usr/bin/env python3
"""Execute a verified diagnostic_plan using bounded read-only ve requests."""
import argparse
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess

from diagnostic_plan import build_plan

SOURCES = {'DescribeSlowLogs': 'slow', 'DescribeBigKeys': 'big', 'DescribeHotKeys': 'hot',
           'DescribeTasks': 'tasks', 'LookupEvents': 'audit', 'GetMetricData': 'metrics'}
MODES = {'slow': 'cursor', 'big': 'sample', 'hot': 'sample', 'tasks': 'total',
         'audit': 'token', 'metrics': 'series'}


def field(obj, path):
    for key in path.split('.'):
        if not isinstance(obj, dict) or key not in obj:
            raise ValueError('missing contracted response field')
        obj = obj[key]
    return obj


def validate(plan, contracts):
    """Rebuild requests from scope; never execute plan argv or arbitrary actions."""
    scope, window = plan['scope'], plan['window']
    if not isinstance(plan['requests'], list) or not plan['requests']:
        raise ValueError('empty plan')
    seen = set()
    for req in plan['requests']:
        source = SOURCES.get(req['action'])
        if source is None or source in seen:
            raise ValueError('unsupported or duplicate action')
        seen.add(source)
        metric = None
        if source == 'metrics':
            b = req['body']
            dims = b['Instances'][0]['Dimensions']
            metric = {k: b[k] for k in ('Namespace', 'SubNamespace', 'MetricName', 'Period', 'StatisticsMethods')}
            metric['Dimensions'] = [d for d in dims if d['Name'] != 'ResourceID']
        expected = build_plan(scope['region'], scope['instance_id'], window['start'], window['end'],
                              [source], metric)['requests'][0]
        if any(req[k] != expected[k] for k in ('service', 'action', 'body')):
            raise ValueError('request differs from bound diagnostic plan')
        contract = contracts[req['action']]
        if not isinstance(contract.get('source'), str) or not contract['source'].strip():
            raise ValueError('record verified response contract source')
        for key in ['items'] + ({'cursor': ['cursor', 'done'], 'token': ['cursor'], 'total': ['total']}.get(MODES[source], [])):
            if not isinstance(contract.get(key), str) or not contract[key].startswith('Result.'):
                raise ValueError('response paths must be explicit Result fields')


def collect(plan, contracts, call, max_pages=10):
    validate(plan, contracts)  # validate all requests before any cloud call
    if type(max_pages) is not int or not 1 <= max_pages <= 10:
        raise ValueError('page budget must be 1..10')
    results = []
    for req in plan['requests']:
        mode = MODES[SOURCES[req['action']]]
        c, body = contracts[req['action']], copy.deepcopy(req['body'])
        record = {'action': req['action'], 'status': 'partial', 'pages': [],
                  'contract_source': c['source'], 'reason': 'page_budget_exhausted'}
        cursors, fingerprints, expected_total, count = set(), set(), None, 0
        for page in range(max_pages):
            try:
                response = call(req['service'], req['action'], plan['scope']['region'], body)
                if not isinstance(response, dict):
                    raise ValueError('response must be object')
                meta = response.get('ResponseMetadata')
                if not isinstance(meta, dict) or meta.get('Error'):
                    raise ValueError('API error or missing metadata')
                items = field(response, c['items'])
                if not isinstance(items, list):
                    raise ValueError('contracted items must be an array')
                fingerprint = json.dumps(items, sort_keys=True)
                if items and fingerprint in fingerprints:
                    record['reason'] = 'repeated_page'
                    break
                fingerprints.add(fingerprint)
                record['pages'].append({'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                                        'request_id': meta.get('RequestId'), 'items': items})
                count += len(items)
                if mode in ('sample', 'series'):
                    record.update(status='sample' if mode == 'sample' else 'unverified_coverage',
                                  reason='bounded_sample' if mode == 'sample' else 'verify_dimensions_and_time_coverage')
                    break
                if mode == 'total':
                    total = field(response, c['total'])
                    if type(total) is not int or total < count or (expected_total is not None and total != expected_total):
                        record['reason'] = 'invalid_or_changed_total'
                        break
                    expected_total = total
                    done = count == total
                    if not done and not items:
                        record['reason'] = 'empty_page_before_total'
                        break
                    body['PageNumber'] += 1
                else:
                    if mode == 'cursor':
                        done = field(response, c['done'])
                        if type(done) is not bool:
                            raise ValueError('ListOver must be boolean')
                        token = '' if done else field(response, c['cursor'])
                    else:
                        token = field(response, c['cursor'])
                        if not isinstance(token, str):
                            raise ValueError('NextToken must be string; absent field is not end proof')
                        done = token == ''
                    if not done:
                        if not isinstance(token, str) or not token or token in cursors:
                            record['reason'] = 'missing_or_repeated_cursor'
                            break
                        cursors.add(token)
                        body['Context' if mode == 'cursor' else 'NextToken'] = token
                if done:
                    record.update(status='complete', reason='contracted_end_reached')
                    break
            except (ValueError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
                record.update(status='partial' if record['pages'] else 'error', reason='request_or_contract_error')
                break  # no automatic retry, guessed metrics, or fallback identity
        results.append(record)
    return {'scope': plan['scope'], 'window': plan['window'], 'sources': results,
            'limitation': 'Empty samples do not prove health; series require dimension/time validation.'}


def ve_call(service, action, region, body):
    result = subprocess.run(['ve', service, action, '--region', region, '--body', json.dumps(body),
                             '--output', 'json'], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=60, check=False)
    if result.returncode:
        raise ValueError('CLI failed; raw stderr omitted')
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True, type=Path)
    parser.add_argument('--contracts', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-pages', type=int, default=10)
    args = parser.parse_args()
    try:
        if (os.environ.get('VE_CALLER_TYPE') != 'ai_agent' or not os.environ.get('VE_CALLER_NAME')
                or os.environ.get('VE_SKILL_NAME') != 'volcengine-redis'):
            raise ValueError('caller metadata required')
        plan = json.loads(args.plan.read_text())
        contracts = json.loads(args.contracts.read_text())
        validate(plan, contracts)
        if not 1 <= args.max_pages <= 10:
            raise ValueError('invalid budget')
        # Reserve a private new file before querying; never overwrite cached evidence or follow a symlink.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            result = collect(plan, contracts, ve_call, args.max_pages)
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'saved': True, 'statuses': [r['status'] for r in result['sources']]}))
        return 2 if any(r['status'] in ('error', 'partial') for r in result['sources']) else 0
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        print(json.dumps({'saved': False, 'error': 'Check plan, verified contracts, caller metadata and new output path. No credentials printed.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
