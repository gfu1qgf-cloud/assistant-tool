"""Untrusted API citations, kept as bounded data; never fetch their URLs."""
from urllib.parse import urlsplit


def text_with_sources(text, metadata):
    links={}
    for item in (metadata.get('sources') or [])[:1000]:
        safe=source(item.get('url'),item.get('title'))
        if safe: links[safe['url']]=safe['title']
    if not links: return text
    return text+'\n\n联网来源：\n'+'\n'.join(title+'：'+url for url,title in links.items())


def source(url, title='', start=None, end=None):
    if not isinstance(url,str) or len(url)>4096: return None
    try:
        parsed=urlsplit(url)
        if parsed.scheme not in {'https','http'} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError: return None
    item={'url':url,'title':str(title or parsed.hostname)[:500]}
    if type(start) is int and type(end) is int and 0<=start<=end:
        item.update(start=start,end=end)
    return item


def annotations(values, offset=0):
    items=[]
    for value in (values or [])[:200]:
        if not isinstance(value,dict) or value.get('type')!='url_citation': continue
        citation=value.get('url_citation') or value
        start,end=citation.get('start_index'),citation.get('end_index')
        item=source(citation.get('url'),citation.get('title'),
                    start+offset if type(start) is int else None,end+offset if type(end) is int else None)
        if item: items.append(item)
    return items


def response_metadata(provider, data):
    result={'sources':[], 'search_calls':None, 'reasoning_tokens':0}
    if provider=='openai':
        result.update(sources=data.get('sources') or [],search_calls=data.get('search_calls',0),
                      reasoning_tokens=(data.get('usage') or {}).get('reasoning_tokens',0))
    elif provider=='gemini':
        candidate=(data.get('candidates') or [{}])[0]
        grounding=candidate.get('groundingMetadata') or {}
        chunks=grounding.get('groundingChunks') or []
        by_index={}
        for index,chunk in enumerate(chunks[:200]):
            web=chunk.get('web') or {}; item=source(web.get('uri'),web.get('title'))
            if item: by_index[index]=item
        for support in (grounding.get('groundingSupports') or [])[:200]:
            segment=support.get('segment') or {}
            for index in (support.get('groundingChunkIndices') or [])[:200]:
                if index in by_index:
                    item=source(by_index[index]['url'],by_index[index]['title'],segment.get('startIndex',0),segment.get('endIndex'))
                    if item: result['sources'].append(item)
        result['sources'].extend(by_index.values())
        result['reasoning_tokens']=(data.get('usageMetadata') or {}).get('thoughtsTokenCount',0)
        result['search_queries']=len(grounding.get('webSearchQueries') or [])
    else:
        choice=(data.get('choices') or [{}])[0]
        result['sources']=annotations((choice.get('message') or {}).get('annotations'))
        result['reasoning_tokens']=((data.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens',0)
    # Deduplicate identical citation positions without losing distinct inline citations.
    unique={}
    for item in result['sources'][:1000]:
        safe=source(item.get('url'),item.get('title'),item.get('start'),item.get('end'))
        if safe: unique[(safe['url'],safe.get('start'),safe.get('end'))]=safe
    result['sources']=list(unique.values())
    return result
