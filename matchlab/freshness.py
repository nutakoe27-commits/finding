"""Quote observation windows consistent with the feed's update cadence."""


def quote_ttl(document):
    return 45 if document.get('source_kind')=='stream' and document.get('phase')=='live' else 180
