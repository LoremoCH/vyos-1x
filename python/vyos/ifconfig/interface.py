# Copyright 2019-2020 VyOS maintainers and contributors <maintainers@vyos.io>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library.  If not, see <http://www.gnu.org/licenses/>.

import os
import re
import json
import jmespath

from copy import deepcopy
from glob import glob

from ipaddress import IPv4Network
from ipaddress import IPv6Address
from ipaddress import IPv6Network
from netifaces import ifaddresses
# this is not the same as socket.AF_INET/INET6
from netifaces import AF_INET
from netifaces import AF_INET6

from vyos import ConfigError
from vyos.configdict import list_diff
from vyos.configdict import dict_merge
from vyos.template import render
from vyos.util import mac2eui64
from vyos.validate import is_ipv4
from vyos.validate import is_ipv6
from vyos.validate import is_intf_addr_assigned
from vyos.validate import assert_boolean
from vyos.validate import assert_list
from vyos.validate import assert_mac
from vyos.validate import assert_mtu
from vyos.validate import assert_positive
from vyos.validate import assert_range

from vyos.ifconfig.control import Control
from vyos.ifconfig.vrrp import VRRP
from vyos.ifconfig.operational import Operational
from vyos.ifconfig import Section

def get_ethertype(ethertype_val):
    if ethertype_val == '0x88A8':
        return '802.1ad'
    elif ethertype_val == '0x8100':
        return '802.1q'
    else:
        raise ConfigError('invalid ethertype "{}"'.format(ethertype_val))

class Interface(Control):
    # This is the class which will be used to create
    # self.operational, it allows subclasses, such as
    # WireGuard to modify their display behaviour
    OperationalClass = Operational

    options = ['debug', 'create']
    required = []
    default = {
        'type': '',
        'debug': True,
        'create': True,
    }
    definition = {
        'section': '',
        'prefixes': [],
        'vlan': False,
        'bondable': False,
        'broadcast': False,
        'bridgeable':  False,
        'eternal': '',
    }

    _command_get = {
        'admin_state': {
            'shellcmd': 'ip -json link show dev {ifname}',
            'format': lambda j: 'up' if 'UP' in jmespath.search('[*].flags | [0]', json.loads(j)) else 'down',
        },
        'vlan_protocol': {
            'shellcmd': 'ip -json -details link show dev {ifname}',
            'format': lambda j: jmespath.search('[*].linkinfo.info_data.protocol | [0]', json.loads(j)),
        },
    }

    _command_set = {
        'admin_state': {
            'validate': lambda v: assert_list(v, ['up', 'down']),
            'shellcmd': 'ip link set dev {ifname} {value}',
        },
        'mac': {
            'validate': assert_mac,
            'shellcmd': 'ip link set dev {ifname} address {value}',
        },
        'vrf': {
            'convert': lambda v: f'master {v}' if v else 'nomaster',
            'shellcmd': 'ip link set dev {ifname} {value}',
        },
    }

    _sysfs_get = {
        'alias': {
            'location': '/sys/class/net/{ifname}/ifalias',
        },
        'mac': {
            'location': '/sys/class/net/{ifname}/address',
        },
        'mtu': {
            'location': '/sys/class/net/{ifname}/mtu',
        },
        'oper_state':{
            'location': '/sys/class/net/{ifname}/operstate',
        },
    }

    _sysfs_set = {
        'alias': {
            'convert': lambda name: name if name else '\0',
            'location': '/sys/class/net/{ifname}/ifalias',
        },
        'mtu': {
            'validate': assert_mtu,
            'location': '/sys/class/net/{ifname}/mtu',
        },
        'arp_cache_tmo': {
            'convert': lambda tmo: (int(tmo) * 1000),
            'location': '/proc/sys/net/ipv4/neigh/{ifname}/base_reachable_time_ms',
        },
        'arp_filter': {
            'validate': assert_boolean,
            'location': '/proc/sys/net/ipv4/conf/{ifname}/arp_filter',
        },
        'arp_accept': {
            'validate': lambda arp: assert_range(arp,0,2),
            'location': '/proc/sys/net/ipv4/conf/{ifname}/arp_accept',
        },
        'arp_announce': {
            'validate': assert_boolean,
            'location': '/proc/sys/net/ipv4/conf/{ifname}/arp_announce',
        },
        'arp_ignore': {
            'validate': assert_boolean,
            'location': '/proc/sys/net/ipv4/conf/{ifname}/arp_ignore',
        },
        'ipv6_accept_ra': {
            'validate': lambda ara: assert_range(ara,0,3),
            'location': '/proc/sys/net/ipv6/conf/{ifname}/accept_ra',
        },
        'ipv6_autoconf': {
            'validate': lambda aco: assert_range(aco,0,2),
            'location': '/proc/sys/net/ipv6/conf/{ifname}/autoconf',
        },
        'ipv6_forwarding': {
            'validate': lambda fwd: assert_range(fwd,0,2),
            'location': '/proc/sys/net/ipv6/conf/{ifname}/forwarding',
        },
        'ipv6_dad_transmits': {
            'validate': assert_positive,
            'location': '/proc/sys/net/ipv6/conf/{ifname}/dad_transmits',
        },
        'proxy_arp': {
            'validate': assert_boolean,
            'location': '/proc/sys/net/ipv4/conf/{ifname}/proxy_arp',
        },
        'proxy_arp_pvlan': {
            'validate': assert_boolean,
            'location': '/proc/sys/net/ipv4/conf/{ifname}/proxy_arp_pvlan',
        },
        # link_detect vs link_filter name weirdness
        'link_detect': {
            'validate': lambda link: assert_range(link,0,3),
            'location': '/proc/sys/net/ipv4/conf/{ifname}/link_filter',
        },
    }

    @classmethod
    def exists(cls, ifname):
        return os.path.exists(f'/sys/class/net/{ifname}')

    def __init__(self, ifname, **kargs):
        """
        This is the base interface class which supports basic IP/MAC address
        operations as well as DHCP(v6). Other interface which represent e.g.
        and ethernet bridge are implemented as derived classes adding all
        additional functionality.

        For creation you will need to provide the interface type, otherwise
        the existing interface is used

        DEBUG:
        This class has embedded debugging (print) which can be enabled by
        creating the following file:
        vyos@vyos# touch /tmp/vyos.ifconfig.debug

        Example:
        >>> from vyos.ifconfig import Interface
        >>> i = Interface('eth0')
        """

        self.config = deepcopy(self.default)
        for k in self.options:
            if k in kargs:
                self.config[k] = kargs[k]

        # make sure the ifname is the first argument and not from the dict
        self.config['ifname'] = ifname
        self._admin_state_down_cnt = 0

        # we must have updated config before initialising the Interface
        super().__init__(**kargs)
        self.ifname = ifname

        if not self.exists(ifname):
            # Any instance of Interface, such as Interface('eth0')
            # can be used safely to access the generic function in this class
            # as 'type' is unset, the class can not be created
            if not self.config['type']:
                raise Exception(f'interface "{ifname}" not found')

            # Should an Instance of a child class (EthernetIf, DummyIf, ..)
            # be required, then create should be set to False to not accidentally create it.
            # In case a subclass does not define it, we use get to set the default to True
            if self.config.get('create',True):
                for k in self.required:
                    if k not in kargs:
                        name = self.default['type']
                        raise ConfigError(f'missing required option {k} for {name} {ifname} creation')

                self._create()
            # If we can not connect to the interface then let the caller know
            # as the class could not be correctly initialised
            else:
                raise Exception('interface "{}" not found'.format(self.config['ifname']))

        # temporary list of assigned IP addresses
        self._addr = []

        self.operational = self.OperationalClass(ifname)
        self.vrrp = VRRP(ifname)

    def _create(self):
        cmd = 'ip link add dev {ifname} type {type}'.format(**self.config)
        self._cmd(cmd)

    def remove(self):
        """
        Remove interface from operating system. Removing the interface
        deconfigures all assigned IP addresses and clear possible DHCP(v6)
        client processes.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> i = Interface('eth0')
        >>> i.remove()
        """

        # remove all assigned IP addresses from interface - this is a bit redundant
        # as the kernel will remove all addresses on interface deletion, but we
        # can not delete ALL interfaces, see below
        self.flush_addrs()

        # ---------------------------------------------------------------------
        # Any class can define an eternal regex in its definition
        # interface matching the regex will not be deleted

        eternal = self.definition['eternal']
        if not eternal:
            self._delete()
        elif not re.match(eternal, self.ifname):
            self._delete()

    def _delete(self):
        # NOTE (Improvement):
        # after interface removal no other commands should be allowed
        # to be called and instead should raise an Exception:
        cmd = 'ip link del dev {ifname}'.format(**self.config)
        return self._cmd(cmd)

    def get_mtu(self):
        """
        Get/set interface mtu in bytes.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').get_mtu()
        '1500'
        """
        return self.get_interface('mtu')

    def set_mtu(self, mtu):
        """
        Get/set interface mtu in bytes.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_mtu(1400)
        >>> Interface('eth0').get_mtu()
        '1400'
        """
        return self.set_interface('mtu', mtu)

    def get_mac(self):
        """
        Get current interface MAC (Media Access Contrl) address used.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').get_mac()
        '00:50:ab:cd:ef:00'
        """
        return self.get_interface('mac')

    def set_mac(self, mac):
        """
        Set interface MAC (Media Access Contrl) address to given value.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_mac('00:50:ab:cd:ef:01')
        """

        # If MAC is unchanged, bail out early
        if mac == self.get_mac():
            return None

        # MAC address can only be changed if interface is in 'down' state
        prev_state = self.get_admin_state()
        if prev_state == 'up':
            self.set_admin_state('down')

        self.set_interface('mac', mac)

        # Turn an interface to the 'up' state if it was changed to 'down' by this fucntion
        if prev_state == 'up':
            self.set_admin_state('up')

    def set_vrf(self, vrf=''):
        """
        Add/Remove interface from given VRF instance.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_vrf('foo')
        >>> Interface('eth0').set_vrf()
        """
        self.set_interface('vrf', vrf)

    def set_arp_cache_tmo(self, tmo):
        """
        Set ARP cache timeout value in seconds. Internal Kernel representation
        is in milliseconds.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_arp_cache_tmo(40)
        """
        return self.set_interface('arp_cache_tmo', tmo)

    def set_arp_filter(self, arp_filter):
        """
        Filter ARP requests

        1 - Allows you to have multiple network interfaces on the same
            subnet, and have the ARPs for each interface be answered
            based on whether or not the kernel would route a packet from
            the ARP'd IP out that interface (therefore you must use source
            based routing for this to work). In other words it allows control
            of which cards (usually 1) will respond to an arp request.

        0 - (default) The kernel can respond to arp requests with addresses
            from other interfaces. This may seem wrong but it usually makes
            sense, because it increases the chance of successful communication.
            IP addresses are owned by the complete host on Linux, not by
            particular interfaces. Only for more complex setups like load-
            balancing, does this behaviour cause problems.
        """
        return self.set_interface('arp_filter', arp_filter)

    def set_arp_accept(self, arp_accept):
        """
        Define behavior for gratuitous ARP frames who's IP is not
        already present in the ARP table:
        0 - don't create new entries in the ARP table
        1 - create new entries in the ARP table

        Both replies and requests type gratuitous arp will trigger the
        ARP table to be updated, if this setting is on.

        If the ARP table already contains the IP address of the
        gratuitous arp frame, the arp table will be updated regardless
        if this setting is on or off.
        """
        return self.set_interface('arp_accept', arp_accept)

    def set_arp_announce(self, arp_announce):
        """
        Define different restriction levels for announcing the local
        source IP address from IP packets in ARP requests sent on
        interface:
        0 - (default) Use any local address, configured on any interface
        1 - Try to avoid local addresses that are not in the target's
            subnet for this interface. This mode is useful when target
            hosts reachable via this interface require the source IP
            address in ARP requests to be part of their logical network
            configured on the receiving interface. When we generate the
            request we will check all our subnets that include the
            target IP and will preserve the source address if it is from
            such subnet.

        Increasing the restriction level gives more chance for
        receiving answer from the resolved target while decreasing
        the level announces more valid sender's information.
        """
        return self.set_interface('arp_announce', arp_announce)

    def set_arp_ignore(self, arp_ignore):
        """
        Define different modes for sending replies in response to received ARP
        requests that resolve local target IP addresses:

        0 - (default): reply for any local target IP address, configured
            on any interface
        1 - reply only if the target IP address is local address
            configured on the incoming interface
        """
        return self.set_interface('arp_ignore', arp_ignore)

    def set_ipv6_accept_ra(self, accept_ra):
        """
        Accept Router Advertisements; autoconfigure using them.

        It also determines whether or not to transmit Router Solicitations.
        If and only if the functional setting is to accept Router
        Advertisements, Router Solicitations will be transmitted.

        0 - Do not accept Router Advertisements.
        1 - (default) Accept Router Advertisements if forwarding is disabled.
        2 - Overrule forwarding behaviour. Accept Router Advertisements even if
            forwarding is enabled.
        """
        return self.set_interface('ipv6_accept_ra', accept_ra)

    def set_ipv6_autoconf(self, autoconf):
        """
        Autoconfigure addresses using Prefix Information in Router
        Advertisements.
        """
        return self.set_interface('ipv6_autoconf', autoconf)

    def add_ipv6_eui64_address(self, prefix):
        """
        Extended Unique Identifier (EUI), as per RFC2373, allows a host to
        assign itself a unique IPv6 address based on a given IPv6 prefix.

        Calculate the EUI64 from the interface's MAC, then assign it
        with the given prefix to the interface.
        """

        eui64 = mac2eui64(self.get_mac(), prefix)
        prefixlen = prefix.split('/')[1]
        self.add_addr(f'{eui64}/{prefixlen}')

    def del_ipv6_eui64_address(self, prefix):
        """
        Delete the address based on the interface's MAC-based EUI64
        combined with the prefix address.
        """
        eui64 = mac2eui64(self.get_mac(), prefix)
        prefixlen = prefix.split('/')[1]
        self.del_addr(f'{eui64}/{prefixlen}')


    def set_ipv6_forwarding(self, forwarding):
        """
        Configure IPv6 interface-specific Host/Router behaviour.

        False:

        By default, Host behaviour is assumed.  This means:

        1. IsRouter flag is not set in Neighbour Advertisements.
        2. If accept_ra is TRUE (default), transmit Router
           Solicitations.
        3. If accept_ra is TRUE (default), accept Router
           Advertisements (and do autoconfiguration).
        4. If accept_redirects is TRUE (default), accept Redirects.

        True:

        If local forwarding is enabled, Router behaviour is assumed.
        This means exactly the reverse from the above:

        1. IsRouter flag is set in Neighbour Advertisements.
        2. Router Solicitations are not sent unless accept_ra is 2.
        3. Router Advertisements are ignored unless accept_ra is 2.
        4. Redirects are ignored.
        """
        return self.set_interface('ipv6_forwarding', forwarding)

    def set_ipv6_dad_messages(self, dad):
        """
        The amount of Duplicate Address Detection probes to send.
        Default: 1
        """
        return self.set_interface('ipv6_dad_transmits', dad)

    def set_link_detect(self, link_filter):
        """
        Configure kernel response in packets received on interfaces that are 'down'

        0 - Allow packets to be received for the address on this interface
            even if interface is disabled or no carrier.

        1 - Ignore packets received if interface associated with the incoming
            address is down.

        2 - Ignore packets received if interface associated with the incoming
            address is down or has no carrier.

        Default value is 0. Note that some distributions enable it in startup
        scripts.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_link_detect(1)
        """
        return self.set_interface('link_detect', link_filter)

    def get_alias(self):
        """
        Get interface alias name used by e.g. SNMP

        Example:
        >>> Interface('eth0').get_alias()
        'interface description as set by user'
        """
        return self.get_interface('alias')

    def set_alias(self, ifalias=''):
        """
        Set interface alias name used by e.g. SNMP

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_alias('VyOS upstream interface')

        to clear alias e.g. delete it use:

        >>> Interface('eth0').set_ifalias('')
        """
        self.set_interface('alias', ifalias)

    def get_vlan_protocol(self):
        """
        Retrieve VLAN protocol in use, this can be 802.1Q, 802.1ad or None

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0.10').get_vlan_protocol()
        '802.1Q'
        """
        return self.get_interface('vlan_protocol')

    def get_admin_state(self):
        """
        Get interface administrative state. Function will return 'up' or 'down'

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').get_admin_state()
        'up'
        """
        return self.get_interface('admin_state')

    def set_admin_state(self, state):
        """
        Set interface administrative state to be 'up' or 'down'

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_admin_state('down')
        >>> Interface('eth0').get_admin_state()
        'down'
        """
        # A VLAN interface can only be placed in admin up state when
        # the lower interface is up, too
        if self.get_vlan_protocol():
            lower_interface = glob(f'/sys/class/net/{self.ifname}/lower*/flags')[0]
            with open(lower_interface, 'r') as f:
                flags = f.read()
            # If parent is not up - bail out as we can not bring up the VLAN.
            # Flags are defined in kernel source include/uapi/linux/if.h
            if not int(flags, 16) & 1:
                return None

        if state == 'up':
            self._admin_state_down_cnt -= 1
            if self._admin_state_down_cnt < 1:
                return self.set_interface('admin_state', state)
        else:
            self._admin_state_down_cnt += 1
            return self.set_interface('admin_state', state)

    def set_proxy_arp(self, enable):
        """
        Set per interface proxy ARP configuration

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_proxy_arp(1)
        """
        self.set_interface('proxy_arp', enable)

    def set_proxy_arp_pvlan(self, enable):
        """
        Private VLAN proxy arp.
        Basically allow proxy arp replies back to the same interface
        (from which the ARP request/solicitation was received).

        This is done to support (ethernet) switch features, like RFC
        3069, where the individual ports are NOT allowed to
        communicate with each other, but they are allowed to talk to
        the upstream router.  As described in RFC 3069, it is possible
        to allow these hosts to communicate through the upstream
        router by proxy_arp'ing. Don't need to be used together with
        proxy_arp.

        This technology is known by different names:
        In RFC 3069 it is called VLAN Aggregation.
        Cisco and Allied Telesyn call it Private VLAN.
        Hewlett-Packard call it Source-Port filtering or port-isolation.
        Ericsson call it MAC-Forced Forwarding (RFC Draft).

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').set_proxy_arp_pvlan(1)
        """
        self.set_interface('proxy_arp_pvlan', enable)

    def get_addr(self):
        """
        Retrieve assigned IPv4 and IPv6 addresses from given interface.
        This is done using the netifaces and ipaddress python modules.

        Example:
        >>> from vyos.ifconfig import Interface
        >>> Interface('eth0').get_addrs()
        ['172.16.33.30/24', 'fe80::20c:29ff:fe11:a174/64']
        """

        ipv4 = []
        ipv6 = []

        if AF_INET in ifaddresses(self.config['ifname']).keys():
            for v4_addr in ifaddresses(self.config['ifname'])[AF_INET]:
                # we need to manually assemble a list of IPv4 address/prefix
                prefix = '/' + \
                    str(IPv4Network('0.0.0.0/' + v4_addr['netmask']).prefixlen)
                ipv4.append(v4_addr['addr'] + prefix)

        if AF_INET6 in ifaddresses(self.config['ifname']).keys():
            for v6_addr in ifaddresses(self.config['ifname'])[AF_INET6]:
                # Note that currently expanded netmasks are not supported. That means
                # 2001:db00::0/24 is a valid argument while 2001:db00::0/ffff:ff00:: not.
                # see https://docs.python.org/3/library/ipaddress.html
                bits = bin(
                    int(v6_addr['netmask'].replace(':', ''), 16)).count('1')
                prefix = '/' + str(bits)

                # we alsoneed to remove the interface suffix on link local
                # addresses
                v6_addr['addr'] = v6_addr['addr'].split('%')[0]
                ipv6.append(v6_addr['addr'] + prefix)

        return ipv4 + ipv6

    def add_addr(self, addr):
        """
        Add IP(v6) address to interface. Address is only added if it is not
        already assigned to that interface. Address format must be validated
        and compressed/normalized before calling this function.

        addr: can be an IPv4 address, IPv6 address, dhcp or dhcpv6!
              IPv4: add IPv4 address to interface
              IPv6: add IPv6 address to interface
              dhcp: start dhclient (IPv4) on interface
              dhcpv6: start WIDE DHCPv6 (IPv6) on interface

        Returns False if address is already assigned and wasn't re-added.
        Example:
        >>> from vyos.ifconfig import Interface
        >>> j = Interface('eth0')
        >>> j.add_addr('192.0.2.1/24')
        >>> j.add_addr('2001:db8::ffff/64')
        >>> j.get_addr()
        ['192.0.2.1/24', '2001:db8::ffff/64']
        """
        # XXX: normalize/compress with ipaddress if calling functions don't?
        # is subnet mask always passed, and in the same way?

        # do not add same address twice
        if addr in self._addr:
            return False

        addr_is_v4 = is_ipv4(addr)

        # we can't have both DHCP and static IPv4 addresses assigned
        for a in self._addr:
            if ( ( addr == 'dhcp' and a != 'dhcpv6' and is_ipv4(a) ) or
                    ( a == 'dhcp' and addr != 'dhcpv6' and addr_is_v4 ) ):
                raise ConfigError((
                    "Can't configure both static IPv4 and DHCP address "
                    "on the same interface"))

        # add to interface
        if addr == 'dhcp':
            self.set_dhcp(True)
        elif addr == 'dhcpv6':
            self.set_dhcpv6(True)
        elif not is_intf_addr_assigned(self.ifname, addr):
            self._cmd(f'ip addr add "{addr}" '
                    f'{"brd + " if addr_is_v4 else ""}dev "{self.ifname}"')
        else:
            return False

        # add to cache
        self._addr.append(addr)

        return True

    def del_addr(self, addr):
        """
        Delete IP(v6) address from interface. Address is only deleted if it is
        assigned to that interface. Address format must be exactly the same as
        was used when adding the address.

        addr: can be an IPv4 address, IPv6 address, dhcp or dhcpv6!
              IPv4: delete IPv4 address from interface
              IPv6: delete IPv6 address from interface
              dhcp: stop dhclient (IPv4) on interface
              dhcpv6: stop dhclient (IPv6) on interface

        Returns False if address isn't already assigned and wasn't deleted.
        Example:
        >>> from vyos.ifconfig import Interface
        >>> j = Interface('eth0')
        >>> j.add_addr('2001:db8::ffff/64')
        >>> j.add_addr('192.0.2.1/24')
        >>> j.get_addr()
        ['192.0.2.1/24', '2001:db8::ffff/64']
        >>> j.del_addr('192.0.2.1/24')
        >>> j.get_addr()
        ['2001:db8::ffff/64']
        """

        # remove from interface
        if addr == 'dhcp':
            self.set_dhcp(False)
        elif addr == 'dhcpv6':
            self.set_dhcpv6(False)
        elif is_intf_addr_assigned(self.ifname, addr):
            self._cmd(f'ip addr del "{addr}" dev "{self.ifname}"')
        else:
            return False

        # remove from cache
        if addr in self._addr:
            self._addr.remove(addr)

        return True

    def flush_addrs(self):
        """
        Flush all addresses from an interface, including DHCP.

        Will raise an exception on error.
        """
        # stop DHCP(v6) if running
        self.set_dhcp(False)
        self.set_dhcpv6(False)

        # flush all addresses
        self._cmd(f'ip addr flush dev "{self.ifname}"')

    def add_to_bridge(self, br):
        """
        Adds the interface to the bridge with the passed port config.

        Returns False if bridge doesn't exist.
        """

        # check if the bridge exists (on boot it doesn't)
        if br not in Section.interfaces('bridge'):
            return False

        self.flush_addrs()
        # add interface to bridge - use Section.klass to get BridgeIf class
        Section.klass(br)(br, create=False).add_port(self.ifname)

        # TODO: port config (STP)

        return True

    def set_dhcp(self, enable):
        """
        Enable/Disable DHCP client on a given interface.
        """
        if enable not in [True, False]:
            raise ValueError()

        ifname = self.ifname
        config_base = r'/var/lib/dhcp/dhclient'
        config_file = f'{config_base}_{ifname}.conf'
        options_file = f'{config_base}_{ifname}.options'
        pid_file = f'{config_base}_{ifname}.pid'
        lease_file = f'{config_base}_{ifname}.leases'

        if enable and 'disable' not in self._config:
            if jmespath.search('dhcp_options.host_name', self._config) == None:
                # read configured system hostname.
                # maybe change to vyos hostd client ???
                hostname = 'vyos'
                with open('/etc/hostname', 'r') as f:
                    hostname = f.read().rstrip('\n')
                    tmp = {'dhcp_options' : { 'host_name' : hostname}}
                    self._config = dict_merge(tmp, self._config)

            render(options_file, 'dhcp-client/daemon-options.tmpl',
                   self._config, trim_blocks=True)
            render(config_file, 'dhcp-client/ipv4.tmpl',
                   self._config, trim_blocks=True)

            # 'up' check is mandatory b/c even if the interface is A/D, as soon as
            # the DHCP client is started the interface will be placed in u/u state.
            # This is not what we intended to do when disabling an interface.
            return self._cmd(f'systemctl restart dhclient@{ifname}.service')
        else:
            self._cmd(f'systemctl stop dhclient@{ifname}.service')

            # cleanup old config files
            for file in [config_file, options_file, pid_file, lease_file]:
                if os.path.isfile(file):
                    os.remove(file)


    def set_dhcpv6(self, enable):
        """
        Enable/Disable DHCPv6 client on a given interface.
        """
        if enable not in [True, False]:
            raise ValueError()

        ifname = self.ifname
        config_file = f'/run/dhcp6c/dhcp6c.{ifname}.conf'

        if enable and 'disable' not in self._config:
            render(config_file, 'dhcp-client/ipv6.tmpl',
                   self._config, trim_blocks=True)

            # We must ignore any return codes. This is required to enable DHCPv6-PD
            # for interfaces which are yet not up and running.
            return self._popen(f'systemctl restart dhcp6c@{ifname}.service')
        else:
            self._popen(f'systemctl stop dhcp6c@{ifname}.service')

            if os.path.isfile(config_file):
                os.remove(config_file)


    def update(self, config):
        """ General helper function which works on a dictionary retrived by
        get_config_dict(). It's main intention is to consolidate the scattered
        interface setup code and provide a single point of entry when workin
        on any interface. """

        # Cache the configuration - it will be reused inside e.g. DHCP handler
        # XXX: maybe pass the option via __init__ in the future and rename this
        # method to apply()?
        self._config = config

        # Update interface description
        self.set_alias(config.get('description', ''))

        # Ignore link state changes
        value = '2' if 'disable_link_detect' in config else '1'
        self.set_link_detect(value)

        # Configure assigned interface IP addresses. No longer
        # configured addresses will be removed first
        new_addr = config.get('address', [])

        # XXX: T2636 workaround: convert string to a list with one element
        if isinstance(new_addr, str):
            new_addr = [new_addr]

        # always ensure DHCP client is stopped (when not configured explicitly)
        if 'dhcp' not in new_addr:
            self.del_addr('dhcp')

        # always ensure DHCPv6 client is stopped (when not configured as client
        # for IPv6 address or prefix delegation
        dhcpv6pd = jmespath.search('dhcpv6_options.pd', config)
        if 'dhcpv6' not in new_addr or dhcpv6pd == None:
            self.del_addr('dhcpv6')

        # determine IP addresses which are assigned to the interface and build a
        # list of addresses which are no longer in the dict so they can be removed
        cur_addr = self.get_addr()
        for addr in list_diff(cur_addr, new_addr):
            self.del_addr(addr)

        for addr in new_addr:
            self.add_addr(addr)

        # start DHCPv6 client when only PD was configured
        if dhcpv6pd != None:
            self.set_dhcpv6(True)

        # There are some items in the configuration which can only be applied
        # if this instance is not bound to a bridge. This should be checked
        # by the caller but better save then sorry!
        if not any(k in ['is_bond_member', 'is_bridge_member'] for k in config):
            # Bind interface to given VRF or unbind it if vrf node is not set.
            # unbinding will call 'ip link set dev eth0 nomaster' which will
            # also drop the interface out of a bridge or bond - thus this is
            # checked before
            self.set_vrf(config.get('vrf', ''))

        # Configure ARP cache timeout in milliseconds - has default value
        tmp = jmespath.search('ip.arp_cache_timeout', config)
        value = tmp if (tmp != None) else '30'
        self.set_arp_cache_tmo(value)

        # Configure ARP filter configuration
        tmp = jmespath.search('ip.disable_arp_filter', config)
        value = '0' if (tmp != None) else '1'
        self.set_arp_filter(value)

        # Configure ARP accept
        tmp = jmespath.search('ip.enable_arp_accept', config)
        value = '1' if (tmp != None) else '0'
        self.set_arp_accept(value)

        # Configure ARP announce
        tmp = jmespath.search('ip.enable_arp_announce', config)
        value = '1' if (tmp != None) else '0'
        self.set_arp_announce(value)

        # Configure ARP ignore
        tmp = jmespath.search('ip.enable_arp_ignore', config)
        value = '1' if (tmp != None) else '0'
        self.set_arp_ignore(value)

        # Enable proxy-arp on this interface
        tmp = jmespath.search('ip.enable_proxy_arp', config)
        value = '1' if (tmp != None) else '0'
        self.set_proxy_arp(value)

        # Enable private VLAN proxy ARP on this interface
        tmp = jmespath.search('ip.proxy_arp_pvlan', config)
        value = '1' if (tmp != None) else '0'
        self.set_proxy_arp_pvlan(value)

        # IPv6 forwarding
        tmp = jmespath.search('ipv6.disable_forwarding', config)
        value = '0' if (tmp != None) else '1'
        self.set_ipv6_forwarding(value)

        # IPv6 router advertisements
        tmp = jmespath.search('ipv6.address.autoconf', config)
        value = '2' if (tmp != None) else '1'
        if 'dhcpv6' in new_addr:
            value = '2'
        self.set_ipv6_accept_ra(value)

        # IPv6 address autoconfiguration
        tmp = jmespath.search('ipv6.address.autoconf', config)
        value = '1' if (tmp != None) else '0'
        self.set_ipv6_autoconf(value)

        # IPv6 Duplicate Address Detection (DAD) tries
        tmp = jmespath.search('ipv6.dup_addr_detect_transmits', config)
        value = tmp if (tmp != None) else '1'
        self.set_ipv6_dad_messages(value)

        # MTU - Maximum Transfer Unit
        if 'mtu' in config:
            self.set_mtu(config.get('mtu'))

        # Delete old IPv6 EUI64 addresses before changing MAC
        tmp = jmespath.search('ipv6.address.eui64_old', config)
        if tmp:
            for addr in tmp:
                self.del_ipv6_eui64_address(addr)

        # Change interface MAC address - re-set to real hardware address (hw-id)
        # if custom mac is removed. Skip if bond member.
        if 'is_bond_member' not in config:
            mac = config.get('hw_id')
            if 'mac' in config:
                mac = config.get('mac')
            if mac:
                self.set_mac(mac)

        # Manage IPv6 link-local addresses
        tmp = jmespath.search('ipv6.address.no_default_link_local', config)
        # we must check explicitly for None type as if the key is set we will
        # get an empty dict (<class 'dict'>)
        if tmp is not None:
            self.del_ipv6_eui64_address('fe80::/64')
        else:
            self.add_ipv6_eui64_address('fe80::/64')

        # Add IPv6 EUI-based addresses
        tmp = jmespath.search('ipv6.address.eui64', config)
        if tmp:
            # XXX: T2636 workaround: convert string to a list with one element
            if isinstance(tmp, str):
                tmp = [tmp]
            for addr in tmp:
                self.add_ipv6_eui64_address(addr)

        # re-add ourselves to any bridge we might have fallen out of
        if 'is_bridge_member' in config:
            bridge = config.get('is_bridge_member')
            self.add_to_bridge(bridge)

        # remove no longer required 802.1ad (Q-in-Q VLANs)
        for vif_s_id in config.get('vif_s_remove', {}):
            self.del_vlan(vif_s_id)

        # create/update 802.1ad (Q-in-Q VLANs)
        ifname = config['ifname']
        for vif_s_id, vif_s in config.get('vif_s', {}).items():
            tmp=get_ethertype(vif_s.get('ethertype', '0x88A8'))
            s_vlan = self.add_vlan(vif_s_id, ethertype=tmp)
            vif_s['ifname'] = f'{ifname}.{vif_s_id}'
            s_vlan.update(vif_s)

            # remove no longer required client VLAN (vif-c)
            for vif_c_id in vif_s.get('vif_c_remove', {}):
                s_vlan.del_vlan(vif_c_id)

            # create/update client VLAN (vif-c) interface
            for vif_c_id, vif_c in vif_s.get('vif_c', {}).items():
                c_vlan = s_vlan.add_vlan(vif_c_id)
                vif_c['ifname'] = f'{ifname}.{vif_s_id}.{vif_c_id}'
                c_vlan.update(vif_c)

        # remove no longer required 802.1q VLAN interfaces
        for vif_id in config.get('vif_remove', {}):
            self.del_vlan(vif_id)

        # create/update 802.1q VLAN interfaces
        for vif_id, vif in config.get('vif', {}).items():
            vlan = self.add_vlan(vif_id)
            vif['ifname'] = f'{ifname}.{vif_id}'
            vlan.update(vif)
