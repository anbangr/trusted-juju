import os

from twisted.internet.defer import inlineCallbacks, succeed

from txzookeeper.tests.utils import deleteTree

from juju.agents.base import TwistedOptionNamespace
from juju.state.tests.common import StateTestBase
from juju.tests.common import get_test_zookeeper_address


class AgentTestBase(StateTestBase):

    agent_class = None
    juju_directory = None
    setup_environment = True

    @inlineCallbacks
    def setUp(self):
        self.juju_directory = self.makeDir()
        yield super(AgentTestBase, self).setUp()
        assert self.agent_class, "Agent Class must be specified on test"
        if self.setup_environment:
            yield self.push_default_config()
        self.agent = self.agent_class()
        self.options = yield self.get_agent_config()
        self.agent.configure(self.options)
        self.agent.set_watch_enabled(False)

    def tearDown(self):
        if self.agent.client and self.agent.client.connected:
            self.agent.client.close()

        if self.client.connected:
            deleteTree("/", self.client.handle)
            self.client.close()

    def get_agent_config(self):
        options = TwistedOptionNamespace()
        options["juju_directory"] = self.juju_directory
        options["zookeeper_servers"] = get_test_zookeeper_address()
        options["session_file"] = self.makeFile()
        return succeed(options)

    @inlineCallbacks
    def debug_pprint_tree(self, path="/", indent=1):
        children = yield self.client.get_children(path)
        for n in children:
            print " " * indent, "/" + n
            yield self.debug_pprint_tree(
                os.path.join(path, n),
                indent + 1)
