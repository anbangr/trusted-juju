Local provider configuration
----------------------------

The local provider allows for deploying services directly against the local/host machine
using LXC containers with the goal of experimenting with juju and developing formulas.

The local provider has some additional package dependencies. Attempts to use
this provider without these packages installed will terminate with a message
indicating the missing packages.

The following are packages are required.

 - libvirt-bin
 - lxc
 - apt-cacher-ng
 - zookeeper


The local provider can be configured by specifying "provider: local" and a "data-dir":
as an example::

  local:
    type: local
    data-dir: /tmp/local-dev
    admin-secret: b3a5dee4fb8c4fc9a4db04751e5936f4
    default-series: oneiric

Upon running ``juju bootstrap`` a zookeeper instance will be started on the host
along with a machine agent. The bootstrap command will prompt for sudo access
as the machine agent needs to run as root in order to create containers on the
local machine.

The containers created are namespaced in such a way that you can create multiple
environments on a machine. The containers also namespaced by user for multi-user
machines.

Local provider environments do not survive reboots of the host at this time, the
environment will need to be destroyed and recreated after a reboot.


Provider specific options
=========================

  data-dir:
    Directory for zookeeper state and log files.



Troubleshooting
===============

Local development is still a young and evolving feature and because of
the many complex system interactions its possible you'll encounter
some speed bumps along your path. There are a number of log files you
should be aware of when trying to understand how the system behaves.

Once``juju bootstrap`` has run you'll have a directory in place on
your filesystem at the location specified in your
environments.yaml. In here you'll find a number of files related to
the runtime of the local deployment system. The first step is to
verify that the machine-agent.log includes a line similar to::

    2011-10-05 14:43:56,327: juju.agents.machine@INFO: Machine agent started id:0 deploy:<class 'juju.machine.unit.UnitContainerDeployment'> provider:'local'

When you first attempt to deploy a unit the time consuming process of
creating a unit will begin. The first time you run this it can taGke
quite some time and if your impatient and need to check on the process
`lxc-ls` will show you which templates are created or being created
and::

    pgrep lxc| head -1| xargs watch pstree -alU

will give you a refeshing view of the first container as its built.

Once a container is built you should have access to it via ssh through
the `juju ssh` as normal.

If services are not running properly as exposed by `juju status` the
log file in *data-dir/units/master-customize.log* should provide insight
into the nature of the error.

When providing bug reports or including issues the output of the both
the master-customize.log mentioned above and the result of the
devel-tools/juju-inspect-local-provider script. If you are not running
a development checkout this script should be located at
*/usr/lib/juju/juju/misc/devel-tools/juju-inspect-local-provider*

By passing the name of the container as an argument to that script
additional information abou the container will be included in the
output as well. To find the name of the container in question `lxc-ls`
can be used.
