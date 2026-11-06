"""Local development runner with optional LAN practice mode. Never use this server for deployment."""
import ipaddress
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from web import create_app

root = Path(__file__).resolve().parents[1]
lan_ip = os.environ.get('NORTHSTAR_LAN_IP')
if lan_ip:
    address = ipaddress.IPv4Address(lan_ip)
    if not any(address in network for network in map(ipaddress.ip_network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))):
        raise SystemExit('NORTHSTAR_LAN_IP must be this PC’s private IPv4 address.')
folder = root / 'instance'
if lan_ip:
    folder /= 'lan'

folder.mkdir(mode=0o700, exist_ok=True, parents=True)
settings = folder / 'dev-secrets.json'
if not settings.exists():
    data = {'SECRET_KEY':secrets.token_urlsafe(48), 'ENCRYPTION_KEY':Fernet.generate_key().decode()}
    fd = os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data,stream)
with settings.open() as stream:
    config = json.load(stream)
config.update(DATABASE=str(folder/'practice.sqlite3'),SESSION_COOKIE_SECURE=False, PAPER_TRADING=True, BILLING_LIVE_ENABLED=False,
              PUBLIC_URL=os.environ.get('PUBLIC_URL', 'http://127.0.0.1:5000'))
if lan_ip:
    config.update(LAN_PRACTICE_ONLY=True, PUBLIC_URL=f'http://{lan_ip}:5001',
                  STRIPE_SECRET_KEY=None, STRIPE_WEBHOOK_SECRET=None, STRIPE_PRICE_ID=None)
app=create_app(config)
if __name__ == '__main__':
    app.run(host=lan_ip or '127.0.0.1',port=5001 if lan_ip else 5000,debug=False,use_reloader=False)
