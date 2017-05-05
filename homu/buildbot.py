from contextlib import contextmanager
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
