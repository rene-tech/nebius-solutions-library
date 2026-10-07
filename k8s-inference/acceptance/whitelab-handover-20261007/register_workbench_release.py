"""Add one qualified Serverless image to the existing operator release menu.

Preserve every existing release and live API setting. Helm values predate some
operator changes, so do not use a full Helm reconciliation to add one image.
The emitted values fragment records desired chart configuration for the next
deliberately reconciled release; the exact compare/replace patch has a rollback.
This does not itself replace any customer's workspace.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess


def extend(template, release, image):
    after = copy.deepcopy(template)
    matches = [item for container in after['spec']['containers']
               for item in container.get('env', []) if item['name'] == 'FS2_WORKBENCH_RELEASES']
    if len(matches) != 1:
        raise ValueError('Expected one explicit API workbench release map')
    values = json.loads(matches[0]['value'])
    if release in values and values[release] != image:
        raise ValueError('Do not change the image behind an existing release identity')
    values[release] = image
    matches[0]['value'] = json.dumps(values, sort_keys=True, separators=(',', ':'))
    return after, values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,99}', args.release):
        parser.error('Use a stable release name')
    if not re.fullmatch(r'cr\.eu-north1\.nebius\.cloud/e00akg9ndpx77eaexh/lc@sha256:[0-9a-f]{64}', args.image):
        parser.error('Use an immutable image in the existing qualified client repository')
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True)
    kube = ['kubectl', '--context', args.context, '-n', 'fs2-system', '--request-timeout=30s']
    target = ['deployment', 'fs2-serve-control-plane']
    obj = json.loads(subprocess.check_output([*kube, 'get', *target, '-o', 'json']))
    before = obj['spec']['template']
    after, releases = extend(before, args.release, args.image)
    (args.output / 'workbench-release.values.json').write_text(json.dumps({'workbenches': {'releases': releases}}, indent=2) + '\n')
    if before == after:
        print(json.dumps({'release': args.release, 'already_registered': True}))
        return
    for label, old, new in [('patch', before, after), ('rollback', after, before)]:
        patch = [{'op': 'test', 'path': '/spec/template', 'value': old},
                 {'op': 'replace', 'path': '/spec/template', 'value': new}]
        (args.output / (label + '.json')).write_text(json.dumps(patch, indent=2) + '\n')
    command = [*kube, 'patch', *target, '--type=json', '--patch-file', str(args.output / 'patch.json')]
    subprocess.run([*command, '--dry-run=server', '-o', 'name'], check=True)
    if args.apply:
        subprocess.run([*command, '-o', 'name'], check=True)
    print(json.dumps({'release': args.release, 'image': args.image, 'applied': args.apply,
                      'customer_endpoints_changed': False}))


if __name__ == '__main__':
    main()
