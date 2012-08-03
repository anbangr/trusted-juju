 Subordinate service implementation details
===========================================


This document explains the implementation of subordinate services. For
a higher level understanding please refer to the primary :doc:`subordinates
document <subordinate-services>`.


Overview
-------

Principal services can have relationships with subordinates. This is
modeled using extensions to the `client-server` relationship
type. This new relation type is used exclusively between subordinates
and principals and is used to limit communication to only service
units in the same container.


ZooKeeper state
---------------

Subordinate relations use the normal `client-server` relationship type
but store additional information in the relation nodes to maintain a
1-1 mapping from the unit-id of the principal to the unit id of the
subordinate. ::

    relations/
      relation-000000001/
              unit-00000001/
                 client/
                      unit-000000002/
                 server/
                      unit-000000001/
                 settings/
                     unit-00000001/
                     |    private-address: 10.2.2.2
                     -------------------------------
              unit-00000004/
                 client/
                      unit-000000003/
                 server/
                      unit-000000004/
                 settings/
                     unit-00000004/
                     |    private-address: 10.2.2.4
                     -------------------------------
      relation-000000002/
               client/
                    unit-000000002/
               server/
                    unit-000000001/
               settings/
                   unit-00000001/
                   |    private-address: 10.2.2.2
                   -------------------------------

Elements ending with **/** refer to ZK nodes with its children
indented under it. Elements preceded by **|** represent a YAML
encoded structure under the preceding ZK node. The new logical
structure is ::

     relations/
         relation-<id>/
             unit-<principal id>/ # omitted for global scoped relations
                      <role>/ {name: <name>, role: <role>}
                          - list of unit presence nodes

The containment structure varies based on the type of
relationship. Container scoped relations have an additional level of
nesting in the structure which is the unit id of the container for a
given pair of relationship endpoints. Globally scoped relations have
the "client", "server" and "settings" nodes directly under the
"relation-<id>" node.  Supplemental information from the topology
enables this structure to be used in the construction of watches.

Additionally there are changes related to the storage of the relation
in the topology. Keys are added per relationship, including
`interface` (previously referred to as relation_type), `scope` which
is the relation scope (at runtime) and services which includes the
`name` and `role` mappings of each service participating in the
relationship. ::

    relations
        relation-00000001:
            interface: name
            scope: name
            services:
              service-00000001: {name: <name>, role: <role>}
              service-00000002: {name: <name>, role: <role>}

This change resulted in the topology VERSION being incremented from 1
to 2.  Migration from the old topology to the newer VERSION is handled
automatically and is transparent, even with existing deployments.


Watch related changes
---------------------

The unit agent will use watch_relation_states to see when new
relations are added. When a subordinate relation is present deployment
actions will be taken in the unit agents lifecycle.

UnitRelationStates.watch_related_units will dispatch on the relation
and only establish watches between the principal and subordinate units
in the same container.

Deployment
----------

`juju deploy` needs to enforce that services where `subordinate: true`
is defined are not deployed until a `scope: conatiner` relationship is
added to a charm which is `subordinate: false`.

The UnitMachineDeployment/UnitContainerDeployment in machine/unit will
undergo minor refactoring to make it more easily useable by the
UnitAgent to do its deployment of subordinate services. The Unit Agent
is the only entity with direct a relationship to its subordinate unit
and so the UnitAgent does the deployment of its subordinate units
rather than the MachineAgent. This model will continue to work in the
expected LXCEverywhere future.

When a subordinate unit is deployed it is assigned the public and private
addresses of its principal service (even though it may expose its own
ports). This is because networking is dependent on the container its
running it, i.e, that of the principal's service unit.

One interesting caveat is that we don't assign the subordinate unit a
machine id. `juju-status` exposes this information in a way that is
clear and is outlined below. Machine assignment is currently used to
trigger a class of deployment activities which subordinate services do
not take advantage of. It is an error to assign a machine directly to
a subordinate unit (as this indicates a usage error).

`juju.unit.lifecycle._process_service_changes` is currently
responsible for adding unit state to relations. This code will change
so that when relationships are added or removed we have access to the
unit_name of the subordinate. This information is used to annotate the
UnitRelationState with the mapping from principal unit_name to
subordinate unit name and is stored in ZooKeeper as outlined
above. When a relationship is removed we detect this here as well and
update the mapping using the unit_name of the principal.

`juju.state.relation.ServiceRelationState.add_unit_state` will be
augmented to support tracking of the 1-1 mapping between principal and
subordinate unit names. It will take an optional `principal_unit`
argument. This will take the `unit_name` the of principal unit the
service is contained with. Error checking will validate that the
service unit should be subordinate to the principal service in
question.


Unit management
---------------

`juju add-unit` must raise an error informing the admin they can not
add units of a subordinate service. These scale automatically with the
principal service.

`juju remove-unit` produces an error on subordinate services for the
same reason.


Relation management
-------------------

`juju add-relation` and `juju remove-relation` must trigger the
deployment and removal of subordinate units. This is done using the
watch machinery outlined above. Each principal service unit will
deploy a new unit agent for its subordinate when the appropriate new
relationship is added and remove it when the relationship is departed.

The subordinate service will maintain a watch of its relationship to
the principal and should this relationship be removed the subordinate
will transition its state to stopped and then remove its own state
from the container and terminate. This will require follow up work to
handle the proper triggering of stop hooks on the subordinate units
which isn't handled

Status
------

The changes to status are outlined in the user oriented documentation.


Roadmap
-------

This serves as a guide to the planned merge tree. ::

    subordinate-spec
    subordinate-charms  # (charm metadata support)
    subordinate-implicit-interfaces # (juju-info)
    subordinate-relation-type

    subordinate-control-deploy
    subordinate-control-units # (add/remove)
    subordinate-control-status

    subordinate-control-relations # (add/remove)
    subordinate-unit-agent-deploy
