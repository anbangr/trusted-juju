import itertools
import logging
import os
import yaml
import StringIO
import sys

from twisted.internet.defer import Deferred, inlineCallbacks, returnValue
from twisted.internet import reactor
from twisted.trial.unittest import TestCase as TrialTestCase

from txzookeeper import ZookeeperClient
from txzookeeper.managed import ManagedClient

from juju.lib.mocker import MockerTestCase
from juju.tests.common import get_test_zookeeper_address


class TestCase(TrialTestCase, MockerTestCase):
    """
    Base class for all juju tests.
    """

    # Default timeout for any test
    timeout = 5

    # Default value for zookeeper test client
    client = None

    def capture_stream(self, stream_name):
        original = getattr(sys, stream_name)
        new = StringIO.StringIO()

        @self.addCleanup
        def reset_stream():
            setattr(sys, stream_name, original)

        setattr(sys, stream_name, new)
        return new

    def capture_logging(self, name="", level=logging.INFO,
                        log_file=None, formatter=None):
        if log_file is None:
            log_file = StringIO.StringIO()
        log_handler = logging.StreamHandler(log_file)
        if formatter:
            log_handler.setFormatter(formatter)
        logger = logging.getLogger(name)
        logger.addHandler(log_handler)
        old_logger_level = logger.level
        logger.setLevel(level)

        @self.addCleanup
        def reset_logging():
            logger.removeHandler(log_handler)
            logger.setLevel(old_logger_level)

        return log_file

    _missing_attr = object()

    def patch(self, object, attr, value):
        """Replace an object's attribute, and restore original value later.

        Returns the original value of the attribute if any or None.
        """
        original_value = getattr(object, attr, self._missing_attr)

        @self.addCleanup
        def restore_original():
            if original_value is self._missing_attr:
                try:
                    delattr(object, attr)
                except AttributeError:
                    pass
            else:
                setattr(object, attr, original_value)
        setattr(object, attr, value)

        if original_value is self._missing_attr:
            return None
        return original_value

    def change_args(self, *args):
        """Change the cli args to the specified, with restoration later."""
        original_args = sys.argv
        sys.argv = args

        @self.addCleanup
        def restore():
            sys.argv = original_args

    def change_environment(self, **kw):
        """Reset the environment to kwargs. The tests runtime
        environment will be initialized with only those values passed
        as kwargs.

        The original state of the environment will be restored after
        the tests complete.
        """
        # preserve key elements needed for testing
        for env in ["AWS_ACCESS_KEY_ID",
                    "AWS_SECRET_ACCESS_KEY",
                    "EC2_PRIVATE_KEY",
                    "EC2_CERT",
                    "HOME",
                    "ZOOKEEPER_ADDRESS"]:
            if env not in kw:
                kw[env] = os.environ.get(env, "")

        original_environ = dict(os.environ)

        @self.addCleanup
        def cleanup_env():
            os.environ.clear()
            os.environ.update(original_environ)

        os.environ.clear()
        os.environ.update(kw)

    def assertInstance(self, instance, type):
        self.assertTrue(isinstance(instance, type))

    def assertLogLines(self, observed, expected):
        """Asserts that the lines of `expected` exist in order in the log."""
        logged = observed.split("\n")
        it = iter(expected)
        for line in logged:
            it, peekat = itertools.tee(it)
            peeked = next(peekat)
            if peeked in line:
                next(it)  # then consume this line and move on

        self.assertFalse(
            expected,
            "Did not see all expected lines in log, in order: %s, %s" % (
                observed, expected))

    def sleep(self, delay):
        """Non-blocking sleep."""
        deferred = Deferred()
        reactor.callLater(delay, deferred.callback, None)
        return deferred

    @inlineCallbacks
    def poke_zk(self):
        """Create a roundtrip communication to zookeeper.

        An alternative to sleeping in many cases when waiting for
        a zookeeper watch or interaction to trigger a callback.
        """
        if self.client is None:
            raise ValueError("No Zookeeper client to utilize")
        yield self.client.exists("/zookeeper")
        returnValue(True)

    def get_zookeeper_client(self):
        client = ManagedClient(
            get_test_zookeeper_address(), session_timeout=1000)
        return client

    @inlineCallbacks
    def dump_data(self, path="/"):
        client = self.client
        output = {}

        @inlineCallbacks
        def export_tree(path, indent):
            d = {}
            data, stat = yield client.get(path)
            name = path.rsplit('/', 1)[1]

            d['contents'] = _decode_fmt(data, yaml.load)

            children = yield client.get_children(path)
            for name in children:
                if path == "/" and name == "zookeeper":
                    continue

                cd = yield export_tree(path + '/' + name, indent)
                d[name] = cd

            returnValue(d)

        output[path.rsplit('/', 1)[1]] = yield export_tree(path, '')
        returnValue(output)

    @inlineCallbacks
    def assertTree(self, path, data):
        data = yield self.dump_data(path)
        self.assertEqual(data, data)

    @inlineCallbacks
    def dump_tree(self, path="/", format=yaml.load):
        client = self.client
        output = []
        out = output.append

        @inlineCallbacks
        def export_tree(path, indent):
            data, stat = yield client.get(path)
            name = path.rsplit("/", 1)[1]

            properties = _decode_fmt(data, format)
            out(indent + "/" + name)
            indent += "  "
            for i in sorted(properties.iteritems()):
                out(indent + "%s = %r" % i)

            children = yield client.get_children(path)
            for name in sorted(children):
                if path == "/" and name == "zookeeper":
                    continue

                yield export_tree(path + "/" + name, indent)

        yield export_tree(path, "")
        returnValue("\n".join(output) + "\n")


def _decode_fmt(s, decoder):
    s = s.strip()
    if not s:
        data = {}
    try:
        data = decoder(s)
    except:
        data = dict(string_value=s)

    return data
