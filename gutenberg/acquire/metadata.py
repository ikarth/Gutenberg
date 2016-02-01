"""Module to deal with metadata acquisition."""
# pylint:disable=W0603


from __future__ import absolute_import

import abc
import logging
import os
import re
import shutil
import tarfile
import tempfile
from contextlib import closing
from contextlib import contextmanager

from rdflib import plugin
from rdflib.graph import Graph
from rdflib.store import Store
from rdflib.term import URIRef
from six import with_metaclass

from gutenberg._domain_model.exceptions import CacheAlreadyExists
from gutenberg._domain_model.exceptions import InvalidCache
from gutenberg._domain_model.persistence import local_path
from gutenberg._domain_model.vocabulary import DCTERMS
from gutenberg._domain_model.vocabulary import PGTERMS
from gutenberg._util.logging import disable_logging
from gutenberg._util.os import makedirs
from gutenberg._util.os import remove
from gutenberg._util.url import urlopen

_GUTENBERG_CATALOG_URL = \
    r'http://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2'
_DB_IDENTIFIER = 'urn:gutenberg:metadata'


class MetadataCacheManager(with_metaclass(abc.ABCMeta, object)):
    def __init__(self, store, cache_uri):
        self.store = store
        self.cache_uri = cache_uri
        self.graph = Graph(store=self.store, identifier=_DB_IDENTIFIER)
        self.cache_open = False
        self.catalog_source = _GUTENBERG_CATALOG_URL

    def exists(self):
        """Detect if the cache exists.

        """
        return os.path.exists(self._get_local_storage_path())

    def open(self):
        """Opens an existing cache.

        """
        try:
            self.graph.open(self.cache_uri, create=False)
            self._add_namespaces(self.graph)
            self.cache_open = True
        except:
            raise InvalidCache('The cache is invalid or not created')

    def close(self):
        """Closes an opened cache.

        """
        self.graph.close()
        self.cache_open = False

    def delete(self):
        """Delete the cache.

        """
        self.close()
        remove(self._get_local_storage_path())

    def populate(self):
        """Populates a new cache.

        """
        if self.exists():
            raise CacheAlreadyExists('location: %s' % self.cache_uri)

        self._populate_setup()

        self.graph.open(self.cache_uri, create=True)
        with closing(self.graph):
            with self._download_metadata_archive() as metadata_archive:
                for fact in self._iter_metadata_triples(metadata_archive):
                    self.graph.add(fact)

    def _populate_setup(self):
        """Executes operations necessary before the cache can be populated.

        """
        pass

    def refresh(self):
        """Refresh the cache by deleting the old one and creating a new one.

        """
        if self.exists():
            self.delete()
        self.populate()
        self.open()

    def _get_local_storage_path(self):
        return self.cache_uri

    @staticmethod
    def _add_namespaces(graph):
        """Function to ensure that the graph always has some specific namespace
        aliases set.

        """
        graph.bind('pgterms', PGTERMS)
        graph.bind('dcterms', DCTERMS)

    @contextmanager
    def _download_metadata_archive(self):
        """Makes a remote call to the Project Gutenberg servers and downloads
        the entire Project Gutenberg meta-data catalog. The catalog describes
        the texts on Project Gutenberg in RDF. The function returns a
        file-pointer to the catalog.

        """
        with tempfile.NamedTemporaryFile(delete=False) as metadata_archive:
            shutil.copyfileobj(urlopen(self.catalog_source), metadata_archive)
        yield metadata_archive.name
        remove(metadata_archive.name)

    @staticmethod
    def _metadata_is_invalid(fact):
        """Determines if the fact is not well formed.

        """
        return any(isinstance(token, URIRef) and ' ' in token for token in fact)

    @classmethod
    def _iter_metadata_triples(cls, metadata_archive_path):
        """Yields all meta-data of Project Gutenberg texts contained in the
        catalog dump.

        """
        pg_rdf_regex = re.compile(r'pg\d+.rdf$')
        with closing(tarfile.open(metadata_archive_path)) as metadata_archive:
            for item in metadata_archive:
                if pg_rdf_regex.search(item.name):
                    with disable_logging():
                        extracted = metadata_archive.extractfile(item)
                        graph = Graph().parse(extracted)
                    for fact in graph:
                        if cls._metadata_is_invalid(fact):
                            logging.info('skipping invalid triple %s', fact)
                        else:
                            yield fact


class SleepycatMetadataCacheManager(MetadataCacheManager):
    """Default cache manager implementation, based on Sleepycat/Berkeley DB.
    Sleepycat is natively supported by RDFlib so this cache is reasonably fast.

    """
    def __init__(self, cache_uri):
        MetadataCacheManager.__init__(self, 'Sleepycat', cache_uri)

    def _populate_setup(self):
        makedirs(self.cache_uri)


class SqliteMetadataCacheManager(MetadataCacheManager):
    """Cache manager based on SQLite and the RDFlib plugin for SQLAlchemy.
    Quite slow.

    """
    _CACHE_URI_PREFIX = 'sqlite://'

    def __init__(self, cache_uri):
        if not cache_uri.startswith(self._CACHE_URI_PREFIX):
            cache_uri = self._CACHE_URI_PREFIX + cache_uri
        store = plugin.get('SQLAlchemy', Store)(identifier=_DB_IDENTIFIER)
        MetadataCacheManager.__init__(self, store, cache_uri)

    def _get_local_storage_path(self):
        return self.cache_uri[len(self._CACHE_URI_PREFIX):]


_METADATA_CACHE_MANAGER = SleepycatMetadataCacheManager(
    cache_uri=local_path(os.path.join('metadata', 'metadata.db')))


def set_metadata_cache_manager(cache_manager):
    """Sets the metadata cache object to use.

    """
    global _METADATA_CACHE_MANAGER
    if _METADATA_CACHE_MANAGER and _METADATA_CACHE_MANAGER.cache_open:
        _METADATA_CACHE_MANAGER.close()

    _METADATA_CACHE_MANAGER = cache_manager


def get_metadata_cache_manager():
    """Returns the current metadata cache manager object.

    """
    global _METADATA_CACHE_MANAGER
    return _METADATA_CACHE_MANAGER


def load_metadata(refresh_cache=False):
    """Returns a graph representing meta-data for all Project Gutenberg texts.
    Pertinent information about texts or about how texts relate to each other
    (e.g. shared authors, shared subjects) can be extracted using standard RDF
    processing techniques (e.g. SPARQL queries). After making an initial remote
    call to Project Gutenberg's servers, the meta-data is persisted locally.

    """
    global _METADATA_CACHE_MANAGER

    if refresh_cache:
        _METADATA_CACHE_MANAGER.refresh()

    if _METADATA_CACHE_MANAGER.cache_open:
        return _METADATA_CACHE_MANAGER.graph

    _METADATA_CACHE_MANAGER.open()

    return _METADATA_CACHE_MANAGER.graph
