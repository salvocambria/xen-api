# Copyright (c) 2008,2009 Citrix Systems, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published
# by the Free Software Foundation; version 2.1 only. with the special
# exception on linking described in file LICENSE.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
from InterfaceReconfigure import *

import sys
import time

sysfs_bonding_masters = "/sys/class/net/bonding_masters"

def open_pif_ifcfg(pif):
    pifrec = db().get_pif_record(pif)

    interface = pif_netdev_name(pif)
    log("Configuring %s (%s)" % (interface, pifrec['MAC']))

    f = ConfigurationFile("/etc/sysconfig/network-scripts/ifcfg-%s" % interface)

    f.write("# DO NOT EDIT: This file (%s) was autogenerated by %s\n" % \
            (os.path.basename(f.path()), os.path.basename(sys.argv[0])))
    f.write("XEMANAGED=yes\n")
    f.write("DEVICE=%s\n" % interface)
    f.write("ONBOOT=no\n")

    return f

#
# Bare Network Devices -- network devices without IP configuration
#

def netdev_down(netdev):
    """Bring down a bare network device"""
    if not netdev_exists(netdev):
        log("netdev: down: device %s does not exist, ignoring" % netdev)
        return
    run_command(["/sbin/ifdown", netdev])

def netdev_up(netdev, mtu=None):
    """Bring up a bare network device"""
    #if not netdev_exists(netdev):
    #    raise Error("netdev: up: device %s does not exist" % netdev)

    run_command(["/sbin/ifup", netdev])

#
# Bonding driver
#

def load_bonding_driver():
    log("Loading bonding driver")
    run_command(["/sbin/modprobe", "bonding"])
    try:
        # bond_device_exists() uses the contents of sysfs_bonding_masters to work out which devices
        # have already been created.  Unfortunately the driver creates "bond0" automatically at
        # modprobe init.  Get rid of this now or our accounting will go wrong.
        f = open(sysfs_bonding_masters, "w")
        f.write("-bond0")
        f.close()
    except IOError, e:
        log("Failed to load bonding driver: %s" % e)

def bonding_driver_loaded():
    lines = open("/proc/modules").read().split("\n")
    modules = [line.split(" ")[0] for line in lines]
    return "bonding" in modules

def bond_device_exists(name):
    f = open(sysfs_bonding_masters, "r")
    bonds = f.readline().split()
    f.close()
    return name in bonds

def __create_bond_device(name):

    if not bonding_driver_loaded():
        load_bonding_driver()

    if bond_device_exists(name):
        log("bond master %s already exists, not creating" % name)
    else:
        log("Creating bond master %s" % name)
        try:
            f = open(sysfs_bonding_masters, "w")
            f.write("+" + name)
            f.close()
        except IOError, e:
            log("Failed to create %s: %s" % (name, e))

def create_bond_device(pif):
    """Ensures that a bond master device exists in the kernel."""

    if not pif_is_bond(pif):
        return

    __create_bond_device(pif_netdev_name(pif))

def __destroy_bond_device(name):
    if bond_device_exists(name):
        retries = 10 # 10 * 0.5 seconds
        while retries > 0:
            retries = retries - 1
            log("Destroying bond master %s (%d attempts remain)" % (name,retries))
            try:
                f = open(sysfs_bonding_masters, "w")
                f.write("-" + name)
                f.close()
                retries = 0
            except IOError, e:
                time.sleep(0.5)
    else:
        log("bond master %s does not exist, not destroying" % name)

def destroy_bond_device(pif):
    """No, Mr. Bond, I expect you to die."""

    pifrec = db().get_pif_record(pif)

    if not pif_is_bond(pif):
        return

    # If the bonding module isn't loaded then do nothing.
    if not os.access(sysfs_bonding_masters, os.F_OK):
        return

    name = pif_netdev_name(pif)

    __destroy_bond_device(name)

#
# Bridges
#

def pif_is_bridged(pif):
    pifrec = db().get_pif_record(pif)
    nwrec = db().get_network_record(pifrec['network'])

    if nwrec['bridge']:
        # TODO: sanity check that nwrec['bridgeless'] != 'true'
        return True
    else:
        # TODO: sanity check that nwrec['bridgeless'] == 'true'
        return False

def pif_bridge_name(pif):
    """Return the bridge name of a pif.

    PIF must be a bridged PIF."""
    pifrec = db().get_pif_record(pif)

    nwrec = db().get_network_record(pifrec['network'])

    if nwrec['bridge']:
        return nwrec['bridge']
    else:
        raise Error("PIF %(uuid)s does not have a bridge name" % pifrec)

#
# Bring Interface up/down.
#

def bring_down_interface(pif, destroy=False):
    """Bring down the interface associated with PIF.

    Brings down the given interface as well as any physical interfaces
    which are bond slaves of this one. This is because they will be
    required when the bond is brought up."""

    def destroy_bridge(pif):
        """Bring down the bridge associated with a PIF."""
        #if not pif_is_bridged(pif):
        #    return
        bridge = pif_bridge_name(pif)
        if not netdev_exists(bridge):
            log("destroy_bridge: bridge %s does not exist, ignoring" % bridge)
            return
        log("Destroy bridge %s" % bridge)
        netdev_down(bridge)
        run_command(["/usr/sbin/brctl", "delbr", bridge])

    def destroy_vlan(pif):
        vlan = pif_netdev_name(pif)
        if not netdev_exists(vlan):
            log("vconfig del: vlan %s does not exist, ignoring" % vlan)
            return
        log("Destroy vlan device %s" % vlan)
        run_command(["/sbin/vconfig", "rem", vlan])

    if pif_is_vlan(pif):
        interface = pif_netdev_name(pif)
        log("bring_down_interface: %s is a VLAN" % interface)
        netdev_down(interface)

        if destroy:
            destroy_vlan(pif)
            destroy_bridge(pif)
        else:
            return

        slave = pif_get_vlan_slave(pif)
        if db().get_pif_record(slave)['currently_attached']:
            log("bring_down_interface: vlan slave is currently attached")
            return

        masters = pif_get_vlan_masters(slave)
        masters = [m for m in masters if m != pif and db().get_pif_record(m)['currently_attached']]
        if len(masters) > 0:
            log("bring_down_interface: vlan slave has other masters")
            return

        log("bring_down_interface: no more masters, bring down vlan slave %s" % pif_netdev_name(slave))
        pif = slave
    else:
        vlan_masters = pif_get_vlan_masters(pif)
        log("vlan masters of %s - %s" % (db().get_pif_record(pif)['device'], [pif_netdev_name(m) for m in vlan_masters]))
        if len([m for m in vlan_masters if db().get_pif_record(m)['currently_attached']]) > 0:
            log("Leaving %s up due to currently attached VLAN masters" % pif_netdev_name(pif))
            return

    # pif is now either a bond or a physical device which needs to be brought down

    # Need to bring down bond slaves first since the bond device
    # must be up to enslave/unenslave.
    bond_slaves = pif_get_bond_slaves_sorted(pif)
    log("bond slaves of %s - %s" % (db().get_pif_record(pif)['device'], [pif_netdev_name(s) for s in bond_slaves]))
    for slave in bond_slaves:
        slave_interface = pif_netdev_name(slave)
        if db().get_pif_record(slave)['currently_attached']:
            log("leave bond slave %s up (currently attached)" % slave_interface)
            continue
        log("bring down bond slave %s" % slave_interface)
        netdev_down(slave_interface)
        # Also destroy the bridge associated with the slave, since
        # it will carry the MAC address and possibly an IP address
        # leading to confusion.
        destroy_bridge(slave)

    interface = pif_netdev_name(pif)
    log("Bring interface %s down" % interface)
    netdev_down(interface)

    if destroy:
        destroy_bond_device(pif)
        destroy_bridge(pif)

def interface_is_up(pif):
    try:
        interface = pif_netdev_name(pif)
        state = open("/sys/class/net/%s/operstate" % interface).read().strip()
        return state == "up"
    except:
        return False # interface prolly doesn't exist

def bring_up_interface(pif):
    """Bring up the interface associated with a PIF.

    Also bring up the interfaces listed in additional.
    """

    # VLAN on bond seems to need bond brought up explicitly, but VLAN
    # on normal device does not. Might as well always bring it up.
    if pif_is_vlan(pif):
        slave = pif_get_vlan_slave(pif)
        if not interface_is_up(slave):
            bring_up_interface(slave)

    interface = pif_netdev_name(pif)

    create_bond_device(pif)

    log("Bring interface %s up" % interface)
    netdev_up(interface)


#
# Datapath topology configuration.
#

def _configure_physical_interface(pif):
    """Write the configuration for a physical interface.

    Writes the configuration file for the physical interface described by
    the pif object.

    Returns the open file handle for the interface configuration file.
    """

    pifrec = db().get_pif_record(pif)

    log("Configuring physical interface %s" % pifrec['device'])

    f = open_pif_ifcfg(pif)

    f.write("TYPE=Ethernet\n")
    f.write("HWADDR=%(MAC)s\n" % pifrec)

    settings,offload = ethtool_settings(pifrec['other_config'])
    if len(settings):
        f.write("ETHTOOL_OPTS=\"%s\"\n" % str.join(" ", settings))
    if len(offload):
        f.write("ETHTOOL_OFFLOAD_OPTS=\"%s\"\n" % str.join(" ", offload))

    mtu = mtu_setting(pifrec['network'], "PIF", pifrec['other_config'])
    if mtu:
        f.write("MTU=%s\n" % mtu)

    return f

def pif_get_bond_slaves_sorted(pif):
    pifrec = db().get_pif_record(pif)

    # build a list of slave's pifs
    slave_pifs = pif_get_bond_slaves(pif)

    # Ensure any currently attached slaves are listed in the opposite order to the order in
    # which they were attached.  The first slave attached must be the last detached since
    # the bond is using its MAC address.
    try:
        attached_slaves = open("/sys/class/net/%s/bonding/slaves" % pifrec['device']).readline().split()
        for slave in attached_slaves:
            pifs = [p for p in db().get_pifs_by_device(slave) if not pif_is_vlan(p)]
            slave_pif = pifs[0]
            slave_pifs.remove(slave_pif)
            slave_pifs.insert(0, slave_pif)
    except IOError:
        pass

    return slave_pifs

def _configure_bond_interface(pif):
    """Write the configuration for a bond interface.

    Writes the configuration file for the bond interface described by
    the pif object. Handles writing the configuration for the slave
    interfaces.

    Returns the open file handle for the bond interface configuration
    file.
    """

    pifrec = db().get_pif_record(pif)

    f = open_pif_ifcfg(pif)

    if pifrec['MAC'] != "":
        f.write("MACADDR=%s\n" % pifrec['MAC'])

    for slave in pif_get_bond_slaves(pif):
        s = _configure_physical_interface(slave)
        s.write("MASTER=%(device)s\n" % pifrec)
        s.write("SLAVE=yes\n")
        s.close()
        f.attach_child(s)

    settings,offload = ethtool_settings(pifrec['other_config'])
    if len(settings):
        f.write("ETHTOOL_OPTS=\"%s\"\n" % str.join(" ", settings))
    if len(offload):
        f.write("ETHTOOL_OFFLOAD_OPTS=\"%s\"\n" % str.join(" ", offload))

    mtu = mtu_setting(pifrec['network'], "Bond-PIF", pifrec['other_config'])
    if mtu:
        f.write("MTU=%s\n" % mtu)

    # The bond option defaults
    bond_options = {
        "mode":   "balance-slb",
        "miimon": "100",
        "downdelay": "200",
        "updelay": "31000",
        "use_carrier": "1",
        }

    # override defaults with values from other-config whose keys being with "bond-"
    oc = pifrec['other_config']
    overrides = filter(lambda (key,val): key.startswith("bond-"), oc.items())
    overrides = map(lambda (key,val): (key[5:], val), overrides)
    bond_options.update(overrides)

    # write the bond options to ifcfg-bondX
    f.write('BONDING_OPTS="')
    for (name,val) in bond_options.items():
        f.write("%s=%s " % (name,val))
    f.write('"\n')
    return f

def _configure_vlan_interface(pif):
    """Write the configuration for a VLAN interface.

    Writes the configuration file for the VLAN interface described by
    the pif object. Handles writing the configuration for the master
    interface if necessary.

    Returns the open file handle for the VLAN interface configuration
    file.
    """

    slave = _configure_pif(pif_get_vlan_slave(pif))

    pifrec = db().get_pif_record(pif)

    f = open_pif_ifcfg(pif)
    f.write("VLAN=yes\n")

    settings,offload = ethtool_settings(pifrec['other_config'])
    if len(settings):
        f.write("ETHTOOL_OPTS=\"%s\"\n" % str.join(" ", settings))
    if len(offload):
        f.write("ETHTOOL_OFFLOAD_OPTS=\"%s\"\n" % str.join(" ", offload))

    mtu = mtu_setting(pifrec['network'], "VLAN-PIF", pifrec['other_config'])
    if mtu:
        f.write("MTU=%s\n" % mtu)

    f.attach_child(slave)

    return f

def _configure_pif(pif):
    """Write the configuration for a PIF object.

    Writes the configuration file the PIF and all dependent
    interfaces (bond slaves and VLAN masters etc).

    Returns the open file handle for the interface configuration file.
    """

    if pif_is_vlan(pif):
        f = _configure_vlan_interface(pif)
    elif pif_is_bond(pif):
        f = _configure_bond_interface(pif)
    else:
        f = _configure_physical_interface(pif)

    f.write("BRIDGE=%s\n" % pif_bridge_name(pif))
    f.close()

    return f

#
#
#

class DatapathBridge(Datapath):
    def __init__(self, pif):
        Datapath.__init__(self, pif)
        log("Configured for Bridge datapath")

    def configure_ipdev(self, cfg):
        if pif_is_bridged(self._pif):
            cfg.write("TYPE=Bridge\n")
            cfg.write("DELAY=0\n")
            cfg.write("STP=off\n")
            cfg.write("PIFDEV=%s\n" % pif_netdev_name(self._pif))
        else:
            cfg.write("TYPE=Ethernet\n")
        
    def preconfigure(self, parent):
        pf = _configure_pif(self._pif)
        parent.attach_child(pf)

    def bring_down_existing(self):
        # Bring down any VLAN masters so that we can reconfigure the slave.
        for master in pif_get_vlan_masters(self._pif):
            name = pif_netdev_name(master)
            log("action_up: bring down vlan master %s" % (name))
            netdev_down(name)

        # interface-reconfigure is never explicitly called to down a bond master.
        # However, when we are called to up a slave it is implicit that we are destroying the master.
        bond_masters = pif_get_bond_masters(self._pif)
        for master in bond_masters:
            log("action_up: bring down bond master %s" % (pif_netdev_name(master)))
            # bring down master
            bring_down_interface(master, destroy=True)

        # No masters left - now its safe to reconfigure the slave.
        bring_down_interface(self._pif)
        
    def configure(self):
        bring_up_interface(self._pif)

    def post(self):
        # Bring back any currently-attached VLAN masters
        for master in [v for v in pif_get_vlan_masters(self._pif) if db().get_pif_record(v)['currently_attached']]:
            name = pif_netdev_name(master)
            log("action_up: bring up %s" % (name))
            netdev_up(name)

    def bring_down(self):
        bring_down_interface(self._pif, destroy=True)
