#!/usr/bin/env python
# -*- coding: utf-8 -*-

#############################################################################
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
#############################################################################

import math
import six

import PIL.Image
import PIL.ImageOps

try:
    from girder import logger
except ImportError:
    import logging as logger
    logger.getLogger().setLevel(logger.INFO)

try:
    import numpy
except ImportError:
    logger.warning('Error: Could not import numpy')
    numpy = None

from .base import FileTileSource, TileSourceException
from ..cache_util import LruCacheMetaclass, strhash, methodcache

try:
    import girder
    from .base import GirderTileSource
except ImportError:
    girder = None


@six.add_metaclass(LruCacheMetaclass)
class ImageJFileTileSource(FileTileSource):
    """
    Provides tile access to sliced ImageJ files.
    """
    cacheName = 'tilesource'
    name = 'imagejfile'

    def __init__(self, path, slice=None, **kwargs):
        """
        Initialize the tile class.

        :param path: the associated file path.
        :param slice: a 0-based number for the slice within the image.  None is
            the same as using the central slice.
        """
        super(ImageJFileTileSource, self).__init__(path, **kwargs)

        if slice is not None:
            slice = int(slice)
        self.slice = slice

        largeImagePath = self._getLargeImagePath()

        try:
            self._pilImage = PIL.Image.open(largeImagePath)
        except IOError:
            raise TileSourceException('File cannot be opened via PIL.')
        try:
            self._tags = {
                PIL.TiffTags.TAGS[n]: self._pilImage.tag_v2[n]
                for n in self._pilImage.tag_v2 if n in PIL.TiffTags.TAGS
            }
            self._description = {
                line.split('=', 1)[0]: line.split('=', 1)[1]
                for line in self._tags['ImageDescription'].split('\n')
                if '=' in line}
            self._numslices = int(self._description['slices'])
            if 'ImageJ' not in self._description or self._numslices < 1:
                self._description = None
        except Exception:
            self._description = None
        if not isinstance(self._description, dict):
            raise TileSourceException('File does not contain an ImageJ sliced image description.')
        if (len(self._tags['StripOffsets']) != 1 or
                len(self._tags['StripByteCounts']) != 1 or
                not self._tags.get('StripOffsets')[0] or
                not self._tags.get('StripByteCounts')[0]):
            raise TileSourceException('File has unusable StripOffsets or StripByteCounts')
        if self.slice is not None and self.slice < 0 or self.slice >= self._numslices:
            raise TileSourceException('Slice %r is out of range [0, %d)' % (
                self.slice, self._numslices))
        if slice is None:
            slice = int(self._numslices / 2)
        self._slice = slice
        if slice:
            with open(largeImagePath, 'rb') as file:
                imageBuffer = six.BytesIO()
                print self._tags['StripOffsets'], self._tags['StripByteCounts']
                imageBuffer.write(file.read(self._tags['StripOffsets'][0]))
                file.seek(slice * self._tags['StripByteCounts'][0])
                imageBuffer.write(file.read(self._tags['StripByteCounts'][0]))
                self._pilImage = PIL.Image.open(imageBuffer)

        self._pilImage = PIL.ImageOps.autocontrast(self._pilImage)  # ##DWM::

        # If this is encoded as a 32-bit integer or a 32-bit float, convert it
        # to an 8-bit integer.  This expects the source value to either have a
        # maximum of 1, 2^8-1, 2^16-1, 2^24-1, or 2^32-1, and scales it to
        # [0, 255]
        pilImageMode = self._pilImage.mode.split(';')[0]
        if pilImageMode in ('I', 'F') and numpy:
            imgdata = numpy.asarray(self._pilImage)
            maxval = 256 ** math.ceil(math.log(numpy.max(imgdata) + 1, 256)) - 1
            self._pilImage = PIL.Image.fromarray(numpy.uint8(numpy.multiply(
                imgdata, 255.0 / maxval)))
        self.sizeX = self._pilImage.width
        self.sizeY = self._pilImage.height
        # We have just one tile which is the entire image.
        self.tileWidth = self.sizeX
        self.tileHeight = self.sizeY
        self.levels = 1
        # Throw an exception if too big
        if self.tileWidth <= 0 or self.tileHeight <= 0:
            raise TileSourceException('PIL tile size is invalid.')
        # ##DWM::

    @staticmethod
    def getLRUHash(*args, **kwargs):
        return strhash(
            super(ImageJFileTileSource, ImageJFileTileSource).getLRUHash(
                *args, **kwargs),
            kwargs.get('slice'))

    def getState(self):
        return super(ImageJFileTileSource, self).getState() + ',' + str(
            self.slice)

    @methodcache()
    def getTile(self, x, y, z, pilImageAllowed=False, mayRedirect=False, **kwargs):
        if z != 0:
            raise TileSourceException('z layer does not exist')
        if x != 0:
            raise TileSourceException('x is outside layer')
        if y != 0:
            raise TileSourceException('y is outside layer')
        return self._outputTile(self._pilImage, 'PIL', x, y, z,
                                pilImageAllowed, **kwargs)

    def getMetadata(self):
        metadata = super(ImageJFileTileSource, self).getMetadata()
        metadata['tags'] = self._tags
        metadata['imagej'] = self._description
        metadata['slice'] = self._slice
        return metadata


if girder:
    class ImageJGirderTileSource(ImageJFileTileSource, GirderTileSource):
        """
        Provides tile access to Girder items with a ImageJ file.
        """
        # Cache size is based on what the class needs, which does not include
        # individual tiles
        cacheName = 'tilesource'
        name = 'imagej'

        @staticmethod
        def getLRUHash(*args, **kwargs):
            return strhash(
                super(ImageJGirderTileSource, ImageJGirderTileSource).getLRUHash(
                    *args, **kwargs),
                kwargs.get('slice', args[1] if len(args) >= 2 else None))

        def getState(self):
            return super(ImageJGirderTileSource, self).getState() + ',' + str(
                self.slice)
