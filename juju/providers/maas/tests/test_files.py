# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for juju.providers.maas.files"""

import httplib
from io import BytesIO
import re
from textwrap import dedent
from twisted.internet import defer
from twisted.web import error
from urlparse import urlparse

from juju.errors import (
    FileNotFound, ProviderError, ProviderInteractionError)
from juju.providers.maas.files import encode_multipart_data, MAASFileStorage
from juju.providers.maas.tests.testing import CONFIG, TestCase


class FakeFileStorage(object):
    """A fake http client to MAAS so MAASFileStorage tests can operate."""

    def __init__(self, url, method='GET', postdata=None, headers=None):
        # Store passed data for later inspection.
        self.headers = headers
        self.url = url
        self.data = postdata
        self.action = method

        func = getattr(self, method.lower())
        self.deferred = func()

    def get(self):
        return defer.succeed("blah")

    def post(self):
        return defer.succeed("blah")


class FakeFileStorageReturning404(FakeFileStorage):

    def get(self):
        self.status = str(httplib.NOT_FOUND)
        return defer.fail(error.Error(self.status, "this is a 404", ""))


class FakeFileStorageReturningUnexpectedError(FakeFileStorage):

    def get(self):
        return defer.fail(ZeroDivisionError("numpty"))


class FakeFileStorageWithErrorOnAddingFile(FakeFileStorage):

    def post(self):
        self.status = str(httplib.UNAUTHORIZED)
        return defer.fail(error.Error(self.status, "this is a 401", ""))


class TestMAASFileAPIFunctions(TestCase):

    def test_encode_multipart_data(self):
        # The encode_multipart_data() function should take a list of
        # parameters and files and encode them into a MIME
        # multipart/form-data suitable for posting to the MAAS server.
        params = {"op": "add", "filename": "foo"}
        fileObj = BytesIO(b"random data")
        files = {"file": fileObj}
        body, headers = encode_multipart_data(params, files)

        expected_body_regex = b"""\
            --(?P<boundary>.+)
            Content-Disposition: form-data; name="filename"

            foo
            --(?P=boundary)
            Content-Disposition: form-data; name="op"

            add
            --(?P=boundary)
            Content-Disposition: form-data; name="file"; filename="file"
            Content-Type: application/octet-stream

            random data
            --(?P=boundary)--
            """
        expected_body_regex = dedent(expected_body_regex)
        expected_body_regex = "\r\n".join(expected_body_regex.splitlines())
        expected_body = re.compile(expected_body_regex, re.MULTILINE)
        self.assertRegexpMatches(body, expected_body)

        boundary = expected_body.match(body).group("boundary")
        expected_headers = {
            "content-length": "365",
            "content-type": "multipart/form-data; boundary=%s" % boundary}
        self.assertEqual(expected_headers, headers)


class TestMAASFileStorage(TestCase):

    def test_get_url(self):
        # get_url should return the base URL plus the op params for a
        # file name.
        storage = MAASFileStorage(CONFIG)
        url = storage.get_url("foofile")
        urlparts = urlparse(url)
        self.assertEqual("/maas/api/1.0/files/", urlparts.path)
        self.assertEqual("filename=foofile&op=get", urlparts.query)

    def test_get_succeeds(self):
        self.setup_connection(MAASFileStorage, FakeFileStorage)
        storage = MAASFileStorage(CONFIG)
        d = storage.get("foo")

        def check(value):
            # The underlying code returns a StringIO but because
            # implementations of StringIO and cStringIO are completely
            # different the only reasonable thing to do here is to
            # check to see if the returned object has a "read" method.
            attr = getattr(value, "read")
            self.assertIsNot(None, attr)
            self.assertTrue(value.read)

        d.addCallback(check)
        d.addErrback(self.fail)
        return d

    def test_get_with_bad_filename(self):
        self.setup_connection(MAASFileStorage, FakeFileStorageReturning404)
        storage = MAASFileStorage(CONFIG)
        d = storage.get("foo")

        return self.assertFailure(d, FileNotFound)

    def test_get_with_unexpected_response(self):
        self.setup_connection(
            MAASFileStorage, FakeFileStorageReturningUnexpectedError)
        storage = MAASFileStorage(CONFIG)
        d = storage.get("foo")

        return self.assertFailure(d, ProviderInteractionError)

    def test_put_succeeds(self):
        self.setup_connection(MAASFileStorage, FakeFileStorage)
        storage = MAASFileStorage(CONFIG)
        fileObj = BytesIO("some data")

        d = storage.put("foo", fileObj)
        d.addCallback(self.assertTrue)
        d.addErrback(self.fail)
        return d

    def test_put_with_error_returned(self):
        self.setup_connection(
            MAASFileStorage, FakeFileStorageWithErrorOnAddingFile)
        storage = MAASFileStorage(CONFIG)
        fileObj = BytesIO("some data")
        d = storage.put("foo", fileObj)

        return self.assertFailure(d, ProviderError)
