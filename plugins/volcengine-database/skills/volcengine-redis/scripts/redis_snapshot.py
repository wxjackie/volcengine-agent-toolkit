#!/usr/bin/env python3
"""Bounded read-only Redis inventory and scoped snapshot comparison (stdlib only)."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

CONFIG = ('InstanceName', 'RegionId', 'EngineVersion', 'ShardedCluster', 'ShardNumber',
          'ShardCapacity', 'NodeNumber', 'VpcId', 'ZoneIds', 'PrivateAddress',
          'PrivatePort', 'ChargeType', 'MultiAZ', 'DeletionProtection', 'ServiceType')
OBSERVED = ('Status',)


class EvidenceError(ValueError):
    pass


def projection(instance):
    if not isinstance(instance, dict) or not isinstance(instance.get('InstanceId'), str):
        raise EvidenceError('malformed instance: missing InstanceId')
    result = {'InstanceId': instance['InstanceId']}
    for name in CONFIG + OBSERVED:
        if name in instance:
            result[name] = instance[name]
    if isinstance(instance.get('Capacity'), dict):
        result['Capacity'] = {k: instance['Capacity'][k] for k in ('Total', 'Used')
                              if k in instance['Capacity']}
    return result


def parse_page(payload, region):
    if not isinstance(payload, dict):
        raise EvidenceError('response is not an object')
    meta = payload.get('ResponseMetadata')
    if not isinstance(meta, dict):
        raise EvidenceError('missing ResponseMetadata')
    if meta.get('Error'):
        # Never echo arbitrary provider messages, request bodies or credentials.
        raise EvidenceError('cloud API returned Error; inspect the authorized CLI error separately')
    if meta.get('Region') and meta['Region'] != region:
        raise EvidenceError('response region does not match requested region')
    result = payload.get('Result')
    if not isinstance(result, dict) or not isinstance(result.get('Instances'), list):
        raise EvidenceError('missing Result.Instances')
    total = result.get('TotalInstancesNum')
    if type(total) is not int or total < 0:
        raise EvidenceError('missing or invalid TotalInstancesNum')
    return result['Instances'], total, meta.get('RequestId')


def collect(region, ids, project, fetch, max_pages=100):
    """fetch(params) returns a native DescribeDBInstances response. No retries."""
    if not region or max_pages < 1:
        raise EvidenceError('region and positive max_pages required')
    ids = sorted(set(ids))
    records, requests, missing = {}, [], []
    for target in ids or [None]:
        seen, total = set(), None
        for page in range(1, max_pages + 1):
            params = {'PageNumber': page, 'PageSize': 10}
            if target:
                params['InstanceId'] = target
            if project:
                params['ProjectName'] = project
            items, page_total, request_id = parse_page(fetch(params), region)
            requests.append({'page': page, 'target': target, 'request_id': request_id})
            if total is not None and total != page_total:
                raise EvidenceError('inventory changed during pagination; no complete snapshot')
            total = page_total
            for item in items:
                value = projection(item)
                iid = value['InstanceId']
                if target and iid != target:
                    raise EvidenceError('exact ID query returned a different instance')
                if value.get('RegionId') and value['RegionId'] != region:
                    raise EvidenceError('instance region mismatch')
                if iid in seen:
                    raise EvidenceError('duplicate instance across pages; incomplete evidence')
                seen.add(iid)
                records[iid] = value
            if len(seen) == total:
                if target and not seen:
                    missing.append(target)
                break
            if not items or len(seen) > total:
                raise EvidenceError('pagination count mismatch; incomplete evidence')
        else:
            raise EvidenceError('max_pages reached; incomplete evidence')
    return {'schema_version': 1, 'collected_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'scope': {'region': region, 'instance_ids': ids, 'project': project},
            'complete': True, 'instances': [records[k] for k in sorted(records)],
            'not_found_in_scope': missing, 'requests': requests,
            'coverage': 'DescribeDBInstances projection only; not configuration history or key data'}


def compare(before, after):
    for snap in (before, after):
        if snap.get('schema_version') != 1 or snap.get('complete') is not True:
            raise EvidenceError('comparison requires complete v1 snapshots')
    if before.get('scope') != after.get('scope'):
        raise EvidenceError('snapshot scopes differ; cannot infer drift')
    b = {r['InstanceId']: r for r in before['instances']}
    a = {r['InstanceId']: r for r in after['instances']}
    changes, unknown = [], []
    for iid in sorted(b.keys() & a.keys()):
        for key in CONFIG + OBSERVED + ('Capacity.Total', 'Capacity.Used'):
            def field(row):
                for part in key.split('.'):
                    if not isinstance(row, dict) or part not in row:
                        return False, None
                    row = row[part]
                return True, row
            bp, bv = field(b[iid]); ap, av = field(a[iid])
            if not bp or not ap or bv is None or av is None:
                if bp or ap:
                    unknown.append({'id': iid, 'field': key})
                continue
            # Availability-zone order is not a topology change.
            if key == 'ZoneIds' and isinstance(bv, list) and isinstance(av, list):
                bv, av = sorted(bv), sorted(av)
            if bv != av:
                kind = 'usage' if key == 'Capacity.Used' else ('state' if key in OBSERVED else 'configuration')
                changes.append({'id': iid, 'field': key, 'kind': kind, 'before': bv, 'after': av})
    return {'changes': changes, 'unknown_fields': unknown,
            'newly_observed': sorted(a.keys() - b.keys()),
            'no_longer_observed': sorted(b.keys() - a.keys()),
            'limitation': 'Not observed does not prove deletion; equal snapshots do not exclude intervening operations.'}


def ve_fetch(region, timeout):
    env = os.environ.copy()
    if env.get('VE_CALLER_TYPE') != 'ai_agent' or not env.get('VE_CALLER_NAME') or env.get('VE_SKILL_NAME') != 'volcengine-redis':
        raise EvidenceError('set VE_CALLER_TYPE=ai_agent, actual VE_CALLER_NAME, VE_SKILL_NAME=volcengine-redis')
    def fetch(params):
        cmd = ['ve', 'redis', 'DescribeDBInstances', '--region', region, '--output', 'json']
        for key, value in params.items():
            cmd.extend(['--' + key, str(value)])
        response = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  env=env, stdin=subprocess.DEVNULL, check=False)
        if response.returncode:
            raise EvidenceError('ve failed or policy blocked the read; no retry or confirmation bypass performed')
        try:
            return json.loads(response.stdout)
        except json.JSONDecodeError as exc:
            raise EvidenceError('ve did not return JSON; no snapshot produced') from exc
    return fetch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region', required=True)
    parser.add_argument('--instance-id', action='append', default=[])
    parser.add_argument('--project', default=None)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--timeout', type=int, default=30)
    parser.add_argument('--output', type=Path, required=True, help='New private file; refuses overwrite')
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r'[a-z][a-z0-9-]+', args.region):
            raise EvidenceError('invalid region')
        if any(not re.fullmatch(r'redis-[A-Za-z0-9-]+', i) for i in args.instance_id):
            raise EvidenceError('invalid Redis instance ID')
        if not args.instance_id and not args.project:
            raise EvidenceError('supply instance IDs or explicit project scope')
        if args.timeout <= 0 or args.max_pages <= 0:
            raise EvidenceError('timeout and max-pages must be positive')
        baseline = json.loads(args.baseline.read_text()) if args.baseline else None
        snapshot = collect(args.region, args.instance_id, args.project,
                           ve_fetch(args.region, args.timeout), args.max_pages)
        if baseline is not None:
            snapshot['comparison'] = compare(baseline, snapshot)
        output = json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n'
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(output)
        print(json.dumps({'ok': True, 'instances': len(snapshot['instances']), 'saved': True}))
        return 0
    except EvidenceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # No raw stdout/stderr, private paths, cloud messages or secret-bearing exceptions.
        print(json.dumps({'ok': False, 'error': 'Evidence collection/comparison failed; check scope, CLI authentication/policy, response contract and output path. No complete snapshot was written.'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
