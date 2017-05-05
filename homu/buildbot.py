from contextlib import contextmanager
import json
import re
import requests
import time

INTERRUPTED_BY_HOMU_FMT = 'Interrupted by Homu ({})'
INTERRUPTED_BY_HOMU_RE = re.compile(r'Interrupted by Homu \((.+?)\)')


@contextmanager
def buildbot_sess(repo_cfg):
    sess = requests.Session()

    try:
        sess.api_url = '{}/api/v2'.format(repo_cfg['buildbot']['url'])
        requests.get(sess.api_url)
        sess.v1 = False
    except requests.exceptions.RequestException:
        sess.api_url = repo_cfg['buildbot']['url']
        sess.v1 = True

    sess.post(
        '{}{}/login'.format(
            sess.api_url,
            '' if sess.v1 else '/auth',
        ),
        allow_redirects=False,
        data={
            'username': repo_cfg['buildbot']['username'],
            'passwd': repo_cfg['buildbot']['password'],
        })

    yield sess

    sess.get(
        '{}{}/logout'.format(
            sess.api_url,
            '' if sess.v1 else '/auth',
        ), allow_redirects=False)


def buildbot_stopselected(repo_cfg):
    err = ''
    with buildbot_sess(repo_cfg) as sess:
        if sess.v1:
            res = sess.post(
                sess.api_url + '/builders/_selected/stopselected',   # noqa
                allow_redirects=False,
                data={
                    'selected': repo_cfg['buildbot']['builders'],
                    'comments': INTERRUPTED_BY_HOMU_FMT.format(int(time.time())),  # noqa
            })

            if 'authzfail' in res.text:
                err = 'Authorization failed'
            else:
                mat = re.search('(?s)<div class="error">(.*?)</div>', res.text)
                if mat:
                    err = mat.group(1).strip()
                    if not err:
                        err = 'Unknown error'
                else:
                    err = ''
        else:
            # Buildbot 0.9 only accepts builder ids, so we need to translate the
            # name list into an id list
            builders = requests.get('{}/builders?field=builderid&field=name'.format(
                sess.api_url,
            )).json()['builders']
            id_map = {b['name']: b['builderid'] for b in builders}
            for builder in repo_cfg['buildbot']['builders']:
                builds = requests.get('{}/builders/{}/builds?field=complete&field=number'.format(
                    sess.api_url,
                    id_map[builder],
                )).json()['builds']
                builds = [b for b in builds if not b['complete']]
                for build in builds:
                    res = sess.post(
                        '{}/builders/{}/builds/{}'.format(
                            sess.api_url,
                            id_map[builder],
                            build['number'],
                        ),
                        allow_redirects=False,
                        json={
                            'jsonrpc': '2.0',
                            'method': 'stop',
                            'id': 'stop',
                            'params': {
                                'reason': INTERRUPTED_BY_HOMU_FMT.format(int(time.time())),  # noqa
                            },
                        },
                    )

                    if not res.ok:
                        err = '{}: {}'.format(
                            res.status_code,
                            res.json()['error']['message'],
                        )

    return err


def buildbot_rebuild(sess, builder, url):
    err = ''
    if sess.v1:
        res = sess.post(url + '/rebuild', allow_redirects=False, data={
            'useSourcestamp': 'exact',
            'comments': 'Initiated by Homu',
        })

        if 'authzfail' in res.text:
            err = 'Authorization failed'
        elif builder in res.text:
            err = ''
        else:
            mat = re.search('<title>(.+?)</title>', res.text)
            err = mat.group(1) if mat else 'Unknown error'
    else:
        res = sess.post(url.replace('#', 'api/v2/'), allow_redirects=False, json={
            'jsonrpc': '2.0',
            'method': 'rebuild',
            'id': 'rebuild',
            'params': {
                'useSourcestamp': 'exact',
                'comments': 'Initiated by Homu',
            },
        })

        if not res.ok:
            err = '{}: {}'.format(
                res.status_code,
                res.json()['error']['message'],
            )

    return err


class BuildbotBuilderStep:
    def __init__(self, step):
        self.name = step['name']
        self.number = step.get('number', -1)
        self.text = step.get('text', [])
        self.urls = step.get('urls', [])

class BuildbotStatusPacket:
    def __init__(self, v1, packet):
        # Buildbot 0.9+ does not have events, only a complete flag depending on
        # whether this was triggered by buildStarted or buildFinished
        self._v1 = v1
        if v1:
            self.event = packet['event']
        else:
            self.event = 'buildFinished' if packet['complete'] else 'buildStarted'
        self._info = packet['payload']['build'] if v1 else packet

        self.builder_name = self._info['builderName'] if v1 else self._info['builder']['name']
        self.properties = dict(x[:2] for x in self._info['properties']) if v1 else {k: v[0] for k, v in self._info['properties'].items()}
        self.results = self._info['results']
        self.steps = [BuildbotBuilderStep(s) for s in self._info['steps']]
        self.text = self._info['text' if v1 else 'state_string']

    def url(self, repo_cfg):
        return self._info['url'] if not self._v1 else '{}/builders/{}/builds/{}'.format(
            repo_cfg['buildbot']['url'],
            self.builder_name,
            self.properties['buildnumber'],
        )

    def interrupted(self):
        return 'interrupted' in self.text if self._v1 else self.results == 6

    def interrupt_reason(self, repo_cfg):
        if self._v1:
            step_name = ''
            for step in reversed(self.steps):
                if 'interrupted' in step.text:
                    step_name = step.name
                    break

            if step_name:
                url = (
                    '{}/builders/{}/builds/{}/steps/{}/logs/interrupt'  # noqa
                ).format(
                    repo_cfg['buildbot']['url'],
                    self.builder_name,
                    self.properties['buildnumber'],
                    step_name,
                )
                res = requests.get(url)
                return res.text
        else:
            def cancelled_url(builder_id, build_number, step_number):
                return (
                    '{}/api/v2/builders/{}/builds/{}/steps/{}/logs/cancelled/contents?field=content'  # noqa
                ).format(
                    repo_cfg['buildbot']['url'],
                    builder_id,
                    build_number,
                    step_number,
                )
            for step in reversed(self.steps):
                res = requests.get(cancelled_url(
                    self._info['builderid'],
                    self.properties['buildnumber'],
                    step.number))
                if res.ok:
                    return res.json()['logchunks'][0]['content']
                else:
                    # Trigger steps don't store the cancelled reason, so we have
                    # to search for it recursively
                    for url in step.urls:
                        if 'buildrequests/' in url['url']:
                            buildrequest_id = url['url'].rsplit('/', 1)[-1]
                            builder_id = requests.get(
                                '{}/api/v2/buildrequests/{}?field=builderid'.format(
                                    repo_cfg['buildbot']['url'],
                                    buildrequest_id,
                                )).json()['buildrequests'][0]['builderid']
                            build_number = requests.get(
                                '{}/api/v2/builders/{}/builds?field=number&field=buildrequestid&buildrequestid__eq={}'.format(
                                    repo_cfg['buildbot']['url'],
                                    builder_id,
                                    buildrequest_id,
                                )).json()['builds'][0]['number']
                            steps = requests.get(
                                '{}/api/v2/builders/{}/builds/{}/steps?field=number'.format(
                                    repo_cfg['buildbot']['url'],
                                    builder_id,
                                    build_number,
                                )).json()['steps']
                            for inner_step in reversed(steps):
                                res = requests.get(cancelled_url(
                                    builder_id,
                                    build_number,
                                    inner_step['number']))
                                if res.ok:
                                    return res.json()['logchunks'][0]['content']
        return None

    def __repr__(self):
        return {k: v for k, v in self._info.items() if k != 'secret'}.__repr__()


class BuildbotStatus:
    def __init__(self, request):
        v1 = request.json is None
        self.packets = [BuildbotStatusPacket(v1, p) for p in (
            json.loads(request.forms.packets) if v1 else [request.json])]
        self.secret = request.forms.secret if v1 else request.json['secret']
