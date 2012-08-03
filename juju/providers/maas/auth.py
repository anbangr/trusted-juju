# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""OAuth functions for authorisation against a maas server."""

import oauth.oauth as oauth
from twisted.internet import reactor
from twisted.web.client import HTTPClientFactory
from urlparse import urlparse


DEFAULT_FACTORY = HTTPClientFactory
DEFAULT_CONNECT = reactor.connectTCP


def _ascii_url(url):
    """Ensure that the given URL is ASCII, encoding if necessary."""
    if isinstance(url, unicode):
        urlparts = urlparse(url)
        urlparts = urlparts._replace(
            netloc=urlparts.netloc.encode("idna"))
        url = urlparts.geturl()
    return url.encode("ascii")


class MAASOAuthConnection(object):
    """Helper class to provide an OAuth auth'd connection to MAAS."""

    factory = staticmethod(DEFAULT_FACTORY)
    connect = staticmethod(DEFAULT_CONNECT)

    def __init__(self, oauth_info):
        consumer_key, resource_token, resource_secret = oauth_info
        resource_tok_string = "oauth_token_secret=%s&oauth_token=%s" % (
            resource_secret, resource_token)
        self.resource_token = oauth.OAuthToken.from_string(resource_tok_string)
        self.consumer_token = oauth.OAuthConsumer(consumer_key, "")

    def oauth_sign_request(self, url, headers):
        """Sign a request.

        @param url: The URL to which the request is to be sent.
        @param headers: The headers in the request.
        """
        oauth_request = oauth.OAuthRequest.from_consumer_and_token(
            self.consumer_token, token=self.resource_token, http_url=url)
        oauth_request.sign_request(
            oauth.OAuthSignatureMethod_PLAINTEXT(), self.consumer_token,
            self.resource_token)
        headers.update(oauth_request.to_header())

    def dispatch_query(self, request_url, method="GET",
                       data=None, headers=None):
        """Dispatch an OAuth-signed request to L{request_url}.

        @param request_url: The URL to which the request is to be sent.
        @param method: The HTTP method, e.g. C{GET}, C{POST}, etc.
        @param data: The data to send, if any.
        @type data: A byte string.
        @param headers: Headers to including in the request.
        """
        if headers is None:
            headers = {}
        self.oauth_sign_request(request_url, headers)
        self.client = self.factory(
            url=_ascii_url(request_url), method=method,
            headers=headers, postdata=data)
        urlparts = urlparse(request_url)
        self.connect(urlparts.hostname, urlparts.port, self.client)
        return self.client.deferred
