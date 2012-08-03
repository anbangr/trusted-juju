Machine Placement
=================


Motivation
----------

To fulfil a popular set of use cases, juju needs a mechanism to control
placement of service units; to fulfil the simplest possible version of this
story, we consider two possible cases:

* User needs to specify minimum hardware requirements * User needs to specify
  other, context-dependent, hardware attributes

This feature allows us to remove the temporary EC2 environment properties
`default-image-id` and `default-machine-type`, and makes obsolete the
requirement to interact with cobbler out-of-band when placing services and units
in an orchestra environment.


Constraints
-----------

We introduce "machine constraints", which are used to encode an administrator's
hardware requirements. Constraints can be set for environments and services,
with lookups for each key falling back from more specific to more general
settings, with default values set by juju when otherwise unspecified [0]_.
Changes to constraints do not affect any unit that's already been placed on a
machine.

Constraints will be controlled with a new command, `juju set-constraints`,
taking an optional `--service-name` and any number of `key=value` pairs. When
the service name is specified, the constraints are set on that service;
otherwise they are set on the environment. An empty `value` is treated as "the
juju default", allowing users to ignore environment settings at the service
level without having to explicitly remember and use the juju default value.
There is no way to change the juju default values.

We also extend the syntax for `juju deploy`, such that `--constraints` expects a
single string of space-separated constraints, understood as above; these
constraints will be set on the service before the first unit is deployed. `juju
bootstrap` will also understand the `--constraints` argument, and will set them
on the environment.

The constraints fall into two categories:

* Generic constraints: based on the minimal properties exposed by every
  provider. These are:

  * `cpu`: The minimum processing power of the machine, measured in `ECU
    http://en.wikipedia.org/wiki/Amazon_Elastic_Compute_Cloud#Elastic_compute_units`_,
    defaulting to 1; any real number >= 0 is valid.  * `mem`: The minimum memory
    for the machine, defaulting to 512MB; any real number >= 0 and suffixed with
    M, G or T is valid.  * `arch`: The machine's processor architecture. Unset
    by default; valid values are "386", "amd64", and "arm".

  In the case of `cpu` or `mem`, the value `0` is treated specially, and used to
  denote that the value doesn't matter at all. EC2 could, for example,
  special-case `cpu=0` to allow for deploying to a t1.micro without naming it
  explicitly, but this is not strictly necessary; in the general case, `<key>=0`
  simply means "do not filter on `<key>`.

  When interpreting generic constraints, juju is expected to deploy machines
  with as little wastage as possible: that is to say, given a `mem=3G`
  constraint and two available machines with 4G and 6G respectively, juju would
  deploy to the 4G machine.

* Provider constraints: based on the additional properties exposed by a given
  provider, which are not expected to be directly portable to other providers:

  * For EC2, we expect to expose:

    * `ec2-zone`: availability zone within the EC2 region. Unset by default (so
      we get what we're given); valid values depend on the current EC2 region
      (which is set in environments.yaml, as before).  * `ec2-instance-type`:
      instance type. Unset by default (so we fall back to the juju-level `mem`
      and `cpu` requirements); valid values are those machine types that EC2 has
      available in the given region.

  * For orchestra, we expect to expose:

    * `orchestra-name`: to allow admins to specify a specific instance by name,
      if required. Unset by default; the valid values are the set of instance
      names exposed by cobbler.  * `orchestra-classes`: a comma-separated list
      of cobbler mgmt-classes to which the instance must belong. Empty by
      default; the valid values are the existing cobbler mgmt-classes, excluding
      the current values of `available-mgmt-class` and `acquired-mgmt-class`
      (from the orchestra environment settings).

  Provider constraints are only effective when used with the appropriate
  provider, and are silently ignored when specified with a different provider.

  Note that provider constraints can overlap with generic constraints: in such a
  situation, the tightest constraints take precendence, such that `mem=8G
  ec2-instance-type=m1.small` would actually produce an `m1.xlarge` (the
  smallest instance with at least 8G of RAM); meanwhile, `cpu=1
  ec2-instance-type=m1.large` would produce an m1.large.

Finally, please note that for the local provider, given its nature and its
intended use as a development tool, no constraints have any effect whatsoever.


Issues
------

Please note that this spec depends on features of juju and orchestra that are
not present at the time of writing:

* We need orchestra to expose `arch`, `cpu`, and `mem`; as long as the data is
  available, the orchestra team is free to expose them in whatever way is
  convenient; but the feature will be severely hobbled if we don't have access
  at all.


Potential Enhancements
----------------------

The above spec is intended to be small and focused; several enhancements are
possible and may be desirable in the future. They include:

  * Unit-level constraints, specified at `juju add-unit` time, and inheriting
    naturally from service, environment, and juju.

  * An additional generic constraint, `storage`: The minimum persistent disk
    space for the machine, defaulting to 4GB. Valid inputs as for `mem`.

* Max constraints: allow the `cpu` and `mem` (and maybe `storage`) constraints
  to also take the value `max`, meaning "the best available".

* Additional provider constraints, such as:

    * `orchestra-dns-name`: potentially useful for sysadmins who don't primarily
      think of their systems by `orchestra-name`.

* Co-location constraints: for determining machine placement in terms of
  existing juju components. Possibilities include:

  * `place-in=<machine-id>`: On a separate container in the machine with juju id
    `machine-id`.

  * `place-with=<service-name>`: On a separate container in *any* machine with a
    unit of `service-name` deployed.

  * `scale-with=<service-name>`: On a separate container in *every* machine
    running a unit of `service-name`; henceforth every add or remove of a unit
    of `service-name` will lead to the addition or removal of the corresponding
    unit of the requested service.

  Note that it is not advisable to implement co-location constraints without
  having the ability to deploy units in isolated containers on single machines;
  and doing this is somewhat tricky, so this is almost certainly impossible to
  achieve by 12.04.

* Provider->generic constraint translation: In the event of our implementing
  stacks, it may be useful to be able to convert provider-specific constraints
  into generic ones (where possible) to facilitate creation of
  provider-independent stacks.


.. [0] So: if an environment has specified `ec2-zone=a mem=1G` and a service has
specified `mem=2G`, instances of that service in that environment will inherit
the `ec2-zone` setting of "a", and the juju default `cpu` of "1", while using
the locally-specified `mem` of "2G".
