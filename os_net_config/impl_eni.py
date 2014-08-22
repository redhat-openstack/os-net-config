# -*- coding: utf-8 -*-

# Copyright 2014 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging

import netaddr
import os_net_config
from os_net_config import objects
from os_net_config import utils

from os_net_config.openstack.common import processutils


logger = logging.getLogger(__name__)


# TODO(?): should move to interfaces.d
def _network_config_path():
    return "/etc/network/interfaces"


class ENINetConfig(os_net_config.NetConfig):
    """Debian/Ubuntu implementation for network config

       Configure iface/bridge/routes using debian/ubuntu
       /etc/network/interfaces format.
    """

    def __init__(self):
        self.interfaces = {}
        self.routes = {}
        self.bridges = {}
        logger.info('Ifcfg net config provider created.')

    def _add_common(self, interface, static_addr=None):

        ovs_extra = []
        data = ""
        address_data = ""
        if static_addr:
            address_data += "    address %s\n" % static_addr.ip
            address_data += "    netmask %s\n" % static_addr.netmask
        else:
            v4_addresses = interface.v4_addresses()
            if v4_addresses:
                data += self._add_common(interface, v4_addresses[0])

            v6_addresses = interface.v6_addresses()
            if v6_addresses:
                data += self._add_common(interface, v6_addresses[0])

            if data:
                return data

        if isinstance(interface, objects.Vlan):
            _iface = "iface vlan%i " % interface.vlan_id
        else:
            _iface = "iface %s " % interface.name
        if static_addr and static_addr.version == 6:
            _iface += "inet6 "
        else:
            _iface += "inet "
        if interface.use_dhcp:
            _iface += "dhcp\n"
        elif interface.addresses:
            _iface += "static\n"
        else:
            _iface += "manual\n"
        if isinstance(interface, objects.OvsBridge):
            data += "auto %s\n" % interface.name
            data += "allow-ovs %s\n" % interface.name
            data += _iface
            data += address_data
            data += "    ovs_type OVSBridge\n"
            if interface.members:
                data += "    ovs_ports"
                for i in interface.members:
                    data += " %s" % i.name
                data += "\n"
                for mem in interface.members:
                    if isinstance(mem, objects.Interface):
                        data += "    pre-up ip addr flush dev %s\n" % mem.name
                if interface.primary_interface_name:
                    mac = utils.interface_mac(interface.primary_interface_name)
                    ovs_extra.append("set bridge %s other-config:hwaddr=%s" %
                                     (interface.name, mac))
            ovs_extra.extend(interface.ovs_extra)
        elif interface.ovs_port:
            if isinstance(interface, objects.Vlan):
                data += "auto vlan%i\n" % interface.vlan_id
                data += "allow-%s vlan%i\n" % (interface.bridge_name,
                                               interface.vlan_id)
                data += _iface
                data += address_data
                data += "    ovs_bridge %s\n" % interface.bridge_name
                data += "    ovs_type OVSIntPort\n"
                data += "    ovs_options tag=%s\n" % interface.vlan_id

            else:
                data += "auto %s\n" % interface.name
                data += "allow-%s %s\n" % (interface.bridge_name,
                                           interface.name)
                data += _iface
                data += address_data
                data += "    ovs_bridge %s\n" % interface.bridge_name
                data += "    ovs_type OVSPort\n"
        elif isinstance(interface, objects.Vlan):
            data += "auto vlan%i\n" % interface.vlan_id
            data += _iface
            data += address_data
            data += "    vlan-raw-device %s\n" % interface.device
        else:
            data += "auto %s\n" % interface.name
            data += _iface
            data += address_data
        if interface.mtu != 1500:
            data += "    mtu %i\n" % interface.mtu

        if ovs_extra:
            data += "    ovs_extra %s\n" % " -- ".join(ovs_extra)

        return data

    def add_interface(self, interface):
        """Add an Interface object to the net config object.

        :param interface: The Interface object to add.
        """
        logger.info('adding interface: %s' % interface.name)
        data = self._add_common(interface)
        logger.debug('interface data: %s' % data)
        self.interfaces[interface.name] = data
        if interface.routes:
            self._add_routes(interface.name, interface.routes)

    def add_bridge(self, bridge):
        """Add an OvsBridge object to the net config object.

        :param bridge: The OvsBridge object to add.
        """
        logger.info('adding bridge: %s' % bridge.name)
        data = self._add_common(bridge)
        logger.debug('bridge data: %s' % data)
        self.bridges[bridge.name] = data
        if bridge.routes:
            self._add_routes(bridge.name, bridge.routes)

    def add_vlan(self, vlan):
        """Add a Vlan object to the net config object.

        :param vlan: The vlan object to add.
        """
        logger.info('adding vlan: %s' % vlan.name)
        data = self._add_common(vlan)
        logger.debug('vlan data: %s' % data)
        self.interfaces[vlan.name] = data
        if vlan.routes:
            self._add_routes(vlan.name, vlan.routes)

    def _add_routes(self, interface_name, routes=[]):
        logger.info('adding custom route for interface: %s' % interface_name)
        data = ""
        for route in routes:
            rt = netaddr.IPNetwork(route.ip_netmask)
            data += "up route add -net %s netmask %s gw %s\n" % (
                    str(rt.ip), str(rt.netmask), route.next_hop)
            data += "down route del -net %s netmask %s gw %s\n" % (
                    str(rt.ip), str(rt.netmask), route.next_hop)
        self.routes[interface_name] = data
        logger.debug('route data: %s' % self.routes[interface_name])

    def apply(self, noop=False, cleanup=False):
        """Apply the network configuration.

        :param noop: A boolean which indicates whether this is a no-op.
        :returns: a dict of the format: filename/data which contains info
            for each file that was changed (or would be changed if in --noop
            mode).
        """
        new_config = ""

        # write out bridges first. This ensures that an ifup -a
        # on reboot brings them up first
        for bridge_name, bridge_data in self.bridges.iteritems():
            route_data = self.routes.get(bridge_name)
            bridge_data += (route_data or '')
            new_config += bridge_data

        for interface_name, iface_data in self.interfaces.iteritems():
            route_data = self.routes.get(interface_name)
            iface_data += (route_data or '')
            new_config += iface_data

        if (utils.diff(_network_config_path(), new_config)):
            if noop:
                return {"/etc/network/interfaces": new_config}
            for interface in self.interfaces.keys():
                logger.info('running ifdown on interface: %s' % interface)
                processutils.execute('/sbin/ifdown', interface,
                                     check_exit_code=False)

            for bridge in self.bridges.keys():
                logger.info('running ifdown on bridge: %s' % bridge)
                processutils.execute('/sbin/ifdown', bridge,
                                     check_exit_code=False)

            logger.info('writing config file')
            utils.write_config(_network_config_path(), new_config)

            for bridge in self.bridges.keys():
                logger.info('running ifup on bridge: %s' % bridge)
                processutils.execute('/sbin/ifup', bridge)

            for interface in self.interfaces.keys():
                logger.info('running ifup on interface: %s' % interface)
                processutils.execute('/sbin/ifup', interface)
        else:
            logger.info('No interface changes are required.')
