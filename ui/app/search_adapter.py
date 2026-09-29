from __future__ import annotations

from datetime import timedelta
from typing import Any


class _Response:
    def __init__(self, body: dict[str, Any], status_code: int = 200):
        self._body, self.status_code = body, status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'Search adapter status {self.status_code}: {self._body}')


class SdkSearchClient:
    """Translate the demo's Search payloads without losing retrieval constraints."""

    def __init__(self, scope, index_name: str):
        self.scope, self.index_name = scope, index_name

    def close(self):
        pass

    def get(self, path: str, timeout: float | None = None):
        try:
            self._run({'size': 1, 'query': {'match_all': None}}, timeout)
            return _Response({'status': 'ok'})
        except Exception as exc:
            return _Response({'error': f'{type(exc).__name__}: {exc}'}, 503)

    def post(self, path: str, json: dict[str, Any], timeout: float | None = None):
        return _Response(self._run(json, timeout))

    def _query(self, obj):
        from couchbase.search import (
            MatchAllQuery, MatchQuery, MatchPhraseQuery, TermQuery,
            ConjunctionQuery, DisjunctionQuery, NumericRangeQuery,
        )
        if not isinstance(obj, dict) or not obj:
            raise ValueError('Search query must be a nonempty object')

        def options(*keys):
            return {k: obj[k] for k in keys if obj.get(k) is not None}

        if 'match_all' in obj:
            return MatchAllQuery(**options('boost'))
        if ('min' in obj or 'max' in obj) and obj.get('field'):
            return NumericRangeQuery(**options(
                'min', 'max', 'inclusive_min', 'inclusive_max', 'field', 'boost'))
        if 'match_phrase' in obj:
            return MatchPhraseQuery(obj['match_phrase'], **options('field', 'analyzer', 'boost'))
        if 'match' in obj:
            return MatchQuery(obj['match'], **options('field', 'fuzziness', 'prefix_length', 'operator', 'analyzer', 'boost'))
        if 'term' in obj:
            return TermQuery(str(obj['term']), **options('field', 'fuzziness', 'prefix_length', 'boost'))
        if 'conjuncts' in obj and obj['conjuncts']:
            return ConjunctionQuery(*[self._query(q) for q in obj['conjuncts']], **options('boost'))
        if 'disjuncts' in obj and obj['disjuncts']:
            return DisjunctionQuery(*[self._query(q) for q in obj['disjuncts']], **options('min', 'boost'))
        raise ValueError(f'Unsupported Search query keys: {sorted(obj)}')

    def _run(self, payload, timeout=None):
        from couchbase.search import SearchRequest, MatchAllQuery
        from couchbase.vector_search import VectorQuery, VectorSearch
        from couchbase.options import SearchOptions

        request = SearchRequest.create(self._query(payload['query'])) if payload.get('query') else None
        vectors = []
        for item in payload.get('knn') or []:
            kwargs = {'num_candidates': int(item.get('k') or payload.get('size') or 10)}
            if item.get('boost') is not None:
                kwargs['boost'] = item['boost']
            if item.get('filter') is not None:
                kwargs['prefilter'] = self._query(item['filter'])
            vectors.append(VectorQuery(item['field'], [float(v) for v in item['vector']], **kwargs))
        if vectors:
            vector_search = VectorSearch(vectors)
            request = request.with_vector_search(vector_search) if request else SearchRequest.create(vector_search)
        if request is None:
            request = SearchRequest.create(MatchAllQuery())
        kwargs = {
            'limit': int(payload.get('size', 10)),
            'skip': int(payload.get('from', 0)),
            'fields': payload.get('fields', ['*']),
            'explain': bool(payload.get('explain', False)),
        }
        if timeout is not None:
            kwargs['timeout'] = timedelta(seconds=float(timeout))
        highlight = payload.get('highlight') or {}
        if highlight.get('style'):
            kwargs['highlight_style'] = highlight['style']
        if highlight.get('fields'):
            kwargs['highlight_fields'] = highlight['fields']
        if 'sort' in payload:
            kwargs['sort'] = payload['sort']
        result = self.scope.search(self.index_name, request, SearchOptions(**kwargs))
        hits = [{
            'id': row.id, 'score': float(row.score or 0),
            'fields': getattr(row, 'fields', None) or {},
            'fragments': getattr(row, 'fragments', None) or {},
        } for row in result.rows()]
        return {'hits': hits, 'total_hits': len(hits)}
