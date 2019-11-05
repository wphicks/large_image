#!/usr/bin/env python
# -*- coding: utf-8 -*-

##############################################################################
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
##############################################################################

from bson import ObjectId
import cherrypy
import datetime
import enum
import jsonschema
import six
import re
import threading
import time
from six.moves import range

from girder import events
from girder import logger
from girder.constants import AccessType, SortDir
from girder.exceptions import AccessException, ValidationException
from girder.models.model_base import AccessControlledModel
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.setting import Setting
from girder.models.user import User

from .annotationelement import Annotationelement
from .image_item import ImageItem
from .. import constants


class AnnotationSchema(object):
    coordSchema = {
        'type': 'array',
        # TODO: validate that z==0 for now
        'items': {
            'type': 'number'
        },
        'minItems': 2,
        'maxItems': 3,
        'name': 'Coordinate',
        # TODO: define origin for 3D images
        'description': 'An X, Y, Z coordinate tuple, in base layer pixel'
                       ' coordinates, where the origin is the upper-left.'
    }

    colorSchema = {
        'type': 'string',
        # We accept colors of the form
        #   #aabbcc                 six digit RRGGBB hex
        #   #abc                    three digit RGB hex
        #   rgb(255, 255, 255)      rgb decimal triplet
        #   rgba(255, 255, 255, 1)  rgba quad with RGB in the range [0-255] and
        #                           alpha [0-1]
        # TODO: make rgb and rgba spec validate that rgb is [0-255] and a is
        # [0-1], rather than just checking if they are digits and such.
        'pattern': r'^(#[0-9a-fA-F]{3,6}|rgb\(\d+,\s*\d+,\s*\d+\)|'
                   r'rgba\(\d+,\s*\d+,\s*\d+,\s*(\d?\.|)\d+\))$'
    }

    userSchema = {
        'type': 'object',
        'additionalProperties': True
    }

    baseShapeSchema = {
        '$schema': 'http://json-schema.org/schema#',
        'id': '/girder/plugins/large_image/models/base_shape',
        'type': 'object',
        'properties': {
            'id': {
                'type': 'string',
                'pattern': '^[0-9a-f]{24}$',
            },
            'type': {'type': 'string'},
            # airplane feature locations
            'user': userSchema,
            'nose': coordSchema,
            'tail': coordSchema,
            'left': coordSchema,
            'right': coordSchema,
            'label': {
                'type': 'object',
                'properties': {
                    'value': {'type': 'string'},
                    'visibility': {
                        'type': 'string',
                        # TODO: change to True, False, None?
                        'enum': ['hidden', 'always', 'onhover']
                    },
                    'fontSize': {
                        'type': 'number',
                        'minimum': 0,
                        'exclusiveMinimum': True,
                    },
                    'color': colorSchema,
                },
                'required': ['value'],
                'additionalProperties': False
            },
            'lineColor': colorSchema,
            'lineWidth': {
                'type': 'number',
                'minimum': 0
            },
            'vector': coordSchema,
            'scalar': {
                'type': 'number'
            },
            'group': {'type': 'string'}
        },
        'required': ['type'],
        'additionalProperties': True
    }
    baseShapePatternProperties = {
        '^%s$' % propertyName: {}
        for propertyName in six.viewkeys(baseShapeSchema['properties'])
        if propertyName != 'type'
    }

    pointShapeSchema = {
        'allOf': [
            baseShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': ['point']
                    },
                    'center': coordSchema,
                    'fillColor': colorSchema
                },
                'required': ['type', 'center'],
                'patternProperties': baseShapePatternProperties,
                'additionalProperties': False
            }
        ]
    }

    arrowShapeSchema = {
        'allOf': [
            baseShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': ['arrow']
                    },
                    'points': {
                        'type': 'array',
                        'items': coordSchema,
                        'minItems': 2,
                        'maxItems': 2,
                    },
                    'fillColor': colorSchema
                },
                'description': 'The first point is the head of the arrow',
                'required': ['type', 'points'],
                'patternProperties': baseShapePatternProperties,
                'additionalProperties': False
            }
        ]
    }

    circleShapeSchema = {
        'allOf': [
            baseShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': ['circle']
                    },
                    'center': coordSchema,
                    'radius': {
                        'type': 'number',
                        'minimum': 0
                    },
                    'fillColor': colorSchema
                },
                'required': ['type', 'center', 'radius'],
                'patternProperties': baseShapePatternProperties,
                'additionalProperties': False
            }
        ]
    }

    polylineShapeSchema = {
        'allOf': [
            baseShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': ['polyline']
                    },
                    'points': {
                        'type': 'array',
                        'items': coordSchema,
                        'minItems': 2,
                    },
                    'fillColor': colorSchema,
                    'closed': {
                        'type': 'boolean',
                        'description': 'polyline is open if closed flag is '
                                       'not specified'
                    },
                },
                'required': ['type', 'points'],
                'patternProperties': baseShapePatternProperties,
                'additionalProperties': False
            }
        ]
    }

    baseRectangleShapeSchema = {
        'allOf': [
            baseShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {'type': 'string'},
                    'center': coordSchema,
                    'width': {
                        'type': 'number',
                        'minimum': 0
                    },
                    'height': {
                        'type': 'number',
                        'minimum': 0
                    },
                    'rotation': {
                        'type': 'number',
                        'description': 'radians counterclockwise around normal',
                    },
                    'normal': coordSchema,
                    'fillColor': colorSchema
                },
                'decription': 'normal is the positive z-axis unless otherwise '
                              'specified',
                'required': ['type', 'center', 'width', 'height'],
                # 'patternProperties': baseShapePatternProperties,
                'additionalProperties': True,
            }
        ]
    }
    baseRectangleShapePatternProperties = {
        '^%s$' % propertyName: {} for propertyName in six.viewkeys(
            baseRectangleShapeSchema['allOf'][1]['properties'])
        if propertyName != 'type'
    }
    baseRectangleShapePatternProperties.update(baseShapePatternProperties)
    rectangleShapeSchema = {
        'allOf': [
            baseRectangleShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': ['rectangle']
                    },
                },
                'required': ['type'],
                'patternProperties': baseRectangleShapePatternProperties,
                'additionalProperties': False
            }
        ]
    }
    rectangleGridShapeSchema = {
        'allOf': [
            baseRectangleShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': ['rectanglegrid']
                    },
                    'widthSubdivisions': {
                        'type': 'integer',
                        'minimum': 1
                    },
                    'heightSubdivisions': {
                        'type': 'integer',
                        'minimum': 1
                    },
                },
                'required': ['type', 'widthSubdivisions', 'heightSubdivisions'],
                'patternProperties': baseRectangleShapePatternProperties,
                'additionalProperties': False,
            }
        ]
    }
    ellipseShapeSchema = {
        'allOf': [
            baseRectangleShapeSchema,
            {
                'type': 'object',
                'properties': {
                    'type': {
                        'type': 'string',
                        'enum': ['ellipse']
                    },
                },
                'required': ['type'],
                'patternProperties': baseRectangleShapePatternProperties,
                'additionalProperties': False
            }
        ]
    }

    annotationElementSchema = {
        '$schema': 'http://json-schema.org/schema#',
        # Shape subtypes are mutually exclusive, so for  efficiency, don't use
        # 'oneOf'
        'anyOf': [
            # If we include the baseShapeSchema, then shapes that are as-yet
            #  invented can be included.
            # baseShapeSchema,
            arrowShapeSchema,
            circleShapeSchema,
            ellipseShapeSchema,
            pointShapeSchema,
            polylineShapeSchema,
            rectangleShapeSchema,
            rectangleGridShapeSchema,
        ]
    }

    annotationSchema = {
        '$schema': 'http://json-schema.org/schema#',
        'id': '/girder/plugins/large_image/models/annotation',
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
                # TODO: Disallow empty?
                'minLength': 1,
            },
            'description': {'type': 'string'},
            'attributes': {
                'type': 'object',
                'additionalProperties': True,
                'title': 'Image Attributes',
                'description': 'Subjective things that apply to the entire '
                               'image.'
            },
            'elements': {
                'type': 'array',
                'items': annotationElementSchema,
                # We want to ensure unique element IDs, if they are set.  If
                # they are not set, we assign them from Mongo.
                'title': 'Image Markup',
                'description': 'Subjective things that apply to a '
                               'spatial region.'
            }
        },
        'additionalProperties': False
    }


class Annotation(AccessControlledModel):
    """
    This model is used to represent an annotation that is associated with an
    item.  The annotation can contain any number of annotationelements, which
    are included because they reference this annoation as a parent.  The
    annotation acts like these are a native part of it, though they are each
    stored as independent models to (eventually) permit faster spatial
    searching.
    """

    validatorAnnotation = jsonschema.Draft4Validator(
        AnnotationSchema.annotationSchema)
    validatorAnnotationElement = jsonschema.Draft4Validator(
        AnnotationSchema.annotationElementSchema)
    idRegex = re.compile('^[0-9a-f]{24}$')
    numberInstance = six.integer_types + (float, )

    class Skill(enum.Enum):
        NOVICE = 'novice'
        EXPERT = 'expert'

    # This is everything except the annotation field, and is used, in part, to
    # determine what gets returned in a general find.
    baseFields = (
        '_id',
        'itemId',
        'creatorId',
        'created',
        'updated',
        'updatedId',
        'public',
        'publicFlags',
        'groups'
        # 'skill',
        # 'startTime'
        # 'stopTime'
    )

    def initialize(self):
        self._writeLock = threading.Lock()
        self.name = 'annotation'
        self.ensureIndices([
            'itemId',
            'created',
            'creatorId',
            ([
                ('itemId', SortDir.ASCENDING),
                ('_active', SortDir.ASCENDING),
            ], {}),
            ([
                ('_annotationId', SortDir.ASCENDING),
                ('_version', SortDir.DESCENDING),
            ], {})
        ])
        self.ensureTextIndex({
            'annotation.name': 10,
            'annotation.description': 1,
        })

        self.exposeFields(AccessType.READ, (
            'annotation', '_version', '_elementQuery', '_active',
        ) + self.baseFields)
        events.bind('model.item.remove', 'large_image', self._onItemRemove)
        events.bind('model.item.copy.prepare', 'large_image.annotation', self._prepareCopyItem)
        events.bind('model.item.copy.after', 'large_image.annotation', self._handleCopyItem)

        self._historyEnabled = Setting().get(
            constants.PluginSettings.LARGE_IMAGE_ANNOTATION_HISTORY)
        # Listen for changes to our relevant settings
        events.bind('model.setting.save.after', 'large_image', self._onSettingChange)
        events.bind('model.setting.remove', 'large_image', self._onSettingChange)

    def _onItemRemove(self, event):
        """
        When an item is removed, also delete associated annotations.

        :param event: the event with the item information.
        """
        item = event.info
        annotations = Annotation().find({'itemId': item['_id']})
        for annotation in annotations:
            if self._historyEnabled:
                # just mark the annotations as inactive
                self.update({'_id': annotation['_id']}, {'$set': {'_active': False}})
            else:
                Annotation().remove(annotation)

    def _prepareCopyItem(self, event):
        # check if this copy should include annotations
        if (cherrypy.request and cherrypy.request.params and
                str(cherrypy.request.params.get('copyAnnotations')).lower() == 'false'):
            return
        srcItem, newItem = event.info
        if Annotation().findOne({
                '_active': {'$ne': False}, 'itemId': srcItem['_id']}):
            newItem['_annotationItemId'] = srcItem['_id']
            Item().save(newItem, triggerEvents=False)

    def _handleCopyItem(self, event):
        newItem = event.info
        srcItemId = newItem.pop('_annotationItemId', None)
        if srcItemId:
            Item().save(newItem, triggerEvents=False)
            self._copyAnnotationsFromOtherItem(srcItemId, newItem)

    def _copyAnnotationsFromOtherItem(self, srcItemId, destItem):
        # Copy annotations from the original item to this one
        query = {'_active': {'$ne': False}, 'itemId': srcItemId}
        annotations = Annotation().find(query)
        total = annotations.count()
        if not total:
            return
        destItemId = destItem['_id']
        folder = Folder().load(destItem['folderId'], force=True)
        count = 0
        for annotation in annotations:
            logger.info('Copying annotation %d of %d from %s to %s',
                        count + 1, total, srcItemId, destItemId)
            # Make sure we have the elements
            annotation = Annotation().load(annotation['_id'], force=True)
            # This could happen, for instance, if the annotation were deleted
            # while we are copying other annotations.
            if annotation is None:
                continue
            annotation['itemId'] = destItemId
            del annotation['_id']
            # Remove existing permissionsi, then give it the same permissions
            # as the item's folder.
            annotation.pop('access', None)
            self.copyAccessPolicies(destItem, annotation, save=False)
            self.setPublic(annotation, folder.get('public'), save=False)
            Annotation().save(annotation)
            count += 1
        logger.info('Copied %d annotations from %s to %s ',
                    count, srcItemId, destItemId)

    def _onSettingChange(self, event):
        settingDoc = event.info
        if settingDoc['key'] in (
                constants.PluginSettings.LARGE_IMAGE_ANNOTATION_HISTORY, ):
            self._historyEnabled = Setting().get(
                constants.PluginSettings.LARGE_IMAGE_ANNOTATION_HISTORY)

    def _loadAndMigrateAnnotation(self, id, *args, **kwargs):
        """
        Load the annotation and add access control information if necessary.
        """
        # We first need to load the full annotation document to perform the
        # migration.
        annotation = self._migrateACL(super(Annotation, self).load(id, force=True))

        # If force is not provided, we can do an access control check without
        # reloading the model.
        if annotation is not None and not kwargs.get('force'):
            self.requireAccess(
                annotation, kwargs.get('user'), kwargs.get('level', AccessType.ADMIN)
            )

        # If any other keyword arguments were provided, we reload the model to avoid
        # having to duplicate code from super class methods.  Since these flags are
        # rarely used the performance implications should be minimal.
        if kwargs.get('objectId') or kwargs.get('fields') or kwargs.get('exc'):
            annotation = super(Annotation, self).load(id, *args, **kwargs)

        return annotation

    def _migrateACL(self, annotation):
        """
        Add access control information to an annotation model.

        Originally annotation models were not access controlled.  This function performs
        the migration for annotations created before this change was made.  The access
        object is copied from the folder containing the image the annotation is attached
        to.   In addition, the creator is given admin access.
        """
        if annotation is None or 'access' in annotation:
            return annotation

        item = Item().load(annotation['itemId'], force=True)
        if item is None:
            logger.warning(
                'Could not generate annotation ACL due to missing item %s', annotation['_id'])
            return annotation

        folder = Folder().load(item['folderId'], force=True)
        if folder is None:
            logger.warning(
                'Could not generate annotation ACL due to missing folder %s', annotation['_id'])
            return annotation

        user = User().load(annotation['creatorId'], force=True)
        if user is None:
            logger.warning(
                'Could not generate annotation ACL %s due to missing user %s', annotation['_id'])
            return annotation

        self.copyAccessPolicies(item, annotation, save=False)
        self.setUserAccess(annotation, user, AccessType.ADMIN, force=True, save=False)
        self.setPublic(annotation, folder.get('public'), save=False)

        # call the super class save method to avoid messing with elements
        super(Annotation, self).save(annotation)
        logger.info('Generated annotation ACL for %s', annotation['_id'])
        return annotation

    def createAnnotation(self, item, creator, annotation, public=None):
        now = datetime.datetime.utcnow()
        doc = {
            'itemId': item['_id'],
            'creatorId': creator['_id'],
            'created': now,
            'updatedId': creator['_id'],
            'updated': now,
            'annotation': annotation,
        }

        # copy access control from the folder containing the image
        folder = Folder().load(item['folderId'], force=True)
        self.copyAccessPolicies(src=folder, dest=doc, save=False)

        if public is None:
            public = folder.get('public', False)
        self.setPublic(doc, public, save=False)

        # give the current user admin access
        self.setUserAccess(doc, user=creator, level=AccessType.ADMIN, save=False)

        return self.save(doc)

    def load(self, id, region=None, getElements=True, *args, **kwargs):
        """
        Load an annotation, adding all or a subset of the elements to it.

        :param region: if present, a dictionary restricting which annotations
            are returned.  See annotationelement.getElements.
        :param getElements: if False, don't get elements associated with this
            annotation.
        :returns: the matching annotation or none.
        """
        # This call can be replaced with the superclass load method if migrations
        # are no longer necessary.
        annotation = self._loadAndMigrateAnnotation(id, *args, **kwargs)

        if annotation is None:
            return

        if getElements:
            # It is possible that we are trying to read the elements of an
            # annotation as another thread is updating them.  In this case,
            # there is a chance, that between when we get the annotation and
            # ask for the elements, the version will have been updated and the
            # elements will have gone away.  To work around the lack of
            # transactions in Mongo, if we don't get any elements, we check if
            # the version has shifted under us, and, if so, requery.  I've put
            # an arbitrary retry limit on this to prevent an infinite loop.
            maxRetries = 3
            for retry in range(maxRetries):
                Annotationelement().getElements(
                    annotation, region)
                if (len(annotation.get('annotation', {}).get('elements')) or
                        retry + 1 == maxRetries):
                    break
                recheck = super(Annotation, self).load(id, *args, **kwargs)
                if (recheck is None or
                        annotation.get('_version') == recheck.get('_version')):
                    break
                annotation = recheck

        self.injectAnnotationGroupSet(annotation)
        return annotation

    def remove(self, annotation, *args, **kwargs):
        """
        When removing an annotation, remove all element associated with it.
        This overrides the collection delete_one method so that all of the
        triggers are fired as expectd and cancelling from an event will work
        as needed.

        :param annotation: the annotation document to remove.
        """
        if self._historyEnabled:
            # just mark the annotations as inactive
            result = self.update({'_id': annotation['_id']}, {'$set': {'_active': False}})
        else:
            delete_one = self.collection.delete_one

            def deleteElements(query, *args, **kwargs):
                ret = delete_one(query, *args, **kwargs)
                Annotationelement().removeElements(annotation)
                return ret

            with self._writeLock:
                self.collection.delete_one = deleteElements
                try:
                    result = super(Annotation, self).remove(annotation, *args, **kwargs)
                finally:
                    self.collection.delete_one = delete_one
        return result

    def save(self, annotation, *args, **kwargs):
        """
        When saving an annotation, override the collection insert_one and
        replace_one methods so that we don't save the elements with the main
        annotation.  Still use the super class's save method, so that all of
        the triggers are fired as expected and cancelling and modifications can
        be done as needed.

        Because Mongo doesn't support transactions, a version number is stored
        with the annotation and with the associated elements.  This is used to
        add the new elements first, then update the annotation, and delete the
        old elements.  The allows version integrity if another thread queries
        the annotation at the same time.

        :param annotation: the annotation document to save.
        :returns: the saved document.  If it is a new document, the _id has
                  been added.
        """
        starttime = time.time()
        with self._writeLock:
            replace_one = self.collection.replace_one
            insert_one = self.collection.insert_one
        version = Annotationelement().getNextVersionValue()
        if '_id' not in annotation:
            oldversion = None
        else:
            if '_annotationId' in annotation:
                annotation['_id'] = annotation['_annotationId']
            # We read the old version from the existing record, because we
            # don't want to trust that the input _version has not been altered
            # or is present.
            oldversion = self.collection.find_one(
                {'_id': annotation['_id']}).get('_version')
        annotation['_version'] = version
        _elementQuery = annotation.pop('_elementQuery', None)
        annotation.pop('_active', None)
        annotation.pop('_annotationId', None)

        def replaceElements(query, doc, *args, **kwargs):
            Annotationelement().updateElements(doc)
            elements = doc['annotation'].pop('elements', None)
            if self._historyEnabled:
                oldAnnotation = self.collection.find_one(query)
                if oldAnnotation:
                    oldAnnotation['_annotationId'] = oldAnnotation.pop('_id')
                    oldAnnotation['_active'] = False
                    insert_one(oldAnnotation)
            ret = replace_one(query, doc, *args, **kwargs)
            if elements:
                doc['annotation']['elements'] = elements
            if not self._historyEnabled:
                Annotationelement().removeOldElements(doc, oldversion)
            return ret

        def insertElements(doc, *args, **kwargs):
            # When creating an annotation, store the elements first, then store
            # the annotation without elements, then restore the elements.
            doc.setdefault('_id', ObjectId())
            if doc['annotation'].get('elements') is not None:
                Annotationelement().updateElements(doc)
            # If we are inserting, we shouldn't have any old elements, so don't
            # bother removing them.
            elements = doc['annotation'].pop('elements', None)
            ret = insert_one(doc, *args, **kwargs)
            if elements is not None:
                doc['annotation']['elements'] = elements
            return ret

        with self._writeLock:
            self.collection.replace_one = replaceElements
            self.collection.insert_one = insertElements
            try:
                result = super(Annotation, self).save(annotation, *args, **kwargs)
            finally:
                self.collection.replace_one = replace_one
                self.collection.insert_one = insert_one
        if _elementQuery:
            result['_elementQuery'] = _elementQuery

        annotation.pop('groups', None)
        self.injectAnnotationGroupSet(annotation)

        logger.debug('Saved annotation in %5.3fs' % (time.time() - starttime))
        events.trigger('large_image.annotations.save_history', {
            'annotation': annotation
        }, async=True)
        return result

    def updateAnnotation(self, annotation, updateUser=None):
        """
        Update an annotation.

        :param annotation: the annotation document to update.
        :param updateUser: the user who is creating the update.
        :returns: the annotation document that was updated.
        """
        annotation['updated'] = datetime.datetime.utcnow()
        annotation['updatedId'] = updateUser['_id'] if updateUser else None
        return self.save(annotation)

    def _similarElementStructure(self, a, b, parentKey=None):  # noqa
        """
        Compare two elements to determine if they are similar enough that if
        one validates, the other should, too.  This is called recursively to
        validate dictionaries.  In general, types must be the same,
        dictionaries must contain the same keys, arrays must be the same
        length.  The only differences that are allowed are numerical values may
        be different, ids may be different, and point arrays may contain
        different numbers of elements.

        :param a: first element
        :param b: second element
        :param parentKey: if set, the key of the dictionary that used for this
            part of the comparison.
        :returns: True if the elements are similar.  False if they are not.
        """
        # This function exceeds the recommended complexity, but since it is
        # needs to be relatively fast, breaking it into smaller functions is
        # probably undesireable.
        if type(a) != type(b):
            return False
        if isinstance(a, dict):
            if len(a) != len(b):
                return False
            for k in a:
                if k not in b:
                    return False
                if k == 'id':
                    if not isinstance(b[k], six.string_types) or not self.idRegex.match(b[k]):
                        return False
                elif parentKey != 'label' or k != 'value':
                    if not self._similarElementStructure(a[k], b[k], k):
                        return False
        elif isinstance(a, list):
            if len(a) != len(b):
                if parentKey != 'points' or len(a) < 3 or len(b) < 3:
                    return False
                # If this is an array of points, let it pass
                for idx in range(len(b)):
                    if (len(b[idx]) != 3 or
                            not isinstance(b[idx][0], self.numberInstance) or
                            not isinstance(b[idx][1], self.numberInstance) or
                            not isinstance(b[idx][2], self.numberInstance)):
                        return False
                return True
            for idx in range(len(a)):
                if not self._similarElementStructure(a[idx], b[idx], parentKey):
                    return False
        elif not isinstance(a, self.numberInstance):
            return a == b
        # Either a number or the dictionary or list comparisons passed
        return True

    def validate(self, doc):
        starttime = time.time()
        try:
            # This block could just use the json validator:
            #   jsonschema.validate(doc.get('annotation'),
            #                       AnnotationSchema.annotationSchema)
            # but this is very slow.  Instead, validate the main structure and
            # then validate each element.  If sequential elements are similar
            # in structure, skip validating them.
            annot = doc.get('annotation')
            elements = annot.get('elements', [])
            annot['elements'] = []
            self.validatorAnnotation.validate(annot)
            lastValidatedElement = None
            for element in elements:
                if isinstance(element.get('id'), ObjectId):
                    element['id'] = str(element['id'])
                if not self._similarElementStructure(element, lastValidatedElement):
                    self.validatorAnnotationElement.validate(element)
                    lastValidatedElement = element
            annot['elements'] = elements
        except jsonschema.ValidationError as exp:
            raise ValidationException(exp)
        logger.debug('Validated in %5.3fs' % (time.time() - starttime))
        elementIds = [entry['id'] for entry in
                      doc['annotation'].get('elements', []) if 'id' in entry]
        if len(set(elementIds)) != len(elementIds):
            raise ValidationException('Annotation Element IDs are not unique')
        return doc

    def versionList(self, annotationId, user=None, limit=0, offset=0,
                    sort=[('_version', -1)], force=False):
        """
        List annotation history entries for a specific annotationId.  Only
        annotations that belong to an existing item that the user is allowed to
        view are included.  If the user is an admin, all annotations will be
        included.

        :param annotationId: the annotation to get history for.
        :param user: the Girder user.
        :param limit: maximum number of history entries to return.
        :param offset: skip this many entries.
        :param sort: the sort method used.  Defaults to reverse _id.
        :param force: if True, don't authenticate the user.
        :yields: the entries in the list
        """
        if annotationId and not isinstance(annotationId, ObjectId):
            annotationId = ObjectId(annotationId)
        # Make sure we have only one of each version, plus apply our filter and
        # sort.  Don't apply limit and offset here, as they are subject to
        # access control and other effects
        entries = self.collection.aggregate([
            {'$match': {'$or': [{'_id': annotationId}, {'_annotationId': annotationId}]}},
            {'$group': {'_id': '$_version', '_doc': {'$first': '$$ROOT'}}},
            {'$replaceRoot': {'newRoot': '$_doc'}},
            {'$sort': {s[0]: s[1] for s in sort}}])
        if not force:
            entries = self.filterResultsByPermission(
                cursor=entries, user=user, level=AccessType.READ,
                limit=limit, offset=offset)
        return entries

    def getVersion(self, annotationId, version, user=None, force=False, *args, **kwargs):
        """
        Get an annotation history version.  This reconstructs the original
        annotation.

        :param annotationId: the annotation to get history for.
        :param version: the specific version to get.
        :param user: the Girder user.  If the user is not an admin, they must
            have read access on the item and the item must exist.
        :param force: if True, don't get the user access.
        """
        if annotationId and not isinstance(annotationId, ObjectId):
            annotationId = ObjectId(annotationId)
        entry = self.findOne({
            '$or': [{'_id': annotationId}, {'_annotationId': annotationId}],
            '_version': int(version)
        }, fields=['_id'])
        if not entry:
            return None
        result = self.load(entry['_id'], user=user, force=force, *args, **kwargs)
        result['_versionId'] = result['_id']
        result['_id'] = result.pop('annotationId', result['_id'])
        return result

    def revertVersion(self, id, version=None, user=None, force=False):
        """
        Revert to a previous version of an annotation.

        :param id: the annotation id.
        :param version: the version to revert to.  None reverts to the previous
            version.  If the annotation was deleted, this is the most recent
            version.
        :param user: the user doing the reversion.
        :param force: if True don't authenticate the user with the associated
            item access.
        """
        if version is None:
            oldVersions = list(Annotation().versionList(id, limit=2, force=True))
            if len(oldVersions) >= 1 and oldVersions[0].get('_active') is False:
                version = oldVersions[0]['_version']
            elif len(oldVersions) >= 2:
                version = oldVersions[1]['_version']
        annotation = Annotation().getVersion(id, version, user, force=force)
        if annotation is None:
            return
        # If this is the most recent (active) annotation, don't do anything.
        # Otherwise, revert it.
        if not annotation.get('_active', True):
            if not force:
                self.requireAccess(annotation, user=user, level=AccessType.WRITE)
            annotation = Annotation().updateAnnotation(annotation, updateUser=user)
        return annotation

    def findAnnotatedImages(self, imageNameFilter=None, creator=None,
                            user=None, level=AccessType.ADMIN, force=None,
                            offset=0, limit=0, sort=None, **kwargs):
        r"""
        Find images associated with annotations.

        The list returned by this function is paginated and filtered by access control using
        the standard girder kwargs.

        :param imageNameFilter: A string used to filter images by name.  An image name matches
            if it (or a subtoken) begins with this string.  Subtokens are generated by splitting
            by the regex ``[\W_]+``  This filter is case-insensitive.
        :param creator: Filter by a user who is the creator of the annotation.
        """
        query = {'_active': {'$ne': False}}
        if creator:
            query['creatorId'] = creator['_id']

        annotations = self.find(
            query, sort=sort, fields=['itemId'])

        images = []
        imageIds = set()
        for annotation in annotations:
            # short cut if the image has already been added to the results
            if annotation['itemId'] in imageIds:
                continue

            try:
                item = ImageItem().load(annotation['itemId'], level=level, user=user, force=force)
            except AccessException:
                item = None

            # ignore if no such item exists
            if not item:
                continue

            if not self._matchImageName(item['name'], imageNameFilter or ''):
                continue

            if len(imageIds) >= offset:
                images.append(item)

            imageIds.add(item['_id'])
            if len(images) == limit:
                break
        return images

    def _matchImageName(self, imageName, matchString):
        matchString = matchString.lower()
        imageName = imageName.lower()
        if imageName.startswith(matchString):
            return True
        tokens = re.split(r'[\W_]+', imageName, flags=re.UNICODE)
        for token in tokens:
            if token.startswith(matchString):
                return True
        return False

    def injectAnnotationGroupSet(self, annotation):
        if 'groups' not in annotation:
            annotation['groups'] = Annotationelement().getElementGroupSet(annotation)
            query = {
                '_id': ObjectId(annotation['_id'])
            }
            update = {
                '$set': {
                    'groups': annotation['groups']
                }
            }
            self.collection.update_one(query, update)
        return annotation
