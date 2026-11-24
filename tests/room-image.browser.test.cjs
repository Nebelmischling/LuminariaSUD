// Run: NODE_PATH=<directory containing playwright> node --test tests/room-image.browser.test.cjs
// Optional PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH selects an existing browser.
// All HTTP is intercepted: no game process, player files, or image provider is touched.
const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');
let browser;
before(async () => {
  browser = await chromium.launch({headless:true, executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined});
});
after(async () => { if(browser) await browser.close(); });
function snapshot(id='0_0_0', session='run-1') {
  const [x,y,z] = id.split('_').map(Number);
  return {session_id:session, room:{id, name:'Room '+id, description:'Stone walls', exits:[{dir:'North',kind:'exit'}]}, player:{position:{x,y,z}}};
}
async function openGame(t, imageHandler) {
  const page = await browser.newPage();
  t.after(() => page.close());
  await page.context().addCookies([{name:'game-session',value:'browser-session',url:'http://luminaria.test/'}]);
  const images = [], commands = [], errors = [];
  let current = snapshot();
  const render = async state=>{ current=state; await page.evaluate(s=>renderSnapshot(s),state); };
  await page.addInitScript(() => {
    window.imageURLs = {created:[],revoked:[],aborted:0};
    const create=URL.createObjectURL.bind(URL), revoke=URL.revokeObjectURL.bind(URL), originalFetch=window.fetch;
    URL.createObjectURL=blob=>{const url=create(blob);window.imageURLs.created.push(url);return url;};
    URL.revokeObjectURL=url=>{window.imageURLs.revoked.push(url);revoke(url);};
    window.fetch=(url, options)=>{
      if(url==='/api/room-image' && options?.signal) options.signal.addEventListener('abort',()=>window.imageURLs.aborted++);
      return originalFetch(url, options);
    };
  });
  page.on('pageerror', error => errors.push(error.message));
  await page.route('http://luminaria.test/**', async route => {
    const url = new URL(route.request().url());
    if(url.pathname === '/') return route.fulfill({contentType:'text/html',body:html});
    if(url.pathname === '/api/room-image') {
      const request=route.request();
      images.push({method:request.method(),body:request.postDataJSON(),cookie:request.headers().cookie});
      return imageHandler(route, images.length, current.room.id);
    }
    if(url.pathname === '/api/command') {
      const command = route.request().postDataJSON();
      commands.push(command);
      if(command.text==='n') current=snapshot('1_0_0');
      return route.fulfill({json:{started:true}});
    }
    if(url.pathname === '/api/state') return route.fulfill({json:{snapshot:current}});
    if(url.pathname === '/api/events') return route.fulfill({json:{events:[],cursor:0,busy:false,snapshot:current}});
    return route.fulfill({status:404});
  });
  await page.goto('http://luminaria.test/');
  await page.waitForFunction(() => document.querySelector('#roombody').textContent.includes('Stone walls'));
  return {page,images,commands,errors,render};
}
async function until(predicate) {
  const deadline = Date.now()+3000;
  while(!predicate() && Date.now()<deadline) await new Promise(resolve=>setTimeout(resolve,20));
  assert.ok(predicate(), 'expected browser condition within 3 seconds');
}

test('first visit posts using same-origin game session once, then displays binary image', async t => {
  const {page,images,errors} = await openGame(t, route=>route.fulfill({contentType:'image/png',body:png}));
  await until(()=>images.length===1);
  assert.deepEqual(images[0], {method:'POST',body:null,cookie:'game-session=browser-session'});
  await page.waitForFunction(() => {
    const img=document.querySelector('#roomimage');
    return img && !img.hidden && img.complete && img.naturalWidth>0;
  });
  await page.evaluate(s=>{renderSnapshot(s);renderSnapshot(s);},snapshot());
  await page.waitForTimeout(80);
  assert.equal(images.length,1);
  assert.deepEqual(errors,[]);
});

test('movement remains available during generation; departure cancels and ignores late images', async t => {
  let release;
  const pending = new Promise(resolve=>{release=resolve;});
  const {page,images,commands,errors,render} = await openGame(t, async (route,count)=>{
    if(count===1) await pending;
    await route.fulfill({contentType:'image/png',body:png}).catch(()=>{});
  });
  t.after(release);
  await until(()=>images.length===1);
  await page.waitForFunction(()=>!document.body.classList.contains('busy'));
  assert.equal(await page.locator('#send').isEnabled(),true);
  await page.click('button[data-cmd="n"]');
  await until(()=>commands.some(command=>command.text==='n'));
  await page.waitForFunction(()=>!document.body.classList.contains('busy'));
  await render(snapshot('1_0_0'));
  await until(()=>images.length===2);
  await page.waitForFunction(()=>document.querySelector('#roomimage').naturalWidth>0);
  release();
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#roomimage').getAttribute('alt'),'Illustration of Room 1_0_0');
  assert.equal(await page.evaluate(()=>window.imageURLs.aborted),1);
  await render(snapshot('2_0_0'));
  await until(()=>images.length===3);
  await page.waitForFunction(()=>document.querySelector('#roomimage').naturalWidth>0);
  const resources = await page.evaluate(()=>window.imageURLs);
  assert.equal(resources.created.length-resources.revoked.length,1,'only current image owns an object URL');
  assert.deepEqual(errors,[]);
});

test('revisits reuse cached images and pagehide releases the displayed URL', async t => {
  const {page,images,errors,render} = await openGame(t, route=>route.fulfill({contentType:'image/png',body:png}));
  await page.waitForFunction(()=>document.querySelector('#roomimage')?.naturalWidth>0);
  await render(snapshot('1_0_0'));
  await until(()=>images.length===2);
  await page.waitForFunction(()=>document.querySelector('#roomimage').naturalWidth>0);
  await render(snapshot());
  await page.waitForFunction(()=>!document.querySelector('#roomimage').hidden && document.querySelector('#roomimage').naturalWidth>0, null, {timeout:2000});
  assert.equal(images.length,2,'revisit must not regenerate');
  await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));
  const resources = await page.evaluate(()=>window.imageURLs);
  assert.deepEqual(resources.revoked.slice().sort(),resources.created.slice().sort());
  assert.equal(await page.locator('#roomimage').isHidden(),true);
  await render(snapshot());
  await page.waitForFunction(()=>!document.querySelector('#roomimage').hidden);
  assert.equal(images.length,2,'bfcache restoration must not regenerate');
  await render(snapshot('0_0_0','run-2'));
  await until(()=>images.length===3);
  assert.equal(images[2].body,null);
  assert.deepEqual(errors,[]);
});

test('invalid image bytes stay hidden and release their object URL', async t => {
  const {page,images,errors,render} = await openGame(t, route=>route.fulfill({contentType:'image/png',body:'not an image'}));
  await until(()=>images.length===1);
  await page.waitForTimeout(150);
  assert.equal(await page.locator('#roomimage').isHidden(),true);
  const resources = await page.evaluate(()=>window.imageURLs);
  assert.equal(resources.created.length,1);
  assert.deepEqual(resources.revoked,resources.created);
  await render(snapshot());
  assert.equal(images.length,1);
  assert.deepEqual(errors,[]);
});

test('a stalled image request times out without retrying or disabling commands', async t => {
  let release;
  const pending=new Promise(resolve=>{release=resolve;});
  const {page,images,render} = await openGame(t, async (route,count)=>{
    if(count===1) return route.fulfill({status:204});
    await pending;
    await route.fulfill({status:204}).catch(()=>{});
  });
  t.after(release);
  await until(()=>images.length===1);
  await page.waitForFunction(()=>!document.body.classList.contains('busy'));
  await page.clock.install();
  await render(snapshot('1_0_0'));
  await until(()=>images.length===2);
  await page.clock.fastForward(90001);
  assert.equal(await page.evaluate(()=>window.imageURLs.aborted),1);
  assert.equal(await page.locator('#send').isEnabled(),true);
  await render(snapshot('1_0_0'));
  assert.equal(images.length,2);
});

for(const [name,handler] of [
  ['disabled',route=>route.fulfill({status:204})],
  ['HTTP failure',route=>route.fulfill({status:503})],
  ['JSON instead of image',route=>route.fulfill({json:{error:'unavailable'}})],
  ['network failure',route=>route.abort('failed')]
]) test(name+' leaves text and movement usable and never retries the same visit', async t => {
  const {page,images,errors,render} = await openGame(t,handler);
  await until(()=>images.length===1);
  await page.waitForFunction(()=>!document.body.classList.contains('busy'));
  await render(snapshot());
  await render(snapshot('1_0_0'));
  await until(()=>images.length===2);
  await render(snapshot());
  await page.waitForTimeout(50);
  assert.equal(images.length,2);
  assert.equal(await page.locator('#roomimage').isHidden(),true);
  assert.equal(await page.locator('#send').isEnabled(),true);
  assert.ok((await page.locator('#roombody').textContent()).includes('Stone walls'));
  assert.deepEqual(errors,[]);
});

test('server room identity prevents current-actor races from showing another location', async t => {
  const {page,images,errors} = await openGame(t, route=>route.fulfill({contentType:'image/png',headers:{'X-Room-Id':'9_9_-1'},body:png}));
  await until(()=>images.length===1);
  await page.waitForTimeout(100);
  assert.equal(await page.locator('#roomimage').isHidden(),true);
  assert.deepEqual(await page.evaluate(()=>window.imageURLs.created),[]);
  assert.deepEqual(errors,[]);
});

test('binary cache is bounded and eviction never causes a repeat generation', async t => {
  const {page,images,render} = await openGame(t, route=>route.fulfill({contentType:'image/png',body:png}));
  await page.waitForFunction(()=>!document.querySelector('#roomimage').hidden);
  for(let room=1;room<10;room++){
    await render(snapshot(room+'_0_0'));
    await until(()=>images.length===room+1);
    await page.waitForFunction(()=>!document.querySelector('#roomimage').hidden);
  }
  await render(snapshot());
  assert.equal(await page.locator('#roomimage').isHidden(),true,'oldest binary was evicted');
  assert.equal(images.length,10,'evicted rooms stay visited');
  await render(snapshot('9_0_0'));
  await page.waitForFunction(()=>!document.querySelector('#roomimage').hidden);
  assert.equal(images.length,10,'recent room uses cache');
});

test('snapshots without a location release old art without requesting an undefined room', async t => {
  const {page,images,render} = await openGame(t, route=>route.fulfill({contentType:'image/png',body:png}));
  await page.waitForFunction(()=>!document.querySelector('#roomimage').hidden);
  await render({room:{},player:{}});
  assert.equal(images.length,1);
  assert.equal(await page.locator('#roomimage').isHidden(),true);
  const resources=await page.evaluate(()=>window.imageURLs);
  assert.deepEqual(resources.revoked,resources.created);
});
