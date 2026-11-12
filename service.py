from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

def canonical_url(url, tracking=('utm_source', 'utm_campaign')):
    parts = urlsplit(url.strip())
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in tracking]
    path = parts.path or '/'
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ''))
