# Copyright (C) 2015-2018 OpenIO SAS, as part of OpenIO SDS
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.


from __future__ import absolute_import
from six import string_types

from io import BytesIO
from functools import wraps, partial
import os
import warnings
import time
import random

try:
    from urllib.parse import quote_plus, unquote_plus
except ImportError:
    from urllib import quote_plus, unquote_plus

from oio.common import exceptions as exc
from oio.api.ec import ECWriteHandler
from oio.api.io import MetachunkPreparer
from oio.api.replication import ReplicatedWriteHandler
from oio.api.backblaze_http import BackblazeUtilsException, BackblazeUtils
from oio.api.backblaze import BackblazeWriteHandler, \
    BackblazeChunkDownloadHandler
from oio.common.utils import cid_from_name, GeneratorIO, monotonic_time
from oio.common.easy_value import float_value, true_value
from oio.common.logger import get_logger
from oio.common.decorators import ensure_headers, ensure_request_id
from oio.common.storage_method import STORAGE_METHODS
from oio.common.constants import OIO_VERSION, CHUNK_HEADERS, HEADER_PREFIX
from oio.common.decorators import handle_account_not_found, \
    handle_container_not_found, handle_object_not_found
from oio.common.storage_functions import _sort_chunks, fetch_stream, \
    fetch_stream_ec


# TODO(FVE): decorate more methods
def patch_kwargs(fnc):
    """
    Patch keyword arguments with the ones passed to the class' constructor.
    """
    @wraps(fnc)
    def _patch_kwargs(self, *args, **kwargs):
        for argk, argv in self._global_kwargs.items():
            if argk not in kwargs:
                kwargs[argk] = argv
        return fnc(self, *args, **kwargs)
    return _patch_kwargs


class ObjectStorageApi(object):
    """
    The Object Storage API.

    High level API that wraps `AccountClient`, `ContainerClient` and
    `DirectoryClient` classes.

    Every method that takes a `kwargs` argument accepts the at least
    the following keywords:

        - `headers`: `dict` of extra headers to pass to the proxy
        - `connection_timeout`: `float`
        - `read_timeout`: `float`
        - `write_timeout`: `float`
    """
    TIMEOUT_KEYS = ('connection_timeout', 'read_timeout', 'write_timeout')
    EXTRA_KEYWORDS = ('chunk_checksum_algo')

    def __init__(self, namespace, logger=None, **kwargs):
        """
        Initialize the object storage API.

        :param namespace: name of the namespace to interract with
        :type namespace: `str`

        :keyword connection_timeout: connection timeout towards proxy and
            rawx services
        :type connection_timeout: `float` seconds
        :keyword read_timeout: timeout for rawx responses and data reads from
            the caller (when uploading), and metadata requests
        :type read_timeout: `float` seconds
        :keyword write_timeout: timeout for rawx write requests
        :type write_timeout: `float` seconds
        :keyword pool_manager: a pooled connection manager that will be used
            for all HTTP based APIs (except rawx)
        :type pool_manager: `urllib3.PoolManager`
        :keyword chunk_checksum_algo: algorithm to use for chunk checksums.
            Only 'md5' and `None` are supported at the moment.
        """
        self.namespace = namespace
        conf = {"namespace": self.namespace}
        self.logger = logger or get_logger(conf)
        self._global_kwargs = {tok: float_value(tov, None)
                               for tok, tov in kwargs.items()
                               if tok in self.__class__.TIMEOUT_KEYS}
        for key in self.__class__.EXTRA_KEYWORDS:
            if key in kwargs:
                self._global_kwargs[key] = kwargs[key]

        from oio.account.client import AccountClient
        from oio.container.client import ContainerClient
        from oio.directory.client import DirectoryClient
        self.directory = DirectoryClient(conf, logger=self.logger, **kwargs)
        self.container = ContainerClient(conf, logger=self.logger, **kwargs)

        # In AccountClient, "endpoint" is the account service, not the proxy
        acct_kwargs = kwargs.copy()
        acct_kwargs["proxy_endpoint"] = acct_kwargs.pop("endpoint", None)
        self.account = AccountClient(conf, logger=self.logger, **acct_kwargs)
        self._blob_client = None
        self._proxy_client = None

    @property
    def blob_client(self):
        """
        A low-level client to rawx services.

        :rtype: `oio.blob.client.BlobClient`
        """
        if self._blob_client is None:
            from oio.blob.client import BlobClient
            perfdata = self.container.perfdata
            connection_pool = self.container.pool_manager
            self._blob_client = BlobClient(
                connection_pool=connection_pool, perfdata=perfdata)
        return self._blob_client

    # FIXME(FVE): this method should not exist
    # This high-level API should use lower-level APIs,
    # not do request directly to the proxy.
    @property
    def proxy_client(self):
        if self._proxy_client is None:
            from oio.common.client import ProxyClient
            conf = self.container.conf
            pool_manager = self.container.pool_manager
            self._proxy_client = ProxyClient(
                conf, pool_manager=pool_manager, no_ns_in_url=True,
                logger=self.logger)
        return self._proxy_client

    @ensure_headers
    @ensure_request_id
    def account_create(self, account, **kwargs):
        """
        Create an account.

        :param account: name of the account to create
        :type account: `str`
        :returns: `True` if the account has been created
        """
        return self.account.account_create(account, **kwargs)

    @handle_account_not_found
    @ensure_headers
    @ensure_request_id
    def account_delete(self, account, **kwargs):
        """
        Delete an account.

        :param account: name of the account to delete
        :type account: `str`
        """
        self.account.account_delete(account, **kwargs)

    @handle_account_not_found
    @ensure_headers
    @ensure_request_id
    def account_show(self, account, **kwargs):
        """
        Get information about an account.
        """
        return self.account.account_show(account, **kwargs)

    @ensure_headers
    @ensure_request_id
    def account_list(self, **kwargs):
        """
        List known accounts.

        Notice that account creation is asynchronous, and an autocreated
        account may appear in the listing only after several seconds.
        """
        return self.account.account_list(**kwargs)

    @handle_account_not_found
    def account_update(self, account, metadata, to_delete=None, **kwargs):
        warnings.warn("You'd better use account_set_properties()",
                      DeprecationWarning, stacklevel=2)
        self.account.account_update(account, metadata, to_delete, **kwargs)

    @handle_account_not_found
    @ensure_headers
    @ensure_request_id
    def account_set_properties(self, account, properties, **kwargs):
        self.account.account_update(account, properties, None, **kwargs)

    @handle_account_not_found
    @ensure_headers
    @ensure_request_id
    def account_del_properties(self, account, properties, **kwargs):
        self.account.account_update(account, None, properties, **kwargs)

    @ensure_headers
    @ensure_request_id
    def container_create(self, account, container, properties=None,
                         **kwargs):
        """
        Create a container.

        :param account: account in which to create the container
        :type account: `str`
        :param container: name of the container
        :type container: `str`
        :param properties: properties to set on the container
        :type properties: `dict`
        :returns: True if the container has been created,
                  False if it already exists
        """
        return self.container.container_create(
            account, container, properties=properties, **kwargs)

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_touch(self, account, container, **kwargs):
        """
        Trigger a notification about the container state.

        :param account: account from which to delete the container
        :type account: `str`
        :param container: name of the container
        :type container: `str`
        """
        self.container.container_touch(account, container, **kwargs)

    @ensure_headers
    @ensure_request_id
    def container_create_many(self, account, containers, properties=None,
                              **kwargs):
        """
        Create Many containers

        :param account: account in which to create the containers
        :type account: `str`
        :param containers: names of the containers
        :type containers: `list`
        :param properties: properties to set on the containers
        :type properties: `dict`
        """
        return self.container.container_create_many(
            account, containers, properties=properties, **kwargs)

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_delete(self, account, container, **kwargs):
        """
        Delete a container.

        :param account: account from which to delete the container
        :type account: `str`
        :param container: name of the container
        :type container: `str`
        """
        self.container.container_delete(account, container, **kwargs)

    @handle_account_not_found
    @ensure_headers
    @ensure_request_id
    def container_list(self, account, limit=None, marker=None,
                       end_marker=None, prefix=None, delimiter=None,
                       **kwargs):
        """
        Get the list of containers of an account.

        :param account: account from which to get the container list
        :type account: `str`
        :keyword limit: maximum number of results to return
        :type limit: `int`
        :keyword marker: name of the container from where to start the listing
        :type marker: `str`
        :keyword end_marker:
        :keyword prefix:
        :keyword delimiter:
        :return: the list of containers of an account
        :rtype: `list` of items (`list`) with 5 fields:
            name, number of objects, number of bytes, 1 if the item
            is a prefix or 0 if the item is actually a container,
            and modification time.
        """
        resp = self.account.container_list(account, limit=limit,
                                           marker=marker,
                                           end_marker=end_marker,
                                           prefix=prefix,
                                           delimiter=delimiter,
                                           **kwargs)
        return resp["listing"]

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_show(self, account, container, **kwargs):
        """
        Get information about a container (user properties).

        :param account: account in which the container is
        :type account: `str`
        :param container: name of the container
        :type container: `str`
        :returns: a `dict` with "properties" containing a `dict`
            of user properties.
        """
        return self.container.container_show(account, container, **kwargs)

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_snapshot(self, account, container, dst_account,
                           dst_container, batch=100, **kwargs):
        """
        Create a copy of the container (only the content of the database)

        :param account: account in which the target is
        :type account: `str`
        :param container: name of the target
        :type container: `str`
        :param dst_account: account in which the snapshot will be.
        :type dst_account: `str`
        :param dst_container: name of the snapshot
        :type dst_container: `str`
        """
        try:
            self.container.container_freeze(account, container, **kwargs)
            self.container.container_snapshot(
                account, container, dst_account, dst_container, **kwargs)
            resp = self.object_list(dst_account, dst_container, **kwargs)
            obj_gen = resp['objects']
            target_beans = []
            copy_beans = []
            for obj in obj_gen:
                data = self.object_locate(
                    account, container, obj["name"], **kwargs)
                chunks = [chunk['url'] for chunk in data[1]]
                copies = self._generate_copies(chunks)
                fullpath = self._generate_fullpath(
                    dst_account, dst_container, obj['name'], obj['version'])
                self._link_chunks(chunks, copies, fullpath[0], **kwargs)
                t_beans, c_beans = self._prepare_meta2_raw_update(
                    data[1], copies, obj['content'])
                target_beans.extend(t_beans)
                copy_beans.extend(c_beans)
                if len(target_beans) > batch:
                    self.container.container_raw_update(
                        target_beans, copy_beans,
                        dst_account, dst_container,
                        frozen=True, **kwargs)
                    target_beans = []
                    copy_beans = []
            if target_beans:
                self.container.container_raw_update(
                    target_beans, copy_beans,
                    dst_account, dst_container,
                    frozen=True, **kwargs)
        finally:
            self.container.container_enable(account, container, **kwargs)

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_get_properties(self, account, container, properties=None,
                                 **kwargs):
        """
        Get information about a container (user and system properties).

        :param account: account in which the container is
        :type account: `str`
        :param container: name of the container
        :type container: `str`
        :param properties: *ignored*
        :returns: a `dict` with "properties" and "system" entries,
            containing respectively a `dict` of user properties and
            a `dict` of system properties.
        """
        return self.container.container_get_properties(account, container,
                                                       properties=properties,
                                                       **kwargs)

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_set_properties(self, account, container, properties=None,
                                 clear=False, **kwargs):
        """
        Set properties on a container.

        :param account: name of the account
        :type account: `str`
        :param container: name of the container where to set properties
        :type container: `str`
        :param properties: a dictionary of properties
        :type properties: `dict`
        :param clear:
        :type clear: `bool`
        :keyword system: dictionary of system properties to set
        """
        return self.container.container_set_properties(
            account, container, properties,
            clear=clear, **kwargs)

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_del_properties(self, account, container, properties,
                                 **kwargs):
        """
        Delete properties of a container.

        :param account: name of the account
        :type account: `str`
        :param container: name of the container to deal with
        :type container: `str`
        :param properties: a list of property keys
        :type properties: `list`
        """
        return self.container.container_del_properties(
            account, container, properties, **kwargs)

    def _delete_exceeding_versions(self, account, container, obj,
                                   versions, maxvers, **kwargs):
        if not versions:
            return
        exceeding_versions = versions[maxvers:]
        for version in exceeding_versions:
            self.object_delete(account, container, obj, version=version,
                               **kwargs)

    @handle_container_not_found
    def container_purge(self, account, container, maxvers=None, **kwargs):
        if maxvers is None:
            props = self.container_get_properties(account, container, **kwargs)
            maxvers = props['system'].get('sys.m2.policy.version', None)
            if maxvers is None:
                _, data = self.proxy_client._request('GET', "config")
                maxvers = data['meta2.max_versions']
        maxvers = int(maxvers)
        if maxvers < 0:
            return
        elif maxvers == 0:
            maxvers = 1

        truncated = True
        marker = None
        last_object_name = None
        versions = None
        while truncated:
            resp = self.object_list(account, container, versions=True,
                                    marker=marker, **kwargs)
            # FIXME `next_marker` is an object name.
            # `object_list` can send twice the same version
            # if there weren't all versions for the last object.
            last_object_name = None
            truncated = resp['truncated']
            marker = resp.get('next_marker', None)
            for obj in resp['objects']:
                if obj['name'] != last_object_name:
                    self._delete_exceeding_versions(
                        account, container, last_object_name, versions,
                        maxvers, **kwargs)
                    last_object_name = obj['name']
                    versions = list()
                if not obj['deleted']:
                    versions.append(obj['version'])
        self._delete_exceeding_versions(
            account, container, last_object_name, versions, maxvers, **kwargs)

    def container_update(self, account, container, metadata, clear=False,
                         **kwargs):
        """
        :deprecated: use `container_set_properties`
        """
        warnings.warn("You'd better use container_set_properties()",
                      DeprecationWarning)
        if not metadata:
            self.container_del_properties(
                account, container, [], **kwargs)
        else:
            self.container_set_properties(
                account, container, metadata, clear, **kwargs)

    @handle_container_not_found
    @patch_kwargs
    @ensure_headers
    @ensure_request_id
    def object_create(self, account, container, file_or_path=None, data=None,
                      etag=None, obj_name=None, mime_type=None,
                      metadata=None, policy=None, key_file=None,
                      append=False, properties=None, **kwargs):
        """
        Create an object or append data to object in *container* of *account*
        with data taken from either *data* (`str` or `generator`) or
        *file_or_path* (path to a file or file-like object).
        The object will be named after *obj_name* if specified, or after
        the base name of *file_or_path*.

        :param account: name of the account where to create the object
        :type account: `str`
        :param container: name of the container where to create the object
        :type container: `str`
        :param file_or_path: file-like object or path to a file from which
            to read object data
        :type file_or_path: `str` or file-like object
        :param data: object data (if `file_or_path` is not set)
        :type data: `str` or `generator`
        :keyword etag: entity tag of the object
        :type etag: `str`
        :keyword obj_name: name of the object to create. If not set, will use
            the base name of `file_or_path`.
        :keyword mime_type: MIME type of the object
        :type mime_type: `str`
        :keyword properties: a dictionary of properties
        :type properties: `dict`
        :keyword policy: name of the storage policy
        :type policy: `str`
        :keyword key_file:
        :param append: if set, data will be append to existing object (or
            object will be created if unset)
        :type append: `bool`

        :keyword perfdata: optional `dict` that will be filled with metrics
            of time spent to resolve the meta2 address, to do the meta2
            requests, and to upload chunks to rawx services.
        :keyword deadline: deadline for the request, in monotonic time
            (`oio.common.utils.monotonic_time`). This supersedes `timeout`
            or `read_timeout` keyword arguments.
        :type deadline: `float` seconds

        :returns: `list` of chunks, size and hash of the what has been uploaded
        """
        if (data, file_or_path) == (None, None):
            raise exc.MissingData()
        src = data if data is not None else file_or_path
        if src is file_or_path:
            # We are asked to read from a file path or a file-like object
            if isinstance(file_or_path, string_types):
                if not os.path.exists(file_or_path):
                    raise exc.FileNotFound("File '%s' not found." %
                                           file_or_path)
                file_name = os.path.basename(file_or_path)
            else:
                try:
                    file_name = os.path.basename(file_or_path.name)
                except AttributeError:
                    file_name = None
            obj_name = obj_name or file_name
        else:
            # We are asked to read from a buffer or an iterator
            try:
                src = BytesIO(src)
            except TypeError:
                src = GeneratorIO(src)

        if not obj_name:
            raise exc.MissingName(
                "No name for the object has been specified"
            )

        sysmeta = {'mime_type': mime_type,
                   'etag': etag}
        if metadata:
            warnings.warn(
                "You'd better use 'properties' instead of 'metadata'",
                DeprecationWarning, stacklevel=4)
            if not properties:
                properties = metadata
            else:
                properties.update(metadata)

        if isinstance(src, BytesIO):
            return self._object_create(
                account, container, obj_name, src, sysmeta,
                properties=properties, policy=policy,
                key_file=key_file, append=append, **kwargs)
        elif hasattr(src, 'read'):
            return self._object_create(
                account, container, obj_name, src, sysmeta,
                properties=properties, policy=policy, key_file=key_file,
                append=append, **kwargs)
        else:
            with open(src, 'rb') as srcf:
                return self._object_create(
                    account, container, obj_name, srcf, sysmeta,
                    properties=properties, policy=policy,
                    key_file=key_file, append=append, **kwargs)

    @ensure_headers
    @ensure_request_id
    def object_touch(self, account, container, obj,
                     version=None, **kwargs):
        """
        Trigger a notification about an object
        (as if it just had been created).

        :param account: name of the account where to create the object
        :type account: `str`
        :param container: name of the container where to create the object
        :type container: `str`
        :param obj: name of the object to touch
        """
        self.container.content_touch(account, container, obj,
                                     version=version, **kwargs)

    @ensure_headers
    @ensure_request_id
    def object_drain(self, account, container, obj,
                     version=None, **kwargs):
        """
        Remove all the chunks of a content, but keep all the metadata.

        :param account: name of the account where the object is present
        :type account: `str`
        :param container: name of the container where the object is present
        :type container: `str`
        :param obj: name of the object to drain
        """
        self.container.content_drain(account, container, obj,
                                     version=version, **kwargs)

    @handle_object_not_found
    @ensure_headers
    @ensure_request_id
    def object_delete(self, account, container, obj,
                      version=None, **kwargs):
        """
        Delete an object from a container. If versioning is enabled and no
        version is specified, the object will be marked as deleted but not
        actually deleted.

        :param account: name of the account the object belongs to
        :type account: `str`
        :param container: name of the container the object belongs to
        :type container: `str`
        :param obj: name of the object to delete
        :param version: version of the object to delete
        :returns: True on success
        """
        return self.container.content_delete(account, container, obj,
                                             version=version, **kwargs)

    @ensure_headers
    @ensure_request_id
    def object_delete_many(self, account, container, objs, **kwargs):
        """
        Delete several objects.

        :param objs: an iterable of object names (should not be a generator)
        :returns: a list of tuples with the name of the object and
            a boolean telling if the object has been successfully deleted
        :rtype: `list` of `tuple`
        """
        return self.container.content_delete_many(
            account, container, objs, **kwargs)

    @handle_object_not_found
    @ensure_headers
    @ensure_request_id
    def object_truncate(self, account, container, obj,
                        version=None, size=None, **kwargs):
        """
        Truncate object at specified size. Only shrink is supported.
        A download may occur if size is not on chunk boundaries.

        :param account: name of the account in which the object is stored
        :param container: name of the container in which the object is stored
        :param obj: name of the object to query
        :param version: version of the object to query
        :param size: new size of object
        """

        # code copied from object_fetch (should be factorized !)
        meta, raw_chunks = self.object_locate(
            account, container, obj, version=version,
            properties=False, **kwargs)
        chunk_method = meta['chunk_method']
        storage_method = STORAGE_METHODS.load(chunk_method)
        chunks = _sort_chunks(raw_chunks, storage_method.ec)

        for pos in sorted(chunks.keys()):
            chunk = chunks[pos][0]
            if (size >= chunk['offset']
                    and size <= chunk['offset'] + chunk['size']):
                break
        else:
            raise exc.OioException("No chunk found at position %d" % size)

        if chunk['offset'] != size:
            # retrieve partial chunk
            ret = self.object_fetch(account, container, obj,
                                    version=version,
                                    ranges=[(chunk['offset'], size-1)])
            # TODO implement a proper object_update
            pos = int(chunk['pos'].split('.')[0])
            self.object_create(account, container, obj_name=obj,
                               data=ret[1], meta_pos=pos,
                               content_id=meta['id'])

        return self.container.content_truncate(account, container, obj,
                                               version=version, size=size,
                                               **kwargs)

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def object_list(self, account, container, limit=None, marker=None,
                    delimiter=None, prefix=None, end_marker=None,
                    properties=False, versions=False, deleted=False,
                    **kwargs):
        """
        Lists objects inside a container.

        :param properties: if True, list object properties along with objects
        :param versions: if True, list all versions of objects
        :param deleted: if True, list also the deleted objects

        :returns: a dict which contains
           * 'objects': the `list` of object descriptions
           * 'prefixes': common prefixes (only if delimiter and prefix are set)
           * 'properties': a `dict` of container properties
           * 'system': a `dict` of system metadata
           * 'truncated': a `bool` telling if the listing was truncated
           * 'next_marker': a `str` to be used as `marker` to get the next
            page of results (in case the listing was truncated)
        """
        hdrs, resp_body = self.container.content_list(
            account, container, limit=limit, marker=marker,
            end_marker=end_marker, prefix=prefix, delimiter=delimiter,
            properties=properties, versions=versions, deleted=deleted,
            **kwargs)

        for obj in resp_body['objects']:
            mtype = obj.get('mime-type')
            if mtype is not None:
                obj['mime_type'] = mtype
                del obj['mime-type']
            version = obj.get('ver')
            if version is not None:
                obj['version'] = version
                del obj['ver']

        resp_body['truncated'] = true_value(
            hdrs.get(HEADER_PREFIX + 'list-truncated'))
        marker_header = HEADER_PREFIX + 'list-marker'
        if marker_header in hdrs:
            resp_body['next_marker'] = unquote_plus(hdrs.get(marker_header))
        return resp_body

    @handle_object_not_found
    @ensure_headers
    @ensure_request_id
    def object_locate(self, account, container, obj,
                      version=None, chunk_info=False, properties=True,
                      **kwargs):
        """
        Get a description of the object along with the list of its chunks.

        :param account: name of the account in which the object is stored
        :param container: name of the container in which the object is stored
        :param obj: name of the object to query
        :param version: version of the object to query
        :param chunk_info: if True, fetch additional information about chunks
            from rawx services (slow). The second element of the returned
            tuple becomes a generator (instead of a list).
        :param properties: should the request return object properties
            along with content description
        :type properties: `bool`

        :returns: a tuple with object metadata `dict` as first element
            and chunk `list` as second element
        """
        obj_meta, chunks = self.container.content_locate(
            account, container, obj, properties=properties,
            version=version, **kwargs)

        # FIXME(FVE): converting to float does not sort properly
        # the chunks of the same metachunk
        def _fetch_ext_info(chunks_):
            for chunk in sorted(chunks_, key=lambda x: float(x['pos'])):
                try:
                    ext_info = self.blob_client.chunk_head(
                        chunk['url'], **kwargs)
                    for key in ('chunk_size', 'chunk_hash', 'full_path'):
                        chunk[key] = ext_info.get(key)
                except exc.OioException as err:
                    chunk['error'] = str(err)
                yield chunk
        if not chunk_info:
            return obj_meta, chunks
        return obj_meta, _fetch_ext_info(chunks)

    def object_analyze(self, *args, **kwargs):
        """
        :deprecated: use `object_locate`
        """
        warnings.warn("You'd better use object_locate()",
                      DeprecationWarning)
        return self.object_locate(*args, **kwargs)

    def _generate_fullchunk_copies(self, chunks, random_hex=60):
        # random_hex is the number of hexadecimals characters to generate for
        # the copy path
        copies = []
        for c in chunks:
            tmp = c.copy()
            rnd = (''.join(random.choice('0123456789ABCDEF')
                           for _ in range(random_hex)))
            tmp['url'] = ''.join([tmp['url'][:-random_hex], rnd])
            copies.append(tmp)
        return copies

    # FIXME(FVE): should be named 'object_link'
    # TODO(FVE): must be optimised to detect that the target account
    # and container are the same as source account and source container,
    # and then use the optimised version (just create an alias in meta2).
    @ensure_headers
    @ensure_request_id
    def object_fastcopy(self, target_account, target_container, target_obj,
                        link_account, link_container, link_obj,
                        version=None, **kwargs):
        """
        Make a shallow copy of an object.
        Works across accounts and across containers.
        """
        meta, chunks = self.object_locate(
            target_account, target_container, target_obj, version=version,
            **kwargs)

        chunks_copies = self._generate_fullchunk_copies(chunks)
        chunks_copies_url = []
        chunks_url = []
        for chunk in chunks:
            chunks_url.append(chunk['url'])
        for chunk in chunks_copies:
            chunks_copies_url.append(chunk['url'])
        fullpath = self._generate_fullpath(link_account, link_container,
                                           link_obj, version)
        data = {'chunks': chunks_copies,
                'properties': meta['properties'] or {}}
        try:
            self._link_chunks(
                chunks_url, chunks_copies_url, fullpath[0], **kwargs)
            self.container.content_create(
                link_account, link_container, link_obj,
                size=meta['length'], data=data, checksum=meta['hash'],
                stgpol=meta['policy'], mime_type=meta['mime_type'],
                chunk_method=meta['chunk_method'], **kwargs)
        except Exception:
            del_resps = self.blob_client.chunk_delete_many(chunks_copies,
                                                           **kwargs)
            for resp in del_resps:
                if isinstance(resp, Exception):
                    self.logger.warn('failed to delete chunk %s (%s)',
                                     resp.chunk['url'], resp)
                elif resp.status not in (204, 404):
                    self.logger.warn('failed to delete chunk %s (HTTP %s)',
                                     resp.chunk['url'], resp.status)
            raise

    @staticmethod
    def _ttfb_wrapper(stream, req_start, perfdata):
        """Keep track of time-to-first-byte and time-to-last-byte"""
        for dat in stream:
            if 'ttfb' not in perfdata:
                perfdata['ttfb'] = monotonic_time() - req_start
            yield dat
        perfdata['ttlb'] = monotonic_time() - req_start

    @patch_kwargs
    @ensure_headers
    @ensure_request_id
    def object_fetch(self, account, container, obj, version=None, ranges=None,
                     key_file=None, **kwargs):
        """
        Download an object.

        :param account: name of the account in which the object is stored
        :param container: name of the container in which the object is stored
        :param obj: name of the object to fetch
        :param version: version of the object to fetch
        :type version: `str`
        :param ranges: a list of object ranges to download
        :type ranges: `list` of `tuple`
        :param key_file: path to the file containing credentials

        :keyword properties: should the request return object properties
            along with content description (True by default)
        :type properties: `bool`
        :keyword perfdata: optional `dict` that will be filled with metrics
            of time spent to resolve the meta2 address, to do the meta2
            request, and the time-to-first-byte, as seen by this API.

        :returns: a dictionary of object metadata and
            a stream of object data
        :rtype: tuple
        """
        perfdata = kwargs.get('perfdata', self.container.perfdata)
        if perfdata is not None:
            req_start = monotonic_time()

        meta, raw_chunks = self.object_locate(
            account, container, obj, version=version, **kwargs)
        chunk_method = meta['chunk_method']
        storage_method = STORAGE_METHODS.load(chunk_method)
        chunks = _sort_chunks(raw_chunks, storage_method.ec)
        meta['container_id'] = cid_from_name(account, container).upper()
        meta['ns'] = self.namespace
        if storage_method.ec:
            stream = fetch_stream_ec(chunks, ranges, storage_method, **kwargs)
        elif storage_method.backblaze:
            stream = self._fetch_stream_backblaze(meta, chunks, ranges,
                                                  storage_method, key_file,
                                                  **kwargs)
        else:
            stream = fetch_stream(chunks, ranges, storage_method, **kwargs)

        if perfdata is not None:
            return meta, self._ttfb_wrapper(stream, req_start, perfdata)
        return meta, stream

    @handle_object_not_found
    @ensure_headers
    @ensure_request_id
    def object_get_properties(self, account, container, obj, **kwargs):
        """
        Get the description of an object along with its user properties.

        :param account: name of the account in which the object is stored
        :param container: name of the container in which the object is stored
        :param obj: name of the object to query
        :returns: a `dict` describing the object

        .. py:data:: example

            {'hash': '6BF60C17CC15EEA108024903B481738F',
             'ctime': '1481031763',
             'deleted': 'False',
             'properties': {
                 u'projet': u'OpenIO-SDS'},
             'length': '43518',
             'hash_method': 'md5',
             'chunk_method': 'ec/algo=liberasurecode_rs_vand,k=6,m=3',
             'version': '1481031762951972',
             'policy': 'EC',
             'id': '20BF2194FD420500CD4729AE0B5CBC07',
             'mime_type': 'application/octet-stream',
             'name': 'Makefile'}
        """
        return self.container.content_get_properties(
            account, container, obj, **kwargs)

    @handle_object_not_found
    @ensure_headers
    @ensure_request_id
    def object_show(self, account, container, obj, version=None, **kwargs):
        """
        Get the description of an object along with
        the dictionary of user-set properties.

        :deprecated: prefer using `object_get_properties`,
            for consistency with `container_get_properties`.
        """
        # stacklevel=5 because we are using 3 decorators
        warnings.warn("You'd better use object_get_properties()",
                      DeprecationWarning, stacklevel=5)
        return self.container.content_show(
            account, container, obj, version=version, **kwargs)

    def object_update(self, account, container, obj, metadata,
                      version=None, clear=False, **kwargs):
        """
        :deprecated: use `object_set_properties`
        """
        warnings.warn("You'd better use object_set_properties()",
                      DeprecationWarning, stacklevel=2)
        if clear:
            self.object_del_properties(
                account, container, obj, [], version=version, **kwargs)
        if metadata:
            self.object_set_properties(
                account, container, obj, metadata, version=version, **kwargs)

    @handle_object_not_found
    @ensure_headers
    @ensure_request_id
    def object_set_properties(self, account, container, obj, properties,
                              version=None, **kwargs):
        """
        Set properties on an object.

        :param account: name of the account in which the object is stored
        :param container: name of the container in which the object is stored
        :param obj: name of the object to query
        :param properties: dictionary of properties
        """
        return self.container.content_set_properties(
            account, container, obj, properties={'properties': properties},
            version=version, **kwargs)

    @handle_object_not_found
    @ensure_headers
    @ensure_request_id
    def object_del_properties(self, account, container, obj, properties,
                              version=None, **kwargs):
        """
        Delete some properties from an object.

        :param properties: list of property keys to delete
        :type properties: `list`
        :returns: True if the property has been deleted (or was missing)
        """
        return self.container.content_del_properties(
            account, container, obj, properties=properties,
            version=version, **kwargs)

    def _generate_fullpath(self, account, container_name, path, version):
        return ['{0}/{1}/{2}/{3}'.format(quote_plus(account),
                                         quote_plus(container_name),
                                         quote_plus(path),
                                         version)]

    def _object_upload(self, ul_handler, **kwargs):
        """Upload data to rawx, measure time it takes."""
        perfdata = kwargs.get('perfdata', self.container.perfdata)
        if perfdata is not None:
            upload_start = monotonic_time()
        ul_chunks, ul_bytes, obj_checksum = ul_handler.stream()
        if perfdata is not None:
            upload_end = monotonic_time()
            val = perfdata.get('rawx', 0.0) + upload_end - upload_start
            perfdata['rawx'] = val
        return ul_chunks, ul_bytes, obj_checksum

    def _object_prepare(self, account, container, obj_name, source,
                        sysmeta, policy=None,
                        key_file=None, **kwargs):
        """Call content/prepare, initialize chunk uploaders."""
        chunk_prep = MetachunkPreparer(
            self.container, account, container, obj_name,
            policy=policy, **kwargs)
        obj_meta = chunk_prep.obj_meta
        obj_meta.update(sysmeta)
        obj_meta['content_path'] = obj_name
        obj_meta['container_id'] = cid_from_name(account, container).upper()
        obj_meta['ns'] = self.namespace
        obj_meta['full_path'] = self._generate_fullpath(
            account, container, obj_name, obj_meta['version'])
        obj_meta['oio_version'] = (obj_meta.get('oio_version')
                                   or OIO_VERSION)

        # XXX content_id is necessary to update an existing object
        kwargs['content_id'] = kwargs.get('content_id', obj_meta['id'])

        storage_method = STORAGE_METHODS.load(obj_meta['chunk_method'])
        if storage_method.ec:
            write_handler_cls = ECWriteHandler
        elif storage_method.backblaze:
            backblaze_info = self._b2_credentials(storage_method, key_file)
            write_handler_cls = partial(BackblazeWriteHandler,
                                        backblaze_info=backblaze_info)
        else:
            write_handler_cls = ReplicatedWriteHandler
        handler = write_handler_cls(
                source, obj_meta, chunk_prep, storage_method, **kwargs)

        return obj_meta, handler, chunk_prep

    def _object_create(self, account, container, obj_name, source,
                       sysmeta, properties=None, policy=None,
                       key_file=None, **kwargs):
        obj_meta, ul_handler, chunk_prep = self._object_prepare(
            account, container, obj_name, source, sysmeta,
            policy=policy, key_file=key_file,
            **kwargs)

        try:
            ul_chunks, ul_bytes, obj_checksum = self._object_upload(
                ul_handler, **kwargs)
        except exc.OioException as ex:
            self.logger.warn(
                'Failed to upload all data (%s), deleting chunks', ex)
            self._delete_orphan_chunks(
                chunk_prep.all_chunks_so_far(),
                obj_meta['container_id'],
                **kwargs)
            raise

        etag = obj_meta.get('etag')
        if etag and etag.lower() != obj_checksum.lower():
            raise exc.EtagMismatch(
                "given etag %s != computed %s" % (etag, obj_checksum))
        obj_meta['etag'] = obj_checksum

        data = {'chunks': ul_chunks, 'properties': properties or {}}
        try:
            # FIXME: we may just pass **obj_meta
            self.container.content_create(
                account, container, obj_name, size=ul_bytes,
                checksum=obj_checksum, data=data,
                stgpol=obj_meta['policy'],
                version=obj_meta['version'], mime_type=obj_meta['mime_type'],
                chunk_method=obj_meta['chunk_method'],
                **kwargs)
        except (exc.Conflict, exc.DeadlineReached) as ex:
            self.logger.warn(
                'Failed to commit to meta2 (%s), deleting chunks', ex)
            self._delete_orphan_chunks(ul_chunks, obj_meta['container_id'],
                                       **kwargs)
            raise
        return ul_chunks, ul_bytes, obj_checksum

    def _delete_orphan_chunks(self, chunks, cid, **kwargs):
        """Delete chunks that have been orphaned by an unfinished upload."""
        # FIXME(FVE): won't work with BackBlaze
        del_resps = self.blob_client.chunk_delete_many(chunks, cid, **kwargs)
        for resp in del_resps:
            if isinstance(resp, Exception):
                self.logger.warn('failed to delete chunk %s (%s)',
                                 resp.chunk['url'], resp)
            elif resp.status not in (204, 404):
                self.logger.warn('failed to delete chunk %s (HTTP %s)',
                                 resp.chunk['url'], resp.status)

    def _b2_credentials(self, storage_method, key_file):
        key_file = key_file or '/etc/oio/sds/b2-appkey.conf'
        try:
            return BackblazeUtils.get_credentials(storage_method, key_file)
        except BackblazeUtilsException as err:
            raise exc.ConfigurationException(str(err))

    def _fetch_stream_backblaze(self, meta, chunks, ranges,
                                storage_method, key_file,
                                **kwargs):
        backblaze_info = self._b2_credentials(storage_method, key_file)
        total_bytes = 0
        current_offset = 0
        size = None
        offset = 0
        for pos in range(len(chunks)):
            if ranges:
                offset = ranges[pos][0]
                size = ranges[pos][1]

            if size is None:
                size = int(meta["length"])
            chunk_size = int(chunks[pos][0]["size"])
            if total_bytes >= size:
                break
            if current_offset + chunk_size > offset:
                if current_offset < offset:
                    _offset = offset - current_offset
                else:
                    _offset = 0
                if chunk_size + total_bytes > size:
                    _size = size - total_bytes
                else:
                    _size = chunk_size
            handler = BackblazeChunkDownloadHandler(
                meta, chunks[pos], _offset, _size,
                backblaze_info=backblaze_info)
            stream = handler.get_stream()
            if not stream:
                raise exc.OioException("Error while downloading")
            total_bytes += len(stream)
            yield stream
            current_offset += chunk_size

    @handle_container_not_found
    @ensure_headers
    @ensure_request_id
    def container_refresh(self, account, container, attempts=3, **kwargs):
        for i in range(attempts):
            try:
                self.account.container_reset(account, container, time.time(),
                                             **kwargs)
            except exc.Conflict:
                if i >= attempts - 1:
                    raise
        try:
            self.container.container_touch(account, container, **kwargs)
        except exc.ClientException as e:
            if e.status != 406 and e.status != 431:
                raise
            # CODE_USER_NOTFOUND or CODE_CONTAINER_NOTFOUND
            metadata = dict()
            metadata["dtime"] = time.time()
            self.account.container_update(account, container, metadata,
                                          **kwargs)

    @handle_account_not_found
    @ensure_headers
    @ensure_request_id
    def account_refresh(self, account=None, **kwargs):
        """
        Refresh counters of an account.

        :param account: name of the account to refresh,
            or None to refresh all accounts (slow)
        :type account: `str`
        """
        if account is None:
            accounts = self.account_list(**kwargs)
            for account in accounts:
                try:
                    self.account_refresh(account, **kwargs)
                except exc.NoSuchAccount:  # account remove in the meantime
                    pass
            return

        self.account.account_refresh(account, **kwargs)

        containers = self.container_list(account, **kwargs)
        for container in containers:
            try:
                self.container_refresh(account, container[0], **kwargs)
            except exc.NoSuchContainer:
                # container remove in the meantime
                pass

        while containers:
            marker = containers[-1][0]
            containers = self.container_list(account, marker=marker, **kwargs)
            if containers:
                for container in containers:
                    try:
                        self.container_refresh(account, container[0], **kwargs)
                    except exc.NoSuchContainer:
                        # container remove in the meantime
                        pass

    def all_accounts_refresh(self, **kwargs):
        """
        :deprecated: call `account_refresh(None)` instead
        """
        warnings.warn("You'd better use account_refresh(None)",
                      DeprecationWarning, stacklevel=2)
        return self.account_refresh(None, **kwargs)

    @handle_account_not_found
    @ensure_headers
    @ensure_request_id
    def account_flush(self, account, **kwargs):
        """
        Flush all containers of an account

        :param account: name of the account to flush
        :type account: `str`
        """
        self.account.account_flush(account, **kwargs)

    def _random_buffer(self, dictionary, num_chars):
        """
        :rtype: `str`
        :returns: `num_chars` randomly taken from `dictionary`
        """
        return ''.join(random.choice(dictionary) for _ in range(num_chars))

    def _generate_copies(self, chunks, random_hex=60):
        """
        Generate new chunk URLs, by replacing the last `random_hex`
        characters of the original URLs by random hexadecimal digits.
        """
        copies = []
        for chunk in chunks:
            tmp = ''.join((chunk[:-random_hex],
                           self._random_buffer('0123456789ABCDEF',
                                               random_hex)))
            copies.append(tmp)
        return copies

    def _link_chunks(self, targets, copies, fullpath, **kwargs):
        """
        Create chunk hard links.

        :param targets: original chunk URLs
        :param copies: new chunk URLs
        :param fullpath: full path to the object whose chunks will
            be hard linked
        """
        out_kwargs = dict(kwargs)
        headers = out_kwargs.pop('headers', dict())
        headers.update(((CHUNK_HEADERS['full_path'], fullpath), ))
        for target, copy in zip(targets, copies):
            res = self.blob_client.chunk_link(target, copy,
                                              headers=headers, **out_kwargs)
            if res.status != 201:
                raise exc.ChunkException(res.status)

    def _prepare_meta2_raw_update(self, targets, copies, content):
        """
        Generate the lists of original and replacement chunk beans
        to be used as input for `container_raw_update`.
        """
        targets_beans = []
        copies_beans = []
        for target, copy in zip(targets, copies):
            targets_beans.append(self._m2_chunk_bean(
                target['url'], target, content))
            copies_beans.append(self._m2_chunk_bean(copy, target, content))
        return targets_beans, copies_beans

    @staticmethod
    def _m2_chunk_bean(url, meta, content):
        """
        Prepare a dictionary to be used as a chunk "bean" (in meta2 sense).
        """
        return {"type": "chunk",
                "id": url,
                "hash": meta['hash'],
                "size": int(meta["size"]),
                "pos": meta["pos"],
                "content": content}

    def object_head(self, account, container, obj, trust_level=0, **kwargs):
        """
        Check for the presence of an object in a container.

        :param trust_level: 0: do not check chunks;
                            1: check if there are enough chunks to read the
                            object;
                            2: check if all chunks are present.
        :type trust_level: `int`
        """
        try:
            if trust_level == 0:
                self.object_get_properties(account, container, obj, **kwargs)
            elif trust_level == 1:
                raise NotImplementedError()
            elif trust_level == 2:
                _, chunks = self.object_locate(account, container, obj,
                                               **kwargs)
                for chunk in chunks:
                    self.blob_client.chunk_head(chunk['url'])
            else:
                raise ValueError(
                    '`trust_level` must be between 0 and 2')
        except (exc.NotFound, exc.NoSuchObject):
            return False
        return True


class ObjectStorageAPI(ObjectStorageApi):
    """
    :deprecated: transitional wrapper for ObjectStorageApi
    """

    def __init__(self, namespace, endpoint=None, **kwargs):
        super(ObjectStorageAPI, self).__init__(namespace,
                                               endpoint=endpoint, **kwargs)
        warnings.simplefilter('once')
        warnings.warn(
            "oio.api.ObjectStorageAPI is deprecated, use oio.ObjectStorageApi",
            DeprecationWarning, stacklevel=2)
