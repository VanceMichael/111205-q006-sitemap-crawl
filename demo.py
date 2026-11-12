import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from fixture_server import site
from service import canonical_url


def run():
    with site() as base:
        with urlopen(base + '/sitemap.xml', timeout=2) as response:
            assert b'section.xml' in response.read()
        with urlopen(base + '/article', timeout=2) as response:
            etag = response.headers['ETag']
        try:
            urlopen(Request(base + '/article', headers={'If-None-Match': etag}), timeout=2)
        except HTTPError as error:
            assert error.code == 304
            error.close()
        else:
            raise AssertionError('conditional response must be 304')
    canonical = canonical_url('https://synthetic.example/article?id=4&utm_source=mail#top')
    assert canonical.endswith('article?id=4')
    return {'canonical': canonical, 'conditional_status': 304, 'sitemap_fixture': 'recursive index with cycle'}


if __name__ == '__main__':
    print(json.dumps(run(), ensure_ascii=False, indent=2))
