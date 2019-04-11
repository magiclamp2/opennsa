"""
OpenNSA JunOS backend.

Should work for EX and QXF switches.
"""

# configure snippet:
#
# ACTIVATION:
# configure
# set vlan opennsa-1234 vlan-id 1234
# set interfaces ge-0/0/1 unit 0 family ethernet-switching vlan members onsa-1234
# set interfaces ge-0/0/2 unit 0 family ethernet-switching vlan members onsa-1234
# commit

# DE-ACTIVATION:
# configure
# delete vlan opennsa-1234 vlan-id 1234
# delete interfaces ge-0/0/1 unit 0 family ethernet-switching vlan members onsa-1234
# delete interfaces ge-0/0/2 unit 0 family ethernet-switching vlan members onsa-1234
# commit

import random

from twisted.python import log
from twisted.internet import defer

from opennsa import constants as cnt, config
from opennsa.backends.common import genericbackend, ssh



# parameterized commands
COMMAND_CONFIGURE               = 'configure'
COMMAND_COMMIT                  = 'commit'
COMMAND_ROLLBACK                = 'rollback'

#COMMAND_SET_VLAN                = 'set vlans opennsa-%i vlan-id %i l3-interface vlan.%i'
COMMAND_SET_VLAN                = 'set vlans opennsa-%i vlan-id %i'
COMMAND_SET_VLAN_L3INT          = 'set vlans opennsa-%i l3-interface vlan.%i'
COMMAND_SET_VLAN_DOT1Q          = 'set vlans opennsa-%i dot1q-tunneling'
COMMAND_SET_VLAN_SWAP           = 'set vlans opennsa-%i interface %s.0 mapping %i swap'
COMMAND_SET_INTERFACE_VLAN      = 'set interfaces %s unit 0 family ethernet-switching vlan members opennsa-%i'

#COMMAND_DELETE_VLAN             = 'delete vlans opennsa-%i'
COMMAND_DELETE_VLAN_L3INT       = 'delete vlans opennsa-%i l3-interface'
COMMAND_DELETE_VLAN_SWAP        = 'delete vlans opennsa-%i interface %s.0'
COMMAND_DELETE_VLAN_DOT1Q       = 'delete vlans opennsa-%i dot1q-tunneling'
COMMAND_DELETE_INTERFACE_VLAN   = 'delete interfaces %s unit 0 family ethernet-switching vlan members opennsa-%i'

LOG_SYSTEM = 'JuniperEX'




def configureVlanCommands(source_port, dest_port, vlan):

    vl = COMMAND_SET_VLAN % (vlan, vlan)
    v1l3int = COMMAND_SET_VLAN_L3INT % (vlan, vlan)
    p1 = COMMAND_SET_INTERFACE_VLAN % (source_port, vlan)
    p2 = COMMAND_SET_INTERFACE_VLAN % (dest_port, vlan)
    commands = [ vl, v1l3int, p1, p2 ]

    return commands

def configureVlansCommands(source_port, source_vlan, dest_port, dest_vlan):
# configure translating vlans
## "source_port/vlan" will always be the translated one
## Swapped port always one less than "source_port"

# (translation, no more vlan deletion, src: X#2222, dest: Y#1111 )
# set vlans opennsa-1111 l3-interface vlan.1111
# set vlans opennsa-2222 dot1q-tunneling
# set vlans opennsa-2222 interface xe-0/0/46.0 mapping 1111 swap
# set interfaces Y unit 0 family ethernet-switching vlan members opennsa-1111
# set interfaces xe-0/0/47 unit 0 family ethernet-switching vlan members opennsa-1111
# set interfaces X unit 0 family ethernet-switching vlan members opennsa-2222

    #trans_port = source_port[:-1] + str( int(source_port[-1]) -1 )

    vd = COMMAND_SET_VLAN % (dest_vlan, dest_vlan)
    vs = COMMAND_SET_VLAN % (source_vlan, source_vlan)
    vdl3int     = COMMAND_SET_VLAN_L3INT % (dest_vlan, dest_vlan)
    vsdot1q     = COMMAND_SET_VLAN_DOT1Q % source_vlan
    vsswap      = COMMAND_SET_VLAN_SWAP % (source_vlan, 'xe-0/0/46', dest_vlan)
    p1vd        = COMMAND_SET_INTERFACE_VLAN % (dest_port, dest_vlan)
    p2vd        = COMMAND_SET_INTERFACE_VLAN % ('xe-0/0/47', dest_vlan)
    p3vs        = COMMAND_SET_INTERFACE_VLAN % (source_port, source_vlan)

    commands = [ vd, vs, vdl3int, vsdot1q, vsswap, p1vd, p2vd, p3vs ]
    return commands

def deleteVlanCommands(source_port, dest_port, vlan):

    p1 = COMMAND_DELETE_INTERFACE_VLAN % (source_port, vlan)
    p2 = COMMAND_DELETE_INTERFACE_VLAN % (dest_port, vlan)
    vl = COMMAND_DELETE_VLAN_L3INT % vlan

    commands = [ p1, p2, vl]
    return commands

def deleteVlansCommands(source_port, source_vlan, dest_port, dest_vlan):
# delete translating vlans
# "source_port/vlan" will always be the translated one

# (new translation, no vlan deletion just configs, src: X#2222, dest: Y#1111 )
# delete interfaces Y unit 0 family ethernet-switching vlan members opennsa-1111
# delete interfaces xe-0/0/47 unit 0 family ethernet-switching vlan members opennsa-1111
# delete interfaces X unit 0 family ethernet-switching vlan members opennsa-2222
# delete vlans opennsa-2222 interface xe-0/0/46.0
# delete vlans opennsa-2222 dot1q-tunneling
# delete vlans opennsa-1111 l3-interface

    p1vd         = COMMAND_DELETE_INTERFACE_VLAN % (dest_port, dest_vlan)
    p2vd         = COMMAND_DELETE_INTERFACE_VLAN % ('xe-0/0/47', dest_vlan)
    p3vs         = COMMAND_DELETE_INTERFACE_VLAN % (source_port, source_vlan)
#    vl = COMMAND_DELETE_VLAN % dest_vlan
    vsswap       = COMMAND_DELETE_VLAN_SWAP % (source_vlan, 'xe-0/0/46')
    vsdot1q      = COMMAND_DELETE_VLAN_DOT1Q % source_vlan
    vdl3int      = COMMAND_DELETE_VLAN_L3INT % dest_vlan

    commands = [ p1vd, p2vd, p3vs, vsswap, vsdot1q, vdl3int ]
    return commands



class SSHChannel:

    name = 'session'

    def __init__(self):

        self.line = ''
        self.wait_defer = None
        self.wait_line  = None

    def sendCommands(self, commands, client):

        LT = '\r' # line termination
        channel = client.invoke_shell()
        channel.settimeout(30)

        while not self.line.endswith('> '):
                 resp = channel.recv(9999)
                 self.line += resp
                 print(resp)
        self.line = ''

        try:
            channel.send(COMMAND_CONFIGURE + LT)
            log.msg('Entered configure mode')

            for cmd in commands:
                log.msg('CMD> %s' % cmd, system=LOG_SYSTEM)
                while not self.line.endswith('# '):
                         resp = channel.recv(9999)
                         self.line += resp
#                        print(resp)
                print(resp)
                self.line = ''
                channel.send(cmd + LT)
#               while channel.recv_ready():
#                    self.line += channel.recv(1024)
#               log.msg(self.line)
#               self.line=''
            # commit commands, check for 'commit complete' as success
            # not quite sure how to handle failure here

#           d = self.waitForLine('commit complete')

            channel.send(COMMAND_COMMIT + LT)

            while not self.line.endswith('# '):
                 resp = channel.recv(9999)
                 self.line += resp
            print(resp)

            if 'fail' in self.line:
                 sendEmail(self.line)
                 channel.send(COMMAND_ROLLBACK + LT)
                 while not self.line.endswith('# '):
                        resp = channel.recv(9999)
                        self.line += resp
                 print(resp)
                 self.line = ''
                 channel.send(COMMAND_COMMIT + LT)
                 while not self.line.endswith('# '):
                        resp = channel.recv(9999)
                        self.line += resp
                 print(resp)
                 raise Exception(self.line)

            self.line = ''

        except Exception, e:
            log.msg('Error sending commands: %s' % str(e))
            raise e

        log.msg('Commands successfully committed')
        channel.close()


class JuniperEXCommandSender:


    def __init__(self, host, port, ssh_host_fingerprint, user, ssh_public_key_path, ssh_private_key_path):

        self.sshconnection = \
             ssh.SSHConnection(host, port, user, ssh_public_key_path, ssh_private_key_path)
             #for now - fingerprint is left unused..

        self.sshclient = self.sshconnection.startConnection()
        self.transport = self.sshclient.get_transport()
        self.transport.set_keepalive(30)
        self.channel = None

    def _getSSHChannel(self):                   #this whole section is for compatibility w/ original code with deferreds

        connection = SSHChannel()
        self.channel = connection
        return defer.succeed(connection)


    def _sendCommands(self, commands):

        def gotChannel(channel):
            d = self.channel.sendCommands(commands, self.sshclient)
            return d

        d = self._getSSHChannel()
        d.addCallback(gotChannel)
        return d


    def setupLink(self, source_port, source_vlan, dest_port, dest_vlan):

        if source_vlan == dest_vlan:
            commands = configureVlanCommands(source_port, dest_port, dest_vlan)
        elif source_vlan > dest_vlan:
            commands = configureVlansCommands(source_port, source_vlan, dest_port, dest_vlan)
        else:
            commands = configureVlansCommands(dest_port, dest_vlan, source_port, source_vlan)
        return self._sendCommands(commands)


    def teardownLink(self, source_port, source_vlan, dest_port, dest_vlan):

        if source_vlan == dest_vlan:
            commands = deleteVlanCommands(source_port, dest_port, dest_vlan)
        elif source_vlan > dest_vlan:
            commands = deleteVlansCommands(source_port, source_vlan, dest_port, dest_vlan)
        else:
            commands = deleteVlansCommands(dest_port, dest_vlan, source_port, source_vlan)
        return self._sendCommands(commands)


# --------


class JunosEXTarget(object):

    def __init__(self, port, vlan=None):
        self.port = port
        self.vlan = vlan

    def __str__(self):
        if self.vlan:
            return '<JunosEXTarget %s#%i>' % (self.port, self.vlan)
        else:
            return '<JunosEXTarget %s>' % self.port



class JuniperEXConnectionManager:

    def __init__(self, port_map, host, port, host_fingerprint, user, ssh_public_key, ssh_private_key):

        self.port_map = port_map
        self.command_sender = JuniperEXCommandSender(host, port, host_fingerprint, user, ssh_public_key, ssh_private_key)


#   def getResource(self, port, label_type, label_value):
#       assert label_type in (None, cnt.ETHERNET_VLAN), 'Label must be None or VLAN'
#       return str(label_value) # vlan is a global resource, only one be used at a time


#   def getTarget(self, port, label_type, label_value):
#       assert label_type in (None, cnt.ETHERNET_VLAN), 'Label must be None or VLAN'
#       if label_type == cnt.ETHERNET_VLAN:
#           vlan = int(label_value)
#           assert 1 <= vlan <= 4095, 'Invalid label value for vlan: %s' % label_value

#        return JunosEXTarget(self.port_map[port], vlan)



    def getResource(self, port, label):
        assert label is not None and label.type_ == cnt.ETHERNET_VLAN, 'Label must be None or VLAN'
        return str(label.labelValue()) # vlan is a global resource, only one be used at a time


    def getTarget(self, port, label):
        assert label is not None and label.type_ == cnt.ETHERNET_VLAN, 'Label must be None or VLAN'

#        return self.port_map[port] + '.' + label.labelValue()

#       if label.type_ == cnt.ETHERNET_VLAN:
#           log.msg('label.values = %s' % (str(label.values)), system=LOG_SYSTEM)
        vlan = int(str(label.labelValue()))
##           log.msg('VLAN = %i' % (vlan), system=LOG_SYSTEM)

#           assert 1 <= vlan <= 4095, 'Invalid label value for vlan: %s' % label.values

        return JunosEXTarget(self.port_map[port], vlan)



    def createConnectionId(self, source_target, dest_target):
        return 'EX-' + str(random.randint(100000,999999))


    def canSwapLabel(self, label_type):
        return label_type == cnt.ETHERNET_VLAN
        #return False


    def setupLink(self, connection_id, source_target, dest_target, bandwidth):

        def linkUp(_):
            log.msg('Link %s -> %s up' % (source_target, dest_target), system=LOG_SYSTEM)

        d = self.command_sender.setupLink(source_target.port, source_target.vlan, dest_target.port, dest_target.vlan)
#       if source_target.vlan >= dest_target.vlan:
#            d = self.command_sender.setupLink(dest_target.port, source_target.port, dest_target.vlan)
#       else:
#            d = self.command_sender.setupLink(source_target.port, dest_target.port, source_target.vlan)

        d.addCallback(linkUp)
        return d


    def teardownLink(self, connection_id, source_target, dest_target, bandwidth):

        def linkDown(_):
            log.msg('Link %s -> %s down' % (source_target, dest_target), system=LOG_SYSTEM)

        d = self.command_sender.teardownLink(source_target.port, source_target.vlan, dest_target.port, dest_target.vlan)
#       if source_target.vlan >= dest_target.vlan:
#            d = self.command_sender.teardownLink(source_target.port, dest_target.port, dest_target.vlan)
#       else:
#            d = self.command_sender.teardownLink(source_target.port, dest_target.port, source_target.vlan)

        d.addCallback(linkDown)
        return d



def JuniperEXBackend(network_name, nrm_ports, parent_requester, cfg):

    name = 'JuniperEX %s' % network_name
    nrm_map  = dict( [ (p.name, p) for p in nrm_ports ] ) # for the generic backend
    port_map = dict( [ (p.name, p.interface) for p in nrm_ports ] ) # for the nrm backend

    # extract config items
    host             = cfg[config.JUNIPER_HOST]
    port             = cfg.get(config.JUNIPER_PORT, 22)
    host_fingerprint = cfg[config.JUNIPER_HOST_FINGERPRINT]
    user             = cfg[config.JUNIPER_USER]
    ssh_public_key   = cfg[config.JUNIPER_SSH_PUBLIC_KEY]
    ssh_private_key  = cfg[config.JUNIPER_SSH_PRIVATE_KEY]

    cm = JuniperEXConnectionManager(port_map, host, port, host_fingerprint, user, ssh_public_key, ssh_private_key)
    return genericbackend.GenericBackend(network_name, nrm_map, cm, parent_requester, name)
