import logging

from twisted.internet.defer import inlineCallbacks
from txzookeeper.client import ZOO_OPEN_ACL_UNSAFE

from juju.machine.constraints import Constraints, ConstraintSet

from .auth import make_ace
from .environment import EnvironmentStateManager, GlobalSettingsStateManager
from .machine import MachineStateManager


log = logging.getLogger("juju.state.init")


class StateHierarchy(object):
    """
    An initializer to the juju zookeeper hierarchy.
    """

    def __init__(self, client, admin_identity, instance_id, constraints_data,
                 provider_type):
        """
        :param client: A zookeeper client
        :param admin_identity: A zookeeper auth identity for the admin.
        :param instance_id: The boostrap node machine id.
        :param constraints_data: A Constraints's data dictionary with which to
            set up the first machine state, and to store in the environment.
        :param provider_type: The type of the environnment machine provider.
        """
        self.client = client
        self.admin_identity = admin_identity
        self.instance_id = instance_id
        self.constraints_data = constraints_data
        self.provider_type = provider_type

    @inlineCallbacks
    def initialize(self):
        log.info("Initializing zookeeper hierarchy")

        acls = [make_ace(self.admin_identity, all=True),
                # XXX till we have roles throughout
                ZOO_OPEN_ACL_UNSAFE]

        yield self.client.create("/charms", acls=acls)
        yield self.client.create("/services", acls=acls)
        yield self.client.create("/machines", acls=acls)
        yield self.client.create("/units", acls=acls)
        yield self.client.create("/relations", acls=acls)

        # In this very specific case, it's OK to create a Constraints object
        # with a non-provider-specific ConstraintSet, because *all* we need it
        # for is its data dict. In *any* other circumstances, this would be Bad
        # and Wrong.
        constraints = Constraints(ConstraintSet(None), self.constraints_data)

        # Poke constraints data into a machine state to represent this machine.
        manager = MachineStateManager(self.client)
        machine_state = yield manager.add_machine_state(constraints)
        yield machine_state.set_instance_id(self.instance_id)

        # Set up environment constraints similarly.
        esm = EnvironmentStateManager(self.client)
        yield esm.set_constraints(constraints)

        # Setup default global settings information.
        settings = GlobalSettingsStateManager(self.client)
        yield settings.set_provider_type(self.provider_type)

        # This must come last, since clients will wait on it.
        yield self.client.create("/initialized", acls=acls)

        # DON'T WRITE ANYTHING HERE.  See line above.
