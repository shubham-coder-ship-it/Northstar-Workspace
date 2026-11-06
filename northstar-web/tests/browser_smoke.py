import os
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cryptography.fernet import Fernet
from werkzeug.serving import make_server
from web import create_app
from playwright.sync_api import sync_playwright

with tempfile.TemporaryDirectory(dir=os.environ.get('NORTHSTAR_TEST_TEMP')) as folder:
 app=create_app({'SECRET_KEY':os.urandom(32).hex(),'ENCRYPTION_KEY':Fernet.generate_key(),'DATABASE':folder+'/browser.db','SESSION_COOKIE_SECURE':False,'PAPER_TRADING':True})
 server=make_server('127.0.0.1',8765,app,threaded=True)
 threading.Thread(target=server.serve_forever,daemon=True).start()
 with sync_playwright() as p:
  browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),args=['--no-sandbox'])
  context=browser.new_context(viewport={'width':1440,'height':1100})
  page=context.new_page();errors=[]
  page.on('pageerror',lambda error:errors.append(str(error)))
  page.goto('http://127.0.0.1:8765')
  page.locator('#auth-toggle').click()
  page.locator('#auth-form [name=full_name]').fill('Browser Tester')
  page.locator('#auth-form [name=email]').fill('browser@example.com')
  page.locator('#auth-form [name=password]').fill('strong-browser-password')
  page.locator('#auth-form [name=confirm]').fill('strong-browser-password')
  page.locator('[data-toggle-password=password]').click()
  assert page.locator('#auth-form [name=password]').get_attribute('type') == 'text'
  assert page.locator('#auth-form [name=confirm]').get_attribute('type') == 'password'
  page.locator('#auth-submit').click()
  page.get_by_text('Account created. Sign in to continue.').wait_for()
  page.locator('#auth-form [name=email]').fill('browser@example.com')
  page.locator('#auth-form [name=password]').fill('strong-browser-password')
  page.locator('#auth-submit').click()
  page.locator('#welcome').wait_for()
  page.locator('#welcome-name').get_by_text('Browser Tester').wait_for()
  page.locator('#choose-practice').click()
  page.locator('#practice-stats').get_by_text('Virtual cash').wait_for()
  page.locator('#order-form [name=quantity]').fill('1')
  page.locator('#order-form [name=quote]').fill('100')
  page.locator('#order-form [name=confirmed]').check()
  page.locator('#order-form button[type=submit]').click()
  page.locator('#notice').get_by_text('filled practice', exact=False).wait_for()
  page.locator('#mode-switch').click()
  page.locator('#professional').wait_for()
  page.locator('#mode-switch').click()
  page.locator('#order-count').get_by_text('orders').wait_for()
  assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
  page.locator('[data-tab=settings]').click()
  assert page.locator('#credentials-form').count() == 0
  assert page.locator('#password-form [name=confirm]').get_attribute('type') == 'password'
  page.locator('#signout').click()
  page.locator('#signout-dialog').get_by_role('button', name='Cancel').click()
  assert not page.locator('#auth-dialog').is_visible()
  page.locator('#subscribe').click()
  page.locator('#notice').get_by_text('Subscriptions are not configured',exact=False).wait_for()
  # Slow network: delay order-history response and verify the refresh button stays disabled.
  page.locator('[data-tab=overview]').click()
  page.route('**/api/orders',lambda route:(__import__('time').sleep(1),route.continue_()))
  page.locator('#refresh').click()
  assert page.locator('#refresh').is_disabled()
  page.wait_for_function("() => !document.querySelector('#refresh').disabled")
  page.unroute('**/api/orders')
  for width in (390,768):
   page.set_viewport_size({'width':width,'height':844})
   assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'overflow at {width}'
   assert page.locator('#order-form').is_visible()
  page.locator('[data-tab=settings]').click()
  page.locator('#signout').click()
  page.locator('#signout-dialog').get_by_role('button', name='Sign Out').click()
  page.locator('#auth-dialog').wait_for()
  assert not errors,errors
  browser.close()
 server.shutdown()
 print('PASS: registration, sign-in, mode switching, practice fill, billing fallback, sign-out confirmation, and layouts at 1440/768/390 pixels; no JavaScript errors')

