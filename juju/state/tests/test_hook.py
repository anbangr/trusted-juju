import yaml

from twisted.internet.defer import inlineCallbacks, returnValue
from juju.lib.pick import pick_attr
from juju.lib.testing import TestCase

from juju.state.endpoint import RelationEndpoint
from juju.state.hook import (
    DepartedRelationHookContext, HookContext, RelationChange,
    RelationHookContext)
from juju.state.errors import (
    UnitRelationStateNotFound, RelationBrokenContextError,
    RelationStateNotFound, InvalidRelationIdentity)
from juju.state.tests.test_relation import RelationTestBase
from juju.state.utils import AddedItem, DeletedItem, ModifiedItem


class RelationChangeTest(TestCase):

    def test_change_properties(self):

        change = RelationChange("db:42", "membership", "mysql/0")
        self.assertEqual(change.relation_name, "db")
        self.assertEqual(change.relation_ident, "db:42")
        self.assertEqual(change.change_type, "membership")
        self.assertEqual(change.unit_name, "mysql/0")


class CommonHookContextTestsMixin(object):

    @inlineCallbacks
    def test_config_get(self):
        """Verify we can get config settings.

        This is a simple test that basic I/O works through the
        context.
        """
        config = yield self.service.get_config()
        config.update({"hello": "world"})
        yield config.write()

        data = yield self.context.get_config()
        self.assertEqual(data, {"hello": "world", "title": "My Title",
                                "username": "admin001"})

        # Verify that context.flush triggers writes as well
        data["goodbye"] = "goodnight"
        yield self.context.flush()
        # get a new yamlstate from the service itself
        config = yield self.service.get_config()
        self.assertEqual(config["goodbye"], "goodnight")

    @inlineCallbacks
    def test_config_get_cache(self):
        """Verify we can get config settings.

        This is a simple test that basic I/O works through the
        context.
        """
        config = yield self.service.get_config()
        config.update({"hello": "world"})
        yield config.write()

        data = yield self.context.get_config()
        self.assertEqual(data, {"hello": "world",
                                "title": "My Title",
                                "username": "admin001"})

        d2 = yield self.context.get_config()
        self.assertIs(data, d2)

    @inlineCallbacks
    def test_hook_knows_service(self):
        """Verify that hooks can get their local service."""
        service = yield self.context.get_local_service()
        self.assertEqual(service.service_name, self.service.service_name)

    @inlineCallbacks
    def test_hook_knows_unit_state(self):
        """Verify that hook has access to its local unit state."""
        unit = yield self.context.get_local_unit_state()
        self.assertEqual(unit.unit_name, self.unit.unit_name)


class HookContextTestBase(RelationTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(HookContextTestBase, self).setUp()
        wordpress_ep = RelationEndpoint(
            "wordpress", "client-server", "database", "client")
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db", "server")
        self.wordpress_states = yield self.\
            add_relation_service_unit_from_endpoints(
                wordpress_ep, mysql_ep)
        self.mysql_states = yield self.add_opposite_service_unit(
            self.wordpress_states)
        self.relation = self.mysql_states["relation"]

    @inlineCallbacks
    def add_another_blog(self, service_name):
        blog_ep = RelationEndpoint(
            service_name, "client-server", "database", "client")
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db", "server")
        yield self.add_relation_service_unit_from_endpoints(
            blog_ep, mysql_ep)

    @inlineCallbacks
    def add_db_admin_tool(self, admin_name):
        """Add another relation, using a different relation name"""
        admin_ep = RelationEndpoint(
            admin_name, "client-server", "admin-app", "client")
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db-admin", "server")
        yield self.add_relation_service_unit_from_endpoints(
            admin_ep, mysql_ep)


class HookContextTest(HookContextTestBase, CommonHookContextTestsMixin):

    @inlineCallbacks
    def setUp(self):
        yield super(HookContextTest, self).setUp()
        self.service = self.wordpress_states["service"]
        self.unit = self.wordpress_states["unit"]
        self.context = HookContext(
            self.client, unit_name=self.unit.unit_name)

    def get_context(self, unit_name):
        return HookContext(self.client, unit_name=unit_name)

    @inlineCallbacks
    def test_get_relation_idents(self):
        """Verify relation idents can be queried on non-relation contexts."""
        yield self.add_another_blog("wordpress2")
        context = self.get_context("mysql/0")
        self.assertEqual(
            set((yield context.get_relation_idents("db"))),
            set(["db:0", "db:1"]))
        self.assertEqual(
            set((yield context.get_relation_idents("not-a-relation"))),
            set())
        
        # Add some more relations, verify nothing changes from this
        # context's perspective
        yield self.add_another_blog("wordpress3")
        yield self.add_db_admin_tool("admin")
        self.assertEqual(
            set((yield context.get_relation_idents("db"))),
            set(["db:0", "db:1"]))
        self.assertEqual(
            set((yield context.get_relation_idents(None))),
            set(["db:0", "db:1"]))

        # Create a new context to see this change
        new_context = self.get_context("mysql/0")
        self.assertEqual(
            set((yield new_context.get_relation_idents("db"))),
            set(["db:0", "db:1", "db:2"]))
        self.assertEqual(
            set((yield new_context.get_relation_idents(None))),
            set(["db:0", "db:1", "db:2", "db-admin:3"]))


class RelationHookContextTest(HookContextTestBase,
                              CommonHookContextTestsMixin):

    @inlineCallbacks
    def setUp(self):
        yield super(RelationHookContextTest, self).setUp()
        self.service = self.wordpress_states["service"]
        self.unit = self.wordpress_states["unit"]
        self.reset_context()

    def reset_context(self):
        self.context = self.get_context(
            self.wordpress_states, "modified", "mysql/0")

    def get_context(self, states, change_type, unit_name):
        change = RelationChange(
            states["service_relation"].relation_ident,
            change_type,
            unit_name)
        return RelationHookContext(
            self.client, states["unit_relation"],
            states["service_relation"].relation_ident,
            unit_name=states["unit"].unit_name)

    @inlineCallbacks
    def test_get(self):
        """Settings from a related unit can be retrieved as a blob."""
        yield self.mysql_states["unit_relation"].set_data({"hello": "world"})

        # use mocker to verify we only access the node once.
        mock_client = self.mocker.patch(self.client)
        mock_client.get(self.get_unit_settings_path(self.mysql_states))
        self.mocker.passthrough()
        self.mocker.replay()

        data = yield self.context.get("mysql/0")
        self.assertEqual(data, {"hello": "world"})

    @inlineCallbacks
    def test_get_uses_copied_dict(self):
        """If we retrieve the settings with a get, modifying those
        values does not modify the underlying write buffer. They
        must be explicitly set.
        """
        yield self.context.set_value("hello", u"world")
        data = yield self.context.get("wordpress/0")
        self.assertEqual(
            data,
            {"hello": "world", "private-address": "wordpress-0.example.com"})
        del data["hello"]
        current_data = yield self.context.get("wordpress/0")
        self.assertNotEqual(current_data, data)

        self.client.set(self.get_unit_settings_path(self.mysql_states),
                        yaml.dump({"hello": "world"}))
        data = yield self.context.get("mysql/0")
        data["abc"] = 1

        data = yield self.context.get("mysql/0")
        del data["hello"]

        current_data = yield self.context.get("mysql/0")
        self.assertEqual(current_data, {"hello": "world"})

    @inlineCallbacks
    def test_get_value(self):
        """Settings from a related unit can be retrieved by name."""

        # Getting a value from an existing empty unit is returns empty
        # strings for all keys.
        port = yield self.context.get_value("mysql/0", "port")
        self.assertEqual(port, "")

        # Write some data to retrieve and refetch the context
        yield self.mysql_states["unit_relation"].set_data({
                "host": "xe.example.com", "port": 2222})

        self.reset_context()

        # use mocker to verify we only access the node once.
        mock_client = self.mocker.patch(self.client)
        mock_client.get(self.get_unit_settings_path(self.mysql_states))
        self.mocker.passthrough()
        self.mocker.replay()

        port = yield self.context.get_value("mysql/0", "port")
        self.assertEqual(port, 2222)

        host = yield self.context.get_value("mysql/0", "host")
        self.assertEqual(host, "xe.example.com")

        magic = yield self.context.get_value("mysql/0", "unknown")
        self.assertEqual(magic, "")

        # fetching from a value from a non existent unit raises an error.
        yield self.assertFailure(
            self.context.get_value("mysql/5", "zebra"),
            UnitRelationStateNotFound)

    @inlineCallbacks
    def test_get_self_value(self):
        """Settings from the unit associated to context can be retrieved.

        This is also holds true for values locally modified on the context.
        """
        data = yield self.context.get_value("wordpress/0", "magic")
        self.assertEqual(data, "")

        yield self.context.set_value("magic", "room")
        data = yield self.context.get_value("wordpress/0", "magic")
        self.assertEqual(data, "room")

    @inlineCallbacks
    def test_set(self):
        """The unit relation settings can be done as a blob."""
        yield self.assertFailure(self.context.set("abc"), TypeError)
        data = {"abc": 12, "bar": "21"}
        yield self.context.set(data)
        changes = yield self.context.flush()
        content, stat = yield self.client.get(
            self.get_unit_settings_path(self.wordpress_states))
        data["private-address"] = "wordpress-0.example.com"
        self.assertEqual(yaml.load(content), data)
        self.assertEqual(changes,
                         [AddedItem("abc", 12), AddedItem("bar", "21")])

    @inlineCallbacks
    def test_set_value(self):
        """Values can be set by name, and are written at flush time."""
        yield self.context.set_value("zebra", 12)
        yield self.context.set_value("donkey", u"abc")
        data, stat = yield self.client.get(
            self.get_unit_settings_path(self.wordpress_states))
        self.assertEqual(yaml.load(data),
                         {"private-address": "wordpress-0.example.com"})

        changes = yield self.context.flush()
        data, stat = yield self.client.get(
            self.get_unit_settings_path(self.wordpress_states))

        self.assertEqual(
            yaml.load(data),
            {"zebra": 12, "donkey": "abc",
             "private-address": "wordpress-0.example.com"})

        self.assertEqual(
            changes,
            [AddedItem("donkey", u"abc"), AddedItem("zebra", 12)])

    @inlineCallbacks
    def test_delete_value(self):
        """A value can be deleted via key.
        """
        yield self.client.set(
            self.get_unit_settings_path(
                self.wordpress_states),
            yaml.dump({"key": "secret"}))

        yield self.context.delete_value("key")
        changes = yield self.context.flush()
        data, stat = yield self.client.get(
            self.get_unit_settings_path(self.wordpress_states))

        self.assertNotIn("key", yaml.load(data))
        self.assertEqual(changes, [DeletedItem("key", "secret")])

    @inlineCallbacks
    def test_delete_nonexistent_value(self):
        """Deleting a non existent key is a no-op.
        """
        yield self.client.set(
            self.get_unit_settings_path(self.wordpress_states),
            yaml.dump({"lantern": "green"}))

        yield self.context.delete_value("key")
        changes = yield self.context.flush()
        data, stat = yield self.client.get(
            self.get_unit_settings_path(self.wordpress_states))

        self.assertEqual(yaml.load(data), {"lantern": "green"})
        self.assertEqual(changes, [])

    @inlineCallbacks
    def test_empty_flush_maintains_value(self):
        """Flushing a context which has no writes is a noop."""
        yield self.client.set(
            self.get_unit_settings_path(self.wordpress_states),
            yaml.dump({"key": "secret"}))
        changes = yield self.context.flush()
        data, stat = yield self.client.get(
            self.get_unit_settings_path(self.wordpress_states))

        self.assertEqual(yaml.load(data),
                         {"key": "secret"})
        self.assertEqual(changes, [])

    @inlineCallbacks
    def test_flush_merges_setting_values(self):
        """When flushing a context we merge the changes with the current
        value of the node. The goal is to allow external processes to
        modify, delete, and add new values and allow those changes to
        persist to the final state, IFF the context has not also modified
        that key. If the context has modified the key, then context change
        takes precendence over the external change.
        """
        data = {"key": "secret",
                "seed": "21",
                "castle": "keep",
                "tower": "moat",
                "db": "wordpress",
                "host": "xe1.example.com"}

        yield self.client.set(
            self.get_unit_settings_path(self.wordpress_states),
            yaml.dump(data))

        # On the context:
        #  - add a new key
        #  - modify an existing key
        #  - delete an old key/value
        yield self.context.set_value("home", "good")
        yield self.context.set_value("db", 21)
        yield self.context.delete_value("seed")
        # Also test conflict on delete, modify, and add
        yield self.context.delete_value("castle")
        yield self.context.set_value("tower", "rock")
        yield self.context.set_value("zoo", "keeper")

        # Outside of the context:
        #  - add a new key/value.
        #  - modify an existing value
        #  - delete a key
        data["port"] = 22
        data["host"] = "xe2.example.com"
        del data["key"]
        # also test conflict on delete, modify, and add
        del data["castle"]
        data["zoo"] = "mammal"
        data["tower"] = "london"

        yield self.client.set(
            self.get_unit_settings_path(self.wordpress_states),
            yaml.dump(data))

        changes = yield self.context.flush()

        data, stat = yield self.client.get(
            self.get_unit_settings_path(self.wordpress_states))

        self.assertEqual(
            yaml.load(data),
            {"port": 22, "host": "xe2.example.com", "db": 21, "home": "good",
             "tower": "rock", "zoo": "keeper"})
        self.assertEqual(
            changes,
            [ModifiedItem("db", "wordpress", 21),
             AddedItem("zoo", "keeper"),
             DeletedItem("seed", "21"),
             AddedItem("home", "good"),
             ModifiedItem("tower", "moat", "rock")])

    @inlineCallbacks
    def test_set_value_existing_setting(self):
        """We can set a value even if we have existing saved settings."""
        yield self.client.set(
            self.get_unit_settings_path(self.wordpress_states),
            yaml.dump({"key": "secret"}))
        yield self.context.set_value("magic", "room")
        value = yield self.context.get_value("wordpress/0", "key")
        self.assertEqual(value, "secret")

        value = yield self.context.get_value("wordpress/0", "magic")
        self.assertEqual(value, "room")

    @inlineCallbacks
    def test_get_members(self):
        """The related units of a  relation can be retrieved."""
        members = yield self.context.get_members()
        self.assertEqual(members, ["mysql/0"])
        # Add a new member and refetch
        yield self.add_related_service_unit(self.mysql_states)
        members2 = yield self.context.get_members()
        # There should be no change in the retrieved members.
        self.assertEqual(members, members2)

    @inlineCallbacks
    def test_get_members_peer(self):
        """When retrieving members from a peer relation, the unit
        associated to the context is not included in the set.
        """
        riak1_states = yield self.add_relation_service_unit(
            "riak", "riak", "peer", "peer")
        riak2_states = yield self.add_related_service_unit(
            riak1_states)
        context = self.get_context(riak1_states, "modified", "riak/1")
        members = yield context.get_members()
        self.assertEqual(members, [riak2_states["unit"].unit_name])

    @inlineCallbacks
    def test_tracking_read_nodes(self):
        """The context tracks which nodes it has read, this is used
        by external components to determine if the context has
        read a value that may have subsequently been modified, and
        act accordingly.
        """
        # notify the context of a change
        self.assertFalse(self.context.has_read("mysql/0"))

        # read the node data
        yield self.context.get("mysql/0")

        # Now verify we've read it
        self.assertTrue(self.context.has_read("mysql/0"))

        # And only it.
        self.assertFalse(self.context.has_read("mysql/1"))

    @inlineCallbacks
    def test_invalid_get_relation_id_and_scope(self):
        """Verify `InvalidRelationIdentity` is raised for invalid idents"""
        yield self.assertFailure(
            self.context.get_relation_id_and_scope("not-a-relation:99"),
            RelationStateNotFound)
        e = yield self.assertFailure(
            self.context.get_relation_id_and_scope("invalid-id:forty-two"),
            InvalidRelationIdentity)
        self.assertEqual(
            str(e), "Not a valid relation id: 'invalid-id:forty-two'")
        yield self.assertFailure(
            self.context.get_relation_id_and_scope("invalid-id*42"),
            InvalidRelationIdentity)
        yield self.assertFailure(
            self.context.get_relation_id_and_scope("invalid-id:42:extra"),
            InvalidRelationIdentity)
        yield self.assertFailure(
            self.context.get_relation_id_and_scope("unknown-name:0"),
            RelationStateNotFound)

    @inlineCallbacks
    def test_get_relation_id_and_scope(self):
        """Verify relation id and scope is returned for relation idents"""
        # The mysql service has relations with two wordpress services,
        # verify that the corresponding relation hook context can see
        # both relations
        yield self.add_another_blog("wordpress2")
        mysql_context = self.get_context(
            self.mysql_states, "modified", "mysql/0")
        self.assertEqual(
            (yield mysql_context.get_relation_id_and_scope("db:0")),
            ("relation-0000000000", "global"))
        self.assertEqual(
            (yield mysql_context.get_relation_id_and_scope("db:1")),
            ("relation-0000000001", "global"))

        # Need to use the correct relation name in the relation id
        yield self.assertFailure(
            mysql_context.get_relation_id_and_scope("database:0"),
            RelationStateNotFound)
        yield self.assertFailure(
            mysql_context.get_relation_id_and_scope("database:1"),
            RelationStateNotFound)
        
        # The first wordpress service can only see the relation it
        # has with mysql in its hook context
        wordpress1_context = self.get_context(
            self.wordpress_states, "modified", "wordpress/0")
        self.assertEqual(
            (yield wordpress1_context.get_relation_id_and_scope("database:0")),
            ("relation-0000000000", "global"))
        yield self.assertFailure(
            wordpress1_context.get_relation_id_and_scope("database:1"),
            RelationStateNotFound)
        yield self.assertFailure(
            wordpress1_context.get_relation_id_and_scope("db:0"),
            RelationStateNotFound)

    @inlineCallbacks
    def test_get_relation_hook_context(self):
        """Verify usage of child hook contexts"""
        yield self.add_another_blog("wordpress2")
        context = self.get_context(self.mysql_states, "modified", "mysql/0")
        self.assertEqual(
            set((yield context.get_relation_idents("db"))),
            set(["db:0", "db:1"]))
        db0 = yield context.get_relation_hook_context("db:0")
        self.assertEqual(db0.relation_ident, context.relation_ident)
        self.assertEqual((yield db0.get_members()), ["wordpress/0"])
        # But unlike through the Invoker, no caching so these contexts
        # will not be identical
        self.assertNotEqual(db0, context)

        db1 = yield context.get_relation_hook_context("db:1")
        self.assertEqual(db1.relation_ident, "db:1")
        self.assertEqual((yield db1.get_members()), ["wordpress2/0"])
        
        # Add some more relations, verify nothing changes from this
        # context's perspective
        yield self.add_another_blog("wordpress3")
        yield self.assertFailure(
            context.get_relation_hook_context("db:2"),
            RelationStateNotFound)

        # Next, create a new context to see this change in child contexts
        new_context = self.get_context(
            self.mysql_states, "modified", "mysql/0")
        db2 = yield new_context.get_relation_hook_context("db:2")
        self.assertEqual(db2.relation_ident, "db:2")
        self.assertEqual((yield db2.get_members()), ["wordpress3/0"])

    @inlineCallbacks
    def test_get_relation_hook_context_while_removing_relation(self):
        """Verify usage of child hook contexts once a relation is removed"""
        yield self.add_another_blog("wordpress2")
        context = self.get_context(self.mysql_states, "modified", "mysql/0")
        self.assertEqual(
            set((yield context.get_relation_idents("db"))),
            set(["db:0", "db:1"]))

        # Remove the first relation (db:0). Verify it's still cached
        # from this context
        yield self.relation_manager.remove_relation_state(
            self.wordpress_states["relation"])
        self.assertEqual(
            set((yield context.get_relation_idents("db"))),
            set(["db:0", "db:1"]))
        db0 = yield context.get_relation_hook_context("db:0")
        db1 = yield context.get_relation_hook_context("db:1")
        self.assertEqual(db0.relation_ident, "db:0")
        self.assertEqual(db1.relation_ident, "db:1")

        # Unit membership changes (with the removed db:0 relation),
        # however, are visible since get_members does another topology
        # read (albeit subsequently cached)
        self.assertEqual((yield db0.get_members()), [])
        self.assertEqual((yield db1.get_members()), ["wordpress2/0"])
        
        # Create a new context and verify db:0 is now gone
        new_context = self.get_context(
            self.mysql_states, "modified", "mysql/0")
        self.assertEqual(
            set((yield new_context.get_relation_idents("db"))),
            set(["db:1"]))
        yield self.assertFailure( 
            new_context.get_relation_hook_context("db:0"),
            RelationStateNotFound)
        db1 = yield new_context.get_relation_hook_context("db:1")
        self.assertEqual(db1.relation_ident, "db:1")
        self.assertEqual((yield db1.get_members()), ["wordpress2/0"])

    @inlineCallbacks
    def test_get_relation_idents(self):
        """Verify relation idents can be queried on relation hook contexts."""
        yield self.add_another_blog("wordpress2")
        context = self.get_context(self.mysql_states, "modified", "mysql/0")
        self.assertEqual(
            set((yield context.get_relation_idents("db"))),
            set(["db:0", "db:1"]))
        self.assertEqual(
            set((yield context.get_relation_idents("not-a-relation"))),
            set())
        
        # Add some more relations, verify nothing changes from this
        # context's perspective
        yield self.add_another_blog("wordpress3")
        yield self.add_db_admin_tool("admin")
        self.assertEqual(
            set((yield context.get_relation_idents("db"))),
            set(["db:0", "db:1"]))
        self.assertEqual(
            set((yield context.get_relation_idents(None))),
            set(["db:0", "db:1"]))

        # Create a new context to see this change
        new_context = self.get_context(
            self.mysql_states, "modified", "mysql/0")
        self.assertEqual(
            set((yield new_context.get_relation_idents("db"))),
            set(["db:0", "db:1", "db:2"]))
        self.assertEqual(
            set((yield new_context.get_relation_idents(None))),
            set(["db:0", "db:1", "db:2", "db-admin:3"]))


class DepartedRelationHookContextTest(
        HookContextTestBase, CommonHookContextTestsMixin):

    @inlineCallbacks
    def setUp(self):
        yield super(DepartedRelationHookContextTest, self).setUp()
        self.service = self.wordpress_states["service"]
        self.unit = self.wordpress_states["unit"]
        relation = self.wordpress_states["service_relation"]
        self.context = DepartedRelationHookContext(
            self.client, self.unit.unit_name, self.unit.internal_id,
            relation.relation_name, relation.internal_relation_id)

    @inlineCallbacks
    def test_get_members(self):
        """Related units cannot be retrieved."""
        members = yield self.context.get_members()
        self.assertEqual(members, [])
        # Add a new member and refetch
        yield self.add_related_service_unit(self.mysql_states)
        members2 = yield self.context.get_members()
        # There should be no change in the retrieved members.
        self.assertEqual(members2, [])

    @inlineCallbacks
    def test_get_self(self):
        """Own settings can be retrieved."""
        self.client.set(self.get_unit_settings_path(self.wordpress_states),
                        yaml.dump({"hello": "world"}))
        data = yield self.context.get(None)
        self.assertEquals(data, {"hello": "world"})

    @inlineCallbacks
    def test_get_self_by_name(self):
        """Own settings can be retrieved by name."""
        self.client.set(self.get_unit_settings_path(self.wordpress_states),
                        yaml.dump({"hello": "world"}))
        data = yield self.context.get("wordpress/0")
        self.assertEquals(data, {"hello": "world"})

    @inlineCallbacks
    def test_get_other(self):
        """Other unit settings cannot be retrieved."""
        e = yield self.assertFailure(
            self.context.get("mysql/0"), RelationBrokenContextError)
        self.assertEquals(
            str(e), "Cannot access other units in broken relation")

    @inlineCallbacks
    def test_get_value_self(self):
        """Own settings can be retrieved."""
        self.client.set(self.get_unit_settings_path(self.wordpress_states),
                        yaml.dump({"hello": "world"}))
        self.assertEquals(
            (yield self.context.get_value("wordpress/0", "hello")), "world")
        self.assertEquals(
            (yield self.context.get_value("wordpress/0", "goodbye")), "")

    @inlineCallbacks
    def test_get_value_other(self):
        """Other unit settings cannot be retrieved."""
        e = yield self.assertFailure(
            self.context.get_value("mysql/0", "anything"),
            RelationBrokenContextError)
        self.assertEquals(
            str(e), "Cannot access other units in broken relation")

    @inlineCallbacks
    def test_set(self):
        """Own settings cannot be changed."""
        e = yield self.assertFailure(
            self.context.set({"anything": "anything"}),
            RelationBrokenContextError)
        self.assertEquals(
            str(e), "Cannot change settings in broken relation")

    @inlineCallbacks
    def test_set_value(self):
        """Own settings cannot be changed."""
        e = yield self.assertFailure(
            self.context.set_value("anything", "anything"),
            RelationBrokenContextError)
        self.assertEquals(
            str(e), "Cannot change settings in broken relation")

    @inlineCallbacks
    def test_delete_value(self):
        """Own settings cannot be changed."""
        e = yield self.assertFailure(
            self.context.delete_value("anything"),
            RelationBrokenContextError)
        self.assertEquals(
            str(e), "Cannot change settings in broken relation")

    @inlineCallbacks
    def test_has_read(self):
        """We can tell whether settings have been read"""
        self.assertFalse(self.context.has_read("wordpress/0"))
        self.assertFalse(self.context.has_read("mysql/0"))
        yield self.context.get(None)
        self.assertTrue(self.context.has_read("wordpress/0"))
        self.assertFalse(self.context.has_read("mysql/0"))
        yield self.assertFailure(
            self.context.get_value("mysql/0", "anything"),
            RelationBrokenContextError)
        self.assertTrue(self.context.has_read("wordpress/0"))
        self.assertFalse(self.context.has_read("mysql/0"))

    @inlineCallbacks
    def test_relation_ident(self):
        """Verify relation ident and enumerate other relations for context."""
        self.assertEqual(self.context.relation_ident, "database:0")
        self.assertEqual((yield self.context.get_relation_idents(None)),
                         ["database:0"])


class SubordinateRelationHookContextTest(HookContextTestBase):

    @inlineCallbacks
    def setUp(self):
        yield super(SubordinateRelationHookContextTest, self).setUp()
        relation = yield self._build_relation()

        self.context = DepartedRelationHookContext(
            self.client, self.unit.unit_name, self.unit.internal_id,
            relation.relation_name, relation.internal_relation_id)

    @inlineCallbacks
    def _build_relation(self):
        mysql_ep = RelationEndpoint("mysql", "juju-info", "juju-info",
                                    "server", "global")
        logging_ep = RelationEndpoint("logging", "juju-info", "juju-info",
                                      "client", "container")

        mysql, my_units = yield self.get_service_and_units_by_charm_name(
            "mysql", 1)
        self.assertFalse((yield mysql.is_subordinate()))

        log, log_units = yield self.get_service_and_units_by_charm_name(
            "logging")
        self.assertTrue((yield log.is_subordinate()))

        # add the relationship so we can create units with  containers
        relation_state, service_states = (yield
            self.relation_manager.add_relation_state(
            mysql_ep, logging_ep))

        log, log_units = yield self.get_service_and_units_by_charm_name(
            "logging",
            containers=my_units)
        self.assertTrue((yield log.is_subordinate()))
        for lu in log_units:
            self.assertTrue((yield lu.is_subordinate()))

        self.mu1 = my_units[0]
        self.lu1 = log_units[0]

        self.mystate = pick_attr(service_states, relation_role="server")
        self.logstate = pick_attr(service_states, relation_role="client")

        yield self.mystate.add_unit_state(self.mu1)
        self.log_unit_relation = yield self.logstate.add_unit_state(self.lu1)
        self.my_unit_relation = yield self.mystate.add_unit_state(self.mu1)
        self.unit = self.lu1
        self.container_unit = self.mu1

        self.service = log
        self.relation_state = relation_state

        returnValue(self.logstate)

    def get_unit_settings_path(self, relation, unit, container=None):
        container_info = ""
        if container:
            container_info = "%s/" % container.internal_id
        unit_relation_path = "/relations/%s/%ssettings/%s" % (
            relation.internal_id,
            container_info,
            unit.internal_id)
        return unit_relation_path

    @inlineCallbacks
    def test_get_departed_members(self):
        """Related units cannot be retrieved."""
        members = yield self.context.get_members()
        self.assertEqual(members, [])
        # Add a new member and refetch
        yield self.add_related_service_unit(self.mysql_states)
        members2 = yield self.context.get_members()
        # There should be no change in the retrieved members.
        self.assertEqual(members2, [])

    @inlineCallbacks
    def test_get_self(self):
        """Own settings can be retrieved."""
        settings_path = self.get_unit_settings_path(
            self.relation_state,
            self.unit,
            self.container_unit)
        self.client.set(settings_path,
                        yaml.dump({"hello": "world"}))
        data = yield self.context.get(None)
        self.assertEquals(data, {"hello": "world"})

    def get_context(self, service_relation, unit_relation, unit,
                    change_type, unit_name):
        change = RelationChange(
            service_relation.relation_ident,
            change_type,
            unit_name)
        return RelationHookContext(
            self.client, unit_relation, change,
            unit_name=unit.unit_name)

    @inlineCallbacks
    def test_get_members_subordinate_context(self):
        context = self.get_context(self.logstate, self.log_unit_relation,
                                   self.lu1, "joined", "logging/0")
        self.assertEquals((yield context.get_members()),
                          ["mysql/1"])

        context = self.get_context(self.mystate, self.my_unit_relation,
                                   self.mu1, "joined", "mysql/1")
        self.assertEquals((yield context.get_members()),
                          ["logging/0"])

    @inlineCallbacks
    def test_get_settings_path(self):
        @inlineCallbacks
        def verify_settings_path(service_relation, unit_relation,
                                 unit, expected):
            sp = yield unit_relation.get_settings_path()
            self.assertEquals(sp, expected)

            context = self.get_context(service_relation, unit_relation,
                                   unit, "joined", "")
            sp2 = yield context.get_settings_path(unit.internal_id)
            self.assertEquals(sp2, expected)

        yield verify_settings_path(self.mystate, self.my_unit_relation,
                                   self.mu1,
                                   "/relations/relation-0000000001/"
                                   "unit-0000000002/settings/unit-0000000002")

        yield verify_settings_path(self.logstate, self.log_unit_relation,
                                   self.lu1,
                                   "/relations/relation-0000000001/"
                                   "unit-0000000002/settings/unit-0000000003")
