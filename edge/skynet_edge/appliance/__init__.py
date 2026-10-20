"""The zero-config appliance layer of the node (docs/NODE_APPLIANCE.md).

* ``detect``: what hardware is attached (``skynet-edge detect``).
* ``autoconfig``: detection + credentials + calibration -> ``edge.yaml``.
* ``credentials`` / ``enroll``: claim-code enrollment and the stored identity.
* ``tap``: preview snapshots and frame stacks served by the running node.
* ``waiting``: what ``skynet-edge run`` does before the node is enrolled.
* ``starcal_job``: the star-calibration scheduler.
* ``netsetup``, ``nm``: NetworkManager helpers and the fallback hotspot.
* ``firstboot``: decision helpers for the first-boot script.
* ``admin``: the root-side handler for restart / shutdown / password requests.
* ``web``: the setup and status page on port 80.

Everything that talks to hardware or system services runs commands with an
argument list (never a shell) and a timeout.
"""
