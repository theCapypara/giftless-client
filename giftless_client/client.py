"""A simple Git LFS client
"""
import base64
import hashlib
import logging
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Tuple

import requests
from six.moves import urllib_parse

from . import exc, transfer, types

FILE_READ_BUFFER_SIZE = 4 * 1024 * 1000  # 4mb, why not

_log = logging.getLogger(__name__)


class LfsClient(object):

    LFS_MIME_TYPE = 'application/vnd.git-lfs+json'

    TRANSFER_ADAPTERS = {'basic': transfer.BasicTransferAdapter,
                         'multipart-basic': transfer.MultipartTransferAdapter}

    TRANSFER_ADAPTER_PRIORITY = ('multipart-basic', 'basic')

    def __init__(self, lfs_server_url, auth_token=None, basic_auth=None, transfer_adapters=TRANSFER_ADAPTER_PRIORITY):
        # type: (str, Optional[str], Optional[Tuple[str, str]], Iterable[str]) -> None
        self._url = lfs_server_url.rstrip('/')
        self._auth_token = auth_token
        self._basic_auth = basic_auth
        if self._auth_token is not None and self._basic_auth is not None:
            raise ValueError("Only either an auth token or basic auth credentials can be supplied, but not both.")
        self._transfer_adapters = transfer_adapters

    def batch(self, prefix, operation, objects, ref=None, transfers=None):
        # type: (Optional[str], str, List[Dict[str, Any]], Optional[str], Optional[List[str]]) -> Dict[str, Any]
        """Send a batch request to the LFS server

        TODO: allow specifying more than one file for a single batch operation
        """
        if prefix is not None:
            url = self._url_for(prefix, 'objects', 'batch')
        else:
            url = self._url_for('objects', 'batch')
        if transfers is None:
            transfers = self._transfer_adapters

        payload = {'transfers': transfers,
                   'operation': operation,
                   'objects': objects}
        if ref:
            payload['ref'] = ref

        headers = {'Content-type': self.LFS_MIME_TYPE,
                   'Accept': self.LFS_MIME_TYPE}
        self._add_auth(headers)

        response = requests.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            raise exc.LfsError("Unexpected response from LFS server: {}".format(response.status_code),
                               status_code=response.status_code)
        _log.debug("Got reply for batch request: %s", response.json())
        return response.json()

    def upload(self, file_obj, organization, repo, **extras):
        # type: (BinaryIO, Optional[str], Optional[str], Any) -> types.ObjectAttributes
        """Upload a file to LFS storage
        """
        object_attrs = self._get_object_attrs(file_obj)
        self._add_extra_object_attributes(object_attrs, extras)
        prefix = None
        if organization is not None or repo is not None:
            prefix = '{}/{}'.format(organization, repo)
        response = self.batch(prefix, 'upload', [object_attrs])

        try:
            adapter = self.TRANSFER_ADAPTERS[response['transfer']]()
        except KeyError:
            raise ValueError("Unsupported transfer adapter: {}".format(response['transfer']))

        adapter.upload(file_obj, response['objects'][0])
        return object_attrs

    def download(self, file_obj, object_sha256, object_size, organization, repo, **extras):
        # type: (BinaryIO, str, int, Optional[str], Optional[str], Any) -> None
        """Download a file and save it to file_obj

        file_obj is expected to be an file-like object open for writing in binary mode

        TODO: allow specifying more than one file for a single batch operation
        """
        object_attrs = {"oid": object_sha256, "size": object_size}
        self._add_extra_object_attributes(object_attrs, extras)

        prefix = None
        if organization is not None or repo is not None:
            prefix = '{}/{}'.format(organization, repo)
        response = self.batch(prefix, 'download', [object_attrs])

        try:
            adapter = self.TRANSFER_ADAPTERS[response['transfer']]()
        except KeyError:
            raise ValueError("Unsupported transfer adapter: {}".format(response['transfer']))

        return adapter.download(file_obj, response['objects'][0])

    def list_locks(self, path=None, id=None, cursor=None, limit=None, refspec=None):
        # type: (Optional[str], Optional[str], Optional[int], Optional[int], Optional[str]) -> dict
        """ This asks the Git LFS server to list locks. The raw response is returned."""
        url = self._url_for('/list')
        headers = {'Content-type': self.LFS_MIME_TYPE,
                   'Accept': self.LFS_MIME_TYPE}
        self._add_auth(headers)

        response = requests.get(url, headers=headers, params={
            path: path, id: id, cursor: cursor, limit: limit, refspec: refspec
        })
        if response.status_code != 200:
            raise exc.LfsError("Unexpected response from LFS server: {}".format(response.status_code),
                               status_code=response.status_code)
        _log.debug("Got reply for info request: %s", response.json())
        return response.json()

    def _url_for(self, *segments, **params):
        # type: (str, str) -> str
        path = '/'.join(segments)
        url = '{url}/{path}'.format(url=self._url, path=path)
        if params:
            url = '{url}?{params}'.format(url=url, params=urllib_parse.urlencode(params))
        return url

    def _add_auth(self, headers):
        # type: (dict) -> None
        if self._auth_token:
            headers['Authorization'] = 'Bearer {}'.format(self._auth_token)
        if self._basic_auth:
            (user, pw) = self._basic_auth
            b64encoded = base64.b64encode('{}:{}'.format(user, pw).encode('ascii'))
            headers['Authorization'] = 'Basic {}'.format(str(b64encoded, 'ascii'))

    @staticmethod
    def _get_object_attrs(file_obj, **extras):
        # type: (BinaryIO, Any) -> types.ObjectAttributes
        digest = hashlib.sha256()
        try:
            while True:
                data = file_obj.read(FILE_READ_BUFFER_SIZE)
                if data:
                    digest.update(data)
                else:
                    break

            size = file_obj.tell()
            oid = digest.hexdigest()
        finally:
            file_obj.seek(0)

        return types.ObjectAttributes(oid=oid, size=size)

    @staticmethod
    def _add_extra_object_attributes(attributes, extras):
        # type: (types.ObjectAttributes, Dict[str, Any]) -> None
        """Add Giftless-specific 'x-...' attributes to an object dict
        """
        for k, v in extras.items():
            attributes['x-{}'.format(k)] = v
