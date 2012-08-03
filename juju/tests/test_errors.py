from juju.errors import (
    JujuError, FileNotFound, FileAlreadyExists, CharmError,
    CharmInvocationError, CharmUpgradeError, NoConnection, InvalidHost,
    InvalidUser, ProviderError, CloudInitError, ProviderInteractionError,
    CannotTerminateMachine, MachinesNotFound, EnvironmentPending,
    EnvironmentNotFound, IncompatibleVersion, InvalidPlacementPolicy,
    ServiceError, ConstraintError, UnknownConstraintError,
    SSLVerificationError)

from juju.lib.testing import TestCase


class ErrorsTest(TestCase):

    def assertIsJujuError(self, error):
        self.assertTrue(isinstance(error, JujuError),
                        "%s is not a subclass of JujuError" %
                        error.__class__.__name__)

    def test_IncompatibleVersion(self):
        error = IncompatibleVersion(123, 42)
        self.assertEqual(
            str(error),
            "Incompatible juju protocol versions (found 123, want 42)")
        self.assertIsJujuError(error)

    def test_FileNotFound(self):
        error = FileNotFound("/path")
        self.assertEquals(str(error), "File was not found: '/path'")
        self.assertIsJujuError(error)

    def test_FileAlreadyExists(self):
        error = FileAlreadyExists("/path")
        self.assertEquals(str(error),
                          "File already exists, won't overwrite: '/path'")
        self.assertIsJujuError(error)

    def test_NoConnection(self):
        error = NoConnection("unable to connect")
        self.assertIsJujuError(error)

    def test_InvalidHost(self):
        error = InvalidHost("Invalid host for SSH forwarding")
        self.assertTrue(isinstance(error, NoConnection))
        self.assertEquals(
            str(error),
            "Invalid host for SSH forwarding")

    def test_InvalidUser(self):
        error = InvalidUser("Invalid SSH key")
        self.assertTrue(isinstance(error, NoConnection))
        self.assertEquals(
            str(error),
            "Invalid SSH key")

    def test_ConstraintError(self):
        error = ConstraintError("bork bork bork")
        self.assertIsJujuError(error)
        self.assertEquals(str(error), "bork bork bork")

    def test_UnknownConstraintError(self):
        error = UnknownConstraintError("meatball")
        self.assertTrue(isinstance(error, ConstraintError))
        self.assertEquals(str(error), "Unknown constraint: 'meatball'")

    def test_ProviderError(self):
        error = ProviderError("Invalid credentials")
        self.assertIsJujuError(error)
        self.assertEquals(str(error), "Invalid credentials")

    def test_CloudInitError(self):
        error = CloudInitError("BORKEN")
        self.assertIsJujuError(error)
        self.assertEquals(str(error), "BORKEN")

    def test_ProviderInteractionError(self):
        error = ProviderInteractionError("Bad Stuff")
        self.assertIsJujuError(error)
        self.assertEquals(str(error), "Bad Stuff")

    def test_CannotTerminateMachine(self):
        error = CannotTerminateMachine(0, "environment would be destroyed")
        self.assertIsJujuError(error)
        self.assertEquals(
            str(error),
            "Cannot terminate machine 0: environment would be destroyed")

    def test_MachinesNotFoundSingular(self):
        error = MachinesNotFound(("i-sublimed",))
        self.assertIsJujuError(error)
        self.assertEquals(error.instance_ids, ["i-sublimed"])
        self.assertEquals(str(error),
                          "Cannot find machine: i-sublimed")

    def test_MachinesNotFoundPlural(self):
        error = MachinesNotFound(("i-disappeared", "i-exploded"))
        self.assertIsJujuError(error)
        self.assertEquals(error.instance_ids, ["i-disappeared", "i-exploded"])
        self.assertEquals(str(error),
                          "Cannot find machines: i-disappeared, i-exploded")

    def test_EnvironmentNotFoundWithInfo(self):
        error = EnvironmentNotFound("problem")
        self.assertIsJujuError(error)
        self.assertEquals(str(error),
                          "juju environment not found: problem")

    def test_EnvironmentNotFoundNoInfo(self):
        error = EnvironmentNotFound()
        self.assertIsJujuError(error)
        self.assertEquals(str(error),
                          "juju environment not found: no details "
                          "available")

    def test_EnvironmentPendingWithInfo(self):
        error = EnvironmentPending("problem")
        self.assertIsJujuError(error)
        self.assertEquals(str(error), "problem")

    def test_InvalidPlacementPolicy(self):
        error = InvalidPlacementPolicy("x", "foobar",  ["a", "b", "c"])
        self.assertIsJujuError(error)
        self.assertEquals(
            str(error),
            ("Unsupported placement policy: 'x' for provider: 'foobar', "
            "supported policies a, b, c"))

    def test_ServiceError(self):
        error = ServiceError("blah")
        self.assertEquals(str(error), "blah")
        self.assertIsJujuError(error)

    def test_CharmError(self):
        error = CharmError("/foo/bar", "blah blah")
        self.assertIsJujuError(error)
        self.assertEquals(str(error), "Error processing '/foo/bar': blah blah")

    def test_CharmInvocationError(self):
        error = CharmInvocationError("/foo/bar", 1)
        self.assertIsJujuError(error)
        self.assertEquals(
            str(error), "Error processing '/foo/bar': exit code 1.")

    def test_CharmInvocationError_with_signal(self):
        error = CharmInvocationError("/foo/bar", None, 13)
        self.assertIsJujuError(error)
        self.assertEquals(
            str(error), "Error processing '/foo/bar': signal 13.")

    def test_CharmUpgradeError(self):
        error = CharmUpgradeError("blah blah")
        self.assertIsJujuError(error)
        self.assertEquals(str(error), "Cannot upgrade charm: blah blah")

    def test_SSLVerificationError(self):
        error = SSLVerificationError("endpoint")
        self.assertIsJujuError(error)
        self.assertEquals(str(error),
                "An SSL endpoint failed verification: endpoint")
