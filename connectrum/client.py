#
# Client connect to an Electrum server.
#
import json, warnings, asyncio, ssl
from .protocol import StratumProtocol
import aiosocks
from .utils import logger
from collections import defaultdict
from .exc import ElectrumErrorResponse

class StratumClient:

    def __init__(self, loop=None):
        '''
            Setup state needed to handle req/resp from a single Stratum server.
            Requires a transport (TransportABC) object to do the communication.
        '''
        self.protocol = None

        self.next_id = 1
        self.inflight = {}
        self.subscriptions = defaultdict(list)

        self.ka_task = None

        self.loop = loop or asyncio.get_event_loop()

        # next step: call connect()

    def _connection_lost(self, protocol):
        # Ignore connection_lost for old connections
        if protocol is not self.protocol:
            return

        self.protocol = None
        logger.warn("Electrum server connection lost")

        # cleanup keep alive task
        if self.ka_task:
            self.ka_task.cancel()
            self.ka_task = None

    def close(self):
        if self.protocol:
            self.protocol.close()
            self.protocol = None
        if self.ka_task:
            self.ka_task.cancel()
            self.ka_task = None
            

    async def connect(self, server_info, proto_code='s', *,
                            use_tor=False, disable_cert_verify=False,
                            proxy=None):
        '''
            Start connection process.
            Destination must be specified in a ServerInfo() record.
        '''
        self.server_info = server_info
        self.proto_code = proto_code

        if proto_code == 'g':       # websocket
            # to do this, we'll need a websockets implementation that
            # operates more like a asyncio.Transport
            # maybe: `asyncws` or `aiohttp` 
            raise NotImplementedError('sorry no WebSocket transport yet')

        hostname, port, use_ssl = server_info.get_port(proto_code)

        if use_tor:
            # connect via Tor proxy proxy, assumed to be on localhost:9050
            try:
                socks_host, socks_port = use_tor
            except TypeError:
                socks_host, socks_port = 'localhost', 9150

            disable_cert_verify = True

            assert not proxy, "Sorry not yet supporting proxy->tor->dest"

            proxy = aiosocks.Socks5Addr(socks_host, int(socks_port))

        if use_ssl == True and disable_cert_verify:
            # Create a more liberal SSL context that won't
            # object to self-signed certicates. This is 
            # very bad on public Internet, but probably ok
            # over Tor
            use_ssl = ssl.create_default_context()
            use_ssl.check_hostname = False
            use_ssl.verify_mode = ssl.CERT_NONE

        if proxy:
            transport, protocol = await aiosocks.create_connection(
                                    StratumProtocol, proxy=proxy,
                                    proxy_auth=None,
                                    remote_resolve=True, ssl=use_ssl,
                                    dst=(hostname, port))
            
        else:
            transport, protocol = await self.loop.create_connection(
                                                    StratumProtocol, host=hostname,
                                                    port=port, ssl=use_ssl)
        if self.protocol:
            self.protocol.close()

        self.protocol = protocol
        protocol.client = self

        self.ka_task = self.loop.create_task(self._keepalive())

        logger.debug("Connected to: %r" % server_info)

    async def _keepalive(self):
        '''
            Keep our connect to server alive forever, with some 
            pointless traffic.
        '''
        while self.protocol:
            vers = await self.RPC('server.version')
            logger.debug("Server version: " + vers)
            await asyncio.sleep(600)


    def _send_request(self, method, params=[], is_subscribe = False):
        '''
            Send a new request to the server. Serialized the JSON and
            tracks id numbers and optional callbacks.
        '''
        # pick a new ID
        self.next_id += 1
        req_id = self.next_id

        # serialize as JSON
        msg = {'id': req_id, 'method': method, 'params': params}

        # subscriptions are a Q, normal requests are a future
        if is_subscribe:
            waitQ = asyncio.Queue()
            self.subscriptions[method].append(waitQ)

        fut = asyncio.Future(loop=self.loop)

        self.inflight[req_id] = (msg, fut)

        # send it via the transport, which serializes it
        self.protocol.send_data(msg)

        return fut if not is_subscribe else (fut, waitQ)

    def _got_response(self, msg):
        '''
            Decode and dispatch responses from the server.

            Has already been unframed and deserialized into an object.
        '''

        #logger.debug("MSG: %r" % msg)

        resp_id = msg.get('id', None)

        if resp_id is None:
            # subscription traffic comes with method set, but no req id.
            method = msg.get('method', None)
            if not method:
                logger.error("Incoming server message had no ID nor method in it", msg)
                return

            # not obvious, but result is on params, not result, for subscriptions
            result = msg.get('params', None)

            logger.debug("Traffic on subscription: %s" % method)

            subs = self.subscriptions.get(method)
            for q in subs:
                self.loop.create_task(q.put(result))

            return

        assert 'method' not in msg
        result = msg.get('result')

        # fetch and forget about the request
        inf = self.inflight.pop(resp_id) 
        if not inf:
            logger.error("Incoming server message had unknown ID in it: %s" % resp_id)
            return

        # it's a future which is done now
        req, rv = inf

        if 'error' in msg:
            err = msg['error']
            
            logger.info("Error response: '%s'" % err)
            rv.set_exception(ElectrumErrorResponse(err, req))

        else:
            rv.set_result(result)

    def RPC(self, method, *params):
        '''
            Perform a remote command.

            Expects a method name, which look like:
                server.peers.subscribe
            .. and sometimes take arguments, all of which are positional.
    
            Returns a future for simple calls, and a asyncio.Queue
            for subscriptions.
        '''
        assert '.' in method
        #assert not method.endswith('subscribe')
        return self._send_request(method, params)

    def subscribe(self, method, *params):
        '''
            Perform a remote command.

            Expects a method name, which look like:
                server.peers.subscribe
            .. and sometimes take arguments, all of which are positional.
    
            Returns a future for simple calls, and a asyncio.Queue
            for subscriptions.
        '''
        assert '.' in method
        assert method.endswith('subscribe')
        return self._send_request(method, params, is_subscribe=True)
        

if __name__ == '__main__':
    from transport import SocketTransport
    from svr_info import KnownServers, ServerInfo

    import logging
    logging.getLogger('connectrum').setLevel(logging.DEBUG)
    #logging.getLogger('asyncio').setLevel(logging.DEBUG)

    loop = asyncio.get_event_loop()
    loop.set_debug(True)

    proto_code = 's'

    if 1:
        ks = KnownServers()
        ks.from_json('servers.json')
        which = ks.select(proto_code, is_onion=True, min_prune=1000)[0]
    else:
        which = ServerInfo({
            "seen_at": 1465686119.022801,
            "ports": "t s",
            "nickname": "dunp",
            "pruning_limit": 10000,
            "version": "1.0",
            "hostname": "erbium1.sytes.net" })

    c = StratumClient(loop=loop)

    loop.run_until_complete(c.connect(which, proto_code, disable_cert_verify=True, use_tor=True))
    
    rv = loop.run_until_complete(c.RPC('server.peers.subscribe'))
    print("DONE!: this server has %d peers" % len(rv))
    loop.close()

    #c.blockchain.address.get_balance(23)
