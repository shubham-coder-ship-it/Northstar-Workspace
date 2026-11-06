import hashlib
import hmac
import io
import json
import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from PIL import Image

from web import create_app


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING':True,'SECRET_KEY':'a'*48,'ENCRYPTION_KEY':Fernet.generate_key(),
                       'DATABASE':str(tmp_path/'test.sqlite3'),'SESSION_COOKIE_SECURE':False})

@pytest.fixture
def client(app):
    return app.test_client()

def csrf(client):
    return client.get('/api/session').json['csrf']

def send(client,path,data=None,method='POST',**headers):
    return client.open(path,method=method,json=data or {},headers={'X-CSRF-Token':csrf(client),**headers})

def register(client,email='user@example.com'):
    password = 'long-password-123!'
    response = send(client, '/api/register', dict(email=email, password=password, confirm=password, full_name='Test User'))
    if response.status_code == 201:
        assert send(client, '/api/login', dict(email=email, password=password)).status_code == 200
    return response

def connect(client):
    return send(client,'/api/credentials',{'key':'a'*64,'secret':'b'*64})

def order(client,token='c'*32,**overrides):
    return send(client,'/api/orders',(dict(symbol='BTCUSDT',side='BUY',kind='MARKET',quantity='0.01',confirmed=True) | overrides),**{'Idempotency-Key':token})

def sql(app,statement,args=()):
    with sqlite3.connect(app.config['DATABASE']) as db:
        return db.execute(statement,args).fetchall()


def test_security_headers_and_cookie(client):
    response=client.get('/')
    assert response.status_code==200
    assert "frame-ancestors 'none'" in response.headers['Content-Security-Policy']
    assert response.headers['X-Content-Type-Options']=='nosniff'
    assert 'HttpOnly' in response.headers['Set-Cookie']
    assert 'SameSite=Lax' in response.headers['Set-Cookie']
    assert response.headers['Cache-Control']=='no-store'

def test_secure_cookie_default(tmp_path):
    app=create_app({'SECRET_KEY':'a'*48,'ENCRYPTION_KEY':Fernet.generate_key(),'DATABASE':str(tmp_path/'secure.db')})
    assert 'Secure' in app.test_client().get('/').headers['Set-Cookie']

def test_csrf_required(client):
    assert client.post('/api/register',json={'email':'x@y.com','password':'a'*16}).status_code==403

@pytest.mark.parametrize('path',['/api/orders','/api/admin/users','/api/profile/image'])
def test_anonymous_denied(client,path):
    assert client.get(path).status_code==401

def test_register_login_password_hash_and_logout(app,client):
    assert register(client).status_code==201
    stored=sql(app,'SELECT password,role FROM users')[0]
    assert stored[0]!='long-password-123!' and stored[1]=='user'
    assert send(client,'/api/logout').status_code==200
    assert client.get('/api/orders').status_code==401
    assert send(client,'/api/login',{'email':'user@example.com','password':'wrong'}).status_code==401
    assert send(client,'/api/login',{'email':'user@example.com','password':'long-password-123!'}).status_code==200

def test_role_injection_and_admin_denied(client):
    assert send(client,'/api/register',{'email':'user@example.com','password':'a'*16,'confirm':'a'*16,'full_name':'Test User','role':'admin'}).status_code==201
    assert send(client,'/api/login',{'email':'user@example.com','password':'a'*16}).json['user']['role']=='user'
    assert client.get('/api/admin/users').status_code==403
    assert send(client,'/api/admin/users/1/access',{'disabled':True}).status_code==403

def test_admin_disable_revokes_session(app,client):
    register(client)
    admin=app.test_client();register(admin,'admin@example.com')
    sql(app,"UPDATE users SET role='admin' WHERE email='admin@example.com'")
    assert admin.get('/api/admin/users').status_code==200
    assert send(admin,'/api/admin/users/1/access',{'disabled':True}).status_code==200
    assert client.get('/api/orders').status_code==401
    assert send(admin,'/api/admin/users/2/access',{'disabled':True}).status_code==400
    assert len(sql(app,"SELECT * FROM audit WHERE action='user_disabled'"))==1

def test_user_isolation(app,client):
    register(client)
    sql(app,"INSERT INTO orders(id,user_id,symbol,side,kind,quantity,status,created) VALUES('test',1,'BTCUSDT','BUY','MARKET','1','NEW',1)")
    other=app.test_client();register(other,'other@example.com')
    assert len(client.get('/api/orders').json['orders'])==1
    assert other.get('/api/orders').json['orders']==[]

def test_encrypted_credentials_not_returned(app,client):
    register(client);assert connect(client).status_code==200
    blob=sql(app,'SELECT credentials FROM users')[0][0]
    assert b'a'*64 not in blob and b'b'*64 not in blob
    assert Fernet(app.config['ENCRYPTION_KEY']).decrypt(blob)==('a'*64+'\n'+'b'*64).encode()
    assert 'secret' not in client.get('/api/session').json
    assert send(client,'/api/credentials',method='DELETE').status_code==200
    assert sql(app,'SELECT credentials FROM users')[0][0] is None

@pytest.mark.parametrize('quantity',['NaN','Infinity','-1','0','1e100','0.000000000000000000000000000001',[],None])
def test_numeric_attacks_rejected(client,quantity):
    register(client)
    assert order(client,quantity=quantity).status_code==400

@pytest.mark.parametrize('data',[
    {'symbol':"BTC';DROP TABLE users;--"}, {'symbol':'<script>'}, {'side':'HACK'},
    {'kind':'STOP_LIMIT'}, {'confirmed':False}, {'kind':'LIMIT','price':'NaN'},
])
def test_invalid_orders_rejected(client,data):
    register(client)
    body=dict(symbol='BTCUSDT',side='BUY',kind='MARKET',quantity='0.01',confirmed=True)
    body.update(data)
    assert send(client,'/api/orders',body,**{'Idempotency-Key':'c'*32}).status_code==400

def test_order_idempotency_and_testnet_routing(client):
    register(client);connect(client)
    with patch('web.Client') as sdk:
        sdk.return_value.futures_create_order.return_value={'orderId':42,'status':'NEW'}
        assert order(client).status_code==201
        assert order(client).status_code==200
        assert sdk.return_value.futures_create_order.call_count==1
        assert sdk.call_args.kwargs['testnet'] is True
        assert sdk.call_args.kwargs['ping'] is False
        assert sdk.return_value.FUTURES_TESTNET_URL=='https://testnet.binancefuture.com/fapi'
        assert order(client,quantity='0.02').status_code==409

def test_timeout_never_retries_or_leaks(client):
    register(client);connect(client)
    with patch('web.Client') as sdk:
        sdk.return_value.futures_create_order.side_effect=TimeoutError('sensitive-internal-secret')
        response=order(client)
        assert response.status_code==502 and response.json['status']=='UNKNOWN'
        assert 'sensitive' not in response.text
        assert order(client).json['status']=='UNKNOWN'
        assert sdk.return_value.futures_create_order.call_count==1

def test_rate_limit_shared_between_clients(app,client):
    other=app.test_client()
    for i in range(10):
        assert send(client,'/api/login',{'email':'nobody@example.com','password':'wrong'}).status_code==401
    assert send(other,'/api/login',{'email':'nobody@example.com','password':'wrong'}).status_code==429

def test_valid_image_reencoded_and_private(app,client):
    register(client)
    buf=io.BytesIO();Image.new('RGB',(30,40),'red').save(buf,format='PNG');buf.seek(0)
    response=client.post('/api/profile/image',data={'image':(buf,'../../avatar.png')},headers={'X-CSRF-Token':csrf(client)})
    assert response.status_code==200
    image=client.get('/api/profile/image')
    assert image.content_type=='image/jpeg'
    decoded=Image.open(io.BytesIO(image.data));assert decoded.size==(256,256)
    other=app.test_client();register(other,'other@example.com')
    assert other.get('/api/profile/image').status_code==404

@pytest.mark.parametrize('content',[b'<svg onload="alert(1)"></svg>', b'not an image',b'<?php echo "oops";?>'])
def test_malicious_uploads_rejected(client,content):
    register(client)
    result=client.post('/api/profile/image',data={'image':(io.BytesIO(content),'avatar.jpg')},headers={'X-CSRF-Token':csrf(client)})
    assert result.status_code==400

def test_oversize_upload(client):
    register(client)
    result=client.post('/api/profile/image',data={'image':(io.BytesIO(b'x'*(2*1024*1024+1)),'avatar.jpg')},headers={'X-CSRF-Token':csrf(client)})
    assert result.status_code==413

def test_json_type_rejected(client):
    assert client.post('/api/register',json=[],headers={'X-CSRF-Token':csrf(client)}).status_code==400

def test_billing_disabled_without_config(client):
    register(client)
    assert send(client,'/api/billing/checkout').status_code==503

def stripe_config(app):
    app.config.update(STRIPE_SECRET_KEY='sk_test_not_real',STRIPE_WEBHOOK_SECRET='whsec_test_not_real',STRIPE_PRICE_ID='price_configured',PUBLIC_URL='https://example.com')

def webhook(client,app,event,signature=True):
    body=json.dumps(event).encode();stamp=int(time.time())
    digest=hmac.new(app.config['STRIPE_WEBHOOK_SECRET'].encode(),str(stamp).encode()+b'.'+body,hashlib.sha256).hexdigest()
    return client.post('/api/billing/webhook',data=body,content_type='application/json',headers={'Stripe-Signature':f't={stamp},v1={digest}' if signature else 'invalid'})

def test_webhook_signature_and_replay(app,client):
    stripe_config(app);register(client)
    sql(app,"UPDATE users SET customer='cus_1' WHERE id=1")
    event=dict(id='evt_1',type='customer.subscription.updated',created=100,data={'object':dict(id='sub_1',customer='cus_1',status='active')})
    assert webhook(client,app,event,False).status_code==400
    with patch('web.stripe.Subscription.list',return_value={'data':[dict(id='sub_1',status='active',created=100,items={'data':[{'price':{'id':'price_configured'}}]})]}) as provider:
        assert webhook(client,app,event).status_code==200
        assert webhook(client,app,event).status_code==200
        assert provider.call_count==1
    assert sql(app,'SELECT subscription_status FROM users')[0][0]=='active'
    assert len(sql(app,'SELECT * FROM billing_events'))==1
    event['id']='evt_old';event['created']=50;event['data']['object']['status']='canceled'
    with patch('web.stripe.Subscription.list',return_value={'data':[dict(id='sub_1',status='active',created=100,items={'data':[{'price':{'id':'price_configured'}}]})]}):
        assert webhook(client,app,event).status_code==200
    assert sql(app,'SELECT subscription_status FROM users')[0][0]=='active'

def test_checkout_price_and_identity_are_server_controlled(app,client):
    stripe_config(app);register(client)
    with patch('web.stripe.Customer.create',return_value=MagicMock(id='cus_1')),patch('web.stripe.checkout.Session.create',return_value=MagicMock(url='https://checkout.stripe.com/test')) as checkout:
        response=send(client,'/api/billing/checkout',{'price':'attacker','user_id':99})
        assert response.status_code==200
        args=checkout.call_args.kwargs
        assert args['line_items']==[{'price':'price_configured','quantity':1}]
        assert args['client_reference_id']=='1'

def test_no_raw_error_leak(app,client):
    register(client)
    with patch('web.stripe.Customer.create',side_effect=RuntimeError('raw-api-key-in-error')):
        stripe_config(app)
        response=send(client,'/api/billing/checkout')
        assert response.status_code==500
        assert 'raw-api-key' not in response.text
        assert response.json['incident']

def test_password_change_revokes_other_sessions(app,client):
    register(client)
    other=app.test_client()
    assert send(other,'/api/login',{'email':'user@example.com','password':'long-password-123!'}).status_code==200
    assert send(client,'/api/password',{'current':'long-password-123!','new':'new-long-password-123!','confirm':'new-long-password-123!'}).status_code==200
    assert other.get('/api/orders').status_code==401
    assert client.get('/api/orders').status_code==401
    assert send(client,'/api/login',{'email':'user@example.com','password':'new-long-password-123!'}).status_code==200

def test_checkout_reused_and_live_payment_disabled(app,client):
    stripe_config(app);register(client)
    with patch('web.stripe.Customer.create',return_value=MagicMock(id='cus_1')),patch('web.stripe.checkout.Session.create',return_value=MagicMock(url='https://checkout.stripe.com/test')) as provider:
        assert send(client,'/api/billing/checkout').status_code==200
        assert send(client,'/api/billing/checkout').status_code==200
        assert provider.call_count==1
    app.config['STRIPE_SECRET_KEY']='sk_live_not_real'
    assert send(client,'/api/billing/checkout').status_code==503

@pytest.mark.parametrize('value',['nan','inf','-inf'])
def test_legacy_cli_rejects_nonfinite_values(value):
    from bot.validators import validate_quantity,validate_price,validate_stop_price,ValidationError
    for check in (lambda:validate_quantity(value),lambda:validate_price(value,True),lambda:validate_stop_price(value,True)):
        with pytest.raises(ValidationError):
            check()

def test_practice_fills_and_analytics(app, client):
    app.config['PAPER_TRADING'] = True
    register(client)
    with patch('web.Client') as exchange:
        buy = order(client, quote='100', quantity='2')
        assert buy.status_code == 201 and buy.json['status'] == 'FILLED_PRACTICE'
        assert order(client, quote='100', quantity='2').status_code == 200
        assert order(client, quote='101', quantity='2').status_code == 409
        sell = order(client, token='d'*32, quote='110', quantity='1', side='SELL')
        assert sell.json['status'] == 'FILLED_PRACTICE'
        pending = order(client, token='e'*32, quote='100', kind='LIMIT', price='90')
        assert pending.json['status'] == 'PENDING_PRACTICE'
        assert order(client, token='f'*32, quote='100', side='SELL', quantity='2').status_code == 400
        exchange.assert_not_called()
    stats = client.get('/api/practice/analytics').json
    assert (stats['balance'], stats['pnl'], stats['total'], stats['wins']) == ('9910', '10', 2, 1)
    other = app.test_client()
    register(other, 'other@example.com')
    assert other.get('/api/practice/analytics').json['total'] == 0


def test_account_email_and_recovery(client, monkeypatch):
    monkeypatch.setenv('SMTP_HOST', 'smtp.example.com')
    monkeypatch.setenv('SMTP_FROM', 'northstar@example.com')
    register(client)
    assert send(client, '/api/profile', dict(full_name='New Name', age='25')).status_code == 200
    assert client.get('/api/session').json['user']['age'] == 25
    with patch('web.smtplib.SMTP_SSL') as mail:
        send_message = mail.return_value.__enter__.return_value.send_message
        assert send(client, '/api/profile/email', dict(email='new@example.com', password='long-password-123!')).status_code == 200
        token = send_message.call_args.args[0].get_content().split('#verify-email=')[1].replace('\n', '')
        assert send(client, '/api/profile/email/verify', dict(token=token)).status_code == 200
        assert send(client, '/api/password/forgot', dict(email='new@example.com')).status_code == 200
        token = send_message.call_args.args[0].get_content().split('#reset=')[1].replace('\n', '')
    password = 'recovered-password-123!'
    reset = dict(token=token, new=password, confirm=password)
    assert send(client, '/api/password/reset', reset).status_code == 200
    assert send(client, '/api/password/reset', reset).status_code == 400
    assert send(client, '/api/login', dict(email='new@example.com', password=password)).status_code == 200

def test_live_account_and_order_are_isolated(app, client):
    app.config['PAPER_TRADING'] = True
    register(client)
    assert client.get('/api/live/account').json['connected'] is False
    data = dict(symbol='BTCUSDT', side='BUY', kind='LIMIT', quantity='0.01', price='50000')
    headers = {'Idempotency-Key': '9'*32}
    assert send(client, '/api/live/orders', data, **headers).status_code == 400
    with patch('web.Client') as factory:
        exchange = factory.return_value
        exchange.futures_account.return_value = {'canTrade':True, 'assets':[{'asset':'USDT','walletBalance':'100'}]}
        exchange.futures_create_order.return_value = {'orderId':23,'status':'NEW'}
        assert send(client, '/api/live/credentials', {'key':'x'*32,'secret':'y'*32}).status_code == 200
        assert factory.call_args.kwargs['testnet'] is False
        assert exchange.FUTURES_URL == 'https://fapi.binance.com/fapi'
        assert client.get('/api/live/account').json['assets'][0]['walletBalance'] == '100'
        assert send(client, '/api/live/orders', dict(data, confirmation='LIVE'), **headers).status_code == 201
        assert send(client, '/api/live/orders', dict(data, confirmation='LIVE'), **headers).status_code == 200
        exchange.futures_create_order.assert_called_once_with(symbol='BTCUSDT', side='BUY', type='LIMIT',
            quantity='0.01', price='50000', timeInForce='GTC', newClientOrderId='ns-'+'9'*32)
        exchange.futures_get_order.return_value = {'orderId':23,'status':'FILLED'}
        assert send(client, '/api/live/orders/'+'9'*32+'/status').json['status'] == 'FILLED'
        assert client.get('/api/orders').json['orders'] == []
        assert len(client.get('/api/live/account').json['orders']) == 1
        assert send(client, '/api/live/credentials', method='DELETE').status_code == 200
        assert send(client, '/api/live/orders', dict(data, confirmation='LIVE'), **headers).status_code == 400
    assert client.get('/explore').status_code == 200

def test_admin_overview_and_practice_reset(app, client):
    app.config['PAPER_TRADING'] = True
    register(client)
    assert client.get('/api/admin/overview').status_code == 403
    assert order(client, quantity='2', quote='100').status_code == 201
    admin = app.test_client()
    register(admin, 'admin@example.com')
    sql(app, "UPDATE users SET role='admin' WHERE email='admin@example.com'")
    report = admin.get('/api/admin/overview').json
    assert report['counts']['users'] == 2 and report['counts']['practice_volume'] == '200'
    assert 'secret' not in report['users'][0]
    assert send(admin, '/api/admin/settings/practice', {'balance':'20000'}).status_code == 403
    assert send(admin, '/api/admin/practice/1/reset', {'confirm':'RESET'}).status_code == 200
    assert client.get('/api/practice/analytics').json['balance'] == '10000'
    assert len(client.get('/api/orders').json['orders']) == 1
    assert order(client, token='d'*32, side='SELL', quantity='1', quote='120').status_code == 400
    assert admin.get('/api/admin/overview').json['events'][0]['action'] == 'practice_reset'

def test_admin_selects_recurring_billing_price_and_member_export(app, client):
    register(client)
    endpoint = '/api/admin/billing/price'
    assert send(client, endpoint, {'price_id':'price_monthly'}).status_code == 403
    assert client.get('/api/billing/member-orders.csv').status_code == 403
    sql(app, "UPDATE users SET role='admin'")
    app.config['STRIPE_SECRET_KEY'] = 'sk_test_example'
    price = dict(active=True, livemode=False, recurring={'interval':'month','interval_count':1},
                 unit_amount=999, currency='usd', billing_scheme='per_unit')
    with patch('web.stripe.Price.retrieve', return_value=price):
        assert send(client, endpoint, {'price_id':'price_monthly'}).status_code == 200
    assert client.get('/api/admin/overview').json['billing_price'] == 'price_monthly'
    sql(app, "UPDATE users SET subscription_status='active'")
    assert client.get('/api/billing/member-orders.csv').status_code == 200
