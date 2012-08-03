# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Helpers for testing juju.providers.maas."""

from collections import namedtuple
import json
from twisted.internet import defer
from xmlrpclib import Fault

from juju.lib import testing
from juju.providers.maas.auth import DEFAULT_FACTORY, MAASOAuthConnection


LogRecord = namedtuple(
    "LogRecord", ("called", "args", "kwargs", "result"))


class TestCase(testing.TestCase):

    def setup_connection(self, cls, factory=DEFAULT_FACTORY):
        """Temporarily override client and connection factories in `cls`.

        This is intended for use with `MAASOAuthConnection` and subclasses.

        The connection factory is always set to a function that logs the call
        but always returns `None`. This prevents tests from making external
        connections.

        Returns a list, to which events will be logged. See `LogRecord` for
        the form these logs take.
        """
        assert issubclass(cls, MAASOAuthConnection), (
            "setup_connection() is only suitable for use "
            "with MAASOAuthConnection and its subclasses.")

        log = []

        def factory_logger(*args, **kwargs):
            instance = factory(*args, **kwargs)
            record = LogRecord("factory", args, kwargs, instance)
            log.append(record)
            return instance

        def connect_logger(*args, **kwargs):
            record = LogRecord("connect", args, kwargs, None)
            log.append(record)
            return None

        self.patch(cls, "factory", staticmethod(factory_logger))
        self.patch(cls, "connect", staticmethod(connect_logger))
        return log


CONFIG = {
    "maas-server": "http://example.com/maas",
    "maas-oauth": ("maas", "DEADBEEF1234", "BEEFDEAD4321"),
    "admin-secret": "whatever",
    "authorized-keys": "ssh-rsa DEADBEEF987654321"}


# All resource URIs include the maas-server path in CONFIG (above) because
# that's what the MAAS server will return.
NODE_JSON = [
    {"macaddress_set": [
            {"resource_uri":
                 ("/maas/api/1.0/"
                  "nodes/node-2666dd64-4671-11e1-93b8-00225f89f211"
                  "/macs/08:34:2a:b5:8a:45/"),
             "mac_address": "08:34:2a:b5:8a:45"},
            {"resource_uri":
                 ("/maas/api/1.0"
                  "/nodes/node-2666dd64-4671-11e1-93b8-00225f89f211"
                  "/macs/dd:67:33:33:1a:bb/"),
             "mac_address": "dd:67:33:33:1a:bb"}],
     "hostname": "sun",
     "system_id": "node-2666dd64-4671-11e1-93b8-00225f89f211",
     "resource_uri":
         "/maas/api/1.0/nodes/node-2666dd64-4671-11e1-93b8-00225f89f211/"},
    {"macaddress_set": [
            {"resource_uri":
                 ("/maas/api/1.0"
                  "/nodes/node-29d7ad70-4671-11e1-93b8-00225f89f211"
                  "/macs/08:05:44:c7:bb:45/"),
             "mac_address": "08:05:44:c7:bb:45"}],
     "hostname": "moon",
     "system_id": "node-29d7ad70-4671-11e1-93b8-00225f89f211",
     "resource_uri":
         "/maas/api/1.0/nodes/node-29d7ad70-4671-11e1-93b8-00225f89f211/"}]


class FakeMAASHTTPConnection(object):
    """A L{MAASHTTPConnection} that fakes all connections.

    Its responses are based on the contents of L{NODE_JSON}.

    See L{MAASHTTPConnection} for more information.
    """

    def __init__(self, url, method='GET', postdata=None, headers=None):
        # Store passed data for later inspection.
        self.headers = headers
        self.url = url
        self.data = postdata
        self.action = method

        func = getattr(self, method.lower())
        self.deferred = func()

    def get(self):
        # List all nodes.
        if self.url.endswith("/nodes/?op=list_allocated"):
            return self.list_nodes()
        # List some nodes.
        elif "nodes/?op=list_allocated&id=" in self.url:
            return self.list_some_nodes()
        # Not recognized.
        else:
            raise AssertionError("Unknown API method called")

    def list_nodes(self):
        return defer.succeed(json.dumps(NODE_JSON))

    def list_some_nodes(self):
        # TODO: Ignores the URL and returns the first node in the test data.
        return defer.succeed(json.dumps([NODE_JSON[0]]))

    def post(self):
        # Power up a node.
        if "start" in self.data:
            return self.start_node()
        # Acquire a node.
        elif "acquire" in self.data:
            return self.acquire_node()
        # Stop a node.
        elif "stop" in self.data:
            return self.stop_node()
        elif "release" in self.data:
            return self.release_node()
        # Not recognized.
        else:
            raise AssertionError("Unknown API method called")

    def start_node(self):
        # Poor man's node selection.
        if NODE_JSON[1]["system_id"] in self.url:
            return defer.succeed(json.dumps(NODE_JSON[1]))
        return defer.succeed(json.dumps(NODE_JSON[0]))

    def acquire_node(self):
        # Implement a poor man's name constraints.
        if "moon" in self.data:
            return defer.succeed(json.dumps(NODE_JSON[1]))
        elif "sun" in self.data:
            return defer.succeed(json.dumps(NODE_JSON[0]))
        return defer.succeed(json.dumps(NODE_JSON[0]))

    def stop_node(self):
        return defer.succeed(json.dumps(NODE_JSON[0]))

    def release_node(self):
        return defer.succeed(json.dumps(NODE_JSON[0]))


class FakeMAASHTTPConnectionWithNoAvailableNodes(FakeMAASHTTPConnection):
    """Special version of L{FakeMAASHTTPConnection} that fakes that no nodes
    are available."""

    def acquire_node(self):
        raise Fault(1, "No available nodes")
