#!/usr/bin/env python
# -*- coding: utf-8 -*-

###############################################################################
#  Copyright Kitware Inc.
#
#  Licensed under the Apache License, Version 2.0 ( the "License" );
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

try:
    from . import loadmodelcache
except ImportError:
    loadmodelcache = None
from .cache import LruCacheMetaclass, tileCache, tileLock, strhash, \
    methodcache
try:
    from .memcache import MemCache
except ImportError:
    MemCache = None
from .cachefactory import CacheFactory, pickAvailableCache
from cachetools import cached, Cache, LRUCache


def clearCaches():
    """
    Clear the tilesource caches and the load model cache.  Note that this does
    not clear memcached (which could be done with tileCache._client.flush_all,
    but this can affect programs other than this one).
    """
    if loadmodelcache:
        loadmodelcache.invalidateLoadModelCache()
    for name in LruCacheMetaclass.namedCaches:
        LruCacheMetaclass.namedCaches[name].clear()
    if hasattr(tileCache, 'clear'):
        try:
            if tileLock:
                with tileLock:
                    tileCache.clear()
            else:
                tileCache.clear()
        except Exception:
            pass


__all__ = ('CacheFactory', 'tileCache', 'tileLock', 'MemCache', 'strhash',
           'LruCacheMetaclass', 'pickAvailableCache', 'cached', 'Cache',
           'LRUCache', 'methodcache', 'clearCaches')
