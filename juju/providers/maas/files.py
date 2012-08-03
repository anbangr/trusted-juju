# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""File Storage API client for MAAS."""

from cStringIO import StringIO
import httplib
import mimetypes
import random
import string
from twisted.web.error import Error
import urllib
from urlparse import urljoin

from juju.errors import (
    FileNotFound,
    ProviderError,
    )
from juju.providers.common.utils import convert_unknown_error
from juju.providers.maas.auth import MAASOAuthConnection


def _convert_error(failure, method, url, errors):
    if failure.check(Error):
        status = failure.value.status
        error = errors.get(int(status))
        if error:
            raise error
        raise ProviderError(
            "Unexpected HTTP %s trying to %s %s" % (status, method, url))
    return convert_unknown_error(failure)


def _random_string(length):
    return ''.join(random.choice(string.letters) for ii in range(length + 1))


def _get_content_type(filename):
    return mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def _encode_field(field_name, data, boundary):
    return ('--' + boundary,
            'Content-Disposition: form-data; name="%s"' % field_name,
            '', str(data))


def _encode_file(name, fileObj, boundary):
    return ('--' + boundary,
            'Content-Disposition: form-data; name="%s"; filename="%s"' %
                (name, name),
            'Content-Type: %s' % _get_content_type(name),
            '', fileObj.read())


def encode_multipart_data(data, files):
    """Create a MIME multipart payload from L{data} and L{files}.

    @param data: A mapping of names (ASCII strings) to data (byte string).
    @param files: A mapping of names (ASCII strings) to file objects ready to
        be read.
    @return: A 2-tuple of C{(body, headers)}, where C{body} is a a byte string
        and C{headers} is a dict of headers to add to the enclosing request in
        which this payload will travel.
    """
    boundary = _random_string(30)

    lines = []
    for name in data:
        lines.extend(_encode_field(name, data[name], boundary))
    for name in files:
        lines.extend(_encode_file(name, files[name], boundary))
    lines.extend(('--%s--' % boundary, ''))
    body = '\r\n'.join(lines)

    headers = {'content-type': 'multipart/form-data; boundary=' + boundary,
               'content-length': str(len(body))}

    return body, headers


class MAASFileStorage(MAASOAuthConnection):
    """A file storage abstraction for MAAS."""

    def __init__(self, config):
        server_url = config["maas-server"]
        if not server_url.endswith('/'):
            server_url += "/"
        self._base_url = urljoin(server_url, "api/1.0/files/")
        self._auth = config["maas-oauth"]
        super(MAASFileStorage, self).__init__(self._auth)

    def get_url(self, name):
        """Return a URL that can be used to access a stored file.

        :param unicode name: the file path for which to provide a URL
        :return: a URL
        :rtype: str
        """
        params = {"op": "get", "filename": name}
        param_string = urllib.urlencode(params)
        return self._base_url + "?" + param_string

    def get(self, name):
        """Get a file object from the MAAS server.

        :param unicode name: path to the desired file
        :return: an open file object
        :rtype: :class:`twisted.internet.defer.Deferred`
        :raises: :exc:`juju.errors.FileNotFound` if the file doesn't exist
        """
        url = self.get_url(name)
        d = self.dispatch_query(url)
        d.addCallback(StringIO)
        error_tab = {httplib.NOT_FOUND: FileNotFound(url)}
        d.addErrback(_convert_error, "GET", url, error_tab)
        return d

    def put(self, name, file_object):
        """Upload a file to MAAS.

        :param unicode name: name with which to store the content
        :param file_object: open file object containing the content
        :rtype: :class:`twisted.internet.defer.Deferred`
        """
        url = self._base_url
        params = {"op": "add", "filename": name}
        files = {"file": file_object}
        body, headers = encode_multipart_data(params, files)
        d = self.dispatch_query(
            url, method="POST", headers=headers, data=body)
        d.addCallback(lambda _: True)
        error_tab = {
            httplib.UNAUTHORIZED: ProviderError(
                "The supplied storage credentials were not "
                "accepted by the server")}
        d.addErrback(_convert_error, "PUT", url, error_tab)
        return d
