# Copyright 2012 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test cases for juju.providers.maas.maas"""

import json
from textwrap import dedent
from twisted.internet.defer import inlineCallbacks, succeed
from urlparse import urlparse

from juju.errors import ProviderError
from juju.providers.maas.maas import extract_system_id, MAASClient
from juju.providers.maas.tests.testing import (
    CONFIG, FakeMAASHTTPConnection, NODE_JSON, TestCase)


class TestFunctions(TestCase):

    def assertExtractSystemID(self, system_id, resource_uri):
        """
        Assert that a system ID extracted from `resource_uri` matches
        `system_id`.
        """
        self.assertEqual(system_id, extract_system_id(resource_uri))

    def test_extract_system_id(self):
        """
        The system ID is extracted from URIs resembling resource URIs.
        """
        self.assertExtractSystemID("fred", "/api/1.0/nodes/fred/")
        self.assertExtractSystemID("fred", "/api/2.3/nodes/fred")
        self.assertExtractSystemID("fred", "/api/3.x/nodes/fred/mac")

    def test_extract_system_id_leading_path_elements(self):
        """
        There can be path elements prior to the resource URI part; these are
        discarded.
        """
        self.assertExtractSystemID("fred", "alice/api/1.0/nodes/fred/")
        self.assertExtractSystemID("fred", "bob/api/2.3/nodes/fred")
        self.assertExtractSystemID("fred", "/carol/api/3.x/nodes/fred/mac")

    def test_extract_system_id_not_a_resource_uri(self):
        """
        `ProviderError` is raised if the argument does not resemble a resource
        URI.
        """
        error = self.assertRaises(
            ProviderError, extract_system_id, "/hoopy/frood")
        self.assertEqual(
            "'/hoopy/frood' does not resemble a MAAS resource URI.",
            str(error))

    def test_extract_system_id_empty_uri(self):
        """
        `ProviderError` is raised if the URI is empty.
        """
        error = self.assertRaises(ProviderError, extract_system_id, "")
        self.assertEqual(
            "'' does not resemble a MAAS resource URI.",
            str(error))

    def test_extract_system_id_with_NODE_JSON(self):
        """
        The system IDs in the sample data are extracted from the sample data's
        resource URIs.
        """
        for node in NODE_JSON:
            self.assertExtractSystemID(
                node["system_id"], node["resource_uri"])


class TestMAASConnection(TestCase):

    def test_init_config(self):
        """
        `MAASClient` gets its configuration from the passed config object. It
        ensures that the server URL has a trailing slash; this is important
        when constructing URLs using resource URIs.
        """
        client = MAASClient(CONFIG)
        expected = [
            CONFIG["maas-server"] + "/", CONFIG["maas-oauth"],
            CONFIG["admin-secret"]]
        actual = [client.url, client.oauth_info, client.admin_secret]
        self.assertEqual(expected, actual)

    def test_init_config_leaves_trailing_slash_on_url(self):
        """
        When the MAAS server is configured with a trailing slash in the URL it
        is left alone (and not doubled up).
        """
        config = CONFIG
        config["maas-server"] = "http://maas.example.com/maas/"
        client = MAASClient(CONFIG)
        self.assertEqual("http://maas.example.com/maas/", client.url)

    def test_oauth_sign_request(self):
        client = MAASClient(CONFIG)
        headers = {}
        client.oauth_sign_request(
            CONFIG["maas-server"], headers)
        auth = headers['Authorization']
        match_regex = (
            'OAuth realm="", oauth_nonce="[^"]+", '
            'oauth_timestamp="[^"]+", oauth_consumer_key="maas", '
            'oauth_signature_method="PLAINTEXT", '
            'oauth_version="1.0", oauth_token="DEADBEEF1234", '
            'oauth_signature="[^"]+"')
        self.assertRegexpMatches(auth, match_regex)


class MAASClientThatReturnsDispatchURL(MAASClient):

    def dispatch_query(self, request_url, *args, **kwargs):
        return succeed(json.dumps(request_url))


class TestMAASClientBase:

    def get_client(self):
        """Return a MAASClient with a FakeMAASHTTPConnection factory."""
        log = self.setup_connection(MAASClient, FakeMAASHTTPConnection)
        return MAASClient(CONFIG), log


class TestMAASClientWithTwisted(TestCase, TestMAASClientBase):

    def assertPathEqual(self, expected_path, observed_url):
        """Assert path of `observed_url` is equal to `expected_path`."""
        observed_path = urlparse(observed_url).path
        self.assertEqual(expected_path, observed_path)

    def test_get_respects_relative_paths(self):
        """`MAASClient.get()` respects a relative path."""
        client = MAASClientThatReturnsDispatchURL(CONFIG)
        self.assertPathEqual("/maas/fred", client.get("fred", ()).result)

    def test_get_respects_absolute_paths(self):
        """`MAASClient.get()` respects an absolute path."""
        client = MAASClientThatReturnsDispatchURL(CONFIG)
        self.assertPathEqual("/fred", client.get("/fred", ()).result)

    def test_post_respects_relative_paths(self):
        """`MAASClient.post()` respects a relative path."""
        client = MAASClientThatReturnsDispatchURL(CONFIG)
        self.assertPathEqual("/maas/fred", client.post("fred", ()).result)

    def test_post_respects_absolute_paths(self):
        """`MAASClient.post()` respects an absolute path."""
        client = MAASClientThatReturnsDispatchURL(CONFIG)
        self.assertPathEqual("/fred", client.post("/fred", ()).result)

    def test_get_nodes_uses_relative_path(self):
        client = MAASClientThatReturnsDispatchURL(CONFIG)
        self.assertPathEqual(
            "/maas/api/1.0/nodes/",
            client.get_nodes().result)

    def test_acquire_node_uses_relative_path(self):
        client = MAASClientThatReturnsDispatchURL(CONFIG)
        self.assertPathEqual(
            "/maas/api/1.0/nodes/",
            client.acquire_node().result)

    @inlineCallbacks
    def test_get_nodes_returns_decoded_json(self):
        client, log = self.get_client()
        result = yield client.get_nodes()
        self.assertEqual(NODE_JSON, result)

    @inlineCallbacks
    def test_get_nodes_takes_resource_uris(self):
        """
        System IDs are extracted from resource URIs passed into get_nodes(),
        where possible. Non resource URIs are passed through.
        """
        client, log = self.get_client()
        resource_uris = ["/api/42/nodes/Ford", "/api/42/nodes/Prefect"]
        yield client.get_nodes(resource_uris)
        factory_call = next(
            call for call in log if call.called == "factory")
        self.assertEqual("GET", factory_call.kwargs["method"])
        self.assertEndsWith(
            factory_call.kwargs["url"],
            '?op=list_allocated&id=Ford&id=Prefect')

    @inlineCallbacks
    def test_get_nodes_connects_with_oauth_credentials(self):
        client, log = self.get_client()
        yield client.get_nodes()
        [factory_call] = [
            record for record in log if record.called == "factory"]
        self.assertIn("Authorization", factory_call.result.headers)

    @inlineCallbacks
    def test_acquire_node(self):
        client, log = self.get_client()
        maas_node_data = yield client.acquire_node()
        # Test that the returned data is a dict containing the node
        # data.
        self.assertIsInstance(maas_node_data, dict)
        self.assertIn("resource_uri", maas_node_data)

    @inlineCallbacks
    def test_acquire_node_connects_with_oauth_credentials(self):
        client, log = self.get_client()
        yield client.acquire_node()
        [factory_call] = [
            record for record in log if record.called == "factory"]
        self.assertIn("Authorization", factory_call.result.headers)

    @inlineCallbacks
    def test_start_node(self):
        resource_uri = NODE_JSON[0]["resource_uri"]
        data = "This is test data."
        client, log = self.get_client()
        returned_data = yield client.start_node(resource_uri, data)
        self.assertEqual(returned_data, NODE_JSON[0])
        # Also make sure that the connection was passed the user_data in
        # the POST data.
        expected_text = dedent(
            """
            Content-Disposition: form-data; name="user_data"

            VGhpcyBpcyB0ZXN0IGRhdGEu
            """)
        expected_text = "\r\n".join(expected_text.splitlines())
        [factory_call] = [
            record for record in log if record.called == "factory"]
        self.assertIn(expected_text, factory_call.result.data)

    @inlineCallbacks
    def test_start_node_connects_with_oauth_credentials(self):
        client, log = self.get_client()
        yield client.start_node("foo", "bar")
        [factory_call] = [
            record for record in log if record.called == "factory"]
        self.assertIn("Authorization", factory_call.result.headers)

    @inlineCallbacks
    def test_stop_node(self):
        # stop_node should power down the node and return its json data.
        resource_uri = NODE_JSON[0]["resource_uri"]
        client, log = self.get_client()
        returned_data = yield client.stop_node(resource_uri)
        self.assertEqual(returned_data, NODE_JSON[0])

    @inlineCallbacks
    def test_stop_node_connects_with_oauth_credentials(self):
        client, log = self.get_client()
        yield client.stop_node("foo")
        [factory_call] = [
            record for record in log if record.called == "factory"]
        self.assertIn("Authorization", factory_call.result.headers)

    @inlineCallbacks
    def test_release_node(self):
        """C{release_node} asks MAAS to release the node back to the pool.

        The node's new state is returned.
        """
        resource_uri = NODE_JSON[0]["resource_uri"]
        client, log = self.get_client()
        returned_data = yield client.release_node(resource_uri)
        self.assertEqual(returned_data, NODE_JSON[0])


class TestConstraints(TestCase, TestMAASClientBase):

    class fake_post:
        def __call__(self, uri, params):
            self.params_used = params

    def set_up_client_with_fake(self):
        fake = self.fake_post()
        client, log = self.get_client()
        self.patch(client, 'post', fake)
        return client

    def test_acquire_node_handles_name_constraint(self):
        # Ensure that the name constraint is passed through to the post
        # method.
        client = self.set_up_client_with_fake()
        constraints = {"maas-name": "gargleblaster"}
        client.acquire_node(constraints)

        name = client.post.params_used.get("name")
        self.assertEqual("gargleblaster", name)

    def test_acquire_node_ignores_unknown_constraints(self):
        # If an unknown constraint is passed it should be ignored.
        client = self.set_up_client_with_fake()
        constraints = {"maas-name": "zaphod", "guinness": "widget"}
        client.acquire_node(constraints)

        guinness = client.post.params_used.get("guinness")
        self.assertIs(None, guinness)
        name = client.post.params_used.get("name")
        self.assertEqual("zaphod", name)
