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


class SSHChannel(ssh.SSHChannel):

    name = 'session'

    def __init__(self, conn):
        ssh.SSHChannel.__init__(self, conn=conn)

        self.line = ''

        self.wait_defer = None
        self.wait_line  = None


    @defer.inlineCallbacks
    def sendCommands(self, commands):
        LT = '\r' # line termination

        try:
            yield self.conn.sendRequest(self, 'shell', '', wantReply=1)
            d = self.waitForLine('>')
            self.write(COMMAND_CONFIGURE + LT)
            yield d

            log.msg('Entered configure mode', debug=True, system=LOG_SYSTEM)

            for cmd in commands:
                log.msg('CMD> %s' % cmd, system=LOG_SYSTEM)
                d = self.waitForLine('[edit]')
                self.write(cmd + LT)
                yield d

            # commit commands, check for 'commit complete' as success
            # not quite sure how to handle failure here

            ## test stuff
            #d = self.waitForLine('[edit]')
            #self.write('commit check' + LT)

            d = self.waitForLine('commit complete')
            self.write(COMMAND_COMMIT + LT)
            yield d

        except Exception as e:
            log.msg('Error sending commands: %s' % str(e))
            raise e

        log.msg('Commands successfully committed', debug=True, system=LOG_SYSTEM)
        self.sendEOF()
        self.closeIt()


    def waitForLine(self, line):
        self.wait_line = line
        self.wait_defer = defer.Deferred()
        return self.wait_defer


    def matchLine(self, line):
        if self.wait_line and self.wait_defer:
            if self.wait_line in line.strip():
                d = self.wait_defer
                self.wait_line  = None
                self.wait_defer = None
                d.callback(self)
            else:
                pass


    def dataReceived(self, data):
        if len(data) == 0:
            pass
        else:
            self.line += data
            if '\n' in data:
                lines = [ line.strip() for line in self.line.split('\n') if line.strip() ]
                self.line = ''
                for l in lines:
                    self.matchLine(l)



class JuniperEXCommandSender:


    def __init__(self, host, port, ssh_host_fingerprint, user, password):

        self.ssh_connection_creator = \
             ssh.SSHConnectionCreator(host, port, [ ssh_host_fingerprint ], user, password)

        self.ssh_connection = None # cached connection


    def _getSSHChannel(self):

        def setSSHConnectionCache(ssh_connection):
            log.msg('SSH Connection created and cached', system=LOG_SYSTEM)
            self.ssh_connection = ssh_connection
            return ssh_connection

        def gotSSHConnection(ssh_connection):
            channel = SSHChannel(conn = ssh_connection)
            ssh_connection.openChannel(channel)
            return channel.channel_open

        if self.ssh_connection and not self.ssh_connection.transport.factory.stopped:
            log.msg('Reusing SSH connection', debug=True, system=LOG_SYSTEM)
            return gotSSHConnection(self.ssh_connection)
        else:
            # since creating a new connection should be uncommon, we log it
            # this makes it possible to see if something fucks up and creates connections continuously
            log.msg('Creating new SSH connection', system=LOG_SYSTEM)
            d = self.ssh_connection_creator.getSSHConnection()
            d.addCallback(setSSHConnectionCache)
            d.addCallback(gotSSHConnection)
            return d


    def _sendCommands(self, commands):

        def gotChannel(channel):
            d = channel.sendCommands(commands)
            return d

        d = self._getSSHChannel()
        d.addCallback(gotChannel)
        return d


    def setupLink(self, source_nrm_port, dest_nrm_port, vlan):

        commands = configureVlanCommands(source_nrm_port, dest_nrm_port, vlan)
        return self._sendCommands(commands)


    def teardownLink(self, source_nrm_port, dest_nrm_port, vlan):

        commands = deleteVlanCommands(source_nrm_port, dest_nrm_port, vlan)
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

    def __init__(self, port_map, host, port, host_fingerprint, user, password):

        self.port_map = port_map
        self.command_sender = JuniperEXCommandSender(host, port, host_fingerprint, user, password)


    def getResource(self, port, label):
        assert label is None or label.type_ == cnt.ETHERNET_VLAN, 'Label must be None or VLAN'
        return label.labelValue() # vlan is a global resource, only one be used at a time


    def getTarget(self, port, label):
        assert label is None or label.type_ == cnt.ETHERNET_VLAN, 'Label must be None or VLAN'
        if label.type_ == cnt.ETHERNET_VLAN:
            vlan = int(label.labelValue())
            assert 1 <= vlan <= 4095, 'Invalid label value for vlan: %s' % label.labelValue()

        return JunosEXTarget(self.port_map[port], vlan)


    def createConnectionId(self, source_target, dest_target):
        return 'EX-' + str(random.randint(100000,999999))


    def canSwapLabel(self, label_type):
        return False # not yet anyway


    def setupLink(self, connection_id, source_target, dest_target, bandwidth):

        assert source_target.vlan == dest_target.vlan, 'VLANs must match'

        def linkUp(_):
            log.msg('Link %s -> %s up' % (source_target, dest_target), system=LOG_SYSTEM)

        d = self.command_sender.setupLink(source_target.port, dest_target.port, dest_target.vlan)
        d.addCallback(linkUp)
        return d


    def teardownLink(self, connection_id, source_target, dest_target, bandwidth):

        assert source_target.vlan == dest_target.vlan, 'VLANs must match'

        def linkDown(_):
            log.msg('Link %s -> %s down' % (source_target, dest_target), system=LOG_SYSTEM)

        d = self.command_sender.teardownLink(source_target.port, dest_target.port, dest_target.vlan)
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
    password   = cfg[config.JUNIPER_PASSWORD]
    

    cm = JuniperEXConnectionManager(port_map, host, port, host_fingerprint, user, password)
    return genericbackend.GenericBackend(network_name, nrm_map, cm, parent_requester, name)
