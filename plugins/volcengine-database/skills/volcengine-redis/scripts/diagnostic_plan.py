#!/usr/bin/env python3
"""Build bounded public API requests; never execute cloud operations."""
import argparse
import datetime as dt
import json


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('time must include an explicit timezone')
    return parsed.astimezone(dt.timezone.utc)


def build_plan(region, instance, start, end, sources, metric=None, now=None):
    if not region.strip() or not instance.strip() or instance.startswith('-'):
        raise ValueError('explicit region and instance required')
    begin, finish = timestamp(start), timestamp(end)
    now = now or dt.datetime.now(dt.timezone.utc)
    if not begin < finish <= now:
        raise ValueError('require start < end <= now')
    if (finish - begin).total_seconds() > 86400:
        raise ValueError('plan window limited to 24h; choose a narrower incident window')
    utc = lambda v: v.strftime('%Y-%m-%dT%H:%M:%SZ')
    requests = []
    for source in dict.fromkeys(sources):
        body = {'InstanceId': instance}
        service = 'redis'
        if source in ('slow', 'big', 'hot'):
            action = {'slow': 'DescribeSlowLogs', 'big': 'DescribeBigKeys', 'hot': 'DescribeHotKeys'}[source]
            body.update(QueryStartTime=utc(begin), QueryEndTime=utc(finish), PageSize=100)
            continuation = ('ListOver=false: reuse Context; stop on missing/repeated cursor' if source == 'slow'
                            else 'bounded sample only; no pagination or full-key coverage claim')
        elif source == 'tasks':
            action = 'DescribeTasks'
            body.update(CreationStartTime=utc(begin), CreationEndTime=utc(finish), PageNumber=1, PageSize=100)
            continuation = 'verify current response total/end contract; increment PageNumber within budget'
        elif source == 'audit':
            service, action = 'cloudtrail', 'LookupEvents'
            body = {'StartTime': int(begin.timestamp()), 'EndTime': int(finish.timestamp()),
                    'MaxResults': 50, 'LookupConditions': [
                        {'LookupConditionKey': 'ResourceID', 'LookupConditionValue': instance}]}
            continuation = 'reuse NextToken with fixed window and scope; stop on repeated token'
        elif source == 'metrics':
            service, action = 'cloudmonitor', 'GetMetricData'
            required = {'Namespace', 'SubNamespace', 'MetricName', 'Period', 'StatisticsMethods'}
            if not isinstance(metric, dict) or not required <= metric.keys():
                raise ValueError('metrics requires a verified metric spec with ' + ', '.join(sorted(required)))
            if any(not isinstance(metric[k], str) or not metric[k].strip() for k in required - {'StatisticsMethods'}):
                raise ValueError('metric identifiers and period must be nonempty strings')
            stats = metric['StatisticsMethods']
            if not isinstance(stats, list) or not stats or any(s not in ('avg', 'min', 'max', 'sum') for s in stats):
                raise ValueError('invalid StatisticsMethods; also check metric-specific support')
            dimensions = metric.get('Dimensions', [])
            if not isinstance(dimensions, list):
                raise ValueError('Dimensions must be an array')
            names = set()
            for d in dimensions:
                if not isinstance(d, dict) or set(d) != {'Name', 'Value'} or any(not isinstance(v, str) or not v for v in d.values()):
                    raise ValueError('invalid dimension')
                if d['Name'] == 'ResourceID' or d['Name'] in names:
                    raise ValueError('ResourceID is bound by target; duplicate dimensions forbidden')
                names.add(d['Name'])
            body = {k: metric[k] for k in required}
            body.update(StartTime=int(begin.timestamp()), EndTime=int(finish.timestamp()),
                        Instances=[{'Dimensions': [{'Name': 'ResourceID', 'Value': instance}] + dimensions}])
            continuation = 'verify returned dimensions/time coverage; missing datapoints are unknown, not zero'
        else:
            raise ValueError('unknown source: ' + source)
        requests.append({'service': service, 'action': action, 'body': body,
                         'argv': ['ve', service, action, '--region', region, '--body', json.dumps(body)],
                         'continuation': continuation})
    if not requests:
        raise ValueError('select at least one source')
    return {'status': 'plan_only_not_executed', 'scope': {'region': region, 'instance_id': instance},
            'window': {'start': utc(begin), 'end': utc(finish)},
            'execution': {'max_pages_per_source': 10, 'on_budget_exhausted': 'partial',
                          'caller_metadata': ['VE_CALLER_TYPE=ai_agent', 'VE_CALLER_NAME=<actual-host>',
                                              'VE_SKILL_NAME=volcengine-redis']},
            'requests': requests}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('region', 'instance-id', 'start', 'end'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--sources', nargs='+', choices=['slow', 'big', 'hot', 'tasks', 'audit', 'metrics'], required=True)
    parser.add_argument('--metric-spec', help='JSON file: verified metric identifiers, period, statistics, optional Dimensions (excluding ResourceID)')
    args = parser.parse_args()
    try:
        metric = None
        if args.metric_spec:
            with open(args.metric_spec) as stream:
                metric = json.load(stream)
        result = build_plan(args.region, args.instance_id, args.start, args.end, args.sources, metric)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
