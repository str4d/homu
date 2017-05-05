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

    sess.post(
        repo_cfg['buildbot']['url'] + '/login',
        allow_redirects=False,
        data={
            'username': repo_cfg['buildbot']['username'],
            'passwd': repo_cfg['buildbot']['password'],
        })

    yield sess

    sess.get(repo_cfg['buildbot']['url'] + '/logout', allow_redirects=False)


def buildbot_stopselected(repo_cfg):
    err = ''
    with buildbot_sess(repo_cfg) as sess:
        res = sess.post(
            repo_cfg['buildbot']['url'] + '/builders/_selected/stopselected',   # noqa
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

    return err


def buildbot_rebuild(sess, builder, url):
    res = sess.post(url + '/rebuild', allow_redirects=False, data={
        'useSourcestamp': 'exact',
        'comments': 'Initiated by Homu',
    })

    err = ''
    if 'authzfail' in res.text:
        err = 'Authorization failed'
    elif builder in res.text:
        err = ''
    else:
        mat = re.search('<title>(.+?)</title>', res.text)
        err = mat.group(1) if mat else 'Unknown error'

    return err


class BuildbotBuilderStep:
    def __init__(self, step):
        self.name = step['name']
        self.text = step.get('text', [])

class BuildbotStatusPacket:
    def __init__(self, packet):
        self.event = packet['event']
        self._info = packet['payload']['build']

        self.builder_name = self._info['builderName']
        self.properties = dict(x[:2] for x in self._info['properties'])
        self.results = self._info['results']
        self.steps = [BuildbotBuilderStep(s) for s in self._info['steps']]
        self.text = self._info['text']

    def url(self, repo_cfg):
        return '{}/builders/{}/builds/{}'.format(
            repo_cfg['buildbot']['url'],
            self.builder_name,
            self.properties['buildnumber'],
        )

    def interrupted(self):
        return 'interrupted' in self.text

    def interrupt_reason(self, repo_cfg):
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
            return None

    def __repr__(self):
        return self._info.__repr__()


class BuildbotStatus:
    def __init__(self, request):
        self.packets = [BuildbotStatusPacket(p) for p in json.loads(request.forms.packets)]
        self.secret = request.forms.secret
