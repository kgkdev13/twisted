# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.protocols.tls}.
"""

from __future__ import division, absolute_import

from zope.interface.verify import verifyObject
from zope.interface import Interface, directlyProvides

from twisted.python.compat import intToBytes, iterbytes
try:
    from OpenSSL.crypto import X509Type
    from OpenSSL.SSL import (TLSv1_METHOD, Error, ConnectionType,
                             WantReadError, Context)
except ImportError:
    # Skip the whole test module if it can't be imported.
    skip = "pyOpenSSL 0.10 or newer required for twisted.protocol.tls"
else:
    # Otherwise, the pyOpenSSL dependency must be satisfied, so all these
    # imports will work.
    from twisted.protocols.tls import TLSMemoryBIOProtocol, TLSMemoryBIOFactory
    from twisted.protocols.tls import _PullToPush, _ProducerMembrane
    from twisted.internet.ssl import PrivateCertificate, CertificateOptions
    from twisted.test.ssl_helpers import (ClientTLSContext, ServerTLSContext,
                                          certPath)
from twisted.test.ssl_helpers import connectedTLSForTest
from twisted.internet.interfaces import IProtocol
from zope.interface.declarations import implementer
from twisted.test.iosim import connectedServerAndClient
from twisted.test.proto_helpers import AccumulatingProtocol

from twisted.python.filepath import FilePath
from twisted.python.failure import Failure
from twisted.python import log
from twisted.internet.interfaces import ISystemHandle, ISSLTransport
from twisted.internet.interfaces import IPushProducer
from twisted.internet.main import CONNECTION_DONE
from twisted.internet.error import ConnectionDone, ConnectionLost
from twisted.internet.defer import Deferred
from twisted.internet.protocol import Protocol, ClientFactory, ServerFactory
from twisted.internet.task import TaskStopped
from twisted.protocols.loopback import loopbackAsync
from twisted.trial.unittest import TestCase
from twisted.test.test_tcp import ConnectionLostNotifyingProtocol
from twisted.test.proto_helpers import StringTransport



class HandshakeCallbackContextFactory(object):
    """
    L{HandshakeCallbackContextFactory} is a factory for SSL contexts which
    allows applications to get notification when the SSL handshake completes.

    @ivar _finished: A L{Deferred} which will be called back when the handshake
        is done.
    """

    # pyOpenSSL needs to expose this.
    # https://bugs.launchpad.net/pyopenssl/+bug/372832
    SSL_CB_HANDSHAKE_DONE = 0x20

    def __init__(self):
        self._finished = Deferred()


    def factoryAndDeferred(cls):
        """
        Create a new L{HandshakeCallbackContextFactory} and return a two-tuple
        of it and a L{Deferred} which will fire when a connection created with
        it completes a TLS handshake.
        """
        contextFactory = cls()
        return contextFactory, contextFactory._finished
    factoryAndDeferred = classmethod(factoryAndDeferred)


    def _info(self, connection, where, ret):
        """
        This is the "info callback" on the context.  It will be called
        periodically by pyOpenSSL with information about the state of a
        connection.  When it indicates the handshake is complete, it will fire
        C{self._finished}.
        """
        if where & self.SSL_CB_HANDSHAKE_DONE:
            self._finished.callback(None)


    def getContext(self):
        """
        Create and return an SSL context configured to use L{self._info} as the
        info callback.
        """
        context = Context(TLSv1_METHOD)
        context.set_info_callback(self._info)
        return context






def buildTLSProtocol(server=False, transport=None):
    """
    Create a protocol hooked up to a TLS transport hooked up to a
    StringTransport.
    """
    # We want to accumulate bytes without disconnecting, so set high limit:
    clientProtocol = AccumulatingProtocol()
    clientFactory = ClientFactory()
    clientFactory.protocol = lambda: clientProtocol

    if server:
        contextFactory = ServerTLSContext()
    else:
        contextFactory = ClientTLSContext()
    wrapperFactory = TLSMemoryBIOFactory(
        contextFactory, not server, clientFactory)
    sslProtocol = wrapperFactory.buildProtocol(None)

    if transport is None:
        transport = StringTransport()
    sslProtocol.makeConnection(transport)
    return clientProtocol, sslProtocol



class TLSMemoryBIOFactoryTests(TestCase):
    """
    Ensure TLSMemoryBIOFactory logging acts correctly.
    """

    def test_quiet(self):
        """
        L{TLSMemoryBIOFactory.doStart} and L{TLSMemoryBIOFactory.doStop} do
        not log any messages.
        """
        contextFactory = ServerTLSContext()

        logs = []
        logger = logs.append
        log.addObserver(logger)
        self.addCleanup(log.removeObserver, logger)
        wrappedFactory = ServerFactory()
        # Disable logging on the wrapped factory:
        wrappedFactory.doStart = lambda: None
        wrappedFactory.doStop = lambda: None
        factory = TLSMemoryBIOFactory(contextFactory, False, wrappedFactory)
        factory.doStart()
        factory.doStop()
        self.assertEqual(logs, [])


    def test_logPrefix(self):
        """
        L{TLSMemoryBIOFactory.logPrefix} amends the wrapped factory's log prefix
        with a short string (C{"TLS"}) indicating the wrapping, rather than its
        full class name.
        """
        contextFactory = ServerTLSContext()
        factory = TLSMemoryBIOFactory(contextFactory, False, ServerFactory())
        self.assertEqual("ServerFactory (TLS)", factory.logPrefix())


    def test_logPrefixFallback(self):
        """
        If the wrapped factory does not provide L{ILoggingContext},
        L{TLSMemoryBIOFactory.logPrefix} uses the wrapped factory's class name.
        """
        class NoFactory(object):
            pass

        contextFactory = ServerTLSContext()
        factory = TLSMemoryBIOFactory(contextFactory, False, NoFactory())
        self.assertEqual("NoFactory (TLS)", factory.logPrefix())



class TLSMemoryBIOTests(TestCase):
    """
    Tests for the implementation of L{ISSLTransport} which runs over another
    L{ITransport}.
    """

    def test_interfaces(self):
        """
        L{TLSMemoryBIOProtocol} instances provide L{ISSLTransport} and
        L{ISystemHandle}.
        """
        proto = TLSMemoryBIOProtocol(None, None)
        self.assertTrue(ISSLTransport.providedBy(proto))
        self.assertTrue(ISystemHandle.providedBy(proto))


    def test_wrappedProtocolInterfaces(self):
        """
        L{TLSMemoryBIOProtocol} instances provide the interfaces provided by
        the transport they wrap.
        """
        class ITransport(Interface):
            pass

        class MyTransport(object):
            def write(self, bytes):
                pass

        clientFactory = ClientFactory()
        contextFactory = ClientTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            contextFactory, True, clientFactory)

        transport = MyTransport()
        directlyProvides(transport, ITransport)
        tlsProtocol = TLSMemoryBIOProtocol(wrapperFactory, Protocol())
        tlsProtocol.makeConnection(transport)
        self.assertTrue(ITransport.providedBy(tlsProtocol))


    def test_getHandle(self):
        """
        L{TLSMemoryBIOProtocol.getHandle} returns the L{OpenSSL.SSL.Connection}
        instance it uses to actually implement TLS.

        This may seem odd.  In fact, it is.  The L{OpenSSL.SSL.Connection} is
        not actually the "system handle" here, nor even an object the reactor
        knows about directly.  However, L{twisted.internet.ssl.Certificate}'s
        C{peerFromTransport} and C{hostFromTransport} methods depend on being
        able to get an L{OpenSSL.SSL.Connection} object in order to work
        properly.  Implementing L{ISystemHandle.getHandle} like this is the
        easiest way for those APIs to be made to work.  If they are changed,
        then it may make sense to get rid of this implementation of
        L{ISystemHandle} and return the underlying socket instead.
        """
        factory = ClientFactory()
        contextFactory = ClientTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(contextFactory, True, factory)
        proto = TLSMemoryBIOProtocol(wrapperFactory, Protocol())
        transport = StringTransport()
        proto.makeConnection(transport)
        self.assertIsInstance(proto.getHandle(), ConnectionType)


    def test_makeConnection(self):
        """
        When L{TLSMemoryBIOProtocol} is connected to a transport, it connects
        the protocol it wraps to a transport.
        """
        clientProtocol = Protocol()
        clientFactory = ClientFactory()
        clientFactory.protocol = lambda: clientProtocol

        contextFactory = ClientTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            contextFactory, True, clientFactory)
        sslProtocol = wrapperFactory.buildProtocol(None)

        transport = StringTransport()
        sslProtocol.makeConnection(transport)

        self.assertNotIdentical(clientProtocol.transport, None)
        self.assertNotIdentical(clientProtocol.transport, transport)
        self.assertIdentical(clientProtocol.transport, sslProtocol)


    def test_handshake(self):
        """
        The TLS handshake is performed when L{TLSMemoryBIOProtocol} is
        connected to a transport.
        """


    def test_handshakeFailure(self):
        """
        L{TLSMemoryBIOProtocol} reports errors in the handshake process to the
        application-level protocol object using its C{connectionLost} method
        and disconnects the underlying transport.
        """
        clientConnectionLost = Deferred()
        clientFactory = ClientFactory()
        clientFactory.protocol = (
            lambda: ConnectionLostNotifyingProtocol(
                clientConnectionLost))

        clientContextFactory = CertificateOptions(method=TLSv1_METHOD)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverConnectionLost = Deferred()
        serverFactory = ServerFactory()
        serverFactory.protocol = (
            lambda: ConnectionLostNotifyingProtocol(
                serverConnectionLost))

        # This context factory rejects any clients which do not present a
        # certificate.
        certificateData = FilePath(certPath).getContent()
        certificate = PrivateCertificate.loadPEM(certificateData)
        serverContextFactory = certificate.options(certificate)
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        def cbConnectionLost(protocol):
            # The connection should close on its own in response to the error
            # induced by the client not supplying the required certificate.
            # After that, check to make sure the protocol's connectionLost was
            # called with the right thing.
            protocol.lostConnectionReason.trap(Error)
        clientConnectionLost.addCallback(cbConnectionLost)
        serverConnectionLost.addCallback(cbConnectionLost)

        c, s, p = connectedServerAndClient(
            lambda: sslServerProtocol,
            lambda: sslClientProtocol
        )

        # Additionally, the underlying transport should have been told to
        # go away.
        self.assertTrue(c.transport.disconnected)
        self.assertTrue(s.transport.disconnected)


    def test_handshakeSucceeded(self):
        """
        When the TLS handshake succeeds, it invokes C{handshakeSucceeded} on
        its L{IProtocol}.
        """
        tlsClient, tlsServer, pump = connectedTLSForTest(
            self, kickoff=False,
            clientProtocol=Protocol, serverProtocol=Protocol,
        )
        def clientHandshook():
            clientHandshook.done = True
        clientHandshook.done = False
        def serverHandshook():
            serverHandshook.done = True
        serverHandshook.done = False
        tlsClient.wrappedProtocol.handshakeCompleted = clientHandshook
        tlsServer.wrappedProtocol.handshakeCompleted = serverHandshook

        # https://en.wikipedia.org/wiki/Transport_Layer_Security#Basic_TLS_handshake
        # Server receives ClientHello
        self.assertTrue(pump.pump())
        self.assertFalse(clientHandshook.done)
        self.assertFalse(serverHandshook.done)
        # Client receives ServerHello / Certificate / ServerHelloDone
        self.assertTrue(pump.pump())
        self.assertFalse(clientHandshook.done)
        self.assertFalse(serverHandshook.done)
        # Server receives ClientKeyExchange / ChangeCipherSpec; handshake then
        # complete from server's perspective.
        self.assertTrue(pump.pump())
        self.assertFalse(clientHandshook.done)
        self.assertTrue(serverHandshook.done)
        # Client receives ChangeCipherSpec; handshake now complete on both
        # sides.
        self.assertTrue(pump.pump())
        self.assertTrue(clientHandshook.done)
        self.assertTrue(serverHandshook.done)
        # If the above description of the handshake is correct, there is no
        # application data on this protocol and the wire should now be
        # quiescent.  As a sanity check, let's make sure that is in fact true.
        self.assertFalse(pump.flush())


    def test_handshakeSucceededOldProtocol(self):
        """
        When the TLS handshake succeeds, a protocol which does not declare that
        it provides L{ITLSProtocol} will not have C{handshakeSucceeded} invoked
        upon it.
        """
        @implementer(IProtocol)
        class JustTheInterfacePlease(object):
            handshook = False
            def makeConnection(self, transport):
                pass

            def dataReceived(self, data):
                pass

            def connectionLost(self, reason):
                pass

            def handshakeCompleted(self):
                self.handshook = True

        c, s, pump = connectedTLSForTest(self,
                                         clientProtocol=JustTheInterfacePlease,
                                         serverProtocol=JustTheInterfacePlease)

        # Handshake all the way.
        pump.flush()

        self.assertEquals(c.wrappedProtocol.handshook, False)
        self.assertEquals(c.wrappedProtocol.handshook, False)


    def test_getPeerCertificate(self):
        """
        L{TLSMemoryBIOProtocol.getPeerCertificate} returns the
        L{OpenSSL.crypto.X509Type} instance representing the peer's
        certificate.
        """
        # Set up a client and server so there's a certificate to grab.
        clientFactory = ClientFactory()
        clientFactory.protocol = Protocol

        clientContextFactory = CertificateOptions(method=TLSv1_METHOD)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverFactory = ServerFactory()
        serverFactory.protocol = Protocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        # Wait for the handshake
        def cbHandshook():
            # Grab the server's certificate and check it out
            cbHandshook.cert = sslClientProtocol.getPeerCertificate()
        cbHandshook.cert = None
        sslClientProtocol.wrappedProtocol.handshakeCompleted = cbHandshook

        c, s, p = connectedServerAndClient(
            lambda: sslServerProtocol,
            lambda: sslClientProtocol,
        )

        p.flush()

        self.assertIsInstance(cbHandshook.cert, X509Type)
        self.assertEqual(
            cbHandshook.cert.digest('md5'),
            b'9B:A4:AB:43:10:BE:82:AE:94:3E:6B:91:F2:F3:40:E8'
        )


    def writeBeforeHandshakeTest(self, sendingProtocol, octets):
        """
        Run test where client sends data before handshake, given the sending
        protocol and expected bytes.
        """
        clientFactory = ClientFactory()
        clientFactory.protocol = sendingProtocol

        clientContextFactory, handshakeDeferred = (
            HandshakeCallbackContextFactory.factoryAndDeferred()
        )
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol()
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        c, s, p = connectedServerAndClient(
            lambda: sslServerProtocol,
            lambda: sslClientProtocol,
        )

        self.assertEqual(serverProtocol.data, octets)


    def test_writeAfterHandshake(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} before the handshake is
        complete are received by the protocol on the other side of the
        connection once the handshake succeeds.
        """
        octets = b"some octets"

        clientProtocol = Protocol()
        clientFactory = ClientFactory()
        clientFactory.protocol = lambda: clientProtocol

        clientContextFactory = CertificateOptions(method=TLSv1_METHOD)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol()
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        # Wait for the handshake to finish before writing anything.
        def cbHandshook():
            clientProtocol.transport.write(octets)
        clientProtocol.handshakeCompleted = cbHandshook

        connectedServerAndClient(lambda: sslServerProtocol,
                                 lambda: sslClientProtocol)

        # Once the connection is lost, make sure the server received the
        # expected octets.
        self.assertEqual(serverProtocol.data, octets)


    def test_writeBeforeHandshake(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} before the handshake is
        complete are received by the protocol on the other side of the
        connection once the handshake succeeds.
        """
        bytes = b"some bytes"

        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.write(bytes)

        self.writeBeforeHandshakeTest(SimpleSendingProtocol, bytes)


    def test_writeSequence(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} with C{writeSequence} are
        received by the protocol on the other side of the connection.
        """
        bytes = b"some bytes"
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.writeSequence(list(iterbytes(bytes)))
        self.writeBeforeHandshakeTest(SimpleSendingProtocol, bytes)


    def test_writeAfterLoseConnection(self):
        """
        Bytes written to L{TLSMemoryBIOProtocol} after C{loseConnection} is
        called are not transmitted (unless there is a registered producer,
        which will be tested elsewhere).
        """
        bytes = b"some bytes"
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.write(bytes)
                self.transport.loseConnection()
                self.transport.write(b"hello")
                self.transport.writeSequence([b"world"])
        self.writeBeforeHandshakeTest(SimpleSendingProtocol, bytes)


    def test_writeUnicodeRaisesTypeError(self):
        """
        Writing C{unicode} to L{TLSMemoryBIOProtocol} throws a C{TypeError}.
        """
        notBytes = u"hello"
        result = []
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                try:
                    self.transport.write(notBytes)
                except TypeError:
                    result.append(True)
                self.transport.write(b"bytes")
                self.transport.loseConnection()
        self.writeBeforeHandshakeTest(SimpleSendingProtocol, b"bytes")
        self.assertEqual(result, [True])


    def test_multipleWrites(self):
        """
        If multiple separate TLS messages are received in a single chunk from
        the underlying transport, all of the application bytes from each
        message are delivered to the application-level protocol.
        """
        octets = [b'a', b'b', b'c', b'd', b'e', b'f', b'g', b'h', b'i']
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                for b in octets:
                    self.transport.write(b)

        clientFactory = ClientFactory()
        clientFactory.protocol = SimpleSendingProtocol

        clientContextFactory = CertificateOptions(method=TLSv1_METHOD)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol()
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectedServerAndClient(lambda: sslServerProtocol,
                                 lambda: sslClientProtocol)

        # Wait for the connection to end, then make sure the server received
        # the bytes sent by the client.
        self.assertEqual(serverProtocol.data, b''.join(octets))


    def test_hugeWrite(self):
        """
        If a very long string is passed to L{TLSMemoryBIOProtocol.write}, any
        trailing part of it which cannot be send immediately is buffered and
        sent later.
        """
        octets = b"some octets"
        factor = 8192
        class SimpleSendingProtocol(Protocol):
            def connectionMade(self):
                self.transport.write(octets * factor)

        clientFactory = ClientFactory()
        clientFactory.protocol = SimpleSendingProtocol

        clientContextFactory = CertificateOptions(method=TLSv1_METHOD)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol()
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        connectedServerAndClient(lambda: sslServerProtocol,
                                 lambda: sslClientProtocol)
        self.assertEqual(serverProtocol.data, octets * factor)


    def test_disorderlyShutdown(self):
        """
        If a L{TLSMemoryBIOProtocol} loses its connection unexpectedly, this is
        reported to the application.
        """
        clientConnectionLost = Deferred()
        clientFactory = ClientFactory()
        clientFactory.protocol = (
            lambda: ConnectionLostNotifyingProtocol(
                clientConnectionLost))

        clientContextFactory = CertificateOptions(method=TLSv1_METHOD)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        # Client speaks first, so the server can be dumb.
        serverProtocol = Protocol()

        loopbackAsync(serverProtocol, sslClientProtocol)

        # Now destroy the connection.
        serverProtocol.transport.loseConnection()

        # And when the connection completely dies, check the reason.
        def cbDisconnected(clientProtocol):
            clientProtocol.lostConnectionReason.trap(Error)
        clientConnectionLost.addCallback(cbDisconnected)
        return clientConnectionLost


    def test_loseConnectionAfterHandshake(self):
        """
        L{TLSMemoryBIOProtocol.loseConnection} sends a TLS close alert and
        shuts down the underlying connection cleanly on both sides, after
        transmitting all buffered data.
        """
        clientFactory = ClientFactory()
        clientProtocol = AccumulatingProtocol()
        clientFactory.protocol = lambda: clientProtocol

        clientContextFactory = CertificateOptions(method=TLSv1_METHOD)
        wrapperFactory = TLSMemoryBIOFactory(
            clientContextFactory, True, clientFactory)
        sslClientProtocol = wrapperFactory.buildProtocol(None)

        serverProtocol = AccumulatingProtocol()
        serverFactory = ServerFactory()
        serverFactory.protocol = lambda: serverProtocol

        serverContextFactory = ServerTLSContext()
        wrapperFactory = TLSMemoryBIOFactory(
            serverContextFactory, False, serverFactory)
        sslServerProtocol = wrapperFactory.buildProtocol(None)

        chunkOfBytes = b"123456890" * 100000

        # Wait for the handshake before dropping the connection.
        def cbHandshake():
            # Write more than a single bio_read, to ensure client will still
            # have some data it needs to write when it receives the TLS close
            # alert, and that simply doing a single bio_read won't be
            # sufficient. Thus we will verify that any amount of buffered data
            # will be written out before the connection is closed, rather than
            # just small amounts that can be returned in a single bio_read:
            clientProtocol.transport.write(chunkOfBytes)
            serverProtocol.transport.loseConnection()
            # Now wait for the client and server to notice.

        clientProtocol.handshakeCompleted = cbHandshake
        connectedServerAndClient(lambda: sslClientProtocol,
                                 lambda: sslServerProtocol)

        # Wait for the connection to end, then make sure the client and server
        # weren't notified of a handshake failure that would cause the test to
        # fail.
        clientProtocol.closedReason.trap(ConnectionDone)
        serverProtocol.closedReason.trap(ConnectionDone)

        # The server should have received all bytes sent by the client:
        self.assertEqual(b"".join(serverProtocol.data), chunkOfBytes)

        # The server should have closed its underlying transport, in addition
        # to whatever it did to shut down the TLS layer.
        self.assertTrue(serverProtocol.transport.disconnected)

        # The client should also have closed its underlying transport once it
        # saw the server shut down the TLS layer, so as to avoid relying on the
        # server to close the underlying connection.
        self.assertTrue(clientProtocol.transport.disconnected)


    def _connectionLostOnlyAfterUnderlyingCloses(self, handshake):
        """
        Assert that the user protocol's connectionLost is only called when
        transport underlying TLS connection has been disconnected.

        @param handshake: If L{True} then the TLS handshake will succeed before
            the underlying TLS connection is disconnected.  If C{False} then
            the TLS handshake will fail before the underlying TLS connection is
            disconnected.
        """
        class LostProtocol(Protocol):
            disconnected = None
            def connectionLost(self, reason):
                self.disconnected = reason
        wrapperFactory = TLSMemoryBIOFactory(ClientTLSContext(),
                                             True, ClientFactory())
        protocol = LostProtocol()
        tlsProtocol = TLSMemoryBIOProtocol(wrapperFactory, protocol)
        transport = StringTransport()
        tlsProtocol.makeConnection(transport)

        if handshake:
            # Pretend TLS shutdown finished cleanly; the underlying transport
            # should be told to close, but the user protocol should not yet be
            # notified:
            expected = ConnectionDone
            tlsProtocol._tlsShutdownFinished(None)
        else:
            # Pretend the TLS handshake failed.
            expected = Error
            tlsProtocol._tlsShutdownFinished(Failure(Error()))

        # At this point the transport should have been told to disconnect ...
        self.assertEqual(transport.disconnecting, True)
        # ... but the application shouldn't have been told that it is
        # disconnected yet.
        self.assertEqual(protocol.disconnected, None)

        # Now close the underlying connection; the user protocol should be
        # notified with the given reason (since TLS closed cleanly):
        tlsProtocol.connectionLost(Failure(CONNECTION_DONE))

        self.assertTrue(protocol.disconnected.check(expected))


    def test_connectionLostOnlyAfterUnderlyingClosesHandshakeOK(self):
        """
        If the handshake succeeds and then the underlying connection closes
        then the reason passed to the application protocol is the reason for the
        underlying transport closing.
        """
        return self._connectionLostOnlyAfterUnderlyingCloses(True)


    def test_connectionLostOnlyAfterUnderlyingClosesHandshakeFailed(self):
        """
        If the handshake fails and then the underlying connection closes then
        the reason passed to the application protocol is the reason for the
        handshake failure.
        """
        return self._connectionLostOnlyAfterUnderlyingCloses(False)


    def test_loseConnectionTwice(self):
        """
        If TLSMemoryBIOProtocol.loseConnection is called multiple times, all
        but the first call have no effect.
        """
        wrapperFactory = TLSMemoryBIOFactory(ClientTLSContext(),
                                             True, ClientFactory())
        tlsProtocol = TLSMemoryBIOProtocol(wrapperFactory, Protocol())
        transport = StringTransport()
        tlsProtocol.makeConnection(transport)
        self.assertEqual(tlsProtocol.disconnecting, False)

        # Make sure loseConnection calls _shutdownTLS the first time (mostly
        # to make sure we've overriding it correctly):
        calls = []
        def _shutdownTLS(shutdown=tlsProtocol._shutdownTLS):
            calls.append(1)
            return shutdown()
        tlsProtocol._shutdownTLS = _shutdownTLS
        tlsProtocol.loseConnection()
        self.assertEqual(tlsProtocol.disconnecting, True)
        self.assertEqual(calls, [1])

        # Make sure _shutdownTLS isn't called a second time:
        tlsProtocol.loseConnection()
        self.assertEqual(calls, [1])


    def test_unexpectedEOF(self):
        """
        Unexpected disconnects get converted to ConnectionLost errors.
        """
        tlsClient, tlsServer, pump = connectedTLSForTest(self, kickoff=False)
        serverProtocol = tlsServer.wrappedProtocol

        # Write data, then disconnect *underlying* transport, resulting in an
        # unexpected TLS disconnect:
        def handshakeDone():
            tlsClient.write(b"hello")
            tlsClient.transport.loseConnection()
        tlsClient.wrappedProtocol.handshakeCompleted = handshakeDone
        pump.flush()

        # Receiver should be disconnected, with ConnectionLost notification
        # (masking the Unexpected EOF SSL error):
        reason = serverProtocol.lostReason
        self.assertTrue(reason.check(ConnectionLost), reason)


    def test_errorWriting(self):
        """
        Errors while writing cause the protocols to be disconnected.
        """
        tlsClient, tlsServer, pump = connectedTLSForTest(self, kickoff=False)

        # Pretend TLS connection is unhappy sending:
        class Wrapper(object):
            def __init__(self, wrapped):
                self._wrapped = wrapped
            def __getattr__(self, attr):
                return getattr(self._wrapped, attr)
            def send(self, *args):
                raise Error("ONO!")
        tlsClient._tlsConnection = Wrapper(tlsClient._tlsConnection)

        # Write some data:
        def handshakeDone():
            handshakeDone.yes = True
            tlsClient.write(b"hello")
        handshakeDone.yes = False
        tlsClient.wrappedProtocol.handshakeCompleted = handshakeDone

        pump.flush()

        reason = tlsClient.wrappedProtocol.lostReason
        self.assertNotIdentical(reason, None, "Connection not lost.")
        # Failed writer should be disconnected with SSL error:
        self.assertTrue(reason.check(Error), reason)



class TLSProducerTests(TestCase):
    """
    The TLS transport must support the IConsumer interface.
    """

    def setupStreamingProducer(self, transport=None):
        class HistoryStringTransport(StringTransport):
            def __init__(self):
                StringTransport.__init__(self)
                self.producerHistory = []

            def pauseProducing(self):
                self.producerHistory.append("pause")
                StringTransport.pauseProducing(self)

            def resumeProducing(self):
                self.producerHistory.append("resume")
                StringTransport.resumeProducing(self)

            def stopProducing(self):
                self.producerHistory.append("stop")
                StringTransport.stopProducing(self)

        clientProtocol, tlsProtocol = buildTLSProtocol(transport=transport)
        producer = HistoryStringTransport()
        clientProtocol.transport.registerProducer(producer, True)
        self.assertEqual(tlsProtocol.transport.streaming, True)
        return clientProtocol, tlsProtocol, producer


    def flushTwoTLSProtocols(self, tlsProtocol, serverTLSProtocol):
        """
        Transfer bytes back and forth between two TLS protocols.
        """
        # We want to make sure all bytes are passed back and forth; JP
        # estimated that 3 rounds should be enough:
        for i in range(3):
            clientData = tlsProtocol.transport.value()
            if clientData:
                serverTLSProtocol.dataReceived(clientData)
                tlsProtocol.transport.clear()
            serverData = serverTLSProtocol.transport.value()
            if serverData:
                tlsProtocol.dataReceived(serverData)
                serverTLSProtocol.transport.clear()
            if not serverData and not clientData:
                break
        self.assertEqual(tlsProtocol.transport.value(), b"")
        self.assertEqual(serverTLSProtocol.transport.value(), b"")


    def test_streamingProducerPausedInNormalMode(self):
        """
        When the TLS transport is not blocked on reads, it correctly calls
        pauseProducing on the registered producer.
        """
        _, tlsProtocol, producer = self.setupStreamingProducer()

        # The TLS protocol's transport pretends to be full, pausing its
        # producer:
        tlsProtocol.transport.producer.pauseProducing()
        self.assertEqual(producer.producerState, 'paused')
        self.assertEqual(producer.producerHistory, ['pause'])
        self.assertEqual(tlsProtocol._producer._producerPaused, True)


    def test_streamingProducerResumedInNormalMode(self):
        """
        When the TLS transport is not blocked on reads, it correctly calls
        resumeProducing on the registered producer.
        """
        _, tlsProtocol, producer = self.setupStreamingProducer()
        tlsProtocol.transport.producer.pauseProducing()
        self.assertEqual(producer.producerHistory, ['pause'])

        # The TLS protocol's transport pretends to have written everything
        # out, so it resumes its producer:
        tlsProtocol.transport.producer.resumeProducing()
        self.assertEqual(producer.producerState, 'producing')
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])
        self.assertEqual(tlsProtocol._producer._producerPaused, False)


    def test_streamingProducerPausedInWriteBlockedOnReadMode(self):
        """
        When the TLS transport is blocked on reads, it correctly calls
        pauseProducing on the registered producer.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()

        # Write to TLS transport. Because we do this before the initial TLS
        # handshake is finished, writing bytes triggers a WantReadError,
        # indicating that until bytes are read for the handshake, more bytes
        # cannot be written. Thus writing bytes before the handshake should
        # cause the producer to be paused:
        clientProtocol.transport.write(b"hello")
        self.assertEqual(producer.producerState, 'paused')
        self.assertEqual(producer.producerHistory, ['pause'])
        self.assertEqual(tlsProtocol._producer._producerPaused, True)


    def test_streamingProducerResumedInWriteBlockedOnReadMode(self):
        """
        When the TLS transport is blocked on reads, it correctly calls
        resumeProducing on the registered producer.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()

        # Write to TLS transport, triggering WantReadError; this should cause
        # the producer to be paused. We use a large chunk of data to make sure
        # large writes don't trigger multiple pauses:
        clientProtocol.transport.write(b"hello world" * 320000)
        self.assertEqual(producer.producerHistory, ['pause'])

        # Now deliver bytes that will fix the WantRead condition; this should
        # unpause the producer:
        serverProtocol, serverTLSProtocol = buildTLSProtocol(server=True)
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])
        self.assertEqual(tlsProtocol._producer._producerPaused, False)

        # Make sure we haven't disconnected for some reason:
        self.assertEqual(tlsProtocol.transport.disconnecting, False)
        self.assertEqual(producer.producerState, 'producing')


    def test_streamingProducerTwice(self):
        """
        Registering a streaming producer twice throws an exception.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()
        originalProducer = tlsProtocol._producer
        producer2 = object()
        self.assertRaises(RuntimeError,
            clientProtocol.transport.registerProducer, producer2, True)
        self.assertIdentical(tlsProtocol._producer, originalProducer)


    def test_streamingProducerUnregister(self):
        """
        Unregistering a streaming producer removes it, reverting to initial state.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()
        clientProtocol.transport.unregisterProducer()
        self.assertEqual(tlsProtocol._producer, None)
        self.assertEqual(tlsProtocol.transport.producer, None)


    def loseConnectionWithProducer(self, writeBlockedOnRead):
        """
        Common code for tests involving writes by producer after
        loseConnection is called.
        """
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer()
        serverProtocol, serverTLSProtocol = buildTLSProtocol(server=True)

        if not writeBlockedOnRead:
            # Do the initial handshake before write:
            self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        else:
            # In this case the write below will trigger write-blocked-on-read
            # condition...
            pass

        # Now write, then lose connection:
        clientProtocol.transport.write(b"x ")
        clientProtocol.transport.loseConnection()
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)

        # Underlying transport should not have loseConnection called yet, nor
        # should producer be stopped:
        self.assertEqual(tlsProtocol.transport.disconnecting, False)
        self.assertFalse("stop" in producer.producerHistory)

        # Writes from client to server should continue to go through, since we
        # haven't unregistered producer yet:
        clientProtocol.transport.write(b"hello")
        clientProtocol.transport.writeSequence([b" ", b"world"])

        # Unregister producer; this should trigger TLS shutdown:
        clientProtocol.transport.unregisterProducer()
        self.assertNotEqual(tlsProtocol.transport.value(), b"")
        self.assertEqual(tlsProtocol.transport.disconnecting, False)

        # Additional writes should not go through:
        clientProtocol.transport.write(b"won't")
        clientProtocol.transport.writeSequence([b"won't!"])

        # Finish TLS close handshake:
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        self.assertEqual(tlsProtocol.transport.disconnecting, True)

        # Bytes made it through, as long as they were written before producer
        # was unregistered:
        self.assertEqual(serverProtocol.data, b"x hello world")


    def test_streamingProducerLoseConnectionWithProducer(self):
        """
        loseConnection() waits for the producer to unregister itself, then
        does a clean TLS close alert, then closes the underlying connection.
        """
        return self.loseConnectionWithProducer(False)


    def test_streamingProducerLoseConnectionWithProducerWBOR(self):
        """
        Even when writes are blocked on reading, loseConnection() waits for
        the producer to unregister itself, then does a clean TLS close alert,
        then closes the underlying connection.
        """
        return self.loseConnectionWithProducer(True)


    def test_streamingProducerBothTransportsDecideToPause(self):
        """
        pauseProducing() events can come from both the TLS transport layer and
        the underlying transport. In this case, both decide to pause,
        underlying first.
        """
        class PausingStringTransport(StringTransport):
            _didPause = False

            def write(self, data):
                if not self._didPause and self.producer is not None:
                    self._didPause = True
                    self.producer.pauseProducing()
                StringTransport.write(self, data)


        class TLSConnection(object):
            def __init__(self):
                self.l = []

            def send(self, bytes):
                # on first write, don't send all bytes:
                if not self.l:
                    bytes = bytes[:-1]
                # pause on second write:
                if len(self.l) == 1:
                    self.l.append("paused")
                    raise WantReadError()
                # otherwise just take in data:
                self.l.append(bytes)
                return len(bytes)

            def bio_write(self, data):
                pass

            def bio_read(self, size):
                return b'X'

            def recv(self, size):
                raise WantReadError()

        transport = PausingStringTransport()
        clientProtocol, tlsProtocol, producer = self.setupStreamingProducer(
            transport)
        self.assertEqual(producer.producerState, 'producing')

        # Shove in fake TLSConnection that will raise WantReadError the second
        # time send() is called. This will allow us to have bytes written to
        # to the PausingStringTransport, so it will pause the producer. Then,
        # WantReadError will be thrown, triggering the TLS transport's
        # producer code path.
        tlsProtocol._tlsConnection = TLSConnection()
        clientProtocol.transport.write(b"hello")
        self.assertEqual(producer.producerState, 'paused')
        self.assertEqual(producer.producerHistory, ['pause'])

        # Now, underlying transport resumes, and then we deliver some data to
        # TLS transport so that it will resume:
        tlsProtocol.transport.producer.resumeProducing()
        self.assertEqual(producer.producerState, 'producing')
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])
        tlsProtocol.dataReceived(b"hello")
        self.assertEqual(producer.producerState, 'producing')
        self.assertEqual(producer.producerHistory, ['pause', 'resume'])


    def test_streamingProducerStopProducing(self):
        """
        If the underlying transport tells its producer to stopProducing(),
        this is passed on to the high-level producer.
        """
        _, tlsProtocol, producer = self.setupStreamingProducer()
        tlsProtocol.transport.producer.stopProducing()
        self.assertEqual(producer.producerState, 'stopped')


    def test_nonStreamingProducer(self):
        """
        Non-streaming producers get wrapped as streaming producers.
        """
        clientProtocol, tlsProtocol = buildTLSProtocol()
        producer = NonStreamingProducer(clientProtocol.transport)

        # Register non-streaming producer:
        clientProtocol.transport.registerProducer(producer, False)
        streamingProducer = tlsProtocol.transport.producer._producer

        # Verify it was wrapped into streaming producer:
        self.assertIsInstance(streamingProducer, _PullToPush)
        self.assertEqual(streamingProducer._producer, producer)
        self.assertEqual(streamingProducer._consumer, clientProtocol.transport)
        self.assertEqual(tlsProtocol.transport.streaming, True)

        # Verify the streaming producer was started, and ran until the end:
        def done(ignore):
            # Our own producer is done:
            self.assertEqual(producer.consumer, None)
            # The producer has been unregistered:
            self.assertEqual(tlsProtocol.transport.producer, None)
            # The streaming producer wrapper knows it's done:
            self.assertEqual(streamingProducer._finished, True)
        producer.result.addCallback(done)

        serverProtocol, serverTLSProtocol = buildTLSProtocol(server=True)
        self.flushTwoTLSProtocols(tlsProtocol, serverTLSProtocol)
        return producer.result


    def test_interface(self):
        """
        L{_ProducerMembrane} implements L{IPushProducer}.
        """
        producer = StringTransport()
        membrane = _ProducerMembrane(producer)
        self.assertTrue(verifyObject(IPushProducer, membrane))


    def registerProducerAfterConnectionLost(self, streaming):
        """
        If a producer is registered after the transport has disconnected, the
        producer is not used, and its stopProducing method is called.
        """
        clientProtocol, tlsProtocol = buildTLSProtocol()
        clientProtocol.connectionLost = lambda reason: reason.trap(Error)

        class Producer(object):
            stopped = False

            def resumeProducing(self):
                return 1/0 # this should never be called

            def stopProducing(self):
                self.stopped = True

        # Disconnect the transport:
        tlsProtocol.connectionLost(Failure(ConnectionDone()))

        # Register the producer; startProducing should not be called, but
        # stopProducing will:
        producer = Producer()
        tlsProtocol.registerProducer(producer, False)
        self.assertIdentical(tlsProtocol.transport.producer, None)
        self.assertEqual(producer.stopped, True)


    def test_streamingProducerAfterConnectionLost(self):
        """
        If a streaming producer is registered after the transport has
        disconnected, the producer is not used, and its stopProducing method
        is called.
        """
        self.registerProducerAfterConnectionLost(True)


    def test_nonStreamingProducerAfterConnectionLost(self):
        """
        If a non-streaming producer is registered after the transport has
        disconnected, the producer is not used, and its stopProducing method
        is called.
        """
        self.registerProducerAfterConnectionLost(False)



class NonStreamingProducer(object):
    """
    A pull producer which writes 10 times only.
    """

    counter = 0
    stopped = False

    def __init__(self, consumer):
        self.consumer = consumer
        self.result = Deferred()

    def resumeProducing(self):
        if self.counter < 10:
            self.consumer.write(intToBytes(self.counter))
            self.counter += 1
            if self.counter == 10:
                self.consumer.unregisterProducer()
                self._done()
        else:
            if self.consumer is None:
                raise RuntimeError("BUG: resume after unregister/stop.")


    def pauseProducing(self):
        raise RuntimeError("BUG: pause should never be called.")


    def _done(self):
        self.consumer = None
        d = self.result
        del self.result
        d.callback(None)


    def stopProducing(self):
        self.stopped = True
        self._done()



class NonStreamingProducerTests(TestCase):
    """
    Non-streaming producers can be adapted into being streaming producers.
    """

    def streamUntilEnd(self, consumer):
        """
        Verify the consumer writes out all its data, but is not called after
        that.
        """
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        # The producer will call unregisterProducer(), and we need to hook
        # that up so the streaming wrapper is notified; the
        # TLSMemoryBIOProtocol will have to do this itself, which is tested
        # elsewhere:
        def unregister(orig=consumer.unregisterProducer):
            orig()
            streamingProducer.stopStreaming()
        consumer.unregisterProducer = unregister

        done = nsProducer.result
        def doneStreaming(_):
            # All data was streamed, and the producer unregistered itself:
            self.assertEqual(consumer.value(), b"0123456789")
            self.assertEqual(consumer.producer, None)
            # And the streaming wrapper stopped:
            self.assertEqual(streamingProducer._finished, True)
        done.addCallback(doneStreaming)

        # Now, start streaming:
        streamingProducer.startStreaming()
        return done


    def test_writeUntilDone(self):
        """
        When converted to a streaming producer, the non-streaming producer
        writes out all its data, but is not called after that.
        """
        consumer = StringTransport()
        return self.streamUntilEnd(consumer)


    def test_pause(self):
        """
        When the streaming producer is paused, the underlying producer stops
        getting resumeProducing calls.
        """
        class PausingStringTransport(StringTransport):
            writes = 0

            def __init__(self):
                StringTransport.__init__(self)
                self.paused = Deferred()

            def write(self, data):
                self.writes += 1
                StringTransport.write(self, data)
                if self.writes == 3:
                    self.producer.pauseProducing()
                    d = self.paused
                    del self.paused
                    d.callback(None)


        consumer = PausingStringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        # Make sure the consumer does not continue:
        def shouldNotBeCalled(ignore):
            self.fail("BUG: The producer should not finish!")
        nsProducer.result.addCallback(shouldNotBeCalled)

        done = consumer.paused
        def paused(ignore):
            # The CooperatorTask driving the producer was paused:
            self.assertEqual(streamingProducer._coopTask._pauseCount, 1)
        done.addCallback(paused)

        # Now, start streaming:
        streamingProducer.startStreaming()
        return done


    def test_resume(self):
        """
        When the streaming producer is paused and then resumed, the underlying
        producer starts getting resumeProducing calls again after the resume.

        The test will never finish (or rather, time out) if the resume
        producing call is not working.
        """
        class PausingStringTransport(StringTransport):
            writes = 0

            def write(self, data):
                self.writes += 1
                StringTransport.write(self, data)
                if self.writes == 3:
                    self.producer.pauseProducing()
                    self.producer.resumeProducing()

        consumer = PausingStringTransport()
        return self.streamUntilEnd(consumer)


    def test_stopProducing(self):
        """
        When the streaming producer is stopped by the consumer, the underlying
        producer is stopped, and streaming is stopped.
        """
        class StoppingStringTransport(StringTransport):
            writes = 0

            def write(self, data):
                self.writes += 1
                StringTransport.write(self, data)
                if self.writes == 3:
                    self.producer.stopProducing()

        consumer = StoppingStringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        done = nsProducer.result
        def doneStreaming(_):
            # Not all data was streamed, and the producer was stopped:
            self.assertEqual(consumer.value(), b"012")
            self.assertEqual(nsProducer.stopped, True)
            # And the streaming wrapper stopped:
            self.assertEqual(streamingProducer._finished, True)
        done.addCallback(doneStreaming)

        # Now, start streaming:
        streamingProducer.startStreaming()
        return done


    def resumeProducingRaises(self, consumer, expectedExceptions):
        """
        Common implementation for tests where the underlying producer throws
        an exception when its resumeProducing is called.
        """
        class ThrowingProducer(NonStreamingProducer):

            def resumeProducing(self):
                if self.counter == 2:
                    return 1/0
                else:
                    NonStreamingProducer.resumeProducing(self)

        nsProducer = ThrowingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        consumer.registerProducer(streamingProducer, True)

        # Register log observer:
        loggedMsgs = []
        log.addObserver(loggedMsgs.append)
        self.addCleanup(log.removeObserver, loggedMsgs.append)

        # Make consumer unregister do what TLSMemoryBIOProtocol would do:
        def unregister(orig=consumer.unregisterProducer):
            orig()
            streamingProducer.stopStreaming()
        consumer.unregisterProducer = unregister

        # Start streaming:
        streamingProducer.startStreaming()

        done = streamingProducer._coopTask.whenDone()
        done.addErrback(lambda reason: reason.trap(TaskStopped))
        def stopped(ign):
            self.assertEqual(consumer.value(), b"01")
            # Any errors from resumeProducing were logged:
            errors = self.flushLoggedErrors()
            self.assertEqual(len(errors), len(expectedExceptions))
            for f, (expected, msg), logMsg in zip(
                errors, expectedExceptions, loggedMsgs):
                self.assertTrue(f.check(expected))
                self.assertIn(msg, logMsg['why'])
            # And the streaming wrapper stopped:
            self.assertEqual(streamingProducer._finished, True)
        done.addCallback(stopped)
        return done


    def test_resumeProducingRaises(self):
        """
        If the underlying producer raises an exception when resumeProducing is
        called, the streaming wrapper should log the error, unregister from
        the consumer and stop streaming.
        """
        consumer = StringTransport()
        done = self.resumeProducingRaises(
            consumer,
            [(ZeroDivisionError, "failed, producing will be stopped")])
        def cleanShutdown(ignore):
            # Producer was unregistered from consumer:
            self.assertEqual(consumer.producer, None)
        done.addCallback(cleanShutdown)
        return done


    def test_resumeProducingRaiseAndUnregisterProducerRaises(self):
        """
        If the underlying producer raises an exception when resumeProducing is
        called, the streaming wrapper should log the error, unregister from
        the consumer and stop streaming even if the unregisterProducer call
        also raise.
        """
        consumer = StringTransport()
        def raiser():
            raise RuntimeError()
        consumer.unregisterProducer = raiser
        return self.resumeProducingRaises(
            consumer,
            [(ZeroDivisionError, "failed, producing will be stopped"),
             (RuntimeError, "failed to unregister producer")])


    def test_stopStreamingTwice(self):
        """
        stopStreaming() can be called more than once without blowing
        up. This is useful for error-handling paths.
        """
        consumer = StringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        streamingProducer.startStreaming()
        streamingProducer.stopStreaming()
        streamingProducer.stopStreaming()
        self.assertEqual(streamingProducer._finished, True)


    def test_interface(self):
        """
        L{_PullToPush} implements L{IPushProducer}.
        """
        consumer = StringTransport()
        nsProducer = NonStreamingProducer(consumer)
        streamingProducer = _PullToPush(nsProducer, consumer)
        self.assertTrue(verifyObject(IPushProducer, streamingProducer))
