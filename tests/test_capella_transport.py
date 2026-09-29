"""Offline adapter contract checks; live SDK/Capella checks remain separate."""
import importlib.util
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import timedelta

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('capella_adapter_under_test', ROOT / 'ui/app/search_adapter.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

class Item:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs

class Request:
    def __init__(self, item):
        self.item, self.vector = item, None
    @classmethod
    def create(cls, item):
        return cls(item)
    def with_vector_search(self, vector):
        self.vector = vector
        return self

class CapellaSearchContract(unittest.TestCase):
    def setUp(self):
        search = ModuleType('couchbase.search')
        for name in ('MatchAllQuery', 'MatchQuery', 'MatchPhraseQuery', 'TermQuery',
                     'ConjunctionQuery', 'DisjunctionQuery', 'NumericRangeQuery'):
            setattr(search, name, type(name, (Item,), {}))
        search.SearchRequest = Request
        vector = ModuleType('couchbase.vector_search')
        vector.VectorQuery = type('VectorQuery', (Item,), {})
        vector.VectorSearch = type('VectorSearch', (Item,), {})
        options = ModuleType('couchbase.options')
        options.SearchOptions = lambda **kwargs: kwargs
        self.mods = patch.dict('sys.modules', {'couchbase.search': search,
            'couchbase.vector_search': vector, 'couchbase.options': options})
        self.mods.start()
        self.addCleanup(self.mods.stop)
        self.call = None
        def scope_search(index, request, opts):
            self.call = (index, request, opts)
            return SimpleNamespace(rows=lambda: iter([]))
        self.client = adapter.SdkSearchClient(SimpleNamespace(search=scope_search), 'demo-index')

    def test_hybrid_keeps_prefilter_timeout_boost_and_highlight(self):
        body = {'query': {'match': 'space', 'field': 'title', 'boost': 4},
                'knn': [{'field': 'embedding', 'vector': [1, 0], 'k': 40,
                         'filter': {'term': 'movie', 'field': 'contentType'}}],
                'highlight': {'style': 'html', 'fields': ['title']}}
        self.client.post('/query', body, timeout=1.5)
        _, req, opts = self.call
        self.assertEqual(req.item.kwargs['boost'], 4)
        vec = req.vector.args[0][0]
        self.assertEqual(vec.kwargs['prefilter'].kwargs['field'], 'contentType')
        self.assertEqual(opts['timeout'], timedelta(seconds=1.5))
        self.assertEqual(opts['highlight_fields'], ['title'])
        self.assertEqual(vec.args[1], [1.0, 0.0])

    def test_numeric_bounds_keep_exclusivity(self):
        q = self.client._query({'field': 'voteAverage', 'min': 7, 'inclusive_min': False})
        self.assertEqual(q.kwargs['min'], 7)
        self.assertIs(q.kwargs['inclusive_min'], False)
        self.assertNotIn('max', q.kwargs)

    def test_unknown_query_fails_instead_of_matching_everything(self):
        with self.assertRaises(ValueError):
            self.client._query({'unsupported_operator': 'value'})

    def test_health_uses_its_timeout(self):
        self.assertEqual(self.client.get('/index', timeout=4).status_code, 200)
        self.assertEqual(self.call[2]['timeout'], timedelta(seconds=4))

    def test_vector_only_omits_lexical_query(self):
        self.client.post('/query', {'knn': [{'field': 'embedding', 'vector': [0, 1]}]})
        self.assertEqual(type(self.call[1].item).__name__, 'VectorSearch')
        self.assertIsNone(self.call[1].vector)

class CapellaValidationContract(unittest.TestCase):
    def validate(self, **updates):
        # Run the new provider-aware Capella block with explicit settings.
        source = (ROOT / 'scripts/12-validate-environment.py').read_text()
        begin = source.index('if settings.deployment_target == "capella":\n    required_names')
        code = source[begin:source.index('\nrequired_files = [', begin)]
        env = {'CB_CONN_STRING': 'couchbases://example.invalid', 'CB_USERNAME': 'app',
               'CB_PASSWORD': 'secret', 'CHAT_BASE_URL': 'http://ollama:11434',
               'CHAT_MODEL': 'llama', 'EMBEDDING_BASE_URL': 'http://ollama:11434',
               'EMBEDDING_MODEL': 'embed', 'START_AGENT_MEMORY': 'false'}
        env.update(updates.pop('env', {}))
        cfg = dict(deployment_target='capella', chat_provider='ollama',
                   embedding_provider='openai_compatible', mcp_enabled=False,
                   agent_catalog_enabled=False)
        cfg.update(updates)
        namespace = {'settings': SimpleNamespace(**cfg), 'os': os, 'errors': [],
                     'is_placeholder': lambda value: 'CHANGE_ME' in value or 'YOUR_' in value}
        with patch.dict(os.environ, env, clear=True):
            exec(code, namespace)
        return namespace['errors']

    def test_ollama_does_not_require_paid_model_keys(self):
        self.assertEqual(self.validate(), [])

    def test_model_service_requires_key(self):
        errors = self.validate(chat_provider='capella_model_service')
        self.assertTrue(any('CHAT_API_KEY' in error for error in errors))

    def test_non_tls_loader_is_rejected(self):
        errors = self.validate(env={'CB_LOADER_CONN_STRING': 'couchbase://localhost'})
        self.assertTrue(any('CB_LOADER_CONN_STRING' in error for error in errors))

if __name__ == '__main__':
    unittest.main()
