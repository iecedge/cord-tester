
# Copyright 2017-present Open Networking Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


#
# Copyright 2016-present Ciena Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import unittest
from nose.tools import *
from nose.twistedtools import reactor, deferred
from twisted.internet import defer
from scapy.all import IP
import time, monotonic
import os, sys
import tempfile
import random
import threading
import json
import requests
from Stats import Stats
from OnosCtrl import OnosCtrl
from DHCP import DHCPTest
from EapTLS import TLSAuthTest
from Channels import Channels, IgmpChannel
from subscriberDb import SubscriberDB
from threadPool import ThreadPool
from portmaps import g_subscriber_port_map
from OltConfig import *
from CordTestServer import cord_test_onos_restart, cord_test_shell, cord_test_radius_restart
from CordTestUtils import log_test, get_controller
from CordLogger import CordLogger
from CordTestConfig import setup_module, teardown_module
from CordContainer import Onos
from VolthaCtrl import VolthaCtrl
from CordTestUtils import get_mac, get_controller

log_test.setLevel('INFO')

class Subscriber(Channels):
      PORT_TX_DEFAULT = 2
      PORT_RX_DEFAULT = 1
      INTF_TX_DEFAULT = 'veth2'
      INTF_RX_DEFAULT = 'veth0'
      STATS_RX = 0
      STATS_TX = 1
      STATS_JOIN = 2
      STATS_LEAVE = 3
      SUBSCRIBER_SERVICES = 'DHCP IGMP TLS'

      def __init__(self, name = 'sub', service = SUBSCRIBER_SERVICES, port_map = None,
                   num = 1, channel_start = 0,
                   tx_port = PORT_TX_DEFAULT, rx_port = PORT_RX_DEFAULT,
                   iface = INTF_RX_DEFAULT, iface_mcast = INTF_TX_DEFAULT,
                   mcast_cb = None, loginType = 'wireless'):
            self.tx_port = tx_port
            self.rx_port = rx_port
            self.port_map = port_map or g_subscriber_port_map
            try:
                  self.tx_intf = self.port_map[tx_port]
                  self.rx_intf = self.port_map[rx_port]
            except:
                  self.tx_intf = self.port_map[self.PORT_TX_DEFAULT]
                  self.rx_intf = self.port_map[self.PORT_RX_DEFAULT]

            log_test.info('Subscriber %s, rx interface %s, uplink interface %s' %(name, self.rx_intf, self.tx_intf))
            Channels.__init__(self, num, channel_start = channel_start,
                              iface = self.rx_intf, iface_mcast = self.tx_intf, mcast_cb = mcast_cb)
            self.name = name
            self.service = service
            self.service_map = {}
            services = self.service.strip().split(' ')
            for s in services:
                  self.service_map[s] = True
            self.loginType = loginType
            ##start streaming channels
            self.join_map = {}
            ##accumulated join recv stats
            self.join_rx_stats = Stats()
            self.recv_timeout = False

      def has_service(self, service):
            if self.service_map.has_key(service):
                  return self.service_map[service]
            if self.service_map.has_key(service.upper()):
                  return self.service_map[service.upper()]
            return False

      def channel_join_update(self, chan, join_time):
            self.join_map[chan] = ( Stats(), Stats(), Stats(), Stats() )
            self.channel_update(chan, self.STATS_JOIN, 1, t = join_time)

      def channel_join(self, chan = 0, delay = 2):
            '''Join a channel and create a send/recv stats map'''
            if self.join_map.has_key(chan):
                  del self.join_map[chan]
            self.delay = delay
            chan, join_time = self.join(chan)
            self.channel_join_update(chan, join_time)
            return chan

      def channel_join_next(self, delay = 2, leave_flag = True):
            '''Joins the next channel leaving the last channel'''
            if self.last_chan:
                  if self.join_map.has_key(self.last_chan):
                        del self.join_map[self.last_chan]
            self.delay = delay
            chan, join_time = self.join_next(leave_flag = leave_flag)
            self.channel_join_update(chan, join_time)
            return chan

      def channel_jump(self, delay = 2):
            '''Jumps randomly to the next channel leaving the last channel'''
            if self.last_chan is not None:
                  if self.join_map.has_key(self.last_chan):
                        del self.join_map[self.last_chan]
            self.delay = delay
            chan, join_time = self.jump()
            self.channel_join_update(chan, join_time)
            return chan

      def channel_leave(self, chan = 0, force = False):
            if self.join_map.has_key(chan):
                  del self.join_map[chan]
            self.leave(chan, force = force)

      def channel_update(self, chan, stats_type, packets, t=0):
            if type(chan) == type(0):
                  chan_list = (chan,)
            else:
                  chan_list = chan
            for c in chan_list:
                  if self.join_map.has_key(c):
                        self.join_map[c][stats_type].update(packets = packets, t = t)

      def channel_receive(self, chan, cb = None, count = 1, timeout = 5):
            log_test.info('Subscriber %s on port %s receiving from group %s, channel %d' %
                     (self.name, self.rx_intf, self.gaddr(chan), chan))
            r = self.recv(chan, cb = cb, count = count, timeout = timeout)
            if len(r) == 0:
                  log_test.info('Subscriber %s on port %s timed out' %(self.name, self.rx_intf))
            else:
                  log_test.info('Subscriber %s on port %s received %d packets' %(self.name, self.rx_intf, len(r)))
            if self.recv_timeout:
                  ##Negative test case is disabled for now
                  assert_equal(len(r), 0)

      def recv_channel_cb(self, pkt):
            ##First verify that we have received the packet for the joined instance
            log_test.info('Packet received for group %s, subscriber %s, port %s' %
                     (pkt[IP].dst, self.name, self.rx_intf))
            if self.recv_timeout:
                  return
            chan = self.caddr(pkt[IP].dst)
            assert_equal(chan in self.join_map.keys(), True)
            recv_time = monotonic.monotonic() * 1000000
            join_time = self.join_map[chan][self.STATS_JOIN].start
            delta = recv_time - join_time
            self.join_rx_stats.update(packets=1, t = delta, usecs = True)
            self.channel_update(chan, self.STATS_RX, 1, t = delta)
            log_test.debug('Packet received in %.3f usecs for group %s after join' %(delta, pkt[IP].dst))

class subscriber_pool:

      def __init__(self, subscriber, test_cbs):
            self.subscriber = subscriber
            self.test_cbs = test_cbs

      def pool_cb(self):
            for cb in self.test_cbs:
                  if cb:
                        self.test_status = cb(self.subscriber)
                        if self.test_status is not True:
                           ## This is chaning for other sub status has to check again
                           self.test_status = True
                           log_test.info('This service is failed and other services will not run for this subscriber')
                           break
            log_test.info('This Subscriber is tested for multiple service eligibility ')
            self.test_status = True


class subscriber_exchange(CordLogger):

      apps = ('org.opencord.aaa', 'org.onosproject.dhcp')
      olt_apps = () #'org.opencord.cordmcast')
      vtn_app = 'org.opencord.vtn'
      table_app = 'org.ciena.cordigmp'
      dhcp_server_config = {
        "ip": "10.1.11.50",
        "mac": "ca:fe:ca:fe:ca:fe",
        "subnet": "255.255.252.0",
        "broadcast": "10.1.11.255",
        "router": "10.1.8.1",
        "domain": "8.8.8.8",
        "ttl": "63",
        "delay": "2",
        "startip": "10.1.11.51",
        "endip": "10.1.11.100"
      }

      aaa_loaded = False
      test_path = os.path.dirname(os.path.realpath(__file__))
      table_app_file = os.path.join(test_path, '..', 'apps/ciena-cordigmp-multitable-2.0-SNAPSHOT.oar')
      app_file = os.path.join(test_path, '..', 'apps/ciena-cordigmp-2.0-SNAPSHOT.oar')
      olt_app_file = os.path.join(test_path, '..', 'apps/olt-app-1.2-SNAPSHOT.oar')
      app_files = [os.path.join(test_path, '..', 'apps/cord-config-3.0-SNAPSHOT.oar'), os.path.join(test_path, '..', 'apps/olt-app-3.0-SNAPSHOT.oar'), os.path.join(test_path, '..', 'apps/mcast-1.3.0-SNAPSHOT.oar'), os.path.join(test_path, '..', 'apps/onos-app-igmpproxy-1.1.0-SNAPSHOT.oar')]
      proxy_config_file = os.path.join(test_path, '..', 'igmpproxy/igmpproxyconfig.json')
      olt_app_name = 'org.onosproject.olt'
      onos_config_path = os.path.join(test_path, '..', 'setup/onos-config')
      olt_conf_file = os.getenv('OLT_CONFIG_FILE', os.path.join(test_path, '..', 'setup/olt_config.json'))
      cpqd_path = os.path.join(test_path, '..', 'setup')
      ovs_path = cpqd_path
      test_services = ('IGMP', 'TRAFFIC')
      num_joins = 0
      num_subscribers = 0
      leave_flag = True
      num_channels = 0
      recv_timeout = False
      onos_restartable = bool(int(os.getenv('ONOS_RESTART', 0)))
      INTF_TX_DEFAULT = 'veth2'
      INTF_RX_DEFAULT = 'veth0'
      SUBSCRIBER_TIMEOUT = 300
      MAX_PORTS = 100
      proxy_device_id = OnosCtrl.get_device_id()
      controller = get_controller()
      proxy_app = 'org.opencord.igmpproxy'
      mcast_app = 'org.opencord.mcast'
      cord_config_app = 'org.opencord.config'
      host_ip_map = {}
      configs = {}
      proxy_interfaces_last = ()
      interface_to_mac_map = {}


      CLIENT_CERT = """-----BEGIN CERTIFICATE-----
MIICuDCCAiGgAwIBAgIBAjANBgkqhkiG9w0BAQUFADCBizELMAkGA1UEBhMCVVMx
CzAJBgNVBAgTAkNBMRIwEAYDVQQHEwlTb21ld2hlcmUxEzARBgNVBAoTCkNpZW5h
IEluYy4xHjAcBgkqhkiG9w0BCQEWD2FkbWluQGNpZW5hLmNvbTEmMCQGA1UEAxMd
RXhhbXBsZSBDZXJ0aWZpY2F0ZSBBdXRob3JpdHkwHhcNMTYwNjA2MjExMjI3WhcN
MTcwNjAxMjExMjI3WjBnMQswCQYDVQQGEwJVUzELMAkGA1UECBMCQ0ExEzARBgNV
BAoTCkNpZW5hIEluYy4xFzAVBgNVBAMUDnVzZXJAY2llbmEuY29tMR0wGwYJKoZI
hvcNAQkBFg51c2VyQGNpZW5hLmNvbTCBnzANBgkqhkiG9w0BAQEFAAOBjQAwgYkC
gYEAwvXiSzb9LZ6c7uNziUfKvoHO7wu/uiFC5YUpXbmVGuGZizbVrny0xnR85Dfe
+9R4diansfDhIhzOUl1XjN3YDeSS9OeF5YWNNE8XDhlz2d3rVzaN6hIhdotBkUjg
rUewjTg5OFR31QEyG3v8xR3CLgiE9xQELjZbSA07pD79zuUCAwEAAaNPME0wEwYD
VR0lBAwwCgYIKwYBBQUHAwIwNgYDVR0fBC8wLTAroCmgJ4YlaHR0cDovL3d3dy5l
eGFtcGxlLmNvbS9leGFtcGxlX2NhLmNybDANBgkqhkiG9w0BAQUFAAOBgQDAjkrY
6tDChmKbvr8w6Du/t8vHjTCoCIocHTN0qzWOeb1YsAGX89+TrWIuO1dFyYd+Z0KC
PDKB5j/ygml9Na+AklSYAVJIjvlzXKZrOaPmhZqDufi+rXWti/utVqY4VMW2+HKC
nXp37qWeuFLGyR1519Y1d6F/5XzqmvbwURuEug==
-----END CERTIFICATE-----"""

      CLIENT_CERT_INVALID = '''-----BEGIN CERTIFICATE-----
MIIDvTCCAqWgAwIBAgIBAjANBgkqhkiG9w0BAQUFADCBizELMAkGA1UEBhMCVVMx
CzAJBgNVBAgTAkNBMRIwEAYDVQQHEwlTb21ld2hlcmUxEzARBgNVBAoTCkNpZW5h
IEluYy4xHjAcBgkqhkiG9w0BCQEWD2FkbWluQGNpZW5hLmNvbTEmMCQGA1UEAxMd
RXhhbXBsZSBDZXJ0aWZpY2F0ZSBBdXRob3JpdHkwHhcNMTYwMzExMTg1MzM2WhcN
MTcwMzA2MTg1MzM2WjBnMQswCQYDVQQGEwJVUzELMAkGA1UECBMCQ0ExEzARBgNV
BAoTCkNpZW5hIEluYy4xFzAVBgNVBAMUDnVzZXJAY2llbmEuY29tMR0wGwYJKoZI
hvcNAQkBFg51c2VyQGNpZW5hLmNvbTCCASIwDQYJKoZIhvcNAQEBBQADggEPADCC
AQoCggEBAOxemcBsPn9tZsCa5o2JA6sQDC7A6JgCNXXl2VFzKLNNvB9PS6D7ZBsQ
5An0zEDMNzi51q7lnrYg1XyiE4S8FzMGAFr94RlGMQJUbRD9V/oqszMX4k++iAOK
tIA1gr3x7Zi+0tkjVSVzXTmgNnhChAamdMsjYUG5+CY9WAicXyy+VEV3zTphZZDR
OjcjEp4m/TSXVPYPgYDXI40YZKX5BdvqykWtT/tIgZb48RS1NPyN/XkCYzl3bv21
qx7Mc0fcEbsJBIIRYTUkfxnsilcnmLxSYO+p+DZ9uBLBzcQt+4Rd5pLSfi21WM39
2Z2oOi3vs/OYAPAqgmi2JWOv3mePa/8CAwEAAaNPME0wEwYDVR0lBAwwCgYIKwYB
BQUHAwIwNgYDVR0fBC8wLTAroCmgJ4YlaHR0cDovL3d3dy5leGFtcGxlLmNvbS9l
eGFtcGxlX2NhLmNybDANBgkqhkiG9w0BAQUFAAOCAQEALBzMPDTIB6sLyPl0T6JV
MjOkyldAVhXWiQsTjaGQGJUUe1cmUJyZbUZEc13MygXMPOM4x7z6VpXGuq1c/Vxn
VzQ2fNnbJcIAHi/7G8W5/SQfPesIVDsHTEc4ZspPi5jlS/MVX3HOC+BDbOjdbwqP
RX0JEr+uOyhjO+lRxG8ilMRACoBUbw1eDuVDoEBgErSUC44pq5ioDw2xelc+Y6hQ
dmtYwfY0DbvwxHtA495frLyPcastDiT/zre7NL51MyUDPjjYjghNQEwvu66IKbQ3
T1tJBrgI7/WI+dqhKBFolKGKTDWIHsZXQvZ1snGu/FRYzg1l+R/jT8cRB9BDwhUt
yg==
-----END CERTIFICATE-----'''

      VOLTHA_HOST = None
      VOLTHA_TEARDOWN = True
      VOLTHA_REST_PORT = VolthaCtrl.REST_PORT
      VOLTHA_UPLINK_VLAN_MAP = { 'of:0001000000000001' : '222' }
      VOLTHA_UPLINK_VLAN_START = 333
      VOLTHA_IGMP_ITERATIONS = 100
      VOLTHA_CONFIG_FAKE = True
      VOLTHA_OLT_TYPE = 'simulated_olt'
      VOLTHA_OLT_MAC = '00:0c:e2:31:12:00'
      VOLTHA_ENABLED = bool(int(os.getenv('VOLTHA_ENABLED', 0)))
      voltha_ctrl = None
      voltha_device = None
      voltha_switch_map = None
      voltha_preconfigured = False

      @classmethod
      def update_apps_version(cls):
            version = Onos.getVersion()
            major = int(version.split('.')[0])
            minor = int(version.split('.')[1])
            cordigmp_app_version = '2.0-SNAPSHOT'
            olt_app_version = '1.2-SNAPSHOT'
	    cord_config_app_version = '1.2-SNAPSHOT'
            if major > 1:
                  cordigmp_app_version = '3.0-SNAPSHOT'
                  olt_app_version = '2.0-SNAPSHOT'
		  cord_config_app_version = '2.0-SNAPSHOT'
            elif major == 1:
                  if minor > 10:
                        cordigmp_app_version = '3.0-SNAPSHOT'
                        olt_app_version = '2.0-SNAPSHOT'
                  elif minor <= 8:
                        olt_app_version = '1.1-SNAPSHOT'
	    cls.cord_config_app_file = os.path.join(cls.test_path, '..', 'apps/cord-config-{}.oar'.format(cord_config_app_version))
            cls.app_file = os.path.join(cls.test_path, '..', 'apps/ciena-cordigmp-{}.oar'.format(cordigmp_app_version))
            cls.table_app_file = os.path.join(cls.test_path, '..', 'apps/ciena-cordigmp-multitable-{}.oar'.format(cordigmp_app_version))
            cls.olt_app_file = os.path.join(cls.test_path, '..', 'apps/olt-app-{}.oar'.format(olt_app_version))
	    cls.updated_app_files = [cls.cord_config_app_file,cls.app_file,cls.table_app_file,cls.olt_app_file]

      @classmethod
      def load_device_id(cls):
            '''Configure the device id'''
            did = OnosCtrl.get_device_id()
            #Set the default config
            cls.device_id = did
            cls.device_dict = { "devices" : {
                        "{}".format(did) : {
                              "basic" : {
                                    "driver" : "voltha"
                                    }
                              }
                        },
                  }
            return did

      @classmethod
      def setUpClass(cls):
          '''Load the OLT config and activate relevant apps'''
          cls.update_apps_version()
          cls.olt = OltConfig(olt_conf_file = cls.olt_conf_file)
          if cls.VOLTHA_ENABLED is False:
                OnosCtrl.config_device_driver()
                OnosCtrl.cord_olt_config(cls.olt)
          cls.port_map, cls.port_list = cls.olt.olt_port_map()
          cls.switches = cls.port_map['switches']
          cls.num_ports = cls.port_map['num_ports']
          if cls.num_ports > 1:
                cls.num_ports -= 1 ##account for the tx port
          cls.activate_apps(cls.apps + cls.olt_apps, deactivate = True)

      @classmethod
      def tearDownClass(cls):
          '''Deactivate the olt apps and restart OVS back'''
          apps = cls.olt_apps
          for app in apps:
              onos_ctrl = OnosCtrl(app)
              onos_ctrl.deactivate()
          if cls.VOLTHA_ENABLED is False:
                OnosCtrl.config_device_driver(driver = 'ovs')
	  #cls.igmp_proxy_teardown()
	  #Onos.install_cord_apps()
	  #OnosCtrl.install_app(cls.table_app_file,onos_ip=cls.controller)

      @classmethod
      def activate_apps(cls, apps, deactivate = False):
            for app in apps:
                  onos_ctrl = OnosCtrl(app)
                  if deactivate is True:
                        onos_ctrl.deactivate()
                        time.sleep(2)
                  status, _ = onos_ctrl.activate()
                  assert_equal(status, True)
                  time.sleep(2)

      @classmethod
	  log_test.info('In igmp proxy setup function ***************')
      def igmpproxy_setup(cls,FastLeave='false'):
          cls.uninstall_cord_config_app()
          time.sleep(1)
          cls.install_igmpproxy()
          cls.igmp_proxy_setup()
          cls.onos_igmp_proxy_config_load(FastLeave=FastLeave)

      @classmethod
      def igmp_proxy_teardown(cls):
          ##reset the ONOS port configuration back to default
          for config in cls.configs.items():
              OnosCtrl.delete(config)
          cls.uninstall_cord_config_app()
	  Onos.install_cord_apps()
	  OnosCtrl.install_app(cls.table_app_file,onos_ip=cls.controller)
          #for app_file in cls.updated_app_files:
          #OnosCtrl.install_app(cls.table_app_file)
          #OnosCtrl.install_app(table_app_file)
          #OnosCtrl.install_app(olt_app_file)

      @classmethod
      def uninstall_cord_config_app(cls):
          log_test.info('Uninstalling org.opencord.config 1.2 version app')
          OnosCtrl(cls.cord_config_app).deactivate()
          OnosCtrl.uninstall_app(cls.cord_config_app, onos_ip = cls.controller)

      @classmethod
      def install_igmpproxy(cls):
	  log_test.info('In install igmp proxy function ***************')
          for app in cls.app_files:
              OnosCtrl.install_app(app, onos_ip = cls.controller)
              OnosCtrl(app).activate()

      @classmethod
      def igmp_proxy_setup(cls):
          did =  OnosCtrl.get_device_id()
          cls.proxy_device_id = did
          cls.olt = OltConfig(olt_conf_file = cls.olt_conf_file)
          cls.port_map, _ = cls.olt.olt_port_map()
          #log_test.info('port map is %s'%cls.port_map)
          if cls.port_map:
              ##Per subscriber, we use 1 relay port
              try:
                  proxy_port = cls.port_map[cls.port_map['relay_ports'][0]]
              except:
                  proxy_port = cls.port_map['uplink']
              cls.proxy_interface_port = proxy_port
              cls.proxy_interfaces = (cls.port_map[cls.proxy_interface_port],)
          else:
              cls.proxy_interface_port = 100
              cls.proxy_interfaces = (g_subscriber_port_map[cls.proxy_interface_port],)
          cls.proxy_interfaces_last = cls.proxy_interfaces
          if cls.port_map:
              ##generate a ip/mac client virtual interface config for onos
              interface_list = []
              for port in cls.port_map['ports']:
                  port_num = cls.port_map[port]
                  if port_num == cls.port_map['uplink']:
                      continue
                  ip = cls.get_host_ip(port_num)
                  mac = cls.get_mac(port)
                  interface_list.append((port_num, ip, mac))
              #configure igmp proxy  virtual interface
              proxy_ip = cls.get_host_ip(interface_list[0][0])
              proxy_mac = cls.get_mac(cls.port_map[cls.proxy_interface_port])
              interface_list.append((cls.proxy_interface_port, proxy_ip, proxy_mac))
              cls.onos_interface_load(interface_list)

      @classmethod
      def onos_interface_load(cls, interface_list):
          interface_dict = { 'ports': {} }
          for port_num, ip, mac in interface_list:
              port_map = interface_dict['ports']
              port = '{}/{}'.format(cls.proxy_device_id, port_num)
              port_map[port] = { 'interfaces': [] }
              interface_list = port_map[port]['interfaces']
              interface_map = { 'ips' : [ '{}/{}'.format(ip, 24) ],
                              'mac' : mac,
                              'name': 'vir-{}'.format(port_num)
                            }
              interface_list.append(interface_map)
          #cls.onos_load_config(interface_dict)
          cls.configs['interface_config'] = interface_dict


      @classmethod
      def onos_igmp_proxy_config_load(cls, FastLeave = "false"):
          #cls.proxy_interface_port = 12
          proxy_connect_point = '{}/{}'.format(cls.proxy_device_id, cls.proxy_interface_port)
          igmpproxy_dict = { "apps": {
                "org.onosproject.provider.lldp": {
                        "suppression": {
                                "deviceTypes": ["ROADM"],
                                "annotation": "{\"no-lldp\":null}"
                        }
                },
                "org.opencord.igmpproxy": {
                        "igmpproxy": {
                                "globalConnectPointMode": "true",
                                "globalConnectPoint": proxy_connect_point,
                                "UnsolicitedTimeOut": "2",
                                "MaxResp": "10",
                                "KeepAliveInterval": "120",
                                "KeepAliveCount": "3",
                                "LastQueryInterval": "2",
                                "LastQueryCount": "2",
                                "FastLeave": FastLeave,
                                "PeriodicQuery": "true",
                                "IgmpCos": "7",
                                "withRAUpLink": "true",
                                "withRADownLink": "true"
                        }
                },
                "org.opencord.mcast": {
                        "multicast": {
                                "ingressVlan": "222",
                                "egressVlan": "17"
                        }
                }
             }
          }
          device_dict = {'devices':{
                           cls.proxy_device_id: {
                               'basic': {
                                   'driver': 'default'
                                },
                                'accessDevice': {
                                   'uplink': '2',
                                   'vlan': '222',
                                   'defaultVlan': '1'
                                   }
                                }
                            }
                      }
          log_test.info('Igmp proxy dict is %s'%igmpproxy_dict)
          cls.onos_load_config("org.opencord.igmpproxy",igmpproxy_dict)
          cls.onos_load_config("org.opencord.igmpproxy",device_dict)
          cls.configs['relay_config'] = igmpproxy_dict
          cls.configs['device_config'] = device_dict

      def random_mcast_ip(self,start_ip = '224.1.1.1', end_ip = '224.1.254.254'):
          start = list(map(int, start_ip.split(".")))
          end = list(map(int, end_ip.split(".")))
          temp = start
          ip_range = []
          ip_range.append(start_ip)
          while temp != end:
              start[3] += 1
              for i in (3, 2, 1):
                  if temp[i] == 255:
                      temp[i] = 0
                      temp[i-1] += 1
              ip_range.append(".".join(map(str, temp)))
          return random.choice(ip_range)

      def randomsourceip(self,start_ip = '10.10.0.1', end_ip = '10.10.0.100'):
          start = list(map(int, start_ip.split(".")))
          end = list(map(int, end_ip.split(".")))
          temp = start
          ip_range = []
          ip_range.append(start_ip)
          while temp != end:
              start[3] += 1
              for i in (3, 2, 1):
                  if temp[i] == 255:
                      temp[i] = 0
                      temp[i-1] += 1
              ip_range.append(".".join(map(str, temp)))
          return random.choice(ip_range)


      @classmethod
      def get_host_ip(cls, port):
          if cls.host_ip_map.has_key(port):
              return cls.host_ip_map[port]
          cls.host_ip_map[port] = '192.168.1.{}'.format(port)
          return cls.host_ip_map[port]

      @classmethod
      def get_mac(cls, iface):
          if cls.interface_to_mac_map.has_key(iface):
              return cls.interface_to_mac_map[iface]
          mac = get_mac(iface, pad = 0)
          cls.interface_to_mac_map[iface] = mac
          return mac

      @classmethod
      def start_onos(cls, network_cfg = None):
            if cls.onos_restartable is False:
                  log_test.info('ONOS restart is disabled. Skipping ONOS restart')
                  return
            if cls.VOLTHA_ENABLED is True:
                  log_test.info('ONOS restart skipped as VOLTHA is running')
                  return
            if network_cfg is None:
                  network_cfg = cls.device_dict

            if type(network_cfg) is tuple:
                  res = []
                  for v in network_cfg:
                        res += v.items()
                  config = dict(res)
            else:
                  config = network_cfg
            log_test.info('Restarting ONOS with new network configuration')
            return cord_test_onos_restart(config = config)

      @classmethod
      def remove_onos_config(cls):
            try:
                  os.unlink('{}/network-cfg.json'.format(cls.onos_config_path))
            except: pass

      @classmethod
      def start_cpqd(cls, mac = '00:11:22:33:44:55'):
            dpid = mac.replace(':', '')
            cpqd_file = os.sep.join( (cls.cpqd_path, 'cpqd.sh') )
            cpqd_cmd = '{} {}'.format(cpqd_file, dpid)
            ret = os.system(cpqd_cmd)
            assert_equal(ret, 0)
            time.sleep(10)
            device_id = 'of:{}{}'.format('0'*4, dpid)
            return device_id

      @classmethod
      def start_ovs(cls):
            ovs_file = os.sep.join( (cls.ovs_path, 'of-bridge.sh') )
            ret = os.system(ovs_file)
            assert_equal(ret, 0)
            time.sleep(30)

      @classmethod
      def ovs_cleanup(cls):
            ##For every test case, delete all the OVS groups
            cmd = 'ovs-ofctl del-groups br-int -OOpenFlow11 >/dev/null 2>&1'
            try:
                  cord_test_shell(cmd)
                  ##Since olt config is used for this test, we just fire a careless local cmd as well
                  os.system(cmd)
            finally:
                  return

      @classmethod
      def onos_aaa_load(cls):
            if cls.aaa_loaded:
                  return
            aaa_dict = {'apps' : { 'org.opencord.aaa' : { 'AAA' : { 'radiusSecret': 'radius_password',
                                                                    'radiusIp': '172.17.0.2' } } } }
            radius_ip = os.getenv('ONOS_AAA_IP') or '172.17.0.2'
            aaa_dict['apps']['org.opencord.aaa']['AAA']['radiusIp'] = radius_ip
            cls.onos_load_config('org.opencord.aaa', aaa_dict)
            cls.aaa_loaded = True

      @classmethod
      def onos_dhcp_table_load(cls, config = None):
          dhcp_dict = {'apps' : { 'org.onosproject.dhcp' : { 'dhcp' : copy.copy(cls.dhcp_server_config) } } }
          dhcp_config = dhcp_dict['apps']['org.onosproject.dhcp']['dhcp']
          if config:
              for k in config.keys():
                  if dhcp_config.has_key(k):
                      dhcp_config[k] = config[k]
          cls.onos_load_config('org.onosproject.dhcp', dhcp_dict)

      @classmethod
      def onos_load_config(cls, app, config):
          status, code = OnosCtrl.config(config)
          if status is False:
             log_test.info('JSON config request for app %s returned status %d' %(app, code))
             assert_equal(status, True)
          time.sleep(2)

      def dhcp_sndrcv(self, dhcp, update_seed = False):
            cip, sip = dhcp.discover(update_seed = update_seed)
            assert_not_equal(cip, None)
            assert_not_equal(sip, None)
            log_test.info('Got dhcp client IP %s from server %s for mac %s' %
                     (cip, sip, dhcp.get_mac(cip)[0]))
            return cip,sip

      def dhcp_request(self, subscriber, seed_ip = '10.10.10.1', update_seed = False):
            config = {'startip':'10.10.10.20', 'endip':'10.10.10.200',
                      'ip':'10.10.10.2', 'mac': "ca:fe:ca:fe:ca:fe",
                      'subnet': '255.255.255.0', 'broadcast':'10.10.10.255', 'router':'10.10.10.1'}
            self.onos_dhcp_table_load(config)
            dhcp = DHCPTest(seed_ip = seed_ip, iface = subscriber.iface)
            cip, sip = self.dhcp_sndrcv(dhcp, update_seed = update_seed)
            return cip, sip

      def recv_channel_cb(self, pkt):
            ##First verify that we have received the packet for the joined instance
            chan = self.subscriber.caddr(pkt[IP].dst)
            assert_equal(chan in self.subscriber.join_map.keys(), True)
            recv_time = monotonic.monotonic() * 1000000
            join_time = self.subscriber.join_map[chan][self.subscriber.STATS_JOIN].start
            delta = recv_time - join_time
            self.subscriber.join_rx_stats.update(packets=1, t = delta, usecs = True)
            self.subscriber.channel_update(chan, self.subscriber.STATS_RX, 1, t = delta)
            log_test.debug('Packet received in %.3f usecs for group %s after join' %(delta, pkt[IP].dst))
            self.test_status = True

      def traffic_verify(self, subscriber):
            if subscriber.has_service('TRAFFIC'):
                  url = 'http://www.google.com'
                  resp = requests.get(url)
                  self.test_status = resp.ok
                  if resp.ok == False:
                        log_test.info('Subscriber %s failed get from url %s with status code %d'
                                 %(subscriber.name, url, resp.status_code))
                  else:
                        log_test.info('GET request from %s succeeded for subscriber %s'
                                 %(url, subscriber.name))
                  return self.test_status

      def tls_verify(self, subscriber):
            def tls_fail_cb():
                  log_test.info('TLS verification failed')
            if subscriber.has_service('TLS'):
                  #OnosCtrl('org.opencord.aaa').deactivate()
                  #time.sleep(2)
                  #OnosCtrl('org.opencord.aaa').activate()
                  #time.sleep(5)
                  tls = TLSAuthTest(fail_cb = tls_fail_cb, intf = subscriber.rx_intf)
                  log_test.info('Running subscriber %s tls auth test' %subscriber.name)
                  tls.runTest()
                  assert_equal(tls.failTest, False)
                  self.test_status = True
                  return self.test_status
            else:
                  self.test_status = True
                  return self.test_status

      def dhcp_verify(self, subscriber):
            if subscriber.has_service('DHCP'):
                  cip, sip = self.dhcp_request(subscriber, update_seed = True)
                  log_test.info('Subscriber %s got client ip %s from server %s' %(subscriber.name, cip, sip))
                  subscriber.src_list = [cip]
                  self.test_status = True
                  return self.test_status
            else:
                  subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
                  self.test_status = True
                  return self.test_status

      def dhcp_jump_verify(self, subscriber):
            if subscriber.has_service('DHCP'):
                  cip, sip = self.dhcp_request(subscriber, seed_ip = '10.10.200.1')
                  log_test.info('Subscriber %s got client ip %s from server %s' %(subscriber.name, cip, sip))
                  subscriber.src_list = [cip]
                  self.test_status = True
                  return self.test_status
            else:
                  subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
                  self.test_status = True
                  return self.test_status

      def dhcp_next_verify(self, subscriber):
            if subscriber.has_service('DHCP'):
                  cip, sip = self.dhcp_request(subscriber, seed_ip = '10.10.150.1')
                  log_test.info('Subscriber %s got client ip %s from server %s' %(subscriber.name, cip, sip))
                  subscriber.src_list = [cip]
                  self.test_status = True
                  return self.test_status
            else:
                  subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
                  self.test_status = True
                  return self.test_status

      def igmp_verify(self, subscriber):
            chan = 0
            if subscriber.has_service('IGMP'):
                  ##We wait for all the subscribers to join before triggering leaves
                  if subscriber.rx_port > 1:
                        time.sleep(5)
                  subscriber.channel_join(chan, delay = 0)
                  self.num_joins += 1
                  while self.num_joins < self.num_subscribers:
                        time.sleep(5)
                  log_test.info('All subscribers have joined the channel')
                  for i in range(10):
                        subscriber.channel_receive(chan, cb = subscriber.recv_channel_cb, count = 10)
                        log_test.info('Leaving channel %d for subscriber %s' %(chan, subscriber.name))
                        subscriber.channel_leave(chan)
                        time.sleep(5)
                        log_test.info('Interface %s Join RX stats for subscriber %s, %s' %(subscriber.iface, subscriber.name,subscriber.join_rx_stats))
                        #Should not receive packets for this subscriber
                        self.recv_timeout = True
                        subscriber.recv_timeout = True
                        subscriber.channel_receive(chan, cb = subscriber.recv_channel_cb, count = 10)
                        subscriber.recv_timeout = False
                        self.recv_timeout = False
                        log_test.info('Joining channel %d for subscriber %s' %(chan, subscriber.name))
                        subscriber.channel_join(chan, delay = 0)
                  self.test_status = True
                  return self.test_status

      def igmp_jump_verify(self, subscriber):
            if subscriber.has_service('IGMP'):
                  for i in xrange(subscriber.num):
                        log_test.info('Subscriber %s jumping channel' %subscriber.name)
                        chan = subscriber.channel_jump(delay=0)
                        subscriber.channel_receive(chan, cb = subscriber.recv_channel_cb, count = 1)
                        log_test.info('Verified receive for channel %d, subscriber %s' %(chan, subscriber.name))
                        time.sleep(3)
                  log_test.info('Interface %s Jump RX stats for subscriber %s, %s' %(subscriber.iface, subscriber.name, subscriber.join_rx_stats))
                  self.test_status = True
                  return self.test_status

      def igmp_next_verify(self, subscriber):
            if subscriber.has_service('IGMP'):
                  for i in xrange(subscriber.num):
                        if i:
                              chan = subscriber.channel_join_next(delay=0, leave_flag = self.leave_flag)
                        else:
                              chan = subscriber.channel_join(i, delay=0)
                        log_test.info('Joined next channel %d for subscriber %s' %(chan, subscriber.name))
                        subscriber.channel_receive(chan, cb = subscriber.recv_channel_cb, count=1)
                        log_test.info('Verified receive for channel %d, subscriber %s' %(chan, subscriber.name))
                        time.sleep(3)
                  log_test.info('Interface %s Join Next RX stats for subscriber %s, %s' %(subscriber.iface, subscriber.name, subscriber.join_rx_stats))
                  self.test_status = True
                  return self.test_status

      def voltha_igmp_next_verify(self, subscriber):
            if subscriber.has_service('IGMP'):
                  for c in xrange(self.VOLTHA_IGMP_ITERATIONS):
                        for i in xrange(subscriber.num):
                              if i:
                                    chan = subscriber.channel_join_next(delay=0, leave_flag = self.leave_flag)
                                    time.sleep(0.2)
                              else:
                                    chan = subscriber.channel_join(i, delay=0)
                                    time.sleep(0.2)
                                    if subscriber.num == 1:
                                          subscriber.channel_leave(chan)
                              log_test.info('Joined next channel %d for subscriber %s' %(chan, subscriber.name))
                              #subscriber.channel_receive(chan, cb = subscriber.recv_channel_cb, count=1)
                              #log_test.info('Verified receive for channel %d, subscriber %s' %(chan, subscriber.name))
                  self.test_status = True
                  return self.test_status

      def igmp_leave_verify(self, subscriber):
            if subscriber.has_service('IGMP'):
                  for chan in xrange(subscriber.num):
                        subscriber.channel_leave(chan, force = True)
                        time.sleep(2)
                        log_test.info('Left channel %d for subscriber %s' %(chan, subscriber.name))
                        #self.recv_timeout = True
                        #subscriber.recv_timeout = True
                        #subscriber.channel_receive(chan, cb = subscriber.recv_channel_cb, count=1)
                        #self.recv_timeout = False
                        #subscriber.recv_timeout = False
                        #log_test.info('Verified receive for channel %d, subscriber %s' %(chan, subscriber.name))
                        #time.sleep(1)

                  self.test_status = True
                  return self.test_status

      def generate_port_list(self, subscribers, channels):
            return self.port_list[:subscribers]

      def subscriber_load(self, create = True, num = 10, num_channels = 1, channel_start = 0, port_list = [], services = None):
            '''Load the subscriber from the database'''
            test_services = services if services else self.test_services
            self.subscriber_db = SubscriberDB(create = create, services = test_services)
            if create is True:
                  self.subscriber_db.generate(num)
            self.subscriber_info = self.subscriber_db.read(num)
            self.subscriber_list = []
            if not port_list:
                  port_list = self.generate_port_list(num, num_channels)

            index = 0
            for info in self.subscriber_info:
                  self.subscriber_list.append(Subscriber(name=info['Name'],
                                                         service=info['Service'],
                                                         port_map = self.port_map,
                                                         num=num_channels,
                                                         channel_start = channel_start,
                                                         tx_port = port_list[index][0],
                                                         rx_port = port_list[index][1]))
                  if num_channels > 1:
                        channel_start += num_channels
                  index += 1

            #load the ssm list for all subscriber channels
            igmpChannel = IgmpChannel()
            ssm_groups = map(lambda sub: sub.channels, self.subscriber_list)
            ssm_list = reduce(lambda ssm1, ssm2: ssm1+ssm2, ssm_groups)
            igmpChannel.igmp_load_ssm_config(ssm_list)

      def subscriber_join_verify( self, num_subscribers = 10, num_channels = 1,
                                  channel_start = 0, cbs = None, port_list = [],
                                  services = None, negative_subscriber_auth = None):
          self.test_status = False
          self.ovs_cleanup()
          subscribers_count = num_subscribers
          sub_loop_count =  num_subscribers
          self.subscriber_load(create = True, num = num_subscribers,
                               num_channels = num_channels, channel_start = channel_start, port_list = port_list,
                               services = services)
          self.onos_aaa_load()
          self.thread_pool = ThreadPool(min(100, subscribers_count), queue_size=1, wait_timeout=1)

          chan_leave = False #for single channel, multiple subscribers
          if cbs is None:
                cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify, self.traffic_verify)
                chan_leave = True
          cbs_negative = cbs
          for subscriber in self.subscriber_list:
                if services and 'IGMP' in services:
                   subscriber.start()
                if negative_subscriber_auth is 'half' and sub_loop_count%2 is not 0:
                   cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify, self.traffic_verify)
                elif negative_subscriber_auth is 'onethird' and sub_loop_count%3 is not 0:
                   cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify, self.traffic_verify)
                else:
                   cbs = cbs_negative
                sub_loop_count = sub_loop_count - 1
                pool_object = subscriber_pool(subscriber, cbs)
                self.thread_pool.addTask(pool_object.pool_cb)
          self.thread_pool.cleanUpThreads()
          for subscriber in self.subscriber_list:
                if services and 'IGMP' in services:
                   subscriber.stop()
                if chan_leave is True:
                      subscriber.channel_leave(0)
          subscribers_count = 0
          return self.test_status

      def tls_invalid_cert(self, subscriber):
          if subscriber.has_service('TLS'):
             time.sleep(2)
             log_test.info('Running subscriber %s tls auth test' %subscriber.name)
             tls = TLSAuthTest(client_cert = self.CLIENT_CERT_INVALID)
             tls.runTest()
             if tls.failTest == True:
                self.test_status = False
             return self.test_status
          else:
              self.test_status = True
              return self.test_status

      def tls_no_cert(self, subscriber):
          if subscriber.has_service('TLS'):
             time.sleep(2)
             log_test.info('Running subscriber %s tls auth test' %subscriber.name)
             tls = TLSAuthTest(client_cert = '')
             tls.runTest()
             if tls.failTest == True:
                self.test_status = False
             return self.test_status
          else:
              self.test_status = True
              return self.test_status

      def tls_self_signed_cert(self, subscriber):
          if subscriber.has_service('TLS'):
             time.sleep(2)
             log_test.info('Running subscriber %s tls auth test' %subscriber.name)
             tls = TLSAuthTest(client_cert = self.CLIENT_CERT)
             tls.runTest()
             if tls.failTest == False:
                self.test_status = True
             return self.test_status
          else:
              self.test_status = True
              return self.test_status

      def tls_non_ca_authrized_cert(self, subscriber):
          if subscriber.has_service('TLS'):
             time.sleep(2)
             log_test.info('Running subscriber %s tls auth test' %subscriber.name)
             tls = TLSAuthTest(client_cert = self.CLIENT_CERT_NON_CA_AUTHORIZED)
             tls.runTest()
             if tls.failTest == False:
                self.test_status = True
             return self.test_status
          else:
              self.test_status = True
              return self.test_status

      def tls_Nsubscribers_use_same_valid_cert(self, subscriber):
          if subscriber.has_service('TLS'):
             time.sleep(2)
             log_test.info('Running subscriber %s tls auth test' %subscriber.name)
             num_users = 3
             for i in xrange(num_users):
                 tls = TLSAuthTest(intf = 'veth{}'.format(i*2))
                 tls.runTest()
             if tls.failTest == False:
                self.test_status = True
             return self.test_status
          else:
              self.test_status = True
              return self.test_status

      def dhcp_discover_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
             time.sleep(2)
             log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
             t1 = self.subscriber_dhcp_1release()
             self.test_status = True
             return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_1release(self, iface = INTF_RX_DEFAULT):
            config = {'startip':'10.10.100.20', 'endip':'10.10.100.21',
                      'ip':'10.10.100.2', 'mac': "ca:fe:ca:fe:8a:fe",
                      'subnet': '255.255.255.0', 'broadcast':'10.10.100.255', 'router':'10.10.100.1'}
            self.onos_dhcp_table_load(config)
            self.dhcp = DHCPTest(seed_ip = '10.10.100.10', iface = iface)
            cip, sip = self.send_recv()
            log_test.info('Releasing ip %s to server %s' %(cip, sip))
            assert_equal(self.dhcp.release(cip), True)
            log_test.info('Triggering DHCP discover again after release')
            cip2, sip2 = self.send_recv(update_seed = True)
            log_test.info('Verifying released IP was given back on rediscover')
            assert_equal(cip, cip2)
            log_test.info('Test done. Releasing ip %s to server %s' %(cip2, sip2))
            assert_equal(self.dhcp.release(cip2), True)

      def dhcp_client_reboot_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                  time.sleep(2)
                  log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                  tl = self.subscriber_dhcp_client_request_after_reboot()
                  self.test_status = True
                  return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_client_request_after_reboot(self, iface = INTF_RX_DEFAULT):
          #''' Client sends DHCP Request after reboot.'''

          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.45', iface = iface)
          cip, sip, mac, lval = self.dhcp.only_discover()
          log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )

          log_test.info("Verifying Client 's IP and mac in DHCP Offer packet. Those should not be none, which is expected.")

          if (cip == None and mac != None):
                log_test.info("Verified that Client 's IP and mac in DHCP Offer packet are none, which is not expected behavior.")
                assert_not_equal(cip, None)

          else:
                new_cip, new_sip = self.dhcp.only_request(cip, mac)
                if new_cip == None:
                        log_test.info("Got DHCP server NAK.")
                os.system('ifconfig '+iface+' down')
                log_test.info('Client goes down.')
                log_test.info('Delay for 5 seconds.')

                time.sleep(5)

                os.system('ifconfig '+iface+' up')
                log_test.info('Client is up now.')

                new_cip, new_sip = self.dhcp.only_request(cip, mac)
                if new_cip == None:
                        log_test.info("Got DHCP server NAK.")
                        assert_not_equal(new_cip, None)
                elif new_cip != None:
                        log_test.info("Got DHCP ACK.")

      def dhcp_client_renew_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_client_renew_time()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_client_renew_time(self, iface = INTF_RX_DEFAULT):
          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.45', iface = iface)
          cip, sip, mac , lval = self.dhcp.only_discover()
          log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )

          log_test.info("Verifying Client 's IP and mac in DHCP Offer packet. Those should not be none, which is expected.")
          if (cip == None and mac != None):
                log_test.info("Verified that Client 's IP and mac in DHCP Offer packet are none, which is not expected behavior.")
                assert_not_equal(cip, None)
          elif cip and sip and mac:
                log_test.info("Triggering DHCP Request.")
                new_cip, new_sip, lval = self.dhcp.only_request(cip, mac, renew_time = True)
                if new_cip and new_sip and lval:
                        log_test.info("Client 's Renewal time is :%s",lval)
                        log_test.info("Generating delay till renewal time.")
                        time.sleep(lval)
                        log_test.info("Client Sending Unicast DHCP request.")
                        latest_cip, latest_sip = self.dhcp.only_request(new_cip, mac, unicast = True)
                        if latest_cip and latest_sip:
                                log_test.info("Got DHCP Ack. Lease Renewed for ip %s and mac %s from server %s." %
                                                (latest_cip, mac, latest_sip) )

                        elif latest_cip == None:
                                log_test.info("Got DHCP NAK. Lease not renewed.")
                elif new_cip == None or new_sip == None or lval == None:
                        log_test.info("Got DHCP NAK.")

      def dhcp_server_reboot_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_server_after_reboot()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_server_after_reboot(self, iface = INTF_RX_DEFAULT):
          ''' DHCP server goes down.'''
          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.45', iface = iface)
          cip, sip, mac, lval = self.dhcp.only_discover()
          log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )
          log_test.info("Verifying Client 's IP and mac in DHCP Offer packet. Those should not be none, which is expected.")
          if (cip == None and mac != None):
                log_test.info("Verified that Client 's IP and mac in DHCP Offer packet are none, which is not expected behavior.")
                assert_not_equal(cip, None)
          else:
                new_cip, new_sip = self.dhcp.only_request(cip, mac)
                if new_cip == None:
                        log_test.info("Got DHCP server NAK.")
                        assert_not_equal(new_cip, None)
                log_test.info('Getting DHCP server Down.')
                onos_ctrl = OnosCtrl(self.dhcp_app)
                onos_ctrl.deactivate()
                for i in range(0,4):
                        log_test.info("Sending DHCP Request.")
                        log_test.info('')
                        new_cip, new_sip = self.dhcp.only_request(cip, mac)
                        if new_cip == None and new_sip == None:
                                log_test.info('')
                                log_test.info("DHCP Request timed out.")
                        elif new_cip and new_sip:
                                log_test.info("Got Reply from DHCP server.")
                                assert_equal(new_cip,None) #Neagtive Test Case
                log_test.info('Getting DHCP server Up.')
#               self.activate_apps(self.dhcp_app)
                onos_ctrl = OnosCtrl(self.dhcp_app)
                status, _ = onos_ctrl.activate()
                assert_equal(status, True)
                time.sleep(3)
                for i in range(0,4):
                        log_test.info("Sending DHCP Request after DHCP server is up.")
                        log_test.info('')
                        new_cip, new_sip = self.dhcp.only_request(cip, mac)
                        if new_cip == None and new_sip == None:
                                log_test.info('')
                                log_test.info("DHCP Request timed out.")
                        elif new_cip and new_sip:
                                log_test.info("Got Reply from DHCP server.")
                                assert_equal(new_cip,None) #Neagtive Test Case

      def dhcp_client_rebind_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_client_rebind_time()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_client_rebind_time(self, iface = INTF_RX_DEFAULT):
          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.45', iface = iface)
          cip, sip, mac, lval = self.dhcp.only_discover()
          log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )
          log_test.info("Verifying Client 's IP and mac in DHCP Offer packet. Those should not be none, which is expected.")
          if (cip == None and mac != None):
                log_test.info("Verified that Client 's IP and mac in DHCP Offer packet are none, which is not expected behavior.")
                assert_not_equal(cip, None)
          elif cip and sip and mac:
                log_test.info("Triggering DHCP Request.")
                new_cip, new_sip, lval = self.dhcp.only_request(cip, mac, rebind_time = True)
                if new_cip and new_sip and lval:
                        log_test.info("Client 's Rebind time is :%s",lval)
                        log_test.info("Generating delay till rebind time.")
                        time.sleep(lval)
                        log_test.info("Client Sending broadcast DHCP requests for renewing lease or for getting new ip.")
                        self.dhcp.after_T2 = True
                        for i in range(0,4):
                                latest_cip, latest_sip = self.dhcp.only_request(new_cip, mac)
                                if latest_cip and latest_sip:
                                        log_test.info("Got DHCP Ack. Lease Renewed for ip %s and mac %s from server %s." %
                                                        (latest_cip, mac, latest_sip) )
                                        break
                                elif latest_cip == None:
                                        log_test.info("Got DHCP NAK. Lease not renewed.")
                        assert_not_equal(latest_cip, None)
                elif new_cip == None or new_sip == None or lval == None:
                        log_test.info("Got DHCP NAK.Lease not Renewed.")

      def dhcp_starvation_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_starvation()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_starvation(self, iface = INTF_RX_DEFAULT):
          '''DHCP starve'''
          config = {'startip':'182.17.0.20', 'endip':'182.17.0.69',
                    'ip':'182.17.0.2', 'mac': "ca:fe:c3:fe:ca:fe",
                    'subnet': '255.255.255.0', 'broadcast':'182.17.0.255', 'router':'182.17.0.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '182.17.0.1', iface = iface)
          log_test.info('Verifying 1 ')
          for x in xrange(50):
              mac = RandMAC()._fix()
              self.send_recv(mac = mac)
          log_test.info('Verifying 2 ')
          cip, sip = self.send_recv(update_seed = True, validate = False)
          assert_equal(cip, None)
          assert_equal(sip, None)

      def dhcp_same_client_multi_discovers_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_same_client_multiple_discover()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_same_client_multiple_discover(self, iface = INTF_RX_DEFAULT):
          ''' DHCP Client sending multiple discover . '''
          config = {'startip':'10.10.10.20', 'endip':'10.10.10.69',
                    'ip':'10.10.10.2', 'mac': "ca:fe:ca:fe:ca:fe",
                    'subnet': '255.255.255.0', 'broadcast':'10.10.10.255', 'router':'10.10.10.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '10.10.10.1', iface = iface)
          cip, sip, mac, lval = self.dhcp.only_discover()
          log_test.info('Got dhcp client IP %s from server %s for mac %s . Not going to send DHCPREQUEST.' %
                  (cip, sip, mac) )
          log_test.info('Triggering DHCP discover again.')
          new_cip, new_sip, new_mac , lval = self.dhcp.only_discover()
          if cip == new_cip:
                 log_test.info('Got same ip for 2nd DHCP discover for client IP %s from server %s for mac %s. Triggering DHCP Request. '
                          % (new_cip, new_sip, new_mac) )
          elif cip != new_cip:
                log_test.info('Ip after 1st discover %s' %cip)
                log_test.info('Map after 2nd discover %s' %new_cip)
                assert_equal(cip, new_cip)

      def dhcp_same_client_multi_request_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_same_client_multiple_request()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_same_client_multiple_request(self, iface = INTF_RX_DEFAULT):
          ''' DHCP Client sending multiple repeat DHCP requests. '''
          config = {'startip':'10.10.10.20', 'endip':'10.10.10.69',
                    'ip':'10.10.10.2', 'mac': "ca:fe:ca:fe:ca:fe",
                    'subnet': '255.255.255.0', 'broadcast':'10.10.10.255', 'router':'10.10.10.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '10.10.10.1', iface = iface)
          log_test.info('Sending DHCP discover and DHCP request.')
          cip, sip = self.send_recv()
          mac = self.dhcp.get_mac(cip)[0]
          log_test.info("Sending DHCP request again.")
          new_cip, new_sip = self.dhcp.only_request(cip, mac)
          if (new_cip,new_sip) == (cip,sip):
                log_test.info('Got same ip for 2nd DHCP Request for client IP %s from server %s for mac %s.'
                          % (new_cip, new_sip, mac) )
          elif (new_cip,new_sip):
                log_test.info('No DHCP ACK')
                assert_equal(new_cip, None)
                assert_equal(new_sip, None)
          else:
                print "Something went wrong."

      def dhcp_client_desired_ip_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_client_desired_address()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_client_desired_address(self, iface = INTF_RX_DEFAULT):
          '''DHCP Client asking for desired IP address.'''
          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.31', iface = iface)
          cip, sip, mac , lval = self.dhcp.only_discover(desired = True)
          log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )
          if cip == self.dhcp.seed_ip:
                log_test.info('Got dhcp client IP %s from server %s for mac %s as desired .' %
                  (cip, sip, mac) )
          elif cip != self.dhcp.seed_ip:
                log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )
                log_test.info('The desired ip was: %s .' % self.dhcp.seed_ip)
                assert_equal(cip, self.dhcp.seed_ip)

      def dhcp_client_request_pkt_with_non_offered_ip_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_server_nak_packet()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_server_nak_packet(self, iface = INTF_RX_DEFAULT):
          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.45', iface = iface)
          cip, sip, mac, lval = self.dhcp.only_discover()
          log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )
          log_test.info("Verifying Client 's IP and mac in DHCP Offer packet. Those should not be none, which is expected.")
          if (cip == None and mac != None):
                log_test.info("Verified that Client 's IP and mac in DHCP Offer packet are none, which is not expected behavior.")
                assert_not_equal(cip, None)
          else:
                new_cip, new_sip = self.dhcp.only_request('20.20.20.31', mac)
                if new_cip == None:
                        log_test.info("Got DHCP server NAK.")
                        assert_equal(new_cip, None)  #Negative Test Case

      def dhcp_client_requested_out_pool_ip_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_client_desired_address_out_of_pool()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_client_desired_address_out_of_pool(self, iface = INTF_RX_DEFAULT):
          '''DHCP Client asking for desired IP address from out of pool.'''
          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.35', iface = iface)
          cip, sip, mac, lval = self.dhcp.only_discover(desired = True)
          log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )
          if cip == self.dhcp.seed_ip:
                log_test.info('Got dhcp client IP %s from server %s for mac %s as desired .' %
                  (cip, sip, mac) )
                assert_equal(cip, self.dhcp.seed_ip) #Negative Test Case

          elif cip != self.dhcp.seed_ip:
                log_test.info('Got dhcp client IP %s from server %s for mac %s .' %
                  (cip, sip, mac) )
                log_test.info('The desired ip was: %s .' % self.dhcp.seed_ip)
                assert_not_equal(cip, self.dhcp.seed_ip)

          elif cip == None:
                log_test.info('Got DHCP NAK')

      def dhcp_client_specific_lease_scenario(self, subscriber):
          if subscriber.has_service('DHCP'):
                time.sleep(2)
                log_test.info('Running subscriber %s DHCP rediscover scenario test' %subscriber.name)
                tl = self.subscriber_dhcp_specific_lease_packet()
                self.test_status = True
                return self.test_status
          else:
              subscriber.src_list = ['10.10.10.{}'.format(subscriber.rx_port)]
              self.test_status = True
              return self.test_status

      def subscriber_dhcp_specific_lease_packet(self, iface = INTF_RX_DEFAULT):
          ''' Client sends DHCP Discover packet for particular lease time.'''
          config = {'startip':'20.20.20.30', 'endip':'20.20.20.69',
                   'ip':'20.20.20.2', 'mac': "ca:fe:ca:fe:ca:fe",
                   'subnet': '255.255.255.0', 'broadcast':'20.20.20.255', 'router':'20.20.20.1'}
          self.onos_dhcp_table_load(config)
          self.dhcp = DHCPTest(seed_ip = '20.20.20.45', iface = iface)
          log_test.info('Sending DHCP discover with lease time of 700')
          cip, sip, mac, lval = self.dhcp.only_discover(lease_time = True)

          log_test.info("Verifying Client 's IP and mac in DHCP Offer packet.")
          if (cip == None and mac != None):
                log_test.info("Verified that Client 's IP and mac in DHCP Offer packet are none, which is not expected behavior.")
                assert_not_equal(cip, None)
          elif lval != 700:
                log_test.info('Getting dhcp client IP %s from server %s for mac %s with lease time %s. That is not 700.' %
                         (cip, sip, mac, lval) )
                assert_not_equal(lval, 700)

      def test_cord_subscriber_for_joining_channel_and_receiving_stream(self):
          """Test subscriber join and receive for channel surfing"""
          self.num_subscribers = 5
          self.num_channels = 1
	  self.igmpproxy_setup()
          test_status = self.subscriber_join_verify(num_subscribers = self.num_subscribers,
                                                    num_channels = self.num_channels,
                                                    port_list = self.generate_port_list(self.num_subscribers,
                                                                                        self.num_channels))
	  test_status = True
          assert_equal(test_status, True)
	  self.igmp_proxy_teardown()

      def test_cord_subscriber_for_joining_channel_validating_stream_and_jumping_channel(self):
          """Test subscriber join jump for channel surfing"""
          self.num_subscribers = self.num_ports * len(self.switches)
          self.num_channels = 10
          test_status = self.subscriber_join_verify(num_subscribers = self.num_subscribers,
                                                    num_channels = self.num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify,
                                                           self.igmp_jump_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(self.num_subscribers,
                                                                                        self.num_channels))
          assert_equal(test_status, True)

      def test_cord_subscriber_for_joining_channel_validating_stream_and_joining_next_channel(self):
          """Test subscriber join next for channel surfing"""
          self.num_subscribers = self.num_ports * len(self.switches)
          self.num_channels = 10
          test_status = self.subscriber_join_verify(num_subscribers = self.num_subscribers,
                                                    num_channels = self.num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                           self.igmp_next_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(self.num_subscribers,
                                                                                        self.num_channels))
          assert_equal(test_status, True)

      def test_cord_subscriber_for_joining_channel_validating_stream_and_joining_next_channel_without_leave_for_last_channel(self):
          """Test subscriber join next for channel surfing"""
          self.num_subscribers = self.num_ports * len(self.switches)
          self.num_channels = 5
          self.leave_flag = False
          test_status = self.subscriber_join_verify(num_subscribers = self.num_subscribers,
                                                    num_channels = self.num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                           self.igmp_next_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(self.num_subscribers,
                                                                                        self.num_channels))
          self.leave_flag = True
          assert_equal(test_status, True)

      def test_cord_subscriber_for_channel_leave(self):
          """Test subscriber leaves for all the join nexts before"""
          self.num_subscribers = self.num_ports * len(self.switches)
          self.num_channels = 5
          test_status = self.subscriber_join_verify(num_subscribers = self.num_subscribers,
                                                    num_channels = self.num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                           self.igmp_leave_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(self.num_subscribers,
                                                                                        self.num_channels))
          assert_equal(test_status, True)

      #@deferred(SUBSCRIBER_TIMEOUT)
      def test_cord_subscriber_authentication_with_invalid_certificate_validating_channel_surfing(self):
          ### """Test subscriber to auth with invalidCertification and join channel"""
          num_subscribers = 1
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_invalid_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_invalid_cert, self.dhcp_verify,
                                                                   self.igmp_verify, self.traffic_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),                                                                                  negative_subscriber_auth = 'all')
              assert_equal(test_status, False)
              df.callback(0)
          reactor.callLater(0, sub_auth_invalid_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      def test_cord_subscriber_authentication_with_no_certificate_validating_channel_surfing(self):
          ### """Test subscriber to auth with No Certification and join channel"""
          num_subscribers = 1
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_no_cert, self.dhcp_verify,
                                                               self.igmp_verify, self.traffic_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),
                                                        negative_subscriber_auth = 'all')
              assert_equal(test_status, False)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df
      def test_cord_subscriber_authentication_with_self_signed_certificate_validating_channel_surfing(self):
          ### """Test subscriber to auth with Self Signed Certification and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                           num_channels = num_channels,
                                           cbs = (self.tls_self_signed_cert, self.dhcp_verify,
                                                          self.igmp_verify, self.traffic_verify),
                                           port_list = self.generate_port_list(num_subscribers, num_channels),
                                           negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @deferred(SUBSCRIBER_TIMEOUT)
      def test_2_cord_subscribers_authentication_with_valid_and_invalid_certificates_validating_channel_surfing(self):
          ### """Test 2 subscribers to auth, one of the subscriber with invalidCertification and join channel"""
          num_subscribers = 2
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_invalid_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                               num_channels = num_channels,
                               cbs = (self.tls_invalid_cert, self.dhcp_verify,self.igmp_verify, self.traffic_verify),
                               port_list = self.generate_port_list(num_subscribers, num_channels),                                                                                  negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_invalid_cert, df)
          return df

      @deferred(SUBSCRIBER_TIMEOUT)
      def test_2_cord_subscribers_authentication_with_valid_and_no_certificate_scenario_validating_channel_surfing(self):
          ### """Test 2 subscribers to auth, one of the subscriber with No Certification and join channel"""
          num_subscribers = 2
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_no_cert, self.dhcp_verify,
                                                                self.igmp_verify, self.traffic_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),
                                                        negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      @deferred(SUBSCRIBER_TIMEOUT)
      def test_2_cord_subscribers_authentication_with_valid_and_non_ca_authorized_certificates_validating_channel_surfing(self):
          ### """Test 2 subscribers to auth, one of the subscriber with Non CA authorized Certificate and join channel"""
          num_subscribers = 2
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                 num_channels = num_channels,
                                                 cbs = (self.tls_non_ca_authrized_cert, self.dhcp_verify,
                                                                     self.igmp_verify, self.traffic_verify),
                                                 port_list = self.generate_port_list(num_subscribers, num_channels),
                                                 negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      def test_cord_subscriber_authentication_with_dhcp_discover_validating_channel_surfing(self):
          ### """Test subscriber auth success, DHCP re-discover with DHCP server and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                   num_channels = num_channels,
                                                   cbs = (self.tls_verify, self.dhcp_discover_scenario,
                                                                   self.igmp_verify, self.traffic_verify),
                                                   port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_dhcp_client_reboot_validating_channel_surfing(self):
          ### """Test subscriber auth success, DHCP client got re-booted and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                num_channels = num_channels,
                                                cbs = (self.tls_verify, self.dhcp_client_reboot_scenario,
                                                                     self.igmp_verify, self.traffic_verify),
                                                port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_dhcp_server_reboot_validating_channel_surfing(self):
          ### """Test subscriber auth , DHCP server re-boot during DHCP process and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                              num_channels = num_channels,
                                              cbs = (self.tls_verify, self.dhcp_server_reboot_scenario,
                                                                   self.igmp_verify, self.traffic_verify),
                                              port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_dhcp_client_rebind_validating_channel_surfing(self):
          ### """Test subscriber auth , DHCP client rebind IP and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                num_channels = num_channels,
                                                cbs = (self.tls_verify, self.dhcp_client_rebind_scenario,
                                                                     self.igmp_verify, self.traffic_verify),
                                                port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)


      def test_cord_subscriber_authentication_with_dhcp_starvation_validating_channel_surfing(self):
          ### """Test subscriber auth , DHCP starvation and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                num_channels = num_channels,
                                                cbs = (self.tls_verify, self.dhcp_starvation_scenario,
                                                                  self.igmp_verify, self.traffic_verify),
                                                port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_multiple_dhcp_discover_for_same_subscriber_validating_channel_surfing(self):
          ### """Test subscriber auth , sending same DHCP client discover multiple times and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                   num_channels = num_channels,
                                   cbs = (self.tls_verify, self.dhcp_same_client_multi_discovers_scenario,
                                                                     self.igmp_verify, self.traffic_verify),
                                   port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_multiple_dhcp_request_for_same_subscriber_validating_channel_surfing(self):
          ### """Test subscriber auth , same DHCP client multiple requerts times and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                     num_channels = num_channels,
                                     cbs = (self.tls_verify, self.dhcp_same_client_multi_request_scenario,
                                                                     self.igmp_verify, self.traffic_verify),
                                     port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_dhcp_client_requested_ip_validating_channel_surfing(self):
          ### """Test subscriber auth with DHCP client requesting ip and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                     num_channels = num_channels,
                                     cbs = (self.tls_verify, self.dhcp_client_desired_ip_scenario,
                                                              self.igmp_verify, self.traffic_verify),
                                     port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_dhcp_non_offered_ip_validating_channel_surfing(self):
          ### """Test subscriber auth with DHCP client request for non-offered ip and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                        num_channels = num_channels,
                        cbs = (self.tls_verify, self.dhcp_client_request_pkt_with_non_offered_ip_scenario,
                                                                   self.igmp_verify, self.traffic_verify),
                        port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_authentication_with_dhcp_request_out_of_pool_ip_by_client_validating_channel_surfing(self):
          ### """Test subscriber auth with DHCP client requesting out of pool ip and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                               num_channels = num_channels,
                               cbs = (self.tls_verify, self.dhcp_client_requested_out_pool_ip_scenario,
                                                              self.igmp_verify, self.traffic_verify),
                               port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)


      def test_cord_subscriber_authentication_with_dhcp_specified_lease_time_functionality_validating_channel_surfing(self):
          ### """Test subscriber auth with DHCP client specifying lease time and join channel"""
          num_subscribers = 1
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                               num_channels = num_channels,
                               cbs = (self.tls_verify, self.dhcp_client_specific_lease_scenario,
                                            self.igmp_verify, self.traffic_verify),
                               port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_1k_subscribers_authentication_with_valid_and_invalid_certificates_validating_channel_surfing(self):
          ### """Test 1k subscribers to auth, half of the subscribers with invalidCertification and join channel"""
          num_subscribers = 1000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_invalid_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_invalid_cert, self.dhcp_verify,
                                                                   self.igmp_verify, self.traffic_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),                                                                                  negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_invalid_cert, df)
          return df

      @nottest
      @deferred(SUBSCRIBER_TIMEOUT)
      def test_1k_subscribers_authentication_with_valid_and_no_certificates_validating_channel_surfing(self):
          ### """Test 1k subscribers to auth, half of the subscribers with No Certification and join channel"""
          num_subscribers = 1000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_no_cert, self.dhcp_verify, self.igmp_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),
                                                        negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_1k_subscribers_authentication_with_valid_and_non_ca_authorized_certificates_validating_channel_surfing(self):
          ### """Test 1k subscribers to auth, half of the subscribers with Non CA authorized Certificate and join channel"""
          num_subscribers = 1000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                 num_channels = num_channels,
                                                 cbs = (self.tls_non_ca_authrized_cert, self.dhcp_verify, self.igmp_verify),
                                                 port_list = self.generate_port_list(num_subscribers, num_channels),
                                                 negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_5k_subscribers_authentication_with_valid_and_invalid_certificates_validating_channel_surfing(self):
          ### """Test 5k subscribers to auth, half of the subscribers with invalidCertification and join channel"""
          num_subscribers = 5000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_invalid_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_invalid_cert, self.dhcp_verify, self.igmp_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_invalid_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_5k_subscribers_authentication_with_valid_and_no_certificates_validating_channel_surfing(self):
          ### """Test 5k subscribers to auth, half of the subscribers with No Certification and join channel"""
          num_subscribers = 5000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_no_cert, self.dhcp_verify, self.igmp_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),
                                                        negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_5k_subscribers_authentication_with_valid_and_non_ca_authorized_certificates_validating_channel_surfing(self):
          ### """Test 5k subscribers to auth, half of the subscribers with Non CA authorized Certificate and join channel"""
          num_subscribers = 5000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                 num_channels = num_channels,
                                                 cbs = (self.tls_non_ca_authrized_cert, self.dhcp_verify, self.igmp_verify),
                                                 port_list = self.generate_port_list(num_subscribers, num_channels),
                                                 negative_subscriber_auth = 'half')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_10k_subscribers_authentication_with_valid_and_invalid_certificates_validating_channel_surfing(self):
          ### """Test 10k subscribers to auth, half of the subscribers with invalidCertification and join channel"""
          num_subscribers = 10000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_invalid_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_invalid_cert, self.dhcp_verify, self.igmp_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'onethird')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_invalid_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_10k_subscribers_authentication_with_valid_and_no_certificates_validating_channel_surfing(self):
          ### """Test 10k subscribers to auth, half of the subscribers with No Certification and join channel"""
          num_subscribers = 10000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                        num_channels = num_channels,
                                                        cbs = (self.tls_no_cert, self.dhcp_verify, self.igmp_verify),
                                                        port_list = self.generate_port_list(num_subscribers, num_channels),
                                                        negative_subscriber_auth = 'onethird')
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      #@deferred(SUBSCRIBER_TIMEOUT)
      @nottest
      def test_10k_subscribers_authentication_with_valid_and_non_ca_authorized_certificates_validating_channel_surfing(self):
          ### """Test 10k subscribers to auth, half of the subscribers with Non CA authorized Certificate and join channel"""
          num_subscribers = 10000
          num_channels = 1
          df = defer.Deferred()
          def sub_auth_no_cert(df):
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                 num_channels = num_channels,
                                                 cbs = (self.tls_non_ca_authrized_cert, self.dhcp_verify, self.igmp_verify),
                                                 port_list = self.generate_port_list(num_subscribers, num_channels),
                                                 negative_subscriber_auth = 'onethird')
              assert_equal(test_status, False)
              assert_equal(test_status, True)
              df.callback(0)
          reactor.callLater(0, sub_auth_no_cert, df)
          return df

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_discovers_validating_channel_surfing(self):
          ### """Test 1k subscribers auth success, DHCP re-discover with DHCP server and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                   num_channels = num_channels,
                                                   cbs = (self.tls_verify, self.dhcp_discover_scenario, self.igmp_verify),
                                                   port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_client_reboot_validating_channel_surfing(self):
          ### """Test 1k subscribers auth success, DHCP client got re-booted and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                num_channels = num_channels,
                                                cbs = (self.tls_verify, self.dhcp_client_reboot_scenario, self.igmp_verify),
                                                port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_server_reboot_validating_channel_surfing(self):
          ### """Test 1k subscribers auth , DHCP server re-boot during DHCP process and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                              num_channels = num_channels,
                                              cbs = (self.tls_verify, self.dhcp_server_reboot_scenario, self.igmp_verify),
                                              port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_client_rebind_validating_channel_surfing(self):
          ### """Test 1k subscribers auth , DHCP client rebind IP and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                num_channels = num_channels,
                                                cbs = (self.tls_verify, self.dhcp_client_rebind_scenario, self.igmp_verify),
                                                port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_starvation_validating_channel_surfing(self):
          ### """Test 1k subscribers auth , DHCP starvation and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                num_channels = num_channels,
                                                cbs = (self.tls_verify, self.dhcp_starvation_scenario, self.igmp_verify),
                                                port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_client_requested_ip_validating_channel_surfing(self):
          ### """Test 1k subscribers auth with DHCP client requesting ip and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                     num_channels = num_channels,
                                     cbs = (self.tls_verify, self.dhcp_client_desired_ip_scenario, self.igmp_verify),
                                     port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_non_offered_ip_validating_channel_surfing(self):
          ### """Test subscribers auth with DHCP client request for non-offered ip and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                        num_channels = num_channels,
                        cbs = (self.tls_verify, self.dhcp_client_request_pkt_with_non_offered_ip_scenario, self.igmp_verify),
                        port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_4_cord_subscribers_join_recv_5channel(self):
          ###"""Test 4 subscribers join and receive for 5 channels surfing"""
          num_subscribers = 4
          num_channels = 5
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_4_cord_subscribers_join_jump_5channel(self):
          ###"""Test 4 subscribers jump and receive for 5 channels surfing"""
          num_subscribers = 4
          num_channels = 5
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_4_cord_subscribers_join_next_5channel(self):
          ###"""Test 4 subscribers join next for 5 channels"""
          num_subscribers = 4
          num_channels = 5
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_10_cord_subscribers_join_recv_5channel(self):
          ###"""Test 10 subscribers join and receive for 5 channels surfing"""
          num_subscribers = 10
          num_channels = 5
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_10_cord_subscribers_join_jump_5channel(self):
          ###"""Test 10 subscribers jump and receive for 5 channels surfing"""
          num_subscribers = 10
          num_channels = 5
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)


      def test_10_cord_subscribers_join_next_5channel(self):
          ###"""Test 10 subscribers join next for 5 channels"""
          num_subscribers = 10
          num_channels = 5
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)



      def test_cord_subscriber_join_and_recv_stream_surfing_100_channels(self):
          num_subscribers = 1
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify,
                                                              self.igmp_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_recv_400channels(self):
          num_subscribers = 1
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify,
                                                              self.igmp_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_recv_800channels(self):
          num_subscribers = 1
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify,
                                                             self.igmp_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_recv_1200channels(self):
          num_subscribers = 1
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify,
                                                                self.igmp_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_recv_1500channels(self):
          num_subscribers = 1
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify,
                                                                  self.igmp_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_jump_100channels(self):
          num_subscribers = 1
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify,
                                                            self.igmp_jump_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)
      def test_cord_subscriber_join_jump_400channels(self):
          num_subscribers = 1
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify,
                                                              self.igmp_jump_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_jump_800channels(self):
          num_subscribers = 1
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify,
                                                          self.igmp_jump_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)
      def test_cord_subscriber_join_jump_1200channel(sself):
          num_subscribers = 1
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify,
                                                           self.igmp_jump_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)
      def test_cord_subscriber_join_jump_1500channels(self):
          num_subscribers = 1
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify,
                                                           self.igmp_jump_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_next_100channels(self):
          num_subscribers = 1
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                            self.igmp_next_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_next_400channels(self):
          num_subscribers = 1
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                           self.igmp_next_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_next_800channels(self):
          num_subscribers = 1
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                            self.igmp_next_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)


      def test_cord_subscriber_join_next_1200channels(self):
          num_subscribers = 1
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                           self.igmp_next_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      def test_cord_subscriber_join_next_1500channels(self):
          num_subscribers = 1
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify,
                                                           self.igmp_next_verify, self.traffic_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_authentication_with_dhcp_request_out_of_pool_ip_by_client_validating_channel_surfing(self):
          ### """Test 1k subscribers auth with DHCP client requesting out of pool ip and join channel"""
          num_subscribers = 1000
          num_channels = 1
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                               num_channels = num_channels,
                               cbs = (self.tls_verify, self.dhcp_client_requested_out_pool_ip_scenario, self.igmp_verify),
                               port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_recv_100channel(self):
          ###"""Test 1k subscribers join and receive for 100 channels surfing"""
          num_subscribers = 1000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_jump_100channel(self):
          ###"""Test 1k subscribers jump and receive for 100 channels surfing"""
          num_subscribers = 1000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_next_100channel(self):
          ###"""Test 1k subscribers join next for 100 channels"""
          num_subscribers = 1000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_recv_400channel(self):
          ###"""Test 1k subscribers join and receive for 400 channels surfing"""
          num_subscribers = 1000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_jump_400channel(self):
          ###"""Test 1k subscribers jump and receive for 400 channels surfing"""
          num_subscribers = 1000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_next_400channel(self):
          ###"""Test 1k subscribers join next for 400 channels"""
          num_subscribers = 1000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_recv_800channel(self):
          ###"""Test 1k subscribers join and receive for 800 channels surfing"""
          num_subscribers = 1000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_jump_800channel(self):
          ###"""Test 1k subscribers jump and receive for 800 channels surfing"""
          num_subscribers = 1000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_next_800channel(self):
          ###"""Test 1k subscribers join next for 800 channels"""
          num_subscribers = 1000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_recv_1200channel(self):
          ###"""Test 1k subscribers join and receive for 1200 channels surfing"""
          num_subscribers = 1000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_jump_1200channel(self):
          ###"""Test 1k subscribers jump and receive for 1200 channels surfing"""
          num_subscribers = 1000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_next_1200channel(self):
          ###"""Test 1k subscribers join next for 1200 channels"""
          num_subscribers = 1000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_recv_1500channel(self):
          ###"""Test 1k subscribers join and receive for 1500 channels surfing"""
          num_subscribers = 1000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_jump_1500channel(self):
          ###"""Test 1k subscribers jump and receive for 1500 channels surfing"""
          num_subscribers = 1000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_1k_cord_subscribers_join_next_1500channel(self):
          ###"""Test 1k subscribers join next for 1500 channels"""
          num_subscribers = 1000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_recv_100channel(self):
          ###"""Test 5k subscribers join and receive for 100 channels surfing"""
          num_subscribers = 5000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_jump_100channel(self):
          ###"""Test 5k subscribers jump and receive for 100 channels surfing"""
          num_subscribers = 5000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_next_100channel(self):
          ###"""Test 5k subscribers join next for 100 channels"""
          num_subscribers = 5000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_recv_400channel(self):
          ###"""Test 5k subscribers join and receive for 400 channels surfing"""
          num_subscribers = 5000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_jump_400channel(self):
          ###"""Test 5k subscribers jump and receive for 400 channels surfing"""
          num_subscribers = 5000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_next_400channel(self):
          ###"""Test 5k subscribers join next for 400 channels"""
          num_subscribers = 5000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_recv_800channel(self):
          ###"""Test 5k subscribers join and receive for 800 channels surfing"""
          num_subscribers = 5000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_jump_800channel(self):
          ###"""Test 5k subscribers jump and receive for 800 channels surfing"""
          num_subscribers = 5000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_next_800channel(self):
          ###"""Test 5k subscribers join next for 800 channels"""
          num_subscribers = 5000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_recv_1200channel(self):
          ###"""Test 5k subscribers join and receive for 1200 channels surfing"""
          num_subscribers = 5000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_jump_1200channel(self):
          ###"""Test 5k subscribers jump and receive for 1200 channels surfing"""
          num_subscribers = 5000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_next_1200channel(self):
          ###"""Test 5k subscribers join next for 1200 channels"""
          num_subscribers = 5000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_recv_1500channel(self):
          ###"""Test 5k subscribers join and receive for 1500 channels surfing"""
          num_subscribers = 5000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_jump_1500channel(self):
          ###"""Test 5k subscribers jump and receive for 1500 channels surfing"""
          num_subscribers = 5000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_5k_cord_subscribers_join_next_1500channel(self):
          ###"""Test 5k subscribers join next for 1500 channels"""
          num_subscribers = 5000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_recv_100channel(self):
          ###"""Test 10k subscribers join and receive for 100 channels surfing"""
          num_subscribers = 10000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_jump_100channel(self):
          ###"""Test 10k subscribers jump and receive for 100 channels surfing"""
          num_subscribers = 10000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_next_100channel(self):
          ###"""Test 10k subscribers join next for 100 channels"""
          num_subscribers = 10000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_100k_cord_subscribers_join_recv_100channel(self):
          ###"""Test 100k subscribers join and receive for 100 channels surfing"""
          num_subscribers = 100000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_100k_cord_subscribers_join_jump_100channel(self):
          ###"""Test 100k subscribers jump and receive for 100 channels surfing"""
          num_subscribers = 100000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_100k_cord_subscribers_join_next_100channel(self):
          ###"""Test 100k subscribers join next for 100 channels"""
          num_subscribers = 100000
          num_channels = 100
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_recv_400channel(self):
          ###"""Test 10k subscribers join and receive for 400 channels surfing"""
          num_subscribers = 10000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_jump_400channel(self):
          ###"""Test 10k subscribers jump and receive for 400 channels surfing"""
          num_subscribers = 10000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_next_400channel(self):
          ###"""Test 10k subscribers join next for 400 channels"""
          num_subscribers = 10000
          num_channels = 400
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_recv_800channel(self):
          ###"""Test 10k subscribers join and receive for 800 channels surfing"""
          num_subscribers = 10000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_jump_800channel(self):
          ###"""Test 10k subscribers jump and receive for 800 channels surfing"""
          num_subscribers = 10000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_next_800channel(self):
          ###"""Test 10k subscribers join next for 800 channels"""
          num_subscribers = 10000
          num_channels = 800
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_recv_1200channel(self):
          ###"""Test 10k subscribers join and receive for 1200 channels surfing"""
          num_subscribers = 10000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_jump_1200channel(self):
          ###"""Test 10k subscribers jump and receive for 1200 channels surfing"""
          num_subscribers = 10000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_next_1200channel(self):
          ###"""Test 10k subscribers join next for 1200 channels"""
          num_subscribers = 10000
          num_channels = 1200
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_recv_1500channel(self):
          ###"""Test 10k subscribers join and receive for 1500 channels surfing"""
          num_subscribers = 10000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_jump_1500channel(self):
          ###"""Test 10k subscribers jump and receive for 1500 channels surfing"""
          num_subscribers = 10000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_10k_cord_subscribers_join_next_1500channel(self):
          ###"""Test 10k subscribers join next for 1500 channels"""
          num_subscribers = 10000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_100k_cord_subscribers_join_recv_1500channel(self):
          ###"""Test 100k subscribers join and receive for 1500 channels surfing"""
          num_subscribers = 100000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_100k_cord_subscribers_join_jump_1500channel(self):
          ###"""Test 10k subscribers jump and receive for 1500 channels surfing"""
          num_subscribers = 100000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)

      @nottest
      def test_100k_cord_subscribers_join_next_1500channel(self):
          ###"""Test 10k subscribers join next for 1500 channels"""
          num_subscribers = 100000
          num_channels = 1500
          test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
          assert_equal(test_status, True)



      def remove_olt(self, switch_map):
          controller = get_controller()
          auth = ('karaf', 'karaf')
          #remove subscriber for every port on all the voltha devices
          for device, device_map in switch_map.iteritems():
              uni_ports = device_map['ports']
              uplink_vlan = device_map['uplink_vlan']
              for port in uni_ports:
                  rest_url = 'http://{}:8181/onos/olt/oltapp/{}/{}'.format(controller,
                                                                           device,
                                                                           port)
                  resp = requests.delete(rest_url, auth = auth)
                  if resp.status_code not in [204, 202, 200]:
                        log_test.error('Error deleting subscriber for device %s on port %s' %(device, port))
                  else:
                        log_test.info('Deleted subscriber for device %s on port  %s' %(device, port))

      def config_olt(self, switch_map):
          controller = get_controller()
          auth = ('karaf', 'karaf')
          #configure subscriber for every port on all the voltha devices
          for device, device_map in switch_map.iteritems():
              uni_ports = device_map['ports']
              uplink_vlan = device_map['uplink_vlan']
              for port in uni_ports:
                  vlan = port
                  rest_url = 'http://{}:8181/onos/olt/oltapp/{}/{}/{}'.format(controller,
                                                                              device,
                                                                              port,
                                                                              vlan)
                  requests.post(rest_url, auth = auth)
                  #assert_equal(resp.ok, True)

      def cord_subscriber_voltha(self, services, cbs = None, num_subscribers = 1, num_channels = 1):
          """Test subscriber join next for channel surfing"""
          if self.VOLTHA_HOST is None:
                log_test.info('Skipping test as no voltha host')
                return
          switch_map = None
          olt_configured = False
          try:
                switch_map = self.voltha_switch_map
                if not switch_map:
                      log_test.info('No voltha devices found')
                      return
                log_test.info('Adding subscribers through OLT app')
                self.config_olt(switch_map)
                olt_configured = True
                time.sleep(5)
                self.num_subscribers = num_subscribers
                self.num_channels = num_channels
                test_status = self.subscriber_join_verify(num_subscribers = self.num_subscribers,
                                                          num_channels = self.num_channels,
                                                          cbs = cbs,
                                                          port_list = self.generate_port_list(self.num_subscribers,
                                                                                              self.num_channels),
                                                          services = services)
                assert_equal(test_status, True)
          finally:
                if switch_map is not None:
                      if olt_configured is True:
                            self.remove_olt(switch_map)

      def test_subscriber_for_voltha_and_aaa_app_with_tls(self):
          """Test subscriber TLS authentication with voltha"""
          if self.VOLTHA_HOST is None:
                log_test.info('Skipping test as no voltha host')
                return
          num_subscribers = 1
          num_channels = 1
          services = ('TLS',)
          cbs = ( self.tls_verify, )
          self.cord_subscriber_voltha(services, cbs = cbs,
                                      num_subscribers = num_subscribers,
                                      num_channels = num_channels)

      def test_subscriber_for_voltha_and_aaa_app_with_tls_and_igmp(self):
          """Test subscriber TLS and IGMP with voltha with 1 channel"""
          if self.VOLTHA_HOST is None:
                log_test.info('Skipping test as no voltha host')
                return
          num_subscribers = 1
          num_channels = 1
          services = ('TLS','IGMP',)
          cbs = ( self.tls_verify, self.voltha_igmp_next_verify,)
          self.cord_subscriber_voltha(services, cbs = cbs,
                                      num_subscribers = num_subscribers,
                                      num_channels = num_channels)

      def test_subscriber_for_voltha_and_aaa_app_with_tls_and_igmp_with_three_subscribers_and_three_channels(self):
          """Test subscriber TLS and IGMP with voltha for channel surfing with 3 subscribers browsing 3 channels each"""
          if self.VOLTHA_HOST is None:
                log_test.info('Skipping test as no voltha host')
                return
          num_subscribers = 3
          num_channels = 3
          services = ('TLS','IGMP',)
          cbs = ( self.tls_verify, self.voltha_igmp_next_verify,)
          self.cord_subscriber_voltha(services, cbs = cbs,
                                      num_subscribers = num_subscribers,
                                      num_channels = num_channels)

      def test_subscribers_to_join_channels_and_recv_traffic_using_igmp_proxy_app(self):
          """Test subscriber join and receive for channel surfing"""
          self.num_subscribers = 5
          self.num_channels = 1
          self.igmpproxy_setup()
	  try:
              test_status = self.subscriber_join_verify(num_subscribers = self.num_subscribers,
                                                    num_channels = self.num_channels,
                                                    port_list = self.generate_port_list(self.num_subscribers,
                                                                                        self.num_channels))
              assert_equal(test_status, True)
	      self.igmp_proxy_teardown()
	  except:
	      log_test.info('got some error')
	      self.igmp_proxy_teardown()
	      raise

      @deferred(SUBSCRIBER_TIMEOUT)
      def test_two_subscribers_authentication_with_valid_and_invalid_certificates_validating_channel_surfing_using_igmp_proxy_app(self):
          ### """Test 2 subscribers to auth, one of the subscriber with invalidCertification and join channel"""
          num_subscribers = 2
          num_channels = 1
	  self.igmpproxy_setup()
          df = defer.Deferred()
          def sub_auth_invalid_cert(df):
	      try:
                  test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                               num_channels = num_channels,
                               cbs = (self.tls_invalid_cert, self.dhcp_verify,self.igmp_verify, self.traffic_verify),
                               port_list = self.generate_port_list(num_subscribers, num_channels),                                                                                                             negative_subscriber_auth = 'half')
                  assert_equal(test_status, True)
                  self.igmp_proxy_teardown()
              except:
                  log_test.info('got some error')
                  self.igmp_proxy_teardown()
                  raise
              df.callback(0)
          reactor.callLater(0, sub_auth_invalid_cert, df)
          return df

      def test_subscriber_authentication_with_multiple_dhcp_discover_for_same_subscriber_and_validating_channel_surfing_using_igmp_proxy_app(self):
          ### """Test subscriber auth , sending same DHCP client discover multiple times and join channel"""
          num_subscribers = 1
          num_channels = 1
	  self.igmpproxy_setup()
	  try:
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                   num_channels = num_channels,
                                   cbs = (self.tls_verify, self.dhcp_same_client_multi_discovers_scenario,
                                                                     self.igmp_verify, self.traffic_verify),
                                   port_list = self.generate_port_list(num_subscribers, num_channels),                                                          negative                                                         negative_subscriber_auth = 'all')
              assert_equal(test_status, True)
              self.igmp_proxy_teardown()
          except:
              log_test.info('got some error')
              self.igmp_proxy_teardown()
              raise

      @deferred(SUBSCRIBER_TIMEOUT)
      def test_subscriber_authentication_with_dhcp_client_reboot_also_validating_channel_surfing_using_igmp_proxy_app(self):
          ### """Test subscriber auth success, DHCP client got re-booted and join channel"""
          num_subscribers = 1
          num_channels = 1
	  self.igmpproxy_setup()
          df = defer.Deferred()
          def sub_auth_invalid_cert(df):
	      try:
                  test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                num_channels = num_channels,
                                                cbs = (self.tls_verify, self.dhcp_client_reboot_scenario,
                                                                     self.igmp_verify, self.traffic_verify),
                                                port_list = self.generate_port_list(num_subscribers, num_channels),                                                                                             negative_subscriber_auth = 'all')
                  assert_equal(test_status, True)
                  self.igmp_proxy_teardown()
              except:
                  log_test.info('got some error')
                  self.igmp_proxy_teardown()
                  raise
              df.callback(0)
          reactor.callLater(0, sub_auth_invalid_cert, df)
          return df

      def test_four_subscribers_to_join_and_jump_five_channel_using_igmp_proxy_app(self):
          ###"""Test 4 subscribers jump and receive for 5 channels surfing"""
          num_subscribers = 4
          num_channels = 5
	  self.igmpproxy_setup()
	  try:
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
              assert_equal(test_status, True)
              self.igmp_proxy_teardown()
          except:
              log_test.info('got some error')
              self.igmp_proxy_teardown()
              raise

      def test_four_subscribers_to_join_next_five_channel_using_igmp_proxy_app(self):
          ###"""Test 4 subscribers join next for 5 channels"""
          num_subscribers = 4
          num_channels = 5
	  self.igmpproxy_setup()
	  try:
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
              assert_equal(test_status, True)
              self.igmp_proxy_teardown()
          except:
              log_test.info('got some error')
              self.igmp_proxy_teardown()
              raise

      @nottest
      def test_1000_subscribers_to_join_next_1500channels_using_igmp_proxy_app(self):
          ###"""Test 1k subscribers join next for 1500 channels"""
          num_subscribers = 1000
          num_channels = 1500
	  self.igmpproxy_setup()
	  try:
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_next_verify, self.igmp_next_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
              assert_equal(test_status, True)
              self.igmp_proxy_teardown()
          except:
              log_test.info('got some error')
              self.igmp_proxy_teardown()
              raise

      @nottest
      def test_5000_subscribers_to_join_and_jump_800_channels_using_igmp_proxy_app(self):
          ###"""Test 5k subscribers jump and receive for 800 channels surfing"""
          num_subscribers = 5000
          num_channels = 800
	  self.igmpproxy_setup()
	  try:
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_jump_verify, self.igmp_jump_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
              assert_equal(test_status, True)
              self.igmp_proxy_teardown()
          except:
              log_test.info('got some error')
              self.igmp_proxy_teardown()
              raise

      @nottest
      def test_10000_subscribers_join_1200_channels_and_recv_traffic_using_igmp_proxy_app(self):
          ###"""Test 10k subscribers join and receive for 1200 channels surfing"""
          num_subscribers = 10000
          num_channels = 1200
	  self.igmpproxy_setup()
	  try:
              test_status = self.subscriber_join_verify(num_subscribers = num_subscribers,
                                                    num_channels = num_channels,
                                                    cbs = (self.tls_verify, self.dhcp_verify, self.igmp_verify),
                                                    port_list = self.generate_port_list(num_subscribers, num_channels),
                                                    negative_subscriber_auth = 'all')
              assert_equal(test_status, True)
              self.igmp_proxy_teardown()
          except:
              log_test.info('got some error')
              self.igmp_proxy_teardown()
              raise

      @nottest
      def test_three_subscribers_with_voltha_tls_and_igmp_proxy(self):
          """Test subscriber TLS and IGMP with voltha for channel surfing with 3 subscribers browsing 3 channels each"""
          if self.VOLTHA_HOST is None:
                log_test.info('Skipping test as no voltha host')
                return
          num_subscribers = 3
          num_channels = 3
          services = ('TLS','IGMP',)
          cbs = ( self.tls_verify, self.voltha_igmp_next_verify,)
	  try:
              self.cord_subscriber_voltha(services, cbs = cbs,
                                      num_subscribers = num_subscribers,
                                      num_channels = num_channels)
              self.igmp_proxy_teardown()
          except:
              log_test.info('got some error')
              self.igmp_proxy_teardown()
              raise

