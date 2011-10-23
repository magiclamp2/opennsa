"""
Argia (NRA) Backend.

Uses a set of specific commands (made by Scott Campell) for making
reservations, etc. into Argia.

Author: Henrik Thostrup Jensen <htj@nordu.net>
Copyright: NORDUnet (2011)
"""

import os
import datetime
import StringIO

from xml.etree import ElementTree as ET

from twisted.python import log
from twisted.internet import reactor, protocol, defer, task

from opennsa import error, state



COMMAND_DIR = '/home/nsi/nsiArgia'
COMMAND_BIN = 'nsaResClient.sh'

ARGIA_CLIENT   = os.path.join(COMMAND_DIR, COMMAND_BIN)

ARGIA_CMD_RESERVE   = 'reserve'
ARGIA_CMD_PROVISION = 'provision'
ARGIA_CMD_RELEASE   = 'release'
ARGIA_CMD_TERMINATE = 'cancel'

# These state are internal to the Argia NRM and cannot be shared with the NSI service layer
ARGIA_RESERVED        = 'RESERVED'
ARGIA_SCHEDULED       = 'SCHEDULED'
ARGIA_AUTO_PROVISION  = 'AUTO_PROVISION'
ARGIA_PROVISIONING    = 'PROVISIONING'
ARGIA_PROVISIONED     = 'PROVISIONED'
ARGIA_TERMINATED      = 'TERMINATED'

LOG_SYSTEM = 'opennsa.Argia'


class ArgiaBackendError(Exception):
    """
    Raised when the Argia backend returns an error.
    """



class ArgiaBackend:

    def __init__(self):
        self.connections = []

    def createConnection(self, source_port, dest_port, service_parameters):

        self._checkTiming(service_parameters.start_time, service_parameters.end_time)
        self._checkResourceAvailability(source_port, dest_port, service_parameters.start_time, service_parameters.end_time)
        ac = ArgiaConnection(source_port, dest_port, service_parameters)
        self.connections.append(ac)
        return ac


    # this could be generic
    def _checkTiming(self, res_start, res_end):
        # check that ports are available in the specified schedule
        if res_start in [ None, '' ] or res_end in [ None, '' ]:
            raise error.InvalidRequestError('Reservation must specify start and end time (was either None or '')')

        # sanity checks
        if res_start > res_end:
            raise error.InvalidRequestError('Refusing to make reservation with reverse duration')

        if res_start < datetime.datetime.utcnow():
            raise error.InvalidRequestError('Refusing to make reservation with start time in the past')

        if res_start > datetime.datetime(2025, 1, 1):
            raise error.InvalidRequestError('Refusing to make reservation with start time after 2025')


    def _checkResourceAvailability(self, source_port, dest_port, res_start, res_end):

        # port temporal availability
        def portOverlap(res1_start_time, res1_end_time, res2_start_time, res2_end_time):
            if res1_start_time >= res2_start_time and res1_start_time <= res2_end_time:
                return True
            if res1_start_time <= res2_start_time and res1_start_time <= res2_end_time:
                return True
            return False

        for cn in self.connections:
            if cn.state() == state.TERMINATED:
                continue # don't check terminated state (we need a cleanup thingie)
            csp = cn.service_parameters
            if source_port in [ cn.source_port, cn.dest_port ]:
                if portOverlap(csp.start_time, csp.end_time, res_start, res_end):
                    raise error.ResourceNotAvailableError('Port %s not available in specified time span' % source_port)

            if dest_port == [ cn.source_port, cn.dest_port ]:
                if portOverlap(csp.start_time, csp.end_time, res_start, res_end):
                    raise error.ResourceNotAvailableError('Port %s not available in specified time span' % dest_port)

        # all good



def deferTaskFailed(err):
    if err.check(defer.CancelledError):
        pass # this just means that the task was cancelled
    else:
        log.err(err)



class ArgiaProcessProtocol(protocol.ProcessProtocol):

    def __init__(self, initial_payload=None):
        self.initial_payload = initial_payload
        self.d = defer.Deferred()

        self.stdout = StringIO.StringIO()
        self.stderr = StringIO.StringIO()

    def connectionMade(self):
        if self.initial_payload is not None:
            self.transport.write(self.initial_payload)
        self.transport.closeStdin()

    def outReceived(self, data):
        #print "OUT RECV:", data
        self.stdout.write(data)

    def errReceived(self, data):
        #print "ERR RECV:", data
        self.stderr.write(data)

    def processEnded(self, status):
        exit_code = status.value.exitCode
        #print "PROCESS EXITED:", exit_code
        self.stdout.seek(0)
        self.stderr.seek(0)
        if exit_code == 0:
            self.d.callback(self.stdout)
        else:
            err = ArgiaBackendError('Argia command returned exit code %i' % exit_code)
            self.d.errback(err)



class ArgiaConnection:

#   should implement connection interface instead - does not exist currently

    def __init__(self, source_port, dest_port, service_parameters):
        self.source_port = source_port
        self.dest_port = dest_port
        self.service_parameters = service_parameters

        self.state = state.ConnectionState()

        # this can be reservation id or connection id depending on the state, but it doesn't really matter
        self.argia_id = None
        self.scheduled_transition_call = None


    def stps(self):
        return self.service_parameters.source_stp, self.service_parameters.dest_stp


    def _scheduleStateTransition(self, transition_time, state):

        assert self.scheduled_transition_call is None

        def _switchState(conn, state):
            conn.state.switchState(state)
            log.msg('State transtion. CID: %s, State: %s' % (id(self), self.state()), system=LOG_SYSTEM)

        dt_now = datetime.datetime.utcnow()

        assert transition_time > dt_now

        td = (transition_time - dt_now)
        transition_delta_seconds = (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6.0

        d = task.deferLater(reactor, transition_delta_seconds, _switchState, self, state)
        d.addErrback(deferTaskFailed)
        self.scheduled_transition_call = d
        log.msg('State transition scheduled: In %i seconds to state %s' % (transition_delta_seconds, state), system=LOG_SYSTEM)


    def _logProcessPipes(self, process_proto):
        log.msg('STDOUT:\n%s' % process_proto.stdout.getvalue(), debug=True, system=LOG_SYSTEM)
        log.msg('STDERR:\n%s' % process_proto.stderr.getvalue(), debug=True, system=LOG_SYSTEM)


    def _cancelTransition(self):

        self.scheduled_transition_call.cancel()
        self.scheduled_transition_call = None


    def _constructReservationPayload(self):

        sp = self.service_parameters
        bw = sp.bandwidth

        root = ET.Element('reservationParameters')

        ET.SubElement(root, 'sourceEP').text = self.source_port
        ET.SubElement(root, 'destEP').text = self.dest_port

        bandwidth = ET.SubElement(root, 'bandwidth')
        ET.SubElement(bandwidth, 'desired').text = str(bw.desired)
        if bw.minimum:
            ET.SubElement(bandwidth, 'minimum').text = str(bw.minimum)
        if bw.maximum:
            ET.SubElement(bandwidth, 'maximum').text = str(bw.maximum)

        schedule = ET.SubElement(root, 'schedule')
        ET.SubElement(schedule, 'startTime').text = sp.start_time.isoformat() + 'Z'
        ET.SubElement(schedule, 'endTime').text = sp.end_time.isoformat() + 'Z'
        payload = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root)
        return payload


    def reserve(self):

        log.msg('RESERVE. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)

        try:
            self.state.switchState(state.RESERVING)
        except error.StateTransitionError:
            return defer.fail(error.ReserveError('Cannot reserve connection in state %s' % self.state()))

        payload =  self._constructReservationPayload() #self.source_port, self.dest_port, self.service_parameters)
        process_proto = ArgiaProcessProtocol(payload)

        try:
            reactor.spawnProcess(process_proto, ARGIA_CLIENT, [COMMAND_BIN, ARGIA_CMD_RESERVE], path=COMMAND_DIR)
        except OSError, e:
            return defer.fail(error.ReserverError('Failed to invoke argia control command (%s)' % str(e)))

        d = defer.Deferred()

        def reservationConfirmed(_, pp):
            log.msg('Received reservation reply from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                tree = ET.parse(pp.stdout)
                argia_state = list(tree.getiterator('state'))[0].text
                reservation_id = list(tree.getiterator('reservationId'))[0].text

                if argia_state == ARGIA_RESERVED:
                    self.argia_id = reservation_id
                    self.state.switchState(state.RESERVED)
                    self._scheduleStateTransition(self.service_parameters.start_time, state.SCHEDULED)
                    d.callback(self)
                else:
                    d.errback( error.ReserveError('Got unexpected state from Argia (%s)' % argia_state) )

            except Exception, e:
                log.msg('Error handling reservation reply: %s' % str(e), system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.ReserveError('Error handling reservation reply: %s' % str(e)) )


        def reservationFailed(err, pp):
            log.msg('Received reservation failure from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                self.state.switchState(state.TERMINATED)
                tree = ET.parse(pp.stderr)
                message = list(tree.getiterator('message'))[0].text
                d.errback( error.ReserveError('Reservation failed in Argia backend: %s' % message) )
            except Exception, e:
                log.msg('Error handling reservation failure: %s' % str(e), system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.ReserveError('Error handling reservation failure: %s' % str(e)) )

        process_proto.d.addCallbacks(reservationConfirmed, reservationFailed, callbackArgs=[process_proto], errbackArgs=[process_proto])
        return d


    def provision(self):

        dt_now = datetime.datetime.utcnow()

        if self.service_parameters.end_time <= dt_now:
            return defer.fail(error.ProvisionError('Cannot provision connection after end time (end time: %s, current time: %s).' % (self.service_parameters.end_time, dt_now)))

        # Argia can schedule, so we don't have to

#        elif conn.start_time > dt_now:
#            td = conn.start_time - dt_now
#            # total_seconds() is only available from python 2.7 so we use this
#            start_delta_seconds = (td.microseconds + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6.0
#
#            conn.auto_provision_deferred = task.deferLater(reactor, start_delta_seconds, doProvision, conn)
#            conn.auto_provision_deferred.addErrback(deferTaskFailed)
#            conn.state = AUTO_PROVISION
#            log.msg('Connection %s scheduled for auto-provision in %i seconds ' % (conn_id, start_delta_seconds), system=LOG_SYSTEM)

        log.msg('Provisioning connection. Start time: %s, Current time: %s).' % (self.service_parameters.start_time, dt_now), system=LOG_SYSTEM)

        self.state.switchState(state.PROVISIONING)
        d = defer.Deferred()

        def provisionConfirmed(_, pp):
            log.msg('Received provision reply from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                tree = ET.parse(pp.stdout)
                argia_state = list(tree.getiterator('state'))[0].text
                argia_id    = list(tree.getiterator('reservationId'))[0].text

                if argia_state not in (ARGIA_PROVISIONED, ARGIA_AUTO_PROVISION):
                    d.errback( error.ReserveError('Got unexpected state from Argia (%s)' % argia_state) )
                else:
                    self._cancelTransition()
                    self.state.switchState(state.PROVISIONED)
                    self.argia_id = argia_id
                    log.msg('Connection provisioned. CID: %s' % id(self), system=LOG_SYSTEM)
                    self._scheduleStateTransition(self.service_parameters.start_time, state.TERMINATED)
                    d.callback(self)

            except Exception, e:
                log.msg('Error handling provision reply: %s' % str(e), system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.ReserveError('Error handling reservation reply: %s' % str(e)) )

        def provisionFailed(err, pp):
            log.msg('Received provision failure from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                tree = ET.parse(pp.stderr)
                state = list(tree.getiterator('message'))[0].text
                message = list(tree.getiterator('message'))[0].text

                if state == ARGIA_TERMINATED:
                    self.state.switchState(state.TERMINATED)
                elif state == ARGIA_RESERVED:
                    self.state.switchState(state.RESERVED)
                else:
                    log.msg('Unexpected state from argia provision failure: %s' % state, system=LOG_SYSTEM)

                d.errback( error.ProvisionError(message) )
            except Exception, e:
                log.msg('Error handling provision failure: %s' % str(e), system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.ProvisionError('Error handling reservation failure: %s' % str(e)) )

        process_proto = ArgiaProcessProtocol()
        try:
            reactor.spawnProcess(process_proto, ARGIA_CLIENT, args=[COMMAND_BIN, ARGIA_CMD_PROVISION, self.argia_id], path=COMMAND_DIR)
        except OSError, e:
            return defer.fail(error.ReserverError('Failed to invoke argia control command (%s)' % str(e)))
        process_proto.d.addCallbacks(provisionConfirmed, provisionFailed, callbackArgs=[process_proto], errbackArgs=[process_proto])

        return d


    def release(self):

        log.msg('Releasing connection. CID %s' % id(self), system=LOG_SYSTEM)

        try:
            self.state.switchState(state.RELEASING)
        except error.StateTransitionError:
            return defer.fail(error.ProvisionError('Cannot release connection in state %s' % self.state()))

        d = defer.Deferred()

        def releaseConfirmed(_, pp):
            log.msg('Received reservation reply from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                tree = ET.parse(process_proto.stdout)
                argia_state = list(tree.getiterator('state'))[0].text
                reservation_id = list(tree.getiterator('reservationId'))[0].text

                if argia_state in (ARGIA_SCHEDULED):
                    self._cancelTransition()
                    self.state.switchState(state.SCHEDULED)
                    self.argia_id = reservation_id
                    d.callback(self)
                else:
                    d.errback( error.ReleaseError('Got unexpected state from Argia (%s)' % argia_state) )
            except Exception, e:
                log.msg('Error handling release reply: %s' % str(e), system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.ReleaseError('Error handling release reply: %s' % str(e)) )

        def releaseFailed(err, pp):
            log.msg('Received reservation failure from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                tree = ET.parse(process_proto.stderr)
                message = list(tree.getiterator('message'))[0].text
                argia_state = list(tree.getiterator('state'))[0].text

                if argia_state == ARGIA_PROVISIONED:
                    self.state.switchState(state.PROVISIONED)
                elif argia_state == ARGIA_TERMINATED:
                    self.state.switchState(state.TERMINATED)
                else:
                    log.msg('Unknown state returned from Argia in release faliure (%s)' % message, system=LOG_SYSTEM)

                d.errback( error.ReleaseError('Error releasing connection: %s' % str(err)) )

            except Exception, e:
                log.msg('Error handling release failure: %s' % str(e), system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.ReleaseError('Error handling release failure: %s' % str(e)) )


        process_proto = ArgiaProcessProtocol()
        try:
            reactor.spawnProcess(process_proto, ARGIA_CLIENT, args=[COMMAND_BIN, ARGIA_CMD_RELEASE, self.argia_id], path=COMMAND_DIR)
        except OSError, e:
            return defer.fail(error.ReleaseError('Failed to invoke argia control command (%s)' % str(e)))
        process_proto.d.addCallbacks(releaseConfirmed, releaseFailed, callbackArgs=[process_proto], errbackArgs=[process_proto])

        return d


    def terminate(self):

        log.msg('Terminating reservation. CID %s' % id(self), system=LOG_SYSTEM)

        try:
            self.state.switchState(state.TERMINATING)
        except error.StateTransitionError:
            return defer.fail(error.TerminateError('Cannot terminate connection in state %s' % self.state()))

        d = defer.Deferred()

        def terminateConfirmed(_, pp):
            log.msg('Received terminate reply from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                tree = ET.parse(pp.stdout)
                argia_state = list(tree.getiterator('state'))[0].text
                if argia_state == ARGIA_TERMINATED:
                    self._cancelTransition()
                    self.state.switchState(state.TERMINATED)
                    self.argia_id = None
                    d.callback(self)
                else:
                    d.errback( error.TerminateError('Got unexpected state from Argia (%s)' % argia_state) )
            except Exception, e:
                log.msg('Error handling termination reply: %s' % str(e), system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.TerminateError('Error handling termination reply: %s' % str(e)) )

        def terminateFailed(err, pp):
            log.msg('Received terminate failure from Argia. CID: %s, Ports: %s -> %s' % (id(self), self.source_port, self.dest_port), system=LOG_SYSTEM)
            try:
                tree = ET.parse(pp.stderr)
                message = list(tree.getiterator('message'))[0].text
                argia_state = list(tree.getiterator('state'))[0].text
                if argia_state == ARGIA_PROVISIONED:
                    self.state.switchState(state.PROVISIONED)
                elif argia_state == ARGIA_TERMINATED:
                    self.state.switchState(state.TERMINATED)
                else:
                    log.msg('Unknown state returned from Argia in terminate faliure', system=LOG_SYSTEM)
                d.errback( error.TerminateError('Error terminating connection: %s' % str(message)) )
            except Exception, e:
                log.msg('Error terminating connection in Argia: %s' % message, system=LOG_SYSTEM)
                self._logProcessPipes(pp)
                d.errback( error.TerminateError('Error handling termination failure: %s' % str(e)) )

        process_proto = ArgiaProcessProtocol()
        try:
            reactor.spawnProcess(process_proto, ARGIA_CLIENT, args=[COMMAND_BIN, ARGIA_CMD_TERMINATE, self.argia_id], path=COMMAND_DIR)
        except OSError, e:
            return defer.fail(error.TerminateError('Failed to invoke argia control command (%s)' % str(e)))
        process_proto.d.addCallbacks(terminateConfirmed, terminateFailed, callbackArgs=[process_proto], errbackArgs=[process_proto])

        return d


    def query(self, query_filter):
        pass


