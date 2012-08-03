# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""MAAS API client for Juju"""

from base64 import b64encode
import json
import re
from urllib import urlencode
from urlparse import urljoin

from juju.errors import ProviderError
from juju.providers.common.utils import convert_unknown_error
from juju.providers.maas.auth import MAASOAuthConnection
from juju.providers.maas.files import encode_multipart_data


CONSUMER_SECRET = ""


_re_resource_uri = re.compile(
    '/api/(?P<version>[^/]+)/nodes/(?P<system_id>[^/]+)/?')


def extract_system_id(resource_uri):
    """Extract a system ID from a resource URI.

    This is fairly unforgiving; an exception is raised if the URI given does
    not look like a MAAS node resource URI.

    :param resource_uri: A URI that corresponds to a MAAS node resource.
    :raises: :exc:`juju.errors.ProviderError` when `resource_uri` does not
        resemble a MAAS node resource URI.
    """
    match = _re_resource_uri.search(resource_uri)
    if match is None:
        raise ProviderError(
            "%r does not resemble a MAAS resource URI." % (resource_uri,))
    else:
        return match.group("system_id")


class MAASClient(MAASOAuthConnection):

    def __init__(self, config):
        """Initialise an API client for MAAS.

        :param config: a dict of configuration values; must contain
            'maas-server', 'maas-oauth', 'admin-secret'
        """
        self.url = config["maas-server"]
        if not self.url.endswith('/'):
            self.url += "/"
        self.oauth_info = config["maas-oauth"]
        self.admin_secret = config["admin-secret"]
        super(MAASClient, self).__init__(self.oauth_info)

    def get(self, path, params):
        """Dispatch a C{GET} call to a MAAS server.

        :param uri: The MAAS path for the endpoint to call.
        :param params: A C{dict} of parameters - or sequence of 2-tuples - to
            encode into the request.
        :return: A Deferred which fires with the result of the call.
        """
        url = "%s?%s" % (urljoin(self.url, path), urlencode(params))
        d = self.dispatch_query(url)
        d.addCallback(json.loads)
        d.addErrback(convert_unknown_error)
        return d

    def post(self, path, params):
        """Dispatch a C{POST} call to a MAAS server.

        :param uri: The MAAS path for the endpoint to call.
        :param params: A C{dict} of parameters to encode into the request.
        :return: A Deferred which fires with the result of the call.
        """
        url = urljoin(self.url, path)
        body, headers = encode_multipart_data(params, {})
        d = self.dispatch_query(url, "POST", headers=headers, data=body)
        d.addCallback(json.loads)
        d.addErrback(convert_unknown_error)
        return d

    def get_nodes(self, resource_uris=None):
        """Ask MAAS to return a list of all the nodes it knows about.

        :param resource_uris: The MAAS URIs for the nodes you want to get.
        :return: A Deferred whose value is the list of nodes.
        """
        params = [("op", "list_allocated")]
        if resource_uris is not None:
            params.extend(
                ("id", extract_system_id(resource_uri))
                for resource_uri in resource_uris)
        return self.get("api/1.0/nodes/", params)

    def acquire_node(self, constraints=None):
        """Ask MAAS to assign a node to us.

        :return: A Deferred whose value is the resource URI to the node
            that was acquired.
        """
        params = {"op": "acquire"}
        if constraints is not None:
            name = constraints["maas-name"]
            if name is not None:
                params["name"] = name
        return self.post("api/1.0/nodes/", params)

    def start_node(self, resource_uri, user_data):
        """Ask MAAS to start a node.

        :param resource_uri: The MAAS URI for the node you want to start.
        :param user_data: Any blob of data to be passed to MAAS. Must be
            possible to encode as base64.
        :return: A Deferred whose value is the resource data for the node
            as returned by get_nodes().
        """
        assert isinstance(user_data, str), (
            "User data must be a byte string.")
        params = {"op": "start", "user_data": b64encode(user_data)}
        return self.post(resource_uri, params)

    def stop_node(self, resource_uri):
        """Ask maas to shut down a node.

        :param resource_uri: The MAAS URI for the node you want to stop.
        :return: A Deferred whose value is the resource data for the node
            as returned by get_nodes().
        """
        params = {"op": "stop"}
        return self.post(resource_uri, params)

    def release_node(self, resource_uri):
        """Ask MAAS to release a node from our ownership.

        :param resource_uri: The URI in MAAS for the node you want to release.
        :return: A Deferred which fires with the resource data for the node
            just released.
        """
        params = {"op": "release"}
        return self.post(resource_uri, params)
