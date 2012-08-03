from StringIO import StringIO
import json
import logging
import os
import stat
import sys
import yaml

from twisted.internet import defer
from twisted.internet.process import Process

import juju
from juju import errors
from juju.control.tests.test_status import StatusTestBase
from juju.environment.tests.test_config import EnvironmentsConfigTestBase
from juju.lib.pick import pick_attr
from juju.hooks import invoker
from juju.hooks import commands
from juju.hooks.protocol import UnitSettingsFactory
from juju.lib.mocker import MATCH
from juju.lib.twistutils import get_module_directory
from juju.state import hook
from juju.state.endpoint import RelationEndpoint
from juju.state.errors import RelationStateNotFound
from juju.state.relation import RelationStateManager
from juju.state.tests.test_relation import RelationTestBase


class MockUnitAgent(object):
    """Pretends to implement the client state cache, and the UA hook socket.
    """
    def __init__(self, client, socket_path, charm_dir):
        self.client = client
        self.socket_path = socket_path
        self.charm_dir = charm_dir
        self._clients = {}   # client_id -> HookContext
        self._invokers = {}  # client_id -> Invoker

        self._agent_log = logging.getLogger("unit-agent")
        self._agent_io = StringIO()
        handler = logging.StreamHandler(self._agent_io)
        self._agent_log.addHandler(handler)

        self.server_listen()

    def make_context(self, relation_ident, change_type, unit_name,
                     unit_relation, client_id):
        """Create, record and return a HookContext object for a change."""
        change = hook.RelationChange(relation_ident, change_type, unit_name)
        context = hook.RelationHookContext(
            self.client, unit_relation, relation_ident, unit_name=unit_name)
        self._clients[client_id] = context
        return context, change

    def get_logger(self):
        """Build a logger to be used for a hook."""
        logger = logging.getLogger("hook")
        log_file = StringIO()
        handler = logging.StreamHandler(log_file)
        logger.addHandler(handler)
        return logger

    @defer.inlineCallbacks
    def get_invoker(self, relation_ident, change_type,
                    unit_name, unit_relation, client_id="client_id"):
        """Build an Invoker for the execution of a hook.

        `relation_ident`: relation identity of the relation the Invoker is for.
        `change_type`: the string name of the type of change the hook
                       is invoked for.
        `unit_name`: the name of the local unit of the change.
        `unit_relation`: a UnitRelationState instance for the hook.
        `client_id`: unique client identifier.
        `service`: The local service of the executing hook.
        """
        context, change = self.make_context(
            relation_ident, change_type,
            unit_name, unit_relation, client_id)
        logger = self.get_logger()

        exe = invoker.Invoker(context, change,
                              self.get_client_id(),
                              self.socket_path,
                              self.charm_dir,
                              logger)
        yield exe.start()
        self._invokers[client_id] = exe
        defer.returnValue(exe)

    def get_client_id(self):
        # simulate associating a client_id with a client connection
        # for later context look up. In reality this would be a mapping.
        return "client_id"

    def get_context(self, client_id):
        return self._clients[client_id]

    def lookup_invoker(self, client_id):
        return self._invokers[client_id]

    def stop(self):
        """Stop the process invocation.

        Trigger any registered cleanup functions.
        """
        self.server_socket.stopListening()

    def server_listen(self):
        from twisted.internet import reactor

        # hook context and a logger to the settings factory
        logger = logging.getLogger("unit-agent")
        self.log_file = StringIO()
        handler = logging.StreamHandler(self.log_file)
        logger.addHandler(handler)
        self.server_factory = UnitSettingsFactory(
            self.get_context, self.lookup_invoker, logger)

        self.server_socket = reactor.listenUNIX(
            self.socket_path, self.server_factory)


def get_cli_environ_path(*search_path):
    """Construct a path environment variable.

    This path will contain the juju bin directory and any paths
    passed as *search_path.

    @param search_path: additional directories to put on PATH
    """
    search_path = list(search_path)

    # Look for the top level juju bin directory and make sure
    # that is available for the client utilities.
    bin_path = os.path.normpath(
        os.path.join(get_module_directory(juju), "..", "bin"))

    search_path.append(bin_path)
    search_path.extend(os.environ.get("PATH", "").split(":"))

    return ":".join(search_path)


class InvokerTestBase(EnvironmentsConfigTestBase):

    @defer.inlineCallbacks
    def setUp(self):
        yield super(InvokerTestBase, self).setUp()
        yield self.push_default_config()

    def update_invoker_env(self, local_unit, remote_unit):
        """Update os.env for a hook invocation.

        Update the invoker (and hence the hook) environment with the
        path to the juju cli utils, and the local unit name.
        """
        test_hook_path = os.path.join(
            os.path.abspath(
                os.path.dirname(__file__)).replace("/_trial_temp", ""),
            "hooks")
        self.change_environment(
            PATH=get_cli_environ_path(test_hook_path, "/usr/bin", "/bin"),
            JUJU_UNIT_NAME=local_unit,
            JUJU_REMOTE_UNIT=remote_unit)

    def get_test_hook(self, hook):
        """Search for the test hook under the testing directory.

        Returns the full path name of the hook to be invoked from its
        basename.
        """
        dirname = os.path.dirname(__file__)
        abspath = os.path.abspath(dirname)
        hook_file = os.path.join(abspath, "hooks", hook)

        if not os.path.exists(hook_file):
            # attempt to find it via sys_path
            for p in sys.path:
                hook_file = os.path.normpath(
                    os.path.join(p, dirname, "hooks", hook))
                if os.path.exists(hook_file):
                    return hook_file
            raise IOError("%s doesn't exist" % hook_file)
        return hook_file

    def get_cli_hook(self, hook):
        bin_path = os.path.normpath(
            os.path.join(get_module_directory(juju), "..", "bin"))
        return os.path.join(bin_path, hook)

    def create_hook(self, hook, arguments):
        bin_path = self.get_cli_hook(hook)
        fn = self.makeFile("#!/bin/sh\n'%s' %s" % (bin_path, arguments))
        # make the hook executable
        os.chmod(fn, stat.S_IEXEC | stat.S_IREAD)
        return fn


class TestCompleteInvoker(InvokerTestBase, StatusTestBase):

    @defer.inlineCallbacks
    def setUp(self):
        yield super(TestCompleteInvoker, self).setUp()

        self.update_invoker_env("mysql/0", "wordpress/0")
        self.socket_path = self.makeFile()
        unit_dir = self.makeDir()
        self.makeDir(path=os.path.join(unit_dir, "charm"))
        self.ua = MockUnitAgent(
            self.client,
            self.socket_path,
            unit_dir)

    @defer.inlineCallbacks
    def tearDown(self):
        self.ua.stop()
        yield super(TestCompleteInvoker, self).tearDown()

    @defer.inlineCallbacks
    def build_default_relationships(self):
        state = yield self.build_topology(skip_unit_agents=("*",))
        myr = yield self.relation_state_manager.get_relations_for_service(
            state["services"]["mysql"])
        self.mysql_relation = yield myr[0].add_unit_state(
            state["relations"]["mysql"][0])
        wpr = yield self.relation_state_manager.get_relations_for_service(
            state["services"]["wordpress"])
        wpr = [r for r in wpr if r.internal_relation_id == \
                 self.mysql_relation.internal_relation_id][0]
        self.wordpress_relation = yield wpr.add_unit_state(
            state["relations"]["wordpress"][0])

        defer.returnValue(state)

    @defer.inlineCallbacks
    def test_get_from_different_unit(self):
        """Verify that relation-get works with a remote unit.

        This test will run the logic of relation-get and will ensure
        that, even though we're running the hook within the context of
        unit A, a hook can obtain the data from unit B using
        relation-get. To do this a more complete simulation of the
        runtime is needed than with the local test cases below.
        """
        yield self.build_default_relationships()
        yield self.wordpress_relation.set_data({"hello": "world"})

        hook_log = self.capture_logging("hook")
        exe = yield self.ua.get_invoker("db:42", "add", "mysql/0",
                                  self.mysql_relation,
                                  client_id="client_id")

        yield exe(self.create_hook(
            "relation-get", "--format=json - wordpress/0"))
        self.assertEqual({"hello": "world"},
                         json.loads(hook_log.getvalue()))

    @defer.inlineCallbacks
    def test_spawn_cli_get_hook_no_args(self):
        """Validate the get hook works with no (or all default) args.

        This should default to the remote unit. We do pass a format
        arg so we can marshall the data.
        """
        yield self.build_default_relationships()
        yield self.wordpress_relation.set_data({"hello": "world"})

        hook_log = self.capture_logging("hook")
        exe = yield self.ua.get_invoker(
            "db:42", "add", "mysql/0", self.mysql_relation,
            client_id="client_id")
        exe.environment["JUJU_REMOTE_UNIT"] = "wordpress/0"

        result = yield exe(self.create_hook("relation-get", "--format=json"))
        self.assertEqual(result, 0)
        # verify that its the wordpress data
        self.assertEqual({"hello": "world"},
                         json.loads(hook_log.getvalue()))

    @defer.inlineCallbacks
    def test_spawn_cli_get_implied_unit(self):
        """Validate the get hook can transmit values to the hook."""
        yield self.build_default_relationships()

        hook_log = self.capture_logging("hook")

        # Populate and verify some data we will
        # later extract with the hook
        expected = {"name": "rabbit",
                    "forgotten": "lyrics",
                    "nottobe": "requested"}
        yield self.wordpress_relation.set_data(expected)

        exe = yield self.ua.get_invoker(
            "db:42", "add", "mysql/0", self.mysql_relation,
            client_id="client_id")
        exe.environment["JUJU_REMOTE_UNIT"] = "wordpress/0"

        # invoke relation-get and verify the result
        result = yield exe(self.create_hook("relation-get", "--format=json -"))
        self.assertEqual(result, 0)
        data = json.loads(hook_log.getvalue())
        self.assertEqual(data["name"], "rabbit")
        self.assertEqual(data["forgotten"], "lyrics")

    @defer.inlineCallbacks
    def test_spawn_cli_get_format_shell(self):
        """Validate the get hook can transmit values to the hook."""
        yield self.build_default_relationships()

        hook_log = self.capture_logging("hook")

        # Populate and verify some data we will
        # later extract with the hook
        expected = {"name": "rabbit",
                    "forgotten": "lyrics"}
        yield self.wordpress_relation.set_data(expected)

        exe = yield self.ua.get_invoker(
            "db:42", "add", "mysql/0", self.mysql_relation,
            client_id="client_id")
        exe.environment["JUJU_REMOTE_UNIT"] = "wordpress/0"

        # invoke relation-get and verify the result
        result = yield exe(
            self.create_hook("relation-get", "--format=shell -"))
        self.assertEqual(result, 0)
        data = hook_log.getvalue()

        self.assertEqual('VAR_FORGOTTEN=lyrics\nVAR_NAME=rabbit\n\n', data)

        # and with a single value request
        hook_log.truncate(0)
        result = yield exe(
            self.create_hook("relation-get", "--format=shell name"))
        self.assertEqual(result, 0)
        data = hook_log.getvalue()
        self.assertEqual('VAR_NAME=rabbit\n\n', data)

    @defer.inlineCallbacks
    def test_relation_get_format_shell_bad_vars(self):
        """If illegal values are make somehow available warn."""
        yield self.build_default_relationships()
        hook_log = self.capture_logging("hook")

        # Populate and verify some data we will
        # later extract with the hook
        expected = {"BAR": "none", "funny-chars*":  "should work"}
        yield self.wordpress_relation.set_data(expected)

        exe = yield self.ua.get_invoker(
            "db:42", "add", "mysql/0", self.mysql_relation,
            client_id="client_id")
        exe.environment["JUJU_REMOTE_UNIT"] = "wordpress/0"
        exe.environment["VAR_FOO"] = "jungle"

        result = yield exe(
            self.create_hook("relation-get", "--format=shell -"))
        self.assertEqual(result, 0)

        yield exe.ended
        data = hook_log.getvalue()
        self.assertIn('VAR_BAR=none', data)
        # Verify that illegal shell variable names get converted
        # in an expected way
        self.assertIn("VAR_FUNNY_CHARS_='should work'", data)

        # Verify that it sets VAR_FOO to null because it shouldn't
        # exist in the environment
        self.assertIn("VAR_FOO=", data)
        self.assertIn("The following were omitted from", data)

    @defer.inlineCallbacks
    def test_hook_exec_in_charm_directory(self):
        """Hooks are executed in the charm directory."""
        yield self.build_default_relationships()
        hook_log = self.capture_logging("hook")
        exe = yield self.ua.get_invoker(
            "db:42", "add", "mysql/0", self.mysql_relation,
            client_id="client_id")
        self.assertTrue(os.path.isdir(exe.unit_path))
        exe.environment["JUJU_REMOTE_UNIT"] = "wordpress/0"

        # verify the hook's execution directory
        hook_path = self.makeFile("#!/bin/bash\necho $PWD")
        os.chmod(hook_path, stat.S_IEXEC | stat.S_IREAD)
        result = yield exe(hook_path)
        self.assertEqual(hook_log.getvalue().strip(),
                         os.path.join(exe.unit_path, "charm"))
        self.assertEqual(result, 0)

        # Reset the output capture
        hook_log.seek(0)
        hook_log.truncate()

        # Verify the environment variable is set.
        hook_path = self.makeFile("#!/bin/bash\necho $CHARM_DIR")
        os.chmod(hook_path, stat.S_IEXEC | stat.S_IREAD)
        result = yield exe(hook_path)
        self.assertEqual(hook_log.getvalue().strip(),
                         os.path.join(exe.unit_path, "charm"))

    @defer.inlineCallbacks
    def test_spawn_cli_config_get(self):
        """Validate that config-get returns expected values."""
        yield self.build_default_relationships()

        hook_log = self.capture_logging("hook")

        # Populate and verify some data we will
        # later extract with the hook
        expected = {"name": "rabbit",
                    "forgotten": "lyrics",
                    "nottobe": "requested"}

        exe = yield self.ua.get_invoker(
            "db:42", "add", "mysql/0", self.mysql_relation,
            client_id="client_id")

        context = yield self.ua.get_context("client_id")
        config = yield context.get_config()
        config.update(expected)
        yield config.write()

        # invoke relation-get and verify the result

        result = yield exe(self.create_hook("config-get", "--format=json"))
        self.assertEqual(result, 0)

        data = json.loads(hook_log.getvalue())
        self.assertEqual(data["name"], "rabbit")
        self.assertEqual(data["forgotten"], "lyrics")


class RelationInvokerTestBase(InvokerTestBase, RelationTestBase):

    @defer.inlineCallbacks
    def setUp(self):
        yield super(RelationInvokerTestBase, self).setUp()
        yield self._default_relations()
        self.update_invoker_env("mysql/0", "wordpress/0")
        self.socket_path = self.makeFile()
        unit_dir = self.makeDir()
        self.makeDir(path=os.path.join(unit_dir, "charm"))
        self.ua = MockUnitAgent(
            self.client,
            self.socket_path,
            unit_dir)
        self.log = self.capture_logging(
            formatter=logging.Formatter(
                "%(name)s:%(levelname)s:: %(message)s"),
            level=logging.DEBUG)

    @defer.inlineCallbacks
    def tearDown(self):
        self.ua.stop()
        yield super(RelationInvokerTestBase, self).tearDown()

    @defer.inlineCallbacks
    def _default_relations(self):
        wordpress_ep = RelationEndpoint(
            "wordpress", "client-server", "app", "client")
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db", "server")
        self.wordpress_states = yield self.\
            add_relation_service_unit_from_endpoints(wordpress_ep, mysql_ep)
        self.mysql_states = yield self.add_opposite_service_unit(
            self.wordpress_states)
        self.relation = self.mysql_states["unit_relation"]


class InvokerTest(RelationInvokerTestBase):

    def test_environment(self):
        """Test various way to manipulate the calling environment.
        """
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        exe.environment.update(dict(FOO="bar"))
        env = exe.get_environment()

        # these come from the init argument
        self.assertEqual(env["JUJU_AGENT_SOCKET"], self.socket_path)
        self.assertEqual(env["JUJU_CLIENT_ID"], "client_id")

        # this comes from updating the Invoker.environment
        self.assertEqual(env["FOO"], "bar")

        # and this comes from the unit agent passing through its environment
        self.assertTrue(env["PATH"])
        self.assertEqual(env["JUJU_UNIT_NAME"], "mysql/0")

        # Set for all hooks
        self.assertEqual(env["DEBIAN_FRONTEND"], "noninteractive")
        self.assertEqual(env["APT_LISTCHANGES_FRONTEND"], "none")

    def test_missing_hook(self):
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        self.failUnlessRaises(errors.FileNotFound, exe, "hook-missing")

    def test_noexec_hook(self):
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        hook = self.get_test_hook("noexec-hook")
        error = self.failUnlessRaises(errors.CharmError, exe, hook)

        self.assertEqual(error.path, hook)
        self.assertEqual(error.message, "hook is not executable")

    @defer.inlineCallbacks
    def test_unhandled_signaled_on_hook(self):
        """A hook that executes as a result of an unhandled signal is an error.
        """
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        hook_exec = exe(self.get_test_hook("sleep-hook"))
        # Send the process a signal to kill it
        exe._process.signalProcess("HUP")
        error = yield self.assertFailure(
            hook_exec, errors.CharmInvocationError)
        self.assertIn(
            "sleep-hook': signal 1.", str(error))

    @defer.inlineCallbacks
    def test_spawn_success(self):
        """Validate hook with success from exit."""
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        result = yield exe(self.get_test_hook("success-hook"))
        self.assertEqual(result, 0)
        yield exe.ended
        self.assertIn("WIN", self.log.getvalue())
        self.assertIn("exit code 0", self.log.getvalue())

    @defer.inlineCallbacks
    def test_spawn_fail(self):
        """Validate hook with fail from exit."""
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        d = exe(self.get_test_hook("fail-hook"))
        result = yield self.assertFailure(d, errors.CharmInvocationError)
        self.assertEqual(result.exit_code, 1)
        # ERROR indicate the level name, we are checking that the
        # proper level was logged here
        yield exe.ended
        self.assertIn("ERROR", self.log.getvalue())
        # and the message
        self.assertIn("FAIL", self.log.getvalue())
        self.assertIn("exit code 1", self.log.getvalue())

    @defer.inlineCallbacks
    def test_hanging_hook(self):
        """Verify that a hook that's slow to end is terminated.

        Test this by having the hook fork a process that hangs around
        for a while, necessitating reaping. This happens because the
        child process does not close the parent's file descriptors (as
        expected with daemonization, for example).

        http://www.snailbook.com/faq/background-jobs.auto.html
        provides some insight into what can happen.
        """
        from twisted.internet import reactor

        # Ordinarily the reaper for any such hanging hooks will run in
        # 5s, but we are impatient. Force it to end much sooner by
        # intercepting the reaper setup.
        mock_reactor = self.mocker.patch(reactor)

        # Although we can match precisely on the
        # Process.loseConnection, Mocker gets confused with also
        # trying to match the delay time, using something like
        # `MATCH(lambda x: isinstance(x, (int, float)))`. So instead
        # we hardcode it here as just 5.
        mock_reactor.callLater(
            5, MATCH(lambda x: isinstance(x.im_self, Process)))

        def intercept_reaper_setup(delay, reaper):
            # Given this is an external process, let's sleep for a
            # short period of time
            return reactor.callLater(0.2, reaper)

        self.mocker.call(intercept_reaper_setup)
        self.mocker.replay()

        # The hook script will immediately exit with a status code of
        # 0, but it created a child process (via shell backgrounding)
        # that is running (and will sleep for >10s)
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        result = yield exe(self.get_test_hook("hanging-hook"))
        self.assertEqual(result, 0)

        # Verify after waiting for the process to close (which means
        # the reaper ran!), we get output for the first phase of the
        # hanging hook, but not after its second, more extended sleep.
        yield exe.ended
        self.assertIn("Slept for 50ms", self.log.getvalue())
        self.assertNotIn("Slept for 1s", self.log.getvalue())

        # Lastly there's a nice long sleep that would occur after the
        # default timeout of this test. Successful completion of this
        # test without a timeout means this sleep was never executed.

    def test_path_setup(self):
        """Validate that the path allows finding the executable."""
        from twisted.python.procutils import which
        exe = which("relation-get")
        self.assertTrue(exe)
        self.assertTrue(exe[0].endswith("relation-get"))

    @defer.inlineCallbacks
    def test_spawn_cli_get_hook(self):
        """Validate the get hook can transmit values to the hook"""
        hook_log = self.capture_logging("hook")
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Populate and verify some data we will
        # later extract with the hook
        context = self.ua.get_context("client_id")
        expected = {"a": "b", "c": "d",
                    "private-address": "mysql-0.example.com"}
        yield context.set(expected)
        data = yield context.get("mysql/0")

        self.assertEqual(expected, data)

        # invoke the hook and process the results
        # verifying they are expected
        result = yield exe(self.create_hook("relation-get",
                                             "--format=json - mysql/0"))
        self.assertEqual(result, 0)
        data = hook_log.getvalue()
        self.assertEqual(json.loads(data), expected)

    @defer.inlineCallbacks
    def test_spawn_cli_get_value_hook(self):
        """Validate the get hook can transmit values to the hook."""
        hook_log = self.capture_logging("hook")
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Populate and verify some data we will
        # later extract with the hook
        context = self.ua.get_context("client_id")
        expected = {"name": "rabbit", "private-address": "mysql-0.example.com"}
        yield context.set(expected)
        data = yield context.get("mysql/0")

        self.assertEqual(expected, data)

        # invoke the hook and process the results
        # verifying they are expected
        result = yield exe(self.create_hook("relation-get",
                                            "--format=json name mysql/0"))
        self.assertEqual(result, 0)
        data = hook_log.getvalue()
        self.assertEqual("rabbit", json.loads(data))

    @defer.inlineCallbacks
    def test_spawn_cli_get_unit_private_address(self):
        """Private addresses can be retrieved."""
        hook_log = self.capture_logging("hook")
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")
        result = yield exe(self.create_hook("unit-get", "private-address"))
        self.assertEqual(result, 0)
        data = hook_log.getvalue()
        self.assertEqual("mysql-0.example.com", data.strip())

    @defer.inlineCallbacks
    def test_spawn_cli_get_unit_unknown_public_address(self):
        """If for some hysterical raison, the public address hasn't been set.

        We shouldn't error. This should never happen, the unit agent is sets
        it on startup.
        """
        hook_log = self.capture_logging("hook")
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        result = yield exe(self.create_hook("unit-get", "public-address"))
        self.assertEqual(result, 0)
        data = hook_log.getvalue()
        self.assertEqual("", data.strip())

    def test_get_remote_unit_arg(self):
        """Simple test around remote arg parsing."""
        self.change_environment(JUJU_UNIT_NAME="mysql/0",
                                JUJU_CLIENT_ID="client_id",
                                JUJU_AGENT_SOCKET=self.socket_path)
        client = commands.RelationGetCli()
        client.setup_parser()
        options = client.parse_args(["-", "mysql/1"])
        self.assertEqual(options.unit_name, "mysql/1")

    @defer.inlineCallbacks
    def test_spawn_cli_set_hook(self):
        """Validate the set hook can set values in zookeeper."""
        output = self.capture_logging("hook", level=logging.DEBUG)
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Invoke the hook and process the results verifying they are expected
        hook = self.create_hook("relation-set", "a=b c=d")
        result = yield exe(hook)
        self.assertEqual(result, 0)

        # Verify the context was flushed to zk
        zk_data = yield self.relation.get_data()
        self.assertEqual(
            {"a": "b", "c": "d", "private-address": "mysql-0.example.com"},
            yaml.load(zk_data))
        yield exe.ended
        self.assertIn(
            "Flushed values for hook %r on 'database:42'\n"
            "    Setting changed: u'a'=u'b' (was unset)\n"
            "    Setting changed: u'c'=u'd' (was unset)" % (
                os.path.basename(hook)),
            output.getvalue())

    @defer.inlineCallbacks
    def test_spawn_cli_set_can_delete_and_modify(self):
        """Validate the set hook can delete values in zookeeper."""
        output = self.capture_logging("hook", level=logging.DEBUG)
        hook_directory = self.makeDir()
        hook_file_path = self.makeFile(
            content=("#!/bin/bash\n"
                     "relation-set existing= absent= new-value=2 "
                     "changed=abc changed2=xyz\n"
                     "exit 0\n"),
            basename=os.path.join(hook_directory, "set-delete-test"))
        os.chmod(hook_file_path, stat.S_IRWXU)
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Populate with data that will be deleted
        context = self.ua.get_context("client_id")
        yield context.set(
            {u"existing": u"42",
             u"changed": u"a" * 101,
             u"changed2": u"a" * 100})
        yield context.flush()

        # Invoke the hook and process the results verifying they are expected
        self.assertTrue(os.path.exists(hook_file_path))
        result = yield exe(hook_file_path)
        self.assertEqual(result, 0)

        # Verify the context was flushed to zk
        zk_data = yield self.relation.get_data()
        self.assertEqual(
            {"new-value": "2", "changed": "abc", "changed2": "xyz",
             "private-address": "mysql-0.example.com"},
            yaml.load(zk_data))

        # Verify that unicode/strings longer than 100 characters in
        # representation (including quotes and the u marker) are cut
        # off; 100 is the default cutoff used in the change items
        # __str__ method
        yield exe.ended
        self.assertIn(
            "Flushed values for hook 'set-delete-test' on 'database:42'\n"
            "    Setting changed: u'changed'=u'abc' (was u'%s)\n"
            "    Setting changed: u'changed2'=u'xyz' (was u'%s)\n"
            "    Setting deleted: u'existing' (was u'42')\n"
            "    Setting changed: u'new-value'=u'2' (was unset)" % (
                "a" * 98, "a" * 98),
            output.getvalue())

    @defer.inlineCallbacks
    def test_spawn_cli_set_noop_only_logs_on_change(self):
        """Validate the set hook only logs flushes when there are changes."""
        output = self.capture_logging("hook", level=logging.DEBUG)
        hook_directory = self.makeDir()
        hook_file_path = self.makeFile(
            content=("#!/bin/bash\n"
                     "relation-set no-change=42 absent=\n"
                     "exit 0\n"),
            basename=os.path.join(hook_directory, "set-does-nothing"))
        os.chmod(hook_file_path, stat.S_IRWXU)
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Populate with data that will be *not* be modified
        context = self.ua.get_context("client_id")
        yield context.set({"no-change": "42", "untouched": "xyz"})
        yield context.flush()

        # Invoke the hook and process the results verifying they are expected
        self.assertTrue(os.path.exists(hook_file_path))
        result = yield exe(hook_file_path)
        self.assertEqual(result, 0)

        # Verify the context was flushed to zk
        zk_data = yield self.relation.get_data()
        self.assertEqual({"no-change": "42", "untouched": "xyz",
                          "private-address": "mysql-0.example.com"},
                         yaml.load(zk_data))
        self.assertNotIn(
            "Flushed values for hook 'set-does-nothing'",
            output.getvalue())

    @defer.inlineCallbacks
    def test_logging(self):
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)

        # The echo hook will echo out the value
        # it will also echo to stderr the ERROR variable
        message = "This is only a test"
        error = "All is full of fail"
        default = "Default level"

        exe.environment["MESSAGE"] = message
        exe.environment["ERROR"] = error
        exe.environment["DEFAULT"] = default
        # of the MESSAGE variable
        result = yield exe(self.get_test_hook("echo-hook"))
        self.assertEqual(result, 0)
        yield exe.ended
        self.assertIn(message, self.log.getvalue())

        # Logging used to log an empty response dict
        # assure this doesn't happpen  [b=915506]
        self.assertNotIn("{}", self.log.getvalue())

        # The 'error' was sent via juju-log
        # to the UA. Our test UA has a fake log stream
        # which we can check now
        output = self.ua.log_file.getvalue()
        self.assertIn("ERROR:: " + error, self.log.getvalue())
        self.assertIn("INFO:: " + default, self.log.getvalue())

        assert message not in output, """Log includes unintended messages"""

    @defer.inlineCallbacks
    def test_spawn_cli_list_hook_smart(self):
        """Validate the get hook can transmit values to the hook."""
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Populate and verify some data we will
        # later extract with the hook
        context = self.ua.get_context("client_id")

        # directly manipulate the context to the expected list of
        # members
        expected = ["alpha/0", "beta/0"]
        context._members = expected

        # invoke the hook and process the results
        # verifying they are expected
        exe.environment["FORMAT"] = "smart"
        result = yield exe(self.create_hook("relation-list",
                                            "--format=smart"))

        self.assertEqual(result, 0)
        yield exe.ended
        self.assertIn("alpha/0\nbeta/0\n", self.log.getvalue())

    @defer.inlineCallbacks
    def test_spawn_cli_list_hook_eval(self):
        """Validate the get hook can transmit values to the hook."""
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Populate and verify some data we will
        # later extract with the hook
        context = self.ua.get_context("client_id")

        # directly manipulate the context to the expected list of
        # members
        expected = ["alpha/0", "beta/0"]
        context._members = expected

        # invoke the hook and process the results
        # verifying they are expected
        result = yield exe(self.create_hook("relation-list",
                                            "--format=eval"))

        self.assertEqual(result, 0)
        yield exe.ended
        self.assertIn("alpha/0 beta/0", self.log.getvalue())

    @defer.inlineCallbacks
    def test_spawn_cli_list_hook_json(self):
        """Validate the get hook can transmit values to the hook."""
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Populate and verify some data we will
        # later extract with the hook
        context = self.ua.get_context("client_id")

        # directly manipulate the context to the expected list of
        # members
        expected = ["alpha/0", "beta/0"]
        context._members = expected

        # invoke the hook and process the results
        # verifying they are expected
        result = yield exe(self.create_hook("relation-list", "--format json"))

        self.assertEqual(result, 0)
        yield exe.ended
        self.assertIn('["alpha/0", "beta/0"]', self.log.getvalue())


class ChildRelationHookContextsTest(RelationInvokerTestBase):

    @defer.inlineCallbacks
    def add_a_blog(self, blog_name):
        blog_states = yield self.add_opposite_service_unit(
            (yield self.add_relation_service_unit_to_another_endpoint(
                    self.mysql_states,
                    RelationEndpoint(
                        blog_name, "client-server", "app", "client"))))
        yield blog_states['service_relations'][-1].add_unit_state(
            self.mysql_states['unit'])
        defer.returnValue(blog_states)

    @defer.inlineCallbacks
    def add_db_admin_tool(self, admin_name):
        """Add another relation, using a different relation name"""
        admin_ep = RelationEndpoint(
            admin_name, "client-server", "admin-app", "client")
        mysql_ep = RelationEndpoint(
            "mysql", "client-server", "db-admin", "server")
        yield self.add_relation_service_unit_from_endpoints(
            admin_ep, mysql_ep)

    @defer.inlineCallbacks
    def assert_zk_data(self, context, expected):
        internal_relation_id, _ = yield context.get_relation_id_and_scope(
            context.relation_ident)
        internal_unit_id = (yield context.get_local_unit_state()).internal_id
        path = yield context.get_settings_path(internal_unit_id)
        data, stat = yield self.client.get(path)
        self.assertEqual(yaml.load(data), expected)

    @defer.inlineCallbacks
    def test_implied_relation_hook_context(self):
        """Verify implied hook context is cached and can get relation ids."""
        yield self.add_a_blog("wordpress2")
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")
        implied = exe.get_context()
        self.assertEqual(implied.relation_ident, "db:0")
        # Verify that the same hook context for the implied relation
        # is returned if referenced by its relation id
        self.assertEqual(
            implied,
            self.ua.server_factory.get_invoker("client_id").\
                get_relation_hook_context("db:0"))
        self.assertEqual(
            set((yield implied.get_relation_idents("db"))),
            set(["db:0", "db:1"]))

    @defer.inlineCallbacks
    def test_get_child_relation_hook_context(self):
        """Verify retrieval of a child hook context and methods on it."""
        yield self.add_a_blog("wordpress2")
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation, client_id="client_id")

        # Add another relation, verify it's not yet visible
        yield self.add_a_blog("wordpress3")

        db0 = yield exe.get_relation_hook_context("db:0")
        db1 = yield exe.get_relation_hook_context("db:1")
        self.assertEqual(db1.relation_ident, "db:1")
        self.assertEqual(
            set((yield db1.get_relation_idents("db"))),
            set(["db:0", "db:1"]))
        self.assertEqual(
            db1,
            exe.get_relation_hook_context("db:1"))

        # Not yet visible relation
        self.assertRaises(
            RelationStateNotFound,
            exe.get_relation_hook_context, "db:2")

        # Nonexistent relation idents
        self.assertRaises(
            RelationStateNotFound,
            exe.get_relation_hook_context, "db:12345")

        # Reread parent and child contexts
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation, client_id="client_id")
        db0 = yield exe.get_context()
        db1 = yield exe.get_relation_hook_context("db:1")
        db2 = yield exe.get_relation_hook_context("db:2")

        # Verify that any changes are written out; write directly here
        # using the relation contexts
        yield db0.set({u"a": u"42", u"b": u"xyz"})
        yield db1.set({u"c": u"21"})
        yield db2.set({u"d": u"99"})

        # Then actually execute a successfully hook so flushes occur
        # on both parent and children
        result = yield exe(self.get_test_hook("success-hook"))
        self.assertEqual(result, 0)
        yield exe.ended

        # Verify that all contexts were flushed properly to ZK
        yield self.assert_zk_data(db0, {
                u"a": u"42",
                u"b": u"xyz",
                "private-address": "mysql-0.example.com"})
        yield self.assert_zk_data(db1, {
                u"c": u"21",
                "private-address": "mysql-0.example.com"})
        yield self.assert_zk_data(db2, {
                u"d": u"99",
                "private-address": "mysql-0.example.com"})

        # Verify log is written in order of relations: parent first,
        # then by children
        self.assertLogLines(
            self.log.getvalue(),
            ["Cached relation hook contexts: ['db:1']",
             "Flushed values for hook 'success-hook' on 'db:0'",
             "    Setting changed: u'a'=u'42' (was unset)",
             "    Setting changed: u'b'=u'xyz' (was unset)",
             "    Setting changed: u'c'=u'21' (was unset) on 'db:1'",
             "    Setting changed: u'd'=u'99' (was unset) on 'db:2'"])

    @defer.inlineCallbacks
    def test_get_child_relation_hook_context_while_removing_relation(self):
        """Verify retrieval of a child hook context and methods on it."""
        wordpress2_states = yield self.add_a_blog("wordpress2")
        yield self.add_a_blog("wordpress3")
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation, client_id="client_id")
        relation_state_manager = RelationStateManager(self.client)
        yield relation_state_manager.remove_relation_state(
            wordpress2_states["relation"])
        self.assertEqual(
            set((yield exe.get_context().get_relation_idents("db"))),
            set(["db:0", "db:1", "db:2"]))

        db0 = yield exe.get_relation_hook_context("db:0")
        db1 = yield exe.get_relation_hook_context("db:1")
        db2 = yield exe.get_relation_hook_context("db:2")

        # Verify that any changes are written out; write directly here
        # using the relation contexts
        yield db0.set({u"a": u"42", u"b": u"xyz"})
        yield db1.set({u"c": u"21"})
        yield db2.set({u"d": u"99"})

        # Then actually execute a successfully hook so flushes occur
        # on both parent and children
        result = yield exe(self.get_test_hook("success-hook"))
        self.assertEqual(result, 0)
        yield exe.ended

        # Verify that both contexts were flushed properly to ZK: even
        # if the db:1 relation is gone, we allow its relation settings
        # to be written out to ZK
        yield self.assert_zk_data(db0, {
                u"a": u"42",
                u"b": u"xyz",
                "private-address": "mysql-0.example.com"})
        yield self.assert_zk_data(db1, {
                u"c": u"21",
                "private-address": "mysql-0.example.com"})
        yield self.assert_zk_data(db2, {
                u"d": u"99",
                "private-address": "mysql-0.example.com"})
        self.assertLogLines(
            self.log.getvalue(),
            ["Cached relation hook contexts: ['db:1', 'db:2']",
             "Flushed values for hook 'success-hook' on 'db:0'",
             "    Setting changed: u'a'=u'42' (was unset)",
             "    Setting changed: u'b'=u'xyz' (was unset)",
             "    Setting changed: u'c'=u'21' (was unset) on 'db:1'",
             "    Setting changed: u'd'=u'99' (was unset) on 'db:2'"])

        # Reread parent and child contexts, verify db:1 relation has
        # disappeared from topology
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation, client_id="client_id")
        self.assertEqual(
            set((yield exe.get_context().get_relation_idents("db"))),
            set(["db:0", "db:2"]))
        yield self.assertFailure((yield exe.get_relation_hook_context("db:1")),
                                 RelationStateNotFound)

    @defer.inlineCallbacks
    def test_relation_ids(self):
        """Verify `relation-ids` command returns ids separated by newlines."""
        yield self.add_a_blog("wordpress2")
        hook_log = self.capture_logging("hook")

        # Invoker will be in the context of the mysql/0 service unit
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Then verify the hook lists the relation ids corresponding to
        # the relation name `db`
        hook = self.create_hook("relation-ids", "db")
        result = yield exe(hook)
        self.assertEqual(result, 0)
        yield exe.ended
        # Smart formtting outputs one id per line
        self.assertEqual(
            hook_log.getvalue(), "db:0\ndb:1\n\n")
        # But newlines are just whitespace to the shell or to Python,
        # so they can be split accordingly, adhering to the letter of
        # the spec
        self.assertEqual(
            hook_log.getvalue().split(), ["db:0", "db:1"])

    @defer.inlineCallbacks
    def test_relation_ids_json_format(self):
        """Verify `relation-ids --format=json` command returns ids in json."""
        yield self.add_a_blog("wordpress2")
        yield self.add_db_admin_tool("admin")
        hook_log = self.capture_logging("hook")

        # Invoker will be in the context of the mysql/0 service unit
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Then verify the hook lists the relation ids corresponding to
        # the relation name `db`
        hook = self.create_hook("relation-ids", "--format=json db")
        result = yield exe(hook)
        self.assertEqual(result, 0)
        yield exe.ended
        self.assertEqual(
            set(json.loads(hook_log.getvalue())),
            set(["db:0", "db:1"]))

    @defer.inlineCallbacks
    def test_relation_ids_no_relation_name(self):
        """Verify returns all relation ids if relation name not specified."""
        yield self.add_a_blog("wordpress2")
        yield self.add_db_admin_tool("admin")

        # Invoker will be in the context of the mysql/0 service
        # unit. This test file's mock unit agent does not set the
        # additional environment variables for relation hooks that are
        # set by juju.unit.lifecycle.RelationInvoker; instead it has
        # to be set by individual tests.
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")
        exe.environment["JUJU_RELATION"] = "db"

        # Then verify the hook lists the relation ids corresponding to
        # to JUJU_RELATION (="db")
        hook_log = self.capture_logging("hook")
        hook = self.create_hook("relation-ids", "--format=json")
        result = yield exe(hook)
        self.assertEqual(result, 0)
        yield exe.ended
        self.assertEqual(
            set(json.loads(hook_log.getvalue())),
            set(["db:0", "db:1"]))

        # This time pretend this is a nonrelational hook
        # context. Ignore the relation stuff in the get_invoker
        # function below, really it is a nonrelational hook as far as
        # the hook commands are concerned:
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")
        hook_log = self.capture_logging("hook")
        hook = self.create_hook("relation-ids", "--format=json")
        result = yield exe(hook)

        # Currently, exceptions of all hook commands are only logged;
        # the exit code is always set to 0.
        self.assertEqual(result, 0)
        yield exe.ended
        self.assertIn(
            ("juju.hooks.protocol.MustSpecifyRelationName: "
             "Relation name must be specified"),
            hook_log.getvalue())

    @defer.inlineCallbacks
    def test_relation_set_with_relation_id_option(self):
        """Verify `relation-set` works with -r option."""
        # Invoker will be in the context of the db:0 relation
        yield self.add_a_blog("wordpress2")
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")

        # But set from db:1
        hook = self.create_hook("relation-set", "-r db:1 a=42 b=xyz")
        result = yield exe(hook)
        self.assertEqual(result, 0)
        yield exe.ended

        db1 = exe.get_relation_hook_context("db:1")
        yield self.assert_zk_data(db1, {
                "a": "42",
                "b": "xyz",
                "private-address": "mysql-0.example.com"})
        self.assertLogLines(
            self.log.getvalue(),
            ["Cached relation hook contexts: ['db:1']",
             "Flushed values for hook %r on 'db:0'" % os.path.basename(hook),
             "    Setting changed: u'a'=u'42' (was unset) on 'db:1'",
             "    Setting changed: u'b'=u'xyz' (was unset) on 'db:1'"])

    @defer.inlineCallbacks
    def test_relation_get_with_relation_id_option(self):
        """Verify `relation-get` works with -r option."""
        yield self.add_a_blog("wordpress2")
        hook_log = self.capture_logging("hook")

        # Invoker will be in the context of the db:0 relation
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")

        # First set through the context
        db1 = exe.get_relation_hook_context("db:1")
        yield db1.set({"name": "whiterabbit"})

        # Then get from db:1
        hook = self.create_hook("relation-get",
                                "--format=json -r db:1 - mysql/0")
        result = yield exe(hook)
        self.assertEqual(result, 0)
        yield exe.ended
        self.assertEqual(
            json.loads(hook_log.getvalue()),
            {"private-address": "mysql-0.example.com",
             "name": "whiterabbit"})

    @defer.inlineCallbacks
    def test_relation_list_with_relation_id_option(self):
        """Verify `relation-list` works with -r option."""
        yield self.add_a_blog("wordpress2")
        hook_log = self.capture_logging("hook")

        # Invoker will be in the context of the db:0 relation
        exe = yield self.ua.get_invoker(
            "db:0", "add", "mysql/0", self.relation,
            client_id="client_id")

        # Then verify relation membership can be listed for db:1
        hook = self.create_hook("relation-list", "--format=json -r db:1")
        result = yield exe(hook)
        self.assertEqual(result, 0)
        yield exe.ended
        self.assertEqual(
            json.loads(hook_log.getvalue()),
            ["wordpress2/0"])


class PortCommandsTest(RelationInvokerTestBase):

    def test_path_setup(self):
        """Validate that the path allows finding the executable."""
        from twisted.python.procutils import which
        open_port_exe = which("open-port")
        self.assertTrue(open_port_exe)
        self.assertTrue(open_port_exe[0].endswith("open-port"))

        close_port_exe = which("close-port")
        self.assertTrue(close_port_exe)
        self.assertTrue(close_port_exe[0].endswith("close-port"))

    @defer.inlineCallbacks
    def test_open_and_close_ports(self):
        """Verify that port hook commands run and changes are immediate."""
        unit_state = self.mysql_states["unit"]
        self.assertEqual((yield unit_state.get_open_ports()), [])

        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)
        result = yield exe(self.create_hook("open-port", "80"))
        self.assertEqual(result, 0)
        self.assertEqual(
            (yield unit_state.get_open_ports()),
            [{"port": 80, "proto": "tcp"}])

        result = yield exe(self.create_hook("open-port", "53/udp"))
        self.assertEqual(result, 0)
        self.assertEqual(
            (yield unit_state.get_open_ports()),
            [{"port": 80, "proto": "tcp"},
             {"port": 53, "proto": "udp"}])

        result = yield exe(self.create_hook("open-port", "53/tcp"))
        self.assertEqual(result, 0)
        self.assertEqual(
            (yield unit_state.get_open_ports()),
            [{"port": 80, "proto": "tcp"},
             {"port": 53, "proto": "udp"},
             {"port": 53, "proto": "tcp"}])

        result = yield exe(self.create_hook("open-port", "443/tcp"))
        self.assertEqual(result, 0)
        self.assertEqual(
            (yield unit_state.get_open_ports()),
            [{"port": 80, "proto": "tcp"},
             {"port": 53, "proto": "udp"},
             {"port": 53, "proto": "tcp"},
             {"port": 443, "proto": "tcp"}])

        result = yield exe(self.create_hook("close-port", "80/tcp"))
        self.assertEqual(result, 0)
        self.assertEqual(
            (yield unit_state.get_open_ports()),
            [{"port": 53, "proto": "udp"},
             {"port": 53, "proto": "tcp"},
             {"port": 443, "proto": "tcp"}])

        yield exe.ended
        self.assertLogLines(
            self.log.getvalue(), [
                "opened 80/tcp",
                "opened 53/udp",
                "opened 443/tcp",
                "closed 80/tcp"])

    @defer.inlineCallbacks
    def test_open_port_args(self):
        """Verify that open-port properly reports arg parse errors."""
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)

        result = yield self.assertFailure(
            exe(self.create_hook("open-port", "80/invalid-protocol")),
            errors.CharmInvocationError)
        self.assertEqual(result.exit_code, 2)
        yield exe.ended
        self.assertIn(
            "open-port: error: argument PORT[/PROTOCOL]: "
            "Invalid protocol, must be 'tcp' or 'udp', got 'invalid-protocol'",
            self.log.getvalue())

        result = yield self.assertFailure(
            exe(self.create_hook("open-port", "0/tcp")),
            errors.CharmInvocationError)
        self.assertEqual(result.exit_code, 2)
        yield exe.ended
        self.assertIn(
            "open-port: error: argument PORT[/PROTOCOL]: "
            "Invalid port, must be from 1 to 65535, got 0",
            self.log.getvalue())

        result = yield self.assertFailure(
            exe(self.create_hook("open-port", "80/udp/extra-info")),
            errors.CharmInvocationError)
        self.assertEqual(result.exit_code, 2)
        yield exe.ended
        self.assertIn(
            "open-port: error: argument PORT[/PROTOCOL]: "
            "Invalid format for port/protocol, got '80/udp/extra-info",
            self.log.getvalue())

    @defer.inlineCallbacks
    def test_close_port_args(self):
        """Verify that close-port properly reports arg parse errors."""
        exe = yield self.ua.get_invoker(
            "database:42", "add", "mysql/0", self.relation)

        result = yield self.assertFailure(
            exe(self.create_hook("close-port", "80/invalid-protocol")),
            errors.CharmInvocationError)
        self.assertEqual(result.exit_code, 2)
        yield exe.ended
        self.assertIn(
            "close-port: error: argument PORT[/PROTOCOL]: "
            "Invalid protocol, must be 'tcp' or 'udp', got 'invalid-protocol'",
            self.log.getvalue())

        result = yield self.assertFailure(
            exe(self.create_hook("close-port", "0/tcp")),
            errors.CharmInvocationError)
        self.assertEqual(result.exit_code, 2)
        yield exe.ended
        self.assertIn(
            "close-port: error: argument PORT[/PROTOCOL]: "
            "Invalid port, must be from 1 to 65535, got 0",
            self.log.getvalue())

        result = yield self.assertFailure(
            exe(self.create_hook("close-port", "80/udp/extra-info")),
            errors.CharmInvocationError)
        self.assertEqual(result.exit_code, 2)
        yield exe.ended
        self.assertIn(
            "close-port: error: argument PORT[/PROTOCOL]: "
            "Invalid format for port/protocol, got '80/udp/extra-info",
            self.log.getvalue())


class SubordinateRelationInvokerTest(InvokerTestBase, RelationTestBase):

    @defer.inlineCallbacks
    def setUp(self):
        yield super(SubordinateRelationInvokerTest, self).setUp()
        self.log = self.capture_logging(
            formatter=logging.Formatter(
                "%(name)s:%(levelname)s:: %(message)s"),
            level=logging.DEBUG)

        self.update_invoker_env("mysql/0", "logging/0")
        self.socket_path = self.makeFile()
        unit_dir = self.makeDir()
        self.makeDir(path=os.path.join(unit_dir, "charm"))
        self.ua = MockUnitAgent(
            self.client,
            self.socket_path,
            unit_dir)
        yield self._build_relation()

    @defer.inlineCallbacks
    def tearDown(self):
        self.ua.stop()
        yield super(SubordinateRelationInvokerTest, self).tearDown()

    @defer.inlineCallbacks
    def _build_relation(self):
        mysql_ep = RelationEndpoint("mysql", "juju-info", "juju-info",
                                    "server", "global")
        logging_ep = RelationEndpoint("logging", "juju-info", "juju-info",
                                      "client", "container")

        mysql, my_units = yield self.get_service_and_units_by_charm_name(
            "mysql", 2)
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

        mu1, mu2 = my_units
        lu1, lu2 = log_units

        mystate = pick_attr(service_states, relation_role="server")
        logstate = pick_attr(service_states, relation_role="client")

        yield mystate.add_unit_state(mu1)
        self.relation = yield logstate.add_unit_state(lu1)
        # add the second container (
        yield mystate.add_unit_state(mu2)
        self.relation2 = yield logstate.add_unit_state(lu2)

    @defer.inlineCallbacks
    def test_subordinate_invocation(self):
        exe = yield self.ua.get_invoker(
            "juju-info", "add", "mysql/0", self.relation)
        result = yield exe(self.create_hook("relation-list",
                                            "--format=smart"))
        self.assertEqual(result, 0)
        yield exe.ended

        # verify that we see the proper unit
        self.assertIn("mysql/0", self.log.getvalue())
        # we don't see units in the other container
        self.assertNotIn("mysql/1", self.log.getvalue())

        # reset the log and verify other container
        self.log.seek(0)
        exe = yield self.ua.get_invoker(
            "juju-info", "add", "mysql/1", self.relation2)
        result = yield exe(self.create_hook("relation-list",
                                            "--format=smart"))
        self.assertEqual(result, 0)
        # verify that we see the proper unit
        self.assertIn("mysql/1", self.log.getvalue())
        # we don't see units in the other container
        self.assertNotIn("mysql/0", self.log.getvalue())
