Warpforge Revival - hiding the home address behind a relay
==========================================================

What this does
  A small rented server (the relay) becomes the public address of your game address.
  Players connect to the relay; it passes every connection on, unopened and still encrypted,
  through a private WireGuard tunnel to this PC. This PC opens the tunnel itself, so the router
  needs no open ports and players never see the home address. The relay tells the Revival
  server and Caddy each player's real address, so bans and limits keep working.

  Tunnel: relay 10.66.0.1 <-> this PC 10.66.0.2.  RELAY-ADDRESS below is the relay's public IPv4.


1. WireGuard on this PC
   - Install WireGuard for Windows: https://www.wireguard.com/install/
   - Open it: Add Tunnel (arrow next to it) > Add empty tunnel...
   - Name: warpforge-relay. The window shows a "Public key" - copy it, you need it in step 2.
     (The PrivateKey line stays on this PC. Never share it.)
   - Under the PrivateKey line, add the rest so the whole text reads:

        [Interface]
        PrivateKey = (already filled in - leave it)
        Address = 10.66.0.2/24

        [Peer]
        PublicKey = (the relay's key from step 2)
        AllowedIPs = 10.66.0.1/32
        Endpoint = RELAY-ADDRESS:51820
        PersistentKeepalive = 25

     AllowedIPs must be exactly 10.66.0.1/32: only the relay goes through the tunnel, not your
     other internet traffic. Leave "Block untunneled traffic" unticked.
   - Do not save yet if you do not have the relay's key; keep the window open.


2. Set up the relay (PowerShell, in this folder)
     cd <this folder>
     scp relay-setup.sh root@RELAY-ADDRESS:/root/
     ssh root@RELAY-ADDRESS "bash /root/relay-setup.sh PASTE-THE-PC-PUBLIC-KEY-HERE"

   It takes a minute or two and ends by printing:  PublicKey = ....
   Put that key in the [Peer] PublicKey line of step 1, Save, then Activate the tunnel.
   (After this, signing in to the relay only works with your SSH key, not a password.)


3. Let the tunnel through the Windows firewall (PowerShell as administrator, once)
     New-NetFirewallRule -DisplayName "Warpforge relay" -Direction Inbound -Protocol TCP -LocalPort 8780,443 -RemoteAddress 10.66.0.1 -Action Allow


4. Restart the Revival server and Caddy
   Both were updated for the relay (they read the "who really connected" line from the relay
   only). Close and start them again as usual.
   Caddy needs the proxy_protocol listener (see the README's https section).


5. Check before switching anyone over
   - On the relay:   ssh root@RELAY-ADDRESS "wg show"     -> shows a "latest handshake" line
   - From this PC:   curl.exe -k https://RELAY-ADDRESS:8780/api/v1/status
                     -> {"name": "Warpforge Revival", ... "encrypted": true}


6. Switch the address
   - At your DNS provider: point the A record of your game address at RELAY-ADDRESS.
     If there is an AAAA record for "warpforge", delete it (or put the relay's IPv6 address there). Leave every other record alone.
   - It can take from minutes to an hour for everyone to see the change.


7. Close the router
   Once players connect through the relay, remove the port forwards for 443 and 8780 on the
   router. Nothing needs to come in directly any more. (The tunnel is opened from this PC, so
   it needs no forward either.)


Good to know
  - The tunnel must be active whenever the server should be reachable. The WireGuard app keeps
    an activated tunnel on across restarts.
  - Your PC can also use your game address now: it goes out to the relay and back.
  - The old home address was public in DNS until now; anyone who looked it up before still
    knows it. Most home connections get a new address now and then (or after the router has
    been off for a while), after which it is gone for good.
