# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test cases for juju.providers.maas.auth"""

from juju.providers.maas.auth import _ascii_url
from juju.providers.maas.tests.testing import TestCase


class TestAsciiUrl(TestCase):
    """Tests for L{_ascii_url}."""

    def assertEncode(self, expected, url):
        """Assert that L{_ascii_url} encodes L{url} correctly.

        The encoded URL must be a byte string (i.e. str in Python 2.x or bytes
        in Python 3.x) and be equal to L{expected}.
        """
        url_encoded = _ascii_url(url)
        self.assertIsInstance(url_encoded, str)
        self.assertEqual(expected, url_encoded)

    def test_already_ascii_str(self):
        # A URL passed as a byte string with only ASCII characters is returned
        # unaltered.
        url = "http://www.example.com/some/where"
        self.assertEncode(url, url)

    def test_already_ascii_unicode_str(self):
        # A URL passed as a unicode string with only ASCII characters is
        # returned as a byte string.
        self.assertEncode(
            "http://www.example.com/some/where",
            u"http://www.example.com/some/where")

    def test_non_ascii_str(self):
        # An exception is raised if the URL is byte string containing
        # non-ASCII characters.
        url = "http://fran\xe7aise.example.com/some/where"
        self.assertRaises(UnicodeDecodeError, _ascii_url, url)

    def test_non_ascii_unicode_hostname(self):
        # A URL passed as a unicode string with non-ASCII characters in the
        # hostname part is returned with the hostname IDNA encoded.
        url = u"http://fran\xe7aise.example.com/some/where"
        url_expected = "http://xn--franaise-v0a.example.com/some/where"
        self.assertEncode(url_expected, url)

    def test_non_ascii_unicode_path(self):
        # An exception is raised if the URL is a unicode string with non-ASCII
        # characters in parts outside of the hostname.
        url = u"http://example.com/fran\xe7aise"
        self.assertRaises(UnicodeEncodeError, _ascii_url, url)
